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
    "remote_access",
    "mail_send",
)
DEFAULTS = {
    "workspace_write": "allow",
    "shell_exec": "allow",
    "python_exec": "allow",
    "background_job": "allow",
    "http_request": "allow",
    "remote_access": "confirm",
    "mail_send": "confirm",
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
    if name in ("write_file", "edit_file", "make_folder"):
        return "workspace_write"
    if name == "run_shell":
        return "shell_exec"
    if name == "python_exec":
        return "python_exec"
    if name == "spawn_job":
        return "background_job"
    if name == "http_request":
        return "http_request"
    if name in ("ssh_run", "sftp"):
        return "remote_access"
    if name in ("mail_send", "mail_reply"):
        return "mail_send"
    return ""
