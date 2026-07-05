---
name: agent-workflow-designer
description: pick the right multi-agent/multi-step pattern (sequential, parallel, router, orchestrator, evaluator) and scaffold it before building — use when designing one of Oceano's own visual Workflows, or a spawn_agent pipeline with more than one stage
status: published
notes: ported from claude-skills engineering/agent-workflow-designer (MIT); script copied verbatim, stdlib-only
---
# Agent workflow designer

Directly applicable to Oceano's own node-canvas Workflows and to multi-stage
`spawn_agent` pipelines — pick the pattern before wiring nodes/agents together.

Patterns: **sequential** (strict step-by-step chain) · **parallel** (fan-out/fan-in,
independent subtasks — same shape as multiple `spawn_agent` calls joined at the end) ·
**router** (dispatch by intent, with a fallback) · **orchestrator** (a planner
coordinates specialist steps with dependencies) · **evaluator** (generate → quality
gate → loop, e.g. pairs well with [[self-eval]]).

Scaffold a skeleton before building: `python3 skills/agent-workflow-designer/scripts/workflow_scaffolder.py <pattern> --name <name>` (add `--output <path>.json` to save it).

Rules: start with the smallest pattern that satisfies the requirement — don't
orchestrate what one well-structured prompt or a single agent call can do. Keep
handoff payloads explicit and bounded (pass the specific artifact needed, not the
full upstream context/transcript). Put a timeout on every external-model step. Validate
each stage's output before fanning back in — don't let a bad intermediate result poison
the synthesis step.
