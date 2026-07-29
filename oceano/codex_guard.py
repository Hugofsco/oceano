"""Pre-execution guard for the resident Codex mind.

Resident mutations and shell execution must travel through the Oceano MCP bridge, where
workspace policy, dynamic catalogs, call budgets, structured results, and idempotency apply.
"""
import json
import sys


_BLOCKED = {
    "Bash", "shell", "exec_command",
    "apply_patch", "Edit", "Write",
    "write_file", "edit_file", "make_folder",
    "run_shell", "python_exec", "run_tests", "git",
    "spawn_agent", "send_input", "resume_agent", "wait_agent", "close_agent",
}


def _deny(name, reason=None):
    label = name or "unknown native tool"
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason or (
                f"Native {label} is disabled for the Oceano resident mind. "
                "Use the advertised Oceano MCP body tool instead."
            ),
        }
    }


def decision(payload):
    if not isinstance(payload, dict):
        return _deny("", "Malformed resident Codex hook payload; native execution denied.")
    raw_name = payload.get("tool_name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        return _deny("", "Malformed resident Codex hook payload; native execution denied.")
    name = raw_name.strip()
    # The current hook matcher excludes Oceano MCP names, while this exemption keeps the guard
    # explicitly permissive for them if matcher configuration changes or decision() is reused.
    if name.startswith("mcp__oceano__") or name not in _BLOCKED:
        return {}
    return _deny(name)


def main():
    try:
        payload = json.load(sys.stdin)
    except (TypeError, ValueError):
        payload = {}
    json.dump(decision(payload), sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
