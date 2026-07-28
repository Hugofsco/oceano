"""Shared state and contracts for one agent turn.

This module intentionally has no dependency on ``Agent`` or an LLM provider. Both
blocking and streaming drivers use it as the common orchestration spine while they
retain their existing transport-specific model I/O.
"""
from dataclasses import dataclass, field
import json
import os
import re
import time

from oceano.tools.core import ToolResult


@dataclass(frozen=True)
class ContextCheckpoint:
    """Loss-resistant state carried across conversation compaction."""

    decisions: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    structured: bool = True

    @classmethod
    def parse(cls, text):
        raw = (text or "").strip()
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        try:
            data = json.loads(match.group(0)) if match else None
        except (TypeError, ValueError):
            data = None
        if not isinstance(data, dict):
            return cls(notes=(raw or "(nothing notable)",), structured=False)

        def values(name):
            items = data.get(name) or []
            if isinstance(items, str):
                items = [items]
            return tuple(str(item).strip() for item in items if str(item).strip())[:50]

        return cls(
            decisions=values("decisions"), constraints=values("constraints"),
            artifacts=values("artifacts"), evidence=values("evidence"),
            unresolved=values("unresolved"), notes=values("notes"), structured=True,
        )

    def render(self):
        sections = (
            ("Decisions", self.decisions), ("Constraints", self.constraints),
            ("Artifacts", self.artifacts), ("Verification evidence", self.evidence),
            ("Unresolved work", self.unresolved), ("Additional context", self.notes),
        )
        populated = [(title, items) for title, items in sections if items]
        if not populated:
            return "(nothing notable)"
        return "\n\n".join(title + ":\n" + "\n".join(f"- {item}" for item in items)
                             for title, items in populated)

    def metrics(self):
        return {
            "structured": self.structured,
            "decisions": len(self.decisions), "constraints": len(self.constraints),
            "artifacts": len(self.artifacts), "evidence": len(self.evidence),
            "unresolved": len(self.unresolved), "notes": len(self.notes),
        }


@dataclass(frozen=True)
class TaskSpec:
    """Structured completion contract derived by the adaptive planner."""

    requires_action: bool = False
    verify_code: bool = False
    expected_evidence: tuple[str, ...] = ()

    @classmethod
    def from_plan(cls, plan):
        plan = plan or {}
        evidence = []
        if plan.get("requires_action"):
            evidence.append("tool_action")
        if plan.get("verify_code"):
            evidence.append("code_verification")
        return cls(bool(plan.get("requires_action")), bool(plan.get("verify_code")), tuple(evidence))


@dataclass
class TurnBudget:
    """Per-turn execution budget independent of provider/context budgets."""

    max_steps: int
    max_tool_calls: int = 0
    deadline: float | None = None
    started_at: float = field(default_factory=time.monotonic)
    steps: int = 0
    tool_calls: int = 0

    @classmethod
    def create(cls, max_steps, deadline=None):
        try:
            configured = int(os.environ.get("OCEANO_MAX_TOOL_CALLS", "0"))
        except ValueError:
            configured = 0
        # Historical behavior had no separate call cap. Four calls per model step is
        # deliberately generous while still bounding pathological multi-call replies.
        return cls(max_steps=max_steps, max_tool_calls=configured or max_steps * 4,
                   deadline=deadline)

    def begin_step(self):
        if self.exhausted:
            return False
        self.steps += 1
        return True

    def consume_tool(self):
        if self.tool_calls >= self.max_tool_calls:
            return False
        self.tool_calls += 1
        return True

    @property
    def exhausted(self):
        return self.steps >= self.max_steps or self.tool_calls >= self.max_tool_calls

    @property
    def timed_out(self):
        return self.deadline is not None and time.monotonic() >= self.deadline

    @property
    def elapsed(self):
        return max(0.0, time.monotonic() - self.started_at)


@dataclass
class ToolEvent:
    name: str
    result: ToolResult
    resolved: bool = False
    resolved_by: int | None = None

    @property
    def text(self):
        return self.result.text()


