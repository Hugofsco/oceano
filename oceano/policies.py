"""Runtime policy store for risky capabilities.

Policies are intentionally coarse: a capability is allowed, blocked, or requires an
approval when used from a workflow run. Direct chat/tool use cannot pause mid-turn, so
`confirm` is treated as a refusal there and callers should route the action through a
workflow approval node or change the policy.
"""
import contextvars
import json
from contextlib import contextmanager

import config
from oceano import atomicio

STORE = config.WORKSPACE.parent / "data" / "policies.json"
MODES = ("allow", "confirm", "block")
CAPABILITIES = (
    "workspace_write",
    "shell_exec",
    "python_exec",
    "background_job",
    "http_request",
    "browser_control",
    "remote_access",
    "mail_manage",
    "mail_send",
    "calendar_write",
    "memory_write",
    "schedule_write",
    "notes_write",
    "desktop_control",
)
DEFAULTS = {
    # New capability groups default to the app's historical behavior. Users may
    # tighten them independently without a compatibility-breaking policy migration.
    "workspace_write": "allow",
    "shell_exec": "allow",
    "python_exec": "allow",
    "background_job": "allow",
    "http_request": "allow",
    "browser_control": "allow",
    "remote_access": "confirm",
    "mail_manage": "allow",
    "mail_send": "confirm",
    "calendar_write": "allow",
    "memory_write": "allow",
    "schedule_write": "allow",
    "notes_write": "allow",
    "desktop_control": "allow",
}

# One auditable source of truth for built-in tool capabilities. ToolSpec captures
# this at registration time; schemas may override it through private x-oceano metadata.
TOOL_CAPABILITIES = {
    **{name: "workspace_write" for name in (
        "write_file", "edit_file", "make_folder", "convert", "fetch_media", "speak_to_file",
    )},
    "run_shell": "shell_exec",
    "git": "shell_exec",
    # run_tests EXECUTES a runner chosen from workspace files (make/npm/cargo/a project venv's
    # python), so it belongs with run_shell. It was unmapped, and an unmapped tool resolves to
    # capability "" → mode "allow" permanently — so a user who set shell_exec: block reasonably
    # believed code execution was off while this stayed wide open.
    "run_tests": "shell_exec",
    "python_exec": "python_exec",
    "spawn_job": "background_job",
    "spawn_agent": "background_job",
    "schedule_task": "schedule_write",
    "update_task": "schedule_write",
    "cancel_task": "schedule_write",
    "run_workflow": "background_job",
    "http_request": "http_request",
    **{name: "browser_control" for name in (
        "browser_open", "browser_click", "browser_scroll", "browser_fill", "browser_select",
        "browser_press", "browser_wait", "browser_eval", "browser_hover", "browser_upload",
        "browser_dialog", "browser_tab",
    )},
    "ssh_run": "remote_access",
    "sftp": "remote_access",
    **{name: "mail_manage" for name in (
        "mail_move", "mail_delete", "mail_flag", "mail_save_attachment", "mail_folder",
    )},
    "mail_send": "mail_send",
    "mail_reply": "mail_send",
    **{name: "calendar_write" for name in (
        "add_calendar_event", "add_calendar_events", "update_calendar_event",
        "delete_calendar_event", "manage_calendar",
    )},
    **{name: "memory_write" for name in (
        "remember", "update_memory", "forget_memory", "index_docs", "learn_skill",
    )},
    **{name: "notes_write" for name in (
        "add_note", "update_note", "delete_note", "add_kanban_card", "update_kanban_card",
        "delete_kanban_card",
    )},
    **{name: "desktop_control" for name in (
        "ui_open", "ui_close", "ui_arrange", "desktop_notify", "desktop_pick_file",
        "desktop_save_file", "desktop_reveal_path", "desktop_open_path",
        "desktop_clipboard_read", "desktop_clipboard_write", "desktop_screenshot",
    )},
}



_perm = contextvars.ContextVar("oceano_policy_permit", default=frozenset())


@contextmanager
def permit(*capabilities):
    cur = set(_perm.get())
    cur.update(c for c in capabilities if c)
    tok = _perm.set(frozenset(cur))
    try:
        yield
    finally:
        _perm.reset(tok)


def is_permitted(capability):
    return capability in _perm.get()


def _load():
    try:
        d = json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError):
        d = {}
    return {c: (d.get(c) if d.get(c) in MODES else DEFAULTS[c]) for c in CAPABILITIES}


def get():
    return _load()


def set_policy(capability, mode):
    if capability not in CAPABILITIES or mode not in MODES:
        return False
    d = _load()
    d[capability] = mode
    try:
        atomicio.write_text(STORE, json.dumps(d, indent=2))
    except OSError:
        return False
    return True


def set_all(policies):
    d = _load()
    for cap, mode in (policies or {}).items():
        if cap in CAPABILITIES and mode in MODES:
            d[cap] = mode
    try:
        atomicio.write_text(STORE, json.dumps(d, indent=2))
    except OSError:
        return False
    return True


def capability_for_tool(name):
    return TOOL_CAPABILITIES.get(name, "")