@dataclass
class TurnState:
    """Shared evidence, routing, and budget state for blocking/streaming turns."""

    user_message: str
    route: object
    allowed: set[str]
    task: TaskSpec
    budget: TurnBudget
    only_tools: object = None
    events: list[ToolEvent] = field(default_factory=list)
    corrected: bool = False

    def record(self, name, result):
        structured = result if isinstance(result, ToolResult) else ToolResult.from_value(result)
        event = ToolEvent(name, structured)
        self.events.append(event)
        if structured.ok:
            current = len(self.events) - 1
            # A successful retry of the same tool supersedes its earlier transient failure.
            for prior in self.events[:current]:
                if prior.name == name and not prior.result.ok and prior.result.retryable:
                    prior.resolved = True
                    prior.resolved_by = current
            # Passing verification after a successful mutation is evidence that transient
            # setup/read failures encountered before the mutation no longer block completion.
            verification = {"run_tests", "run_shell", "python_exec"}
            if name in verification:
                last_effect = max(
                    (i for i, item in enumerate(self.events[:current]) if item.result.ok
                     and item.result.side_effects), default=-1)
                if last_effect >= 0:
                    for prior in self.events[:last_effect]:
                        if not prior.result.ok and prior.result.retryable:
                            prior.resolved = True
                            prior.resolved_by = current
        return structured

    @property
    def legacy_events(self):
        return [(event.name, event.text) for event in self.events]

    @property
    def used_tools(self):
        return [event.name for event in self.events]

    @property
    def error_count(self):
        return self.unresolved_error_count

    @property
    def unresolved_errors(self):
        return [event for event in self.events if not event.result.ok and not event.resolved]

    @property
    def unresolved_error_count(self):
        return len(self.unresolved_errors)

    @property
    def historical_error_count(self):
        return sum(not event.result.ok for event in self.events)

    @property
    def side_effects(self):
        return [effect for event in self.events for effect in event.result.side_effects]

    def completion_issues(self):
        """Deterministic, structured completion gate shared by every turn driver."""
        if not (self.task.expected_evidence or self.task.requires_action or self.task.verify_code):
            return []
        calls = set(self.used_tools)
        issues = []
        mutations = {"write_file", "edit_file", "make_folder", "run_shell", "python_exec", "delegate"}
        acted = bool(calls & mutations) or bool(self.side_effects)
        if self.task.requires_action and not acted:
            issues.append("no action tool was used")
        if self.error_count:
            issues.append("at least one tool returned an error")
        verification = {"run_tests", "run_shell", "python_exec"}
        if self.task.verify_code and "delegate" not in calls and not (calls & verification):
            issues.append("the changed code was not exercised")
        return issues

    def metrics(self):
        return {
            "used_tools": self.used_tools,
            "errors": self.error_count,
            "historical_errors": self.historical_error_count,
            "tool_calls": self.budget.tool_calls,
            "model_steps": self.budget.steps,
            "elapsed_ms": round(self.budget.elapsed * 1000),
            "side_effect_count": len(self.side_effects),
        }


class ResidentEventAdapter:
    """Translate resident CLI events into the same evidence model as API/local turns."""

    _NATIVE_NAMES = {
        "shell": "run_shell", "Bash": "run_shell",
        "Read": "read_file", "Write": "write_file", "Edit": "edit_file",
        "Glob": "list_files", "Grep": "code_search",
    }
    _EXIT = re.compile(r"\(exit\s+(\d+)(?:,|\))", re.IGNORECASE)

    def __init__(self, state):
        self.state = state
        self.pending = {}

    @classmethod
    def normalize_name(cls, name):
        value = str(name or "tool")
        for prefix in ("mcp__oceano__", "oceano__"):
            if value.startswith(prefix):
                value = value[len(prefix):]
        return cls._NATIVE_NAMES.get(value, value)

    @staticmethod
    def _args(value):
        if isinstance(value, dict):
            return value
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def tool_call(self, name, args):
        normalized = self.normalize_name(name)
        if not self.state.budget.consume_tool():
            self.state.record(normalized, ToolResult(
                False, error="turn tool-call budget exhausted", code="budget_exhausted"))
            return False
        self.pending.setdefault(normalized, []).append(self._args(args))
        return True

    def tool_result(self, name, value, *, is_error=False):
        from oceano.tools.core import tool_spec
        normalized = self.normalize_name(name)
        args_list = self.pending.get(normalized) or []
        args = args_list.pop(0) if args_list else {}
        text = str(value or "")
        low = text.lstrip().lower()
        match = self._EXIT.search(text)
        if is_error:
            policy_block = "blocked by policy" in low or "requires approval" in low
            if policy_block:
                code = "policy_blocked" if "blocked by policy" in low else "approval_required"
                retryable = False
            elif normalized == "run_tests":
                code, retryable = "tests_failed", True
            elif normalized in {"run_shell", "python_exec"}:
                code, retryable = "command_failed", True
            elif normalized in {"write_file", "edit_file", "make_folder"}:
                code, retryable = "write_failed", True
            else:
                retryable = any(term in low for term in
                                ("not found", "no such", "timed out", "temporar"))
                code = "not_found" if "not found" in low or "no such" in low else "tool_error"
            result = ToolResult(False, error=text or f"{normalized} failed",
                                retryable=retryable, code=code)
        elif match and int(match.group(1)) != 0:
            code = "tests_failed" if normalized == "run_tests" else "command_failed"
            result = ToolResult(False, error=text or f"{normalized} failed",
                                retryable=True, code=code)
        elif (low.startswith("error") or low.startswith("(no such")
              or "traceback (most recent call last)" in low):
            retryable = any(term in low for term in ("not found", "no such", "timed out", "temporar"))
            code = "not_found" if "not found" in low or "no such" in low else "tool_error"
            result = ToolResult(False, error=text, retryable=retryable, code=code)
        else:
            spec = tool_spec(normalized)
            effects = ()
            path = str(args.get("path") or args.get("file_path") or ".")
            if normalized in {"write_file", "edit_file"}:
                effects = (f"file:{path}",)
            elif normalized == "make_folder":
                effects = (f"directory:{path}",)
            elif spec and spec.side_effecting:
                effects = (f"capability:{spec.capability}",)
            result = ToolResult(True, summary=text, side_effects=effects)
        return self.state.record(normalized, result)

    def missing_result(self, name):
        """Record a call whose resident stream ended without a matching result event."""
        normalized = self.normalize_name(name)
        args_list = self.pending.get(normalized) or []
        if args_list:
            args_list.pop(0)
        return self.state.record(normalized, ToolResult(
            False, error="resident tool call ended without a result event",
            retryable=True, code="missing_result"))
