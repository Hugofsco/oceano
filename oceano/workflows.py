"""Workflows — visual, branching recipes the agent runs.

A workflow is a directed graph the user draws on a canvas (Drawflow in the UI):

  nodes : start · tool · instruction · delegate · decision · end
  edges : from -> to  (decision edges carry a branch label: "yes" / "no")

Execution walks the graph from the `start` node, following edges. Most nodes do their
work then follow their single outgoing edge; a `decision` node evaluates a condition and
follows its "yes" or "no" edge instead — that's the branching / decision-tree behaviour.
A decision can be judged three ways (the user picks per node):
  rule     — a deterministic test over the previous step's output (contains/equals/matches/gt/lt)
  model    — the PRIMARY model answers YES/NO given the context (flexible, less predictable)
  delegate — Claude / a cloud model answers YES/NO (for judgments the primary shouldn't make)

The whole run shares ONE Agent, so context accumulates across nodes. A hard node-visit cap
stops any accidental loop from running forever. Runs are recorded so scheduled, unattended
runs stay observable. Storage is one JSON file (atomic); a workflow's cron schedule lives in
the scheduler as a managed task tagged `workflow:<id>`.
"""
import copy
import hashlib
import json
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import config
from oceano import atomicio, policies, secretcrypto, tools, traces

STORE = config.WORKSPACE.parent / "data" / "workflows.json"
RUNS_STORE = config.WORKSPACE.parent / "data" / "workflow_runs.json"      # history, split out so
TRIG_STATE = config.WORKSPACE.parent / "data" / "trigger_state.json"      # the hot store stays small
SECRETS_STORE = config.WORKSPACE.parent / "data" / "wf_secrets.json"      # named {{secret.X}} values
CHECKPOINT_STORE = config.WORKSPACE.parent / "data" / "workflow_checkpoints.json"
SOURCE_PREFIX = "workflow:"
SCHED_PREFIX = "[ FLOW ] "
# start/end + the action nodes; "trigger" is a start that also declares HOW the flow fires (issue 8 C);
# switch=multi-branch, loop=foreach, http/subflow/transform=connectivity+data, approval=human-in-the-loop,
# wait=delay (a duration or a clock time), merge=the join for forked parallel branches,
# agent/await/orchestrate=multi-agent (orchestrate = plugged-in agent nodes run in ordered steps).
NODE_TYPES = ("start", "trigger", "tool", "instruction", "delegate", "decision",
              "switch", "loop", "merge", "http", "subflow", "transform", "approval", "wait",
              "agent", "await", "orchestrate", "end")
_AGENT_PROVIDERS = ("", "claude", "codex", "api", "local")   # "" = the delegation default
_TRIGGER_NODE_KINDS = ("manual", "schedule", "webhook", "keyword", "watch", "email")
_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")
_TRANSFORM_MODES = ("template", "regex", "jsonpath", "python")
_SWITCH_OPS = ("contains", "equals", "matches", "gt", "lt")
_RUNS_PER_WF = 25                    # run history kept per workflow (was one 60-run global cap)
_OUT_CAP = 4000
_VISIT_CAP = 400                     # max node executions per run — loop backstop (raised for foreach)
_LOOP_CAP = 200                      # max iterations a single loop node will run
_SALVAGE_BACKOFF = 15                # seconds before an orchestrator's serial salvage retry
_SUBFLOW_DEPTH = 5                   # how deep nested sub-workflows may go
_HTTP_CAP = 200000                   # cap an HTTP node's captured response body
_WRITE_TIERS = ("", "execute", "write", "shell")   # an agent/delegate node's access opt-in level


def _tool_scope_for(write):
    """CLI-style tool spec for an agent/delegate node's chosen access tier. "" (default) stays
    read-only — an unattended/scheduled flow must not be quietly MORE privileged than the user
    intended; "execute", "write", and "shell" are explicit opt-ins. "execute" permits shell
    verification without file edits; "write" is file-edit only (Read,Glob,Grep,Write,Edit) —
    no execution, on ANY provider. "shell" combines both, which is
    also what unlocks run_tests/git for the api/local providers via delegate._API_TOOL_MAP — a
    node that needs to verify what it wrote (run its tests, touch git) needs "shell", not "write".
    Callers: keep the result in a variable named tool_scope, NOT tools — oceano.tools is a
    MODULE imported at file scope, and a local named `tools` anywhere in a function shadows
    that import for the function's ENTIRE body (Python scopes per-function, not per-branch),
    breaking every "tool" node call in run() with UnboundLocalError. This happened once already."""
    if write == "shell":
        return "Read,Glob,Grep,Write,Edit,Bash"
    if write == "execute":
        return "Read,Glob,Grep,Bash"
    if write == "write":
        return "Read,Glob,Grep,Write,Edit"
    return "Read,Glob,Grep"


def _access_marker(write):
    """Suffix for a node's label/history entries so an elevated access tier stays visible
    after the fact — the same distinction _tool_scope_for reads."""
    if write == "shell":
        return " ⚠"
    if write == "execute":
        return " ▶"
    if write == "write":
        return " ✎"
    return ""

# Live run state so the GUI can RECONNECT to an in-progress run after a browser refresh
# (works for manual AND scheduled runs). Keyed by workflow id; finished runs linger briefly.
_LIVE = {}
_LIVE_LOCK = threading.Lock()
_LIVE_KEEP = 180                     # seconds a finished run stays visible for reconnection
_LIVE_STALE = 1800                   # drop a 'running' entry with no node activity for this long


def _now():
    return datetime.now(timezone.utc).isoformat()


# ---------------- persistence ----------------
def _load():
    try:
        data = json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("workflows", [])
    legacy = data.pop("runs", None)      # one-time: run history moved to its own store, so the
    if legacy:                           # hot store (read on every trigger poll) stays small
        _save_runs(_load_runs() + legacy)
        _save(data)
    data["workflows"] = [_migrate(w) for w in data["workflows"]]
    return data


def _save(data):
    data.pop("runs", None)               # runs live in RUNS_STORE — never write them back here
    atomicio.write_text(STORE, json.dumps(data, indent=2))


def _load_runs():
    try:
        d = json.loads(RUNS_STORE.read_text())
    except (OSError, json.JSONDecodeError):
        d = {}
    return d.get("runs", []) if isinstance(d, dict) else []


def _save_runs(rs):
    atomicio.write_text(RUNS_STORE, json.dumps({"runs": rs}, indent=2))


def _load_checkpoints():
    try:
        d = json.loads(CHECKPOINT_STORE.read_text())
    except (OSError, json.JSONDecodeError):
        d = {}
    return d if isinstance(d, dict) else {}


def _save_checkpoints(data):
    atomicio.write_text(CHECKPOINT_STORE, json.dumps(data, indent=2))


def _save_checkpoint(wf_id, state):
    cps = _load_checkpoints()
    cps[str(wf_id)] = state
    _save_checkpoints(cps)


def _clear_checkpoint(wf_id):
    cps = _load_checkpoints()
    if str(wf_id) in cps:
        del cps[str(wf_id)]
        _save_checkpoints(cps)


def resume_state(wf_id):
    return _load_checkpoints().get(str(wf_id))


# ---------------- named secrets ({{secret.NAME}} — HTTP nodes only) ----------------
# A small write-only store for API keys and tokens a flow's HTTP node needs: values are
# encrypted at rest (secretcrypto) and NEVER surface through the API or the templating engine —
# {{secret.X}} resolves exclusively inside _run_http (url/headers/body), so a prompt-injected
# instruction node can't exfiltrate one, and the resolved value is redacted from the recorded
# output. Names: letters/digits/._-, starting with a letter.
_SECRET_NAME_RE = re.compile(r"[A-Za-z][\w.-]{0,63}")
_SECRET_RE = re.compile(r"\{\{\s*secret\.([\w.-]+)\s*\}\}")


def _load_secrets():
    try:
        d = json.loads(SECRETS_STORE.read_text())
    except (OSError, json.JSONDecodeError):
        d = {}
    return d.get("secrets", {}) if isinstance(d, dict) else {}


def list_secrets():
    """Names only — a stored value never leaves through this (or any) API."""
    return sorted(_load_secrets().keys())


def set_secret(name, value):
    name = (name or "").strip()
    if not _SECRET_NAME_RE.fullmatch(name) or not str(value or ""):
        return False
    s = _load_secrets()
    s[name] = secretcrypto.encrypt(str(value))
    atomicio.write_text(SECRETS_STORE, json.dumps({"secrets": s}, indent=2))
    return True


def delete_secret(name):
    s = _load_secrets()
    if name not in s:
        return False
    del s[name]
    atomicio.write_text(SECRETS_STORE, json.dumps({"secrets": s}, indent=2))
    return True


def _fill_secrets(text, used):
    """Substitute {{secret.NAME}} tokens; every resolved value is appended to `used` so the
    caller can redact them from whatever it records. Unknown names render empty, like any
    other unknown token."""
    def sub(m):
        v = secretcrypto.decrypt(_load_secrets().get(m.group(1), ""))
        if v:
            used.append(v)
        return v
    return _SECRET_RE.sub(sub, str(text or ""))


def _redact(text, used):
    for v in used:
        if v:
            text = text.replace(v, "•••")
    return text


def _next_id(items):
    return max((x["id"] for x in items), default=0) + 1


def _migrate(wf):
    """An older linear workflow ({steps:[...]}) -> a straight-line graph, so nothing breaks."""
    wf.setdefault("triggers", [])
    wf["input"] = _norm_input(wf.get("input"))      # every workflow carries an input declaration
    if "graph" in wf and isinstance(wf["graph"], dict):
        return wf
    steps = wf.pop("steps", []) or []
    nodes = [{"id": 1, "type": "start", "x": 40, "y": 160}]
    edges = []
    prev, nid, y = 1, 2, 160
    for s in steps:
        x = 60 + (nid - 1) * 220
        node = {"id": nid, "type": s.get("type", "instruction"), "x": x, "y": y}
        if node["type"] == "tool":
            node["tool"] = s.get("tool", ""); node["args"] = s.get("args", {})
        elif node["type"] == "delegate":
            node["text"] = s.get("text", ""); node["role"] = s.get("role", "default")
        else:
            node["type"] = "instruction"; node["text"] = s.get("text", "")
        nodes.append(node)
        edges.append({"from": prev, "to": nid, "branch": None})
        prev, nid = nid, nid + 1
    nodes.append({"id": nid, "type": "end", "x": 60 + (nid - 1) * 220, "y": y})
    edges.append({"from": prev, "to": nid, "branch": None})
    wf["graph"] = {"nodes": nodes, "edges": edges}
    return wf


def _norm_graph(graph):
    """Validate/normalize a graph from the client. Keeps only known node fields."""
    if not isinstance(graph, dict):
        return {"nodes": [], "edges": []}
    nodes = []
    for n in graph.get("nodes", []) or []:
        if not isinstance(n, dict) or "id" not in n:
            continue
        t = n.get("type")
        if t not in NODE_TYPES:
            continue
        node = {"id": n["id"], "type": t, "x": n.get("x", 0), "y": n.get("y", 0)}
        try:
            r = int(n.get("retries", 0))             # per-node retry-on-failure (issue 8 D)
            if r > 0:
                node["retries"] = max(0, min(r, 5))
        except (TypeError, ValueError):
            pass
        if t == "trigger":
            node["kind"] = n.get("kind") if n.get("kind") in _TRIGGER_NODE_KINDS else "manual"
            node["cron"] = str(n.get("cron", "")).strip()                 # schedule
            node["pattern"] = str(n.get("pattern", "")).strip()           # keyword
            node["channel"] = n.get("channel") if n.get("channel") in ("any", "web", "telegram") else "any"
            node["folder"] = str(n.get("folder", "")).strip().strip("/")  # watch
            node["account"] = str(n.get("account", "")).strip()           # email
            node["mailFolder"] = str(n.get("mailFolder", "") or "INBOX").strip()
            node["token"] = str(n.get("token", "")).strip()               # webhook (filled on save)
        elif t == "tool":
            node["tool"] = str(n.get("tool", "")).strip()
            node["args"] = n.get("args") if isinstance(n.get("args"), dict) else {}
        elif t == "instruction":
            node["text"] = str(n.get("text", ""))
            # "" → follow the global mind (Settings → Primary intelligence): claude/codex if set
            # and available, else the run's own agent. An explicit value pins THIS node regardless.
            node["provider"] = n.get("provider") if n.get("provider") in _AGENT_PROVIDERS else ""
            node["model"] = str(n.get("model", "")).strip()[:120]      # pin this node's turn to a
            node["baseUrl"] = str(n.get("baseUrl", "")).strip()[:200]  # registered endpoint's model
            node["persona"] = str(n.get("persona", "")).strip()[:80]   # optional persona skill name
        elif t == "delegate":
            node["text"] = str(n.get("text", ""))
            node["role"] = n.get("role") if n.get("role") in ("default", "improve") else "default"
            # "" (default) = read-only, matching the sibling agent node below — a background/
            # unattended delegate must not be quietly MORE privileged than the rest of a flow.
            # Explicit opt-in only: "write" (+run_tests/git) or "shell" (+arbitrary commands).
            node["write"] = n.get("write") if n.get("write") in _WRITE_TIERS else ""
            node["persona"] = str(n.get("persona", "")).strip()[:80]   # optional persona skill name
            try:                                     # absolute cap override; 0/unset = the delegation
                tmo = int(n.get("timeout", 0))       # default (idle-based: a productive build is never
            except (TypeError, ValueError):          # killed for taking long, only a stalled one)
                tmo = 0
            if tmo > 0:
                node["timeout"] = max(60, min(tmo, 7200))
        elif t == "agent":                            # spawn a background sub-agent; the flow continues
            node["task"] = str(n.get("task", ""))
            node["provider"] = n.get("provider") if n.get("provider") in _AGENT_PROVIDERS else ""
            node["model"] = str(n.get("model", "")).strip()[:120]      # pin to a registered
            node["baseUrl"] = str(n.get("baseUrl", "")).strip()[:200]  # endpoint's model ("" = default)
            node["label"] = str(n.get("label", "")).strip()[:80]
            node["write"] = n.get("write") if n.get("write") in _WRITE_TIERS else ""   # "" default = read-only
            node["persona"] = str(n.get("persona", "")).strip()[:80]   # optional persona skill name
            try:
                node["timeout"] = max(1, min(int(n.get("timeout", 600)), 7200))    # seconds
            except (TypeError, ValueError):
                node["timeout"] = 600
        elif t == "await":                            # join: wait for this run's spawned agents
            node["agents"] = str(n.get("agents", "")).strip()   # optional id list; empty = all spawned
            try:
                node["timeout"] = max(1, min(int(n.get("timeout", 900)), 7200))    # seconds
            except (TypeError, ValueError):
                node["timeout"] = 900
        elif t == "orchestrate":                      # run plugged-in agent nodes in ordered steps
            raw_plan = n.get("plan") if isinstance(n.get("plan"), dict) else {}
            plan = {}                                 # agent node id -> step number (same step = parallel)
            for k, v in raw_plan.items():
                try:
                    plan[str(int(k))] = max(1, min(int(v), 20))
                except (TypeError, ValueError):
                    continue
            node["plan"] = plan
            node["mode"] = n.get("mode") if n.get("mode") in ("concat", "summarize") else "concat"
            node["text"] = str(n.get("text", ""))     # compile brief (summarize mode)
            try:
                node["timeout"] = max(1, min(int(n.get("timeout", 900)), 7200))    # seconds PER STEP
            except (TypeError, ValueError):
                node["timeout"] = 900
        elif t == "decision":
            node["mode"] = n.get("mode") if n.get("mode") in ("rule", "model", "delegate") else "model"
            node["question"] = str(n.get("question", ""))
            node["ruleOp"] = n.get("ruleOp") if n.get("ruleOp") in _SWITCH_OPS else "contains"
            node["ruleValue"] = str(n.get("ruleValue", ""))
            node["role"] = n.get("role") if n.get("role") in ("default", "improve") else "default"
        elif t == "switch":
            node["source"] = str(n.get("source", ""))            # expression to test (default: last output)
            cases = []
            for c in n.get("cases", []) or []:
                if not isinstance(c, dict):
                    continue
                lbl = str(c.get("label", "")).strip()[:40]
                if not lbl:
                    continue
                cases.append({"op": c.get("op") if c.get("op") in _SWITCH_OPS else "contains",
                              "value": str(c.get("value", "")), "label": lbl})
            node["cases"] = cases[:12]
        elif t == "loop":
            node["over"] = str(n.get("over", ""))                 # expression → JSON list or newline list
            node["as"] = str(n.get("as", "item")).strip()[:40] or "item"
        elif t == "merge":
            node["mode"] = n.get("mode") if n.get("mode") in ("concat", "json") else "concat"
        elif t == "http":
            node["method"] = n.get("method") if n.get("method") in _HTTP_METHODS else "GET"
            node["url"] = str(n.get("url", "")).strip()
            raw_h = n.get("headers") if isinstance(n.get("headers"), dict) else {}
            # header values (Authorization tokens etc.) are encrypted at rest, like the MCP
            # client's; the read path (_open_secrets) hands plaintext back to the editor/run
            node["headers"] = {str(k): secretcrypto.encrypt(str(v)) for k, v in raw_h.items()}
            node["body"] = str(n.get("body", ""))
        elif t == "subflow":
            node["workflow"] = str(n.get("workflow", "")).strip()         # target name or id
            node["wfInput"] = str(n.get("wfInput", ""))
        elif t == "transform":
            node["mode"] = n.get("mode") if n.get("mode") in _TRANSFORM_MODES else "template"
            node["source"] = str(n.get("source", ""))            # input expression (default: last output)
            node["text"] = str(n.get("text", ""))                # template / regex / json path / python
        elif t == "approval":
            node["prompt"] = str(n.get("prompt", ""))
            try:
                node["timeout"] = max(1, min(int(n.get("timeout", 60)), 1440))   # minutes
            except (TypeError, ValueError):
                node["timeout"] = 60
        elif t == "wait":
            try:
                node["minutes"] = max(1, min(int(n.get("minutes", 1)), 1440))
            except (TypeError, ValueError):
                node["minutes"] = 1
            until = str(n.get("until", "")).strip()               # HH:MM beats minutes when set
            node["until"] = until if re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", until) else ""
        nodes.append(node)
    ids = {n["id"] for n in nodes}
    edges = []
    for e in graph.get("edges", []) or []:
        if isinstance(e, dict) and e.get("from") in ids and e.get("to") in ids:
            br = e.get("branch")
            br = str(br)[:40] if br not in (None, "") else None   # yes/no, switch case labels, loop/done, error
            edges.append({"from": e["from"], "to": e["to"], "branch": br})
    return {"nodes": nodes, "edges": edges}


# ---------------- input / arguments (a workflow as a reusable "skeleton") ----------------
# A workflow may declare it takes ONE input value. At run time that value is substituted into any
# node text/args via the {{input}} placeholder AND seeded into the shared Agent's context, so the
# same graph can process different values each run. The `default` is used when a run is triggered
# with no explicit value (e.g. a scheduled run).
_DEFAULT_INPUT = {"enabled": False, "label": "", "placeholder": "", "required": False, "default": ""}


def _norm_input(d):
    if not isinstance(d, dict):
        return dict(_DEFAULT_INPUT)
    return {"enabled": bool(d.get("enabled")),
            "label": str(d.get("label", ""))[:80],
            "placeholder": str(d.get("placeholder", ""))[:160],
            "required": bool(d.get("required")),
            "default": str(d.get("default", ""))[:4000]}


# ---------------- templating: data flow between nodes (issue 8 A) ----------------
# A node's text/args can reference earlier values with {{...}} tokens:
#   {{input}}            the run's input value
#   {{last}}             the previous node's output
#   {{node.<id>}}        a specific earlier node's output (also {{node.<id>.output}}, {{step.<id>}})
#   {{item}} {{index}}   the current element/position inside a loop (foreach) node
# input/last/item/node.<id> also take a dotted path that digs into JSON output — case-sensitive
# keys, integer parts index lists: {{last.result.url}} · {{node.7.items.0.name}} · {{item.email}}.
# ({{node.<id>.output}} stays the WHOLE output for backward compatibility, even if the JSON has
# an "output" key — use {{node.<id>.output.<path>}} to dig past it.)
# Unknown tokens render empty (never leak the literal braces or internal state).
_TMPL_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


def _walk_path(cur, path):
    """Walk a dotted/bracketed path (a.b.0.c or a[0].b) through parsed JSON; None when a hop
    is missing — the caller turns that into the usual silent-empty render."""
    for part in [p for p in re.split(r"[.\[\]]", path) if p]:
        try:
            cur = cur[int(part)] if part.lstrip("-").isdigit() else cur[part]
        except (KeyError, IndexError, TypeError):
            return None
    return cur


def _dig(raw, path):
    """JSON-parse a value's (string) output and walk `path` into it. '' when the value isn't
    JSON or the path is absent; non-string leaves render as compact JSON."""
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    val = _walk_path(obj, path)
    if val is None:
        return ""
    return val if isinstance(val, str) else json.dumps(val)


def _resolve_token(expr, ctx):
    expr = expr.strip()
    low = expr.lower()
    if low in ("index", "loop.index", "i"):
        return str(ctx.get("index", ""))
    if low == "loop.item":
        return str(ctx.get("item") or "")
    m = re.match(r"(input|last|prev|previous|output|item)(?:\.(.+))?$", expr, re.IGNORECASE)
    if m:
        base = {"input": "input", "item": "item"}.get(m.group(1).lower(), "last")
        raw = str(ctx.get(base) or "")
        return _dig(raw, m.group(2)) if m.group(2) else raw
    m = re.match(r"(?:nodes?|steps?)\.(\d+)(?:\.(.+))?$", expr, re.IGNORECASE)
    if m:
        raw = str(ctx.get("nodes", {}).get(int(m.group(1)), ""))
        path = m.group(2)
        if not path or path.lower() == "output":     # bare/.output → the whole value (compat)
            return raw
        return _dig(raw, path)
    return ""


def _tmpl(value, ctx):
    """Render {{...}} references in a string (or recursively through dict/list tool-args)."""
    if isinstance(value, str):
        return _TMPL_RE.sub(lambda m: _resolve_token(m.group(1), ctx), value)
    if isinstance(value, dict):
        return {k: _tmpl(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_tmpl(v, ctx) for v in value]
    return value


def _persona_prefix(persona):
    """An instruction/delegate/agent node's optional `persona` field names a published skill
    (e.g. "persona-devils-advocate") whose body is prepended to that node's task/text as an
    identity/voice/rules brief. "" (default) is a no-op — this never changes existing workflows.
    A missing/unpublished skill also degrades to a no-op rather than leaking the lookup-failure
    message into a real prompt."""
    persona = (persona or "").strip()
    if not persona:
        return ""
    from oceano import skills
    body = (skills.load_skill(persona) or "").strip()
    if not body or body.startswith("(no such skill") or body.startswith("(skill "):
        return ""
    return body + "\n\n---\n\n"


# ---------------- CRUD ----------------
# what a second run starting while one is in flight does: "skip" (default) records a skipped
# run instead of racing the live one; "allow" is the explicit opt-in to overlapping runs.
_OVERLAP = ("skip", "allow")


def _open_secrets(wf):
    """Decrypt the at-rest-encrypted fields (http header values) of a freshly-loaded workflow.
    Every read path (list_all/get/get_by_name and create/update's return) goes through this, so
    the editor round-trips plaintext while the store keeps ciphertext. Legacy plaintext passes
    through unchanged (secretcrypto.decrypt's contract)."""
    if wf:
        for n in (wf.get("graph") or {}).get("nodes", []):
            if n.get("type") == "http" and isinstance(n.get("headers"), dict):
                n["headers"] = {k: secretcrypto.decrypt(v) if isinstance(v, str) else v
                                for k, v in n["headers"].items()}
    return wf


def list_all():
    return [_open_secrets(w) for w in _load()["workflows"]]


def get(wid):
    return _open_secrets(next((w for w in _load()["workflows"] if w["id"] == wid), None))


def get_by_name(name):
    name = (name or "").strip().lower()
    return _open_secrets(next((w for w in _load()["workflows"] if w["name"].strip().lower() == name), None))


def create(name, description="", graph=None, input_cfg=None, overlap=None):
    data = _load()
    wf = {"id": _next_id(data["workflows"]), "name": (name or "Untitled").strip(),
          "description": (description or "").strip(), "graph": _norm_graph(graph or {}),
          "input": _norm_input(input_cfg), "triggers": [], "created": _now(),
          "overlap": overlap if overlap in _OVERLAP else "skip"}
    _apply_graph_triggers(wf)
    cron = wf.pop("_graph_cron", None)
    data["workflows"].append(wf)
    _save(data)
    if cron is not None:                              # a schedule trigger node → register/clear the cron task
        set_schedule(wf["id"], cron)
    return _open_secrets(wf)


def update(wid, name=None, description=None, graph=None, input_cfg=None, overlap=None):
    data = _load()
    wf = next((w for w in data["workflows"] if w["id"] == wid), None)
    if not wf:
        return None
    if name is not None:
        wf["name"] = name.strip()
    if description is not None:
        wf["description"] = description.strip()
    if overlap in _OVERLAP:
        wf["overlap"] = overlap
    cron = None
    if graph is not None:
        wf["graph"] = _norm_graph(graph)
        _apply_graph_triggers(wf)
        cron = wf.pop("_graph_cron", None)
    if input_cfg is not None:
        wf["input"] = _norm_input(input_cfg)
    _save(data)
    if cron is not None:                              # canvas schedule node is the source of truth for the cron
        set_schedule(wid, cron)
    if name is not None:
        t = _task_for(wid)
        if t:
            from oceano import scheduler
            scheduler.update_task(t["id"], instruction=SCHED_PREFIX + wf["name"], allow_managed=True)
    return _open_secrets(wf)


def remove(wid):
    data = _load()
    before = len(data["workflows"])
    data["workflows"] = [w for w in data["workflows"] if w["id"] != wid]
    _save(data)
    _save_runs([r for r in _load_runs() if r.get("workflow_id") != wid])
    t = _task_for(wid)
    if t:
        from oceano import scheduler
        scheduler.delete_task(t["id"], allow_managed=True)
    return len(data["workflows"]) < before


def wipe():
    """Remove EVERY workflow (Settings → Wipe): the definitions, all run history, and each
    workflow's mirrored scheduler entry. Event triggers (watched folders, webhooks, chat
    keywords, mail watches) die with the store — they're read from it live. Returns how
    many workflows were removed."""
    data = _load()
    ids = [w["id"] for w in data["workflows"]]
    data["workflows"] = []
    _save(data)
    _save_runs([])
    _WATCH_SIG.clear()
    _EMAIL_SEEN.clear()
    _trig_save()
    from oceano import scheduler
    for wid in ids:
        t = _task_for(wid)
        if t:
            scheduler.delete_task(t["id"], allow_managed=True)
    return len(ids)


# ---------------- export / import / duplicate ----------------
def export_wf(wid):
    """A workflow as a portable JSON document: definition + triggers + cron, minus anything
    machine-local — ids, run history, and every webhook secret (minted fresh on import, so a
    shared export can never carry a live trigger URL)."""
    wf = get(wid)
    if not wf:
        return None
    out = json.loads(json.dumps({k: wf.get(k) for k in
                                 ("name", "description", "graph", "input", "triggers", "overlap")}))
    for t in out.get("triggers") or []:
        t.pop("token", None)
    for n in (out.get("graph") or {}).get("nodes", []):
        if n.get("type") == "trigger":
            n.pop("token", None)
    sched = schedule_info(wid)
    out["cron"] = (sched or {}).get("cron", "")
    return out


def import_wf(payload, replace=False):
    """Create a workflow from an export_wf document. Webhook tokens are regenerated and an
    exported cron is re-registered. A name that's already taken is de-duped ("x (2)") — or,
    with replace=True, the existing workflow is UPDATED in place instead: same id, run history
    kept, definition/triggers/schedule taken wholesale from the document. Returns the workflow,
    or None if the payload isn't workflow-shaped."""
    if not isinstance(payload, dict) or not isinstance(payload.get("graph"), dict):
        return None
    base = str(payload.get("name") or "Imported workflow").strip() or "Imported workflow"
    cron = str(payload.get("cron") or "").strip()
    trigs = _norm_triggers(payload.get("triggers"))
    old = get_by_name(base) if replace else None
    if old:
        wf = update(old["id"], description=str(payload.get("description") or ""),
                    graph=payload.get("graph"), input_cfg=payload.get("input"),
                    overlap=payload.get("overlap"))
        # graph trigger nodes (if any) won on update; otherwise the document's side-panel
        # triggers and cron replace whatever the old definition had — replace means replace
        if not any(n.get("type") == "trigger" for n in wf["graph"]["nodes"]):
            set_triggers(wf["id"], trigs)
            set_schedule(wf["id"], cron)
        return get(wf["id"])
    existing = {w["name"].strip().lower() for w in list_all()}
    name, i = base, 2
    while name.strip().lower() in existing:
        name, i = f"{base} ({i})", i + 1
    wf = create(name, str(payload.get("description") or ""), payload.get("graph"),
                input_cfg=payload.get("input"), overlap=payload.get("overlap"))
    if trigs and not wf.get("triggers"):     # graph trigger nodes (if any) already won on create
        set_triggers(wf["id"], trigs)
    if cron and not _task_for(wf["id"]):
        set_schedule(wf["id"], cron)
    return get(wf["id"])


def duplicate(wid):
    src = export_wf(wid)
    return import_wf(src) if src else None


# ---------------- scheduling ----------------
def _task_for(wid):
    from oceano import scheduler
    src = SOURCE_PREFIX + str(wid)
    return next((t for t in scheduler.all_tasks() if t.get("source") == src), None)


def schedule_info(wid):
    t = _task_for(wid)
    return {"cron": t["cron"], "enabled": t["enabled"], "next_run": t.get("next_run")} if t else None


def set_schedule(wid, cron):
    from oceano import scheduler
    wf = get(wid)
    if not wf:
        return None
    cron = (cron or "").strip()
    t = _task_for(wid)
    if not cron:
        if t:
            scheduler.delete_task(t["id"], allow_managed=True)
        return None
    label = SCHED_PREFIX + wf["name"]
    if t:
        scheduler.update_task(t["id"], cron=cron, instruction=label, allow_managed=True)
        return t["id"]
    return scheduler.add_task(cron, label, source=SOURCE_PREFIX + str(wid))


# ---------------- run history ----------------
def runs(workflow_id=None, limit=40):
    _load()                              # the one-time legacy-runs migration lives in _load()
    rs = _load_runs()
    if workflow_id is not None:
        rs = [r for r in rs if r.get("workflow_id") == workflow_id]
    return list(reversed(rs[-limit:]))


def _prune_live():
    now = time.time()
    for k in [k for k, v in _LIVE.items()
              if (v.get("finished") and now - v["finished"] > _LIVE_KEEP)
              or (v.get("status") == "running" and now - v.get("beat", now) > _LIVE_STALE)]:
        _LIVE.pop(k, None)


def live(workflow_id=None):
    """In-progress (and just-finished) runs so the GUI can reconnect after a refresh.
    Returns a list (or the single entry for workflow_id, or None)."""
    with _LIVE_LOCK:
        _prune_live()
        vals = [{**v, "steps": list(v.get("steps") or [])} for v in _LIVE.values()]
    if workflow_id is not None:
        return next((v for v in vals if v["workflow_id"] == workflow_id), None)
    return vals


# ---------------- triggers (event-based runs: watch · webhook · keyword · chain · email) ----------------
_TRIGGER_TYPES = ("watch", "webhook", "keyword", "chain", "email")
_WATCH_SIG = {}                      # (wid, folder) -> last signature; baseline on first sight
_EMAIL_SEEN = {}                     # (wid, account, folder) -> highest seen uid; baseline on first sight
_trig_loaded = False


def _trig_load():
    """Restore the watch/email baselines persisted by the last process, so a restart neither
    re-baselines (silently swallowing anything that happened while Oceano was down) nor
    replays old state. Keys are JSON-encoded tuples (JSON objects can't key on tuples)."""
    global _trig_loaded
    if _trig_loaded:
        return
    _trig_loaded = True
    try:
        d = json.loads(TRIG_STATE.read_text())
    except (OSError, json.JSONDecodeError):
        d = {}
    for k, v in (d.get("watch") or {}).items():
        try:
            wid, folder = json.loads(k)
            _WATCH_SIG[(wid, folder)] = v
        except (ValueError, TypeError):
            continue
    for k, v in (d.get("email") or {}).items():
        try:
            wid, acct, folder = json.loads(k)
            _EMAIL_SEEN[(wid, acct, folder)] = v
        except (ValueError, TypeError):
            continue


def _trig_save():
    d = {"watch": {json.dumps(list(k)): v for k, v in _WATCH_SIG.items()},
         "email": {json.dumps(list(k)): v for k, v in _EMAIL_SEEN.items()}}
    try:
        atomicio.write_text(TRIG_STATE, json.dumps(d))
    except OSError:
        pass


def _norm_triggers(items):
    out = []
    for t in items or []:
        if not isinstance(t, dict) or t.get("type") not in _TRIGGER_TYPES:
            continue
        ty = t["type"]
        n = {"type": ty, "enabled": bool(t.get("enabled", True))}
        if ty == "watch":
            n["folder"] = str(t.get("folder", "")).strip().strip("/")
            if not n["folder"]:
                continue
        elif ty == "webhook":
            n["token"] = str(t.get("token") or "").strip() or secrets.token_urlsafe(18)
        elif ty == "keyword":
            n["pattern"] = str(t.get("pattern", "")).strip()
            n["channel"] = t.get("channel") if t.get("channel") in ("any", "web", "telegram") else "any"
            if not n["pattern"]:
                continue
        elif ty == "chain":
            try:
                n["after"] = int(t.get("after"))
            except (TypeError, ValueError):
                continue
            n["on"] = t.get("on") if t.get("on") in ("success", "any") else "success"
        elif ty == "email":
            n["account"] = str(t.get("account", "")).strip()
            n["folder"] = str(t.get("folder", "INBOX") or "INBOX").strip()
            if not n["account"]:
                continue
        out.append(n)
    return out


# ---------------- triggers declared as nodes ON the canvas (issue 8 C) ----------------
def _fill_webhook_tokens(graph):
    """Give every webhook trigger node a stable secret token (so its URL is shown + reused)."""
    for n in (graph or {}).get("nodes", []):
        if n.get("type") == "trigger" and n.get("kind") == "webhook" and not n.get("token"):
            n["token"] = secrets.token_urlsafe(18)


def _triggers_from_graph(graph):
    """Derive (trigger records, cron|None) from the trigger nodes on the canvas — so triggers live on
    the graph, not only in a side panel. Schedule nodes set the cron; the rest become triggers."""
    triggers, cron = [], None
    for n in (graph or {}).get("nodes", []):
        if n.get("type") != "trigger":
            continue
        k = n.get("kind")
        if k == "schedule" and n.get("cron"):
            cron = n["cron"]
        elif k == "watch" and n.get("folder"):
            triggers.append({"type": "watch", "enabled": True, "folder": n["folder"]})
        elif k == "webhook":
            triggers.append({"type": "webhook", "enabled": True, "token": n.get("token") or secrets.token_urlsafe(18)})
        elif k == "keyword" and n.get("pattern"):
            triggers.append({"type": "keyword", "enabled": True, "pattern": n["pattern"], "channel": n.get("channel", "any")})
        elif k == "email" and n.get("account"):
            triggers.append({"type": "email", "enabled": True, "account": n["account"], "folder": n.get("mailFolder", "INBOX")})
    return triggers, cron


def _apply_graph_triggers(wf):
    """If the graph carries trigger nodes, make them the source of truth for this workflow's triggers
    and cron schedule (called on save). No trigger nodes → the side-panel triggers are left untouched."""
    graph = wf.get("graph") or {}
    _fill_webhook_tokens(graph)
    if not any(n.get("type") == "trigger" for n in graph.get("nodes", [])):
        return
    trigs, cron = _triggers_from_graph(graph)
    wf["triggers"] = _norm_triggers(trigs)
    wf["_graph_cron"] = cron or ""       # picked up by the caller to (re)set the scheduler after save


def get_triggers(wid):
    wf = get(wid)
    return wf.get("triggers", []) if wf else []


def set_triggers(wid, items):
    data = _load()
    wf = next((w for w in data["workflows"] if w["id"] == wid), None)
    if not wf:
        return None
    wf["triggers"] = _norm_triggers(items)
    _save(data)
    return wf["triggers"]


def run_async(wf, trigger="trigger", chain_seen=frozenset(), inp=""):
    """Fire-and-forget a run in a daemon thread (used by every event trigger)."""
    threading.Thread(target=lambda: run(wf, trigger=trigger, _chain_seen=chain_seen, inp=inp), daemon=True).start()


def _folder_sig(folder):
    base = (config.WORKSPACE / folder).resolve()
    if not str(base).startswith(str(config.WORKSPACE.resolve())):   # stay inside the workspace
        return None
    if not base.exists():
        return 0
    items = []
    for p in sorted(base.rglob("*"))[:5000]:
        if p.is_file():
            try:
                st = p.stat()
                items.append((str(p), int(st.st_mtime), st.st_size))
            except OSError:
                pass
    # stable digest, NOT hash(): per-process hash salting would make every persisted baseline
    # mismatch after a restart, firing every watch trigger spuriously
    return hashlib.md5(repr(items).encode()).hexdigest()


def poll_watch_triggers():
    """Run workflows whose watched folder changed since the last tick (called by the engine).
    First sight of a folder records a baseline only — but baselines PERSIST across restarts
    (data/trigger_state.json), so a change made while Oceano was down still fires."""
    _trig_load()
    fired, dirty = 0, False
    for wf in list_all():
        for tr in wf.get("triggers", []):
            if tr.get("type") != "watch" or not tr.get("enabled"):
                continue
            sig = _folder_sig(tr["folder"])
            if sig is None:
                continue
            key = (wf["id"], tr["folder"])
            prev = _WATCH_SIG.get(key, "__new__")
            if sig != prev:
                _WATCH_SIG[key] = sig
                dirty = True
            if prev != "__new__" and sig != prev:
                run_async(wf, trigger="watch"); fired += 1
    if dirty:
        _trig_save()
    return fired


def poll_email_triggers():
    """Run workflows whose email trigger sees NEW mail since the last tick (called by the engine).
    First sight records a baseline (the current newest uid) so old mail is never replayed — and
    baselines PERSIST across restarts (data/trigger_state.json), so mail that arrived while
    Oceano was down still fires. Each genuinely new message fires the workflow once, with a
    compact From/Subject/body as the run input."""
    from oceano import mail
    _trig_load()
    fired, dirty = 0, False
    for wf in list_all():
        for tr in wf.get("triggers", []):
            if tr.get("type") != "email" or not tr.get("enabled"):
                continue
            acct = mail._resolve(tr.get("account"))
            if not acct:
                continue
            folder = tr.get("folder", "INBOX") or "INBOX"
            res = mail.imap_list(acct, folder=folder, limit=15)
            if not res.get("ok"):
                continue
            key = (wf["id"], acct["id"], folder)
            msgs = res.get("messages", [])
            try:
                newest = max((int(m["uid"]) for m in msgs), default=0)
            except (ValueError, KeyError):
                newest = 0
            prev = _EMAIL_SEEN.get(key)
            if _EMAIL_SEEN.get(key) != newest:
                _EMAIL_SEEN[key] = newest
                dirty = True
            if prev is None:                           # baseline only on very first sight
                continue
            for m in sorted(msgs, key=lambda x: int(x.get("uid", 0))):
                try:
                    uid = int(m["uid"])
                except (ValueError, KeyError):
                    continue
                if uid <= prev:
                    continue
                full = mail.imap_read(acct, str(uid), folder)
                inp = (f"From: {full.get('from','')}\nSubject: {full.get('subject','')}\n\n"
                       f"{full.get('body','')}" if full.get("ok")
                       else f"From: {m.get('from','')}\nSubject: {m.get('subject','')}")
                run_async(wf, trigger="email", inp=inp); fired += 1
    if dirty:
        _trig_save()
    return fired


def fire_keyword(message, channel="web"):
    """Run workflows whose keyword trigger matches a chat message. The full message becomes the
    run's input (so a keyword-triggered workflow can process what the user actually said).
    Returns the names fired."""
    raw = (message or "").strip()
    msg = raw.lower()
    fired = []
    if not msg:
        return fired
    for wf in list_all():
        for tr in wf.get("triggers", []):
            if tr.get("type") != "keyword" or not tr.get("enabled") or tr.get("channel") not in ("any", channel):
                continue
            pat = (tr.get("pattern") or "").strip().lower()
            if pat and pat in msg:
                run_async(wf, trigger="keyword", inp=raw); fired.append(wf["name"]); break
    return fired


def fire_chain(after_wid, status, seen=frozenset(), out=""):
    """When a workflow finishes, run any workflow chained after it (loop-guarded by `seen`).
    The upstream workflow's final output is handed to the next as its input — so data flows
    down a chain."""
    for wf in list_all():
        if wf["id"] in seen:
            continue
        for tr in wf.get("triggers", []):
            if (tr.get("type") == "chain" and tr.get("enabled") and tr.get("after") == after_wid
                    and (tr.get("on") == "any" or status == "ok")):
                run_async(wf, trigger="chain", chain_seen=seen, inp=out)
                break


def webhook_run(wid, token, inp=""):
    """Run a workflow if `token` matches one of its enabled webhook triggers. The POST body may
    carry an input value (see the web endpoint) that the workflow processes."""
    wf = _webhook_match(wid, token)
    if wf:
        run_async(wf, trigger="webhook", inp=inp)
    return wf


def webhook_run_sync(wid, token, inp=""):
    """The synchronous flavour (webhook ?wait=1): runs the workflow inline and returns its run
    record, so the HTTP caller gets the final output back. None if the token doesn't match."""
    wf = _webhook_match(wid, token)
    return run(wf, trigger="webhook", inp=inp) if wf else None


def _webhook_match(wid, token):
    wf = get(wid)
    if not wf:
        return None
    for tr in wf.get("triggers", []):
        if (tr.get("type") == "webhook" and tr.get("enabled")
                and secrets.compare_digest(str(tr.get("token", "")), str(token))):
            return wf
    return None


def _record_run(workflow_id, trigger, status, steps, summary):
    rs = _load_runs()
    rec = {"id": _next_id(rs), "workflow_id": workflow_id, "ts": _now(),
           "trigger": trigger, "status": status, "summary": summary, "steps": steps}
    rs.append(rec)
    by = {}                              # prune per workflow — a busy flow can't starve the rest
    for r in rs:
        by.setdefault(r.get("workflow_id"), []).append(r)
    keep = [r for group in by.values() for r in group[-_RUNS_PER_WF:]]
    keep.sort(key=lambda r: r["id"])
    _save_runs(keep)
    return rec


# ---------------- decision evaluation ----------------
def _num(s):
    try:
        return float(re.search(r"-?\d+(?:\.\d+)?", str(s)).group(0))
    except (AttributeError, ValueError):
        return None


def _yesno(text):
    t = (text or "").strip().lower()
    if not t:
        return False
    head = t[:24]
    if "yes" in head and "no" not in head.split():
        return True
    return head.startswith("yes") or head.startswith("true") or head.startswith("y ")


def _decide(node, last_output, ag):
    """Return (branch_bool, detail_str) for a decision node."""
    mode = node.get("mode", "model")
    if mode == "rule":
        src, op, val = last_output or "", node.get("ruleOp", "contains"), str(node.get("ruleValue", ""))
        if op == "contains":
            v = val.lower() in src.lower()
        elif op == "equals":
            v = src.strip() == val.strip()
        elif op == "matches":
            try:
                v = re.search(val, src) is not None
            except re.error:
                v = False
        elif op in ("gt", "lt"):
            a, b = _num(src), _num(val)
            v = (a is not None and b is not None and (a > b if op == "gt" else a < b))
        else:
            v = False
        return v, f"rule: output {op} {val!r} → {'yes' if v else 'no'}"
    q = node.get("question", "") or "Should the workflow take the YES branch?"
    if mode == "delegate":
        from oceano import delegate
        r = delegate.run(f"{q}\n\nMost recent step output:\n{last_output[:2000]}\n\n"
                         "Answer with exactly one word: YES or NO.",
                         cwd=config.WORKSPACE, tools="Read", timeout=300, role=node.get("role", "default"))
        txt = (r.get("output") or "") if r.get("ok") else ""
        return _yesno(txt), f"delegate: {txt.strip()[:60] or '(no answer)'}"
    # mode == "model" — judged by the PRIMARY INTELLIGENCE, exactly like an un-pinned
    # instruction node: the mind (Claude/Codex CLI) when one is set and available, else the
    # primary model at its endpoint. A bare llm.chat() would fall back to the local llama-swap
    # defaults and boot the resident model mid-workflow — that bug shipped twice (first when
    # the primary was a remote endpoint, then when the primary was a CLI mind).
    from oceano import delegate
    prompt = (f"You are a decision gate in a workflow.\n\n{q}\n\n"
              f"Latest step output:\n{last_output[:2000]}\n\n"
              "Answer with exactly one word: YES or NO.")
    mind = delegate.get_mind()
    if mind == "claude" and delegate.available():
        r = delegate.to_claude(prompt, cwd=config.WORKSPACE, tools="Read", timeout=180)
        txt = (r.get("output") or "") if r.get("ok") else ""
        return _yesno(txt), f"model: {txt.strip()[:40] or '(blank)'}"
    if mind == "codex" and delegate.codex_available():
        r = delegate.to_codex(prompt, cwd=config.WORKSPACE, tools="Read", timeout=180)
        txt = (r.get("output") or "") if r.get("ok") else ""
        return _yesno(txt), f"model: {txt.strip()[:40] or '(blank)'}"
    from oceano import llm
    from oceano.agent import _default_primary
    dm, db, dk = _default_primary()
    msg = llm.chat([{"role": "system", "content": "You are a decision gate in a workflow. "
                     "Read the question and the latest output, then answer with exactly one word: YES or NO."},
                    {"role": "user", "content": f"{q}\n\nLatest step output:\n{last_output[:2000]}"}],
                   tools=None, model=dm or None, base_url=db, api_key=dk)
    txt = (getattr(msg, "content", "") or "")
    return _yesno(txt), f"model: {txt.strip()[:40] or '(blank)'}"


# ---------------- multi-branch switch (issue 8 A) ----------------
def _match(op, src, val):
    if op == "contains":
        return val.lower() in src.lower()
    if op == "equals":
        return src.strip() == val.strip()
    if op == "matches":
        try:
            return re.search(val, src) is not None
        except re.error:
            return False
    if op in ("gt", "lt"):
        a, b = _num(src), _num(val)
        return a is not None and b is not None and (a > b if op == "gt" else a < b)
    return False


def _run_switch(node, ctx):
    """Route to the first matching case's labelled edge, else the 'default' edge."""
    src = _tmpl(node.get("source", "") or "{{last}}", ctx)
    for c in node.get("cases", []):
        if _match(c.get("op", "contains"), src, _tmpl(c.get("value", ""), ctx)):
            return c["label"], f"switch → {c['label']}"
    return "default", "switch → default"


# ---------------- HTTP request node (issue 8 B) ----------------
def _run_http(node, ctx):
    from oceano import safety
    method = node.get("method", "GET")
    # {{secret.X}} fills FIRST (only _run_http ever resolves it), then normal templating; every
    # resolved secret is redacted from whatever this node returns/records
    used = []
    url = _tmpl(_fill_secrets(node.get("url", ""), used), ctx).strip()
    if not url:
        return False, "no URL"
    headers = {str(k): _tmpl(_fill_secrets(str(v), used), ctx) for k, v in (node.get("headers") or {}).items()}
    body = _tmpl(_fill_secrets(node.get("body", ""), used), ctx)
    import requests
    # SSRF guard: the URL may be templated from untrusted upstream data ({{last}}, an email body…),
    # so a public URL that redirects to a loopback/metadata address would slip past a one-time check.
    # Follow redirects MANUALLY and re-validate every hop before issuing it.
    cur = url
    try:
        for _ in range(6):
            refusal = safety.check_url(cur)
            if refusal:
                return False, _redact(refusal, used)
            resp = requests.request(method, cur, headers=headers or None,
                                    data=body.encode("utf-8") if body else None,
                                    timeout=30, allow_redirects=False)
            if resp.is_redirect and resp.headers.get("Location"):
                cur = requests.compat.urljoin(cur, resp.headers["Location"])
                continue
            return resp.ok, _redact(f"HTTP {resp.status_code}\n{resp.text[:_HTTP_CAP]}", used)
        return False, "too many redirects"
    except Exception as e:                           # noqa: BLE001
        return False, _redact(f"request failed: {e}", used)


# ---------------- transform / code node (issue 8 B) ----------------
def _run_transform(node, ctx):
    mode = node.get("mode", "template")
    src = _tmpl(node.get("source", "") or "{{last}}", ctx)
    text = node.get("text", "")
    if mode == "template":
        return True, _tmpl(text, ctx)
    if mode == "regex":
        pat = _tmpl(text, ctx)
        try:
            m = re.search(pat, src, re.DOTALL)
        except re.error as e:
            return False, f"bad regex: {e}"
        return True, ((m.group(1) if m.groups() else m.group(0)) if m else "")
    if mode == "jsonpath":
        try:
            cur = json.loads(src)
        except Exception:                            # noqa: BLE001
            return False, "input is not JSON"
        cur = _walk_path(cur, _tmpl(text, ctx).strip())
        if cur is None:
            return True, ""
        return True, cur if isinstance(cur, str) else json.dumps(cur)
    if mode == "python":
        # run via the existing workspace-confined + guarded python_exec tool; `value` holds the input
        code = "value = " + json.dumps(src) + "\n" + _tmpl(text, ctx)
        return True, (tools.run("python_exec", json.dumps({"code": code})) or "")
    return False, f"unknown transform mode {mode!r}"


# ---------------- sub-workflow node (issue 8 B) ----------------
def _run_subflow(node, ctx, depth):
    if depth >= _SUBFLOW_DEPTH:
        return False, "sub-workflow nesting too deep"
    ref = _tmpl(node.get("workflow", ""), ctx).strip()
    sub = get(int(ref)) if ref.isdigit() else None
    sub = sub or get_by_name(ref)
    if not sub:
        return False, f"no workflow {ref!r}"
    sub_inp = _tmpl(node.get("wfInput", "") or "{{last}}", ctx)
    rec = run(sub, trigger="subflow", inp=sub_inp, _depth=depth + 1, nested=True)
    out = (rec or {}).get("output") or (rec or {}).get("summary", "")
    return (rec or {}).get("status") == "ok", out


# ---------------- wait node: pause for a duration or until a clock time ----------------
def _wait_seconds(node):
    """A wait node's delay: seconds until the next occurrence of `until` (HH:MM, local clock)
    when set, else `minutes`. _norm_graph caps both at a day."""
    until = node.get("until") or ""
    if until:
        h, m = until.split(":")
        now = datetime.now()
        target = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()
    return max(1, int(node.get("minutes", 1))) * 60


# ---------------- human-in-the-loop approval (issue 8 D) ----------------
_APPROVALS = {}                      # token -> {event, approved, wf, prompt, since}
_APPROVAL_LOCK = threading.Lock()


def pending_approvals(workflow_id=None):
    with _APPROVAL_LOCK:
        out = [{"token": k, "workflow_id": v["wf"], "prompt": v["prompt"], "since": v["since"]}
               for k, v in _APPROVALS.items() if not v["event"].is_set()]
    return [a for a in out if workflow_id is None or a["workflow_id"] == workflow_id]


def resolve_approval(token, approved):
    with _APPROVAL_LOCK:
        a = _APPROVALS.get(token)
        if not a:
            return False
        a["approved"] = bool(approved)
        a["event"].set()
    return True


def _await_approval(wf_id, prompt, timeout_min, beat):
    token = secrets.token_urlsafe(10)
    ev = threading.Event()
    with _APPROVAL_LOCK:
        _APPROVALS[token] = {"event": ev, "approved": False, "wf": wf_id, "prompt": prompt, "since": time.time()}
    with _LIVE_LOCK:
        st = _LIVE.get(wf_id)
        if st is not None:
            st["awaiting"] = {"token": token, "prompt": prompt}
    deadline = time.time() + max(60, timeout_min * 60)
    while time.time() < deadline:
        if ev.wait(timeout=30):
            break
        beat()                                       # keep the live entry from being pruned while we wait
    with _APPROVAL_LOCK:
        a = _APPROVALS.pop(token, None)
    with _LIVE_LOCK:
        st = _LIVE.get(wf_id)
        if st is not None:
            st["awaiting"] = None
    if a is None or not a["event"].is_set():
        return False, "approval timed out"
    return a["approved"], ("approved" if a["approved"] else "rejected")


def _pinned_agent(node, ag):
    """An Agent on the node's pinned endpoint model that SHARES the run's conversation (the same
    messages list object), so cross-node context keeps accumulating no matter which model took the
    turn. The endpoint's API key resolves like delegate.to_api's (lazy web import; '' if absent)."""
    from oceano.agent import Agent
    base_url = node.get("baseUrl") or ""
    api_key = ""
    if base_url:
        try:
            from oceano.web import server      # lazy: avoid an import cycle at module load
            api_key = server.endpoint_key(base_url)
        except Exception:
            api_key = ""
    pinned = Agent(model=node["model"], base_url=base_url or None,
                   api_key=api_key or "sk-no-key-needed", learn=False,
                   exclude_tools={"run_workflow"})
    pinned.messages = ag.messages
    pinned.on_event = ag.on_event
    return pinned


def _run_orchestrate(node, agents, ctx, ag, spawned, emit, beat):
    """Run the agent nodes plugged into an orchestrator. node['plan'] maps agent node id -> step
    number; unlisted agents default to step 1. Each step's agents spawn in parallel and the step is
    joined before the next starts — later steps get earlier results appended to their task (and can
    also reference them as {{node.ID}}). An agent that fails or times out gets ONE serial retry
    (endpoints with per-key concurrency limits or long first-token queues often succeed alone);
    only then does the step fail, stopping later steps and routing the error edge with the partial
    compile as output. mode 'summarize' rewrites the compile with the shared agent (same engine as
    an instruction node), using node['text'] as the brief.
    Returns (ok, compiled, step_records) — step_records is one {id,type,label,ok,output} dict per
    attached agent that actually ran, in plugged-in order, so the caller can fold them into the
    run's persisted step list. Without this, attached agents show up live (the diagram, the SSE
    log) but vanish from history/reconnect-after-the-fact — only the orchestrator's own single
    compiled entry would survive, since traversal never visits attachment nodes directly."""
    from oceano import agentjobs
    if not agents:
        return False, "orchestrate: no agent nodes are plugged into this orchestrator", []
    plan = node.get("plan") or {}
    step_timeout = node.get("timeout", 900)
    steps = {}
    for a in agents:
        steps.setdefault(int(plan.get(str(a["id"]), 1)), []).append(a)
    name = lambda a: a.get("label") or "agent " + str(a["id"])   # noqa: E731

    def spawn(a, task):
        # synthetic node_start lights the plugged-in agent up on the live run diagram (attached
        # agents are never walked by traversal, so nothing else would emit for them)
        emit({"event": "node_start", "id": a["id"], "type": "agent", "label": _node_label(a)})
        # same read-only-unless-opted-in scope as a standalone agent/delegate node — the
        # attached agent's OWN "write" setting (from its inspector) travels with it into the
        # orchestrator, so plugging an agent in doesn't change its privilege level either way.
        tool_scope = _tool_scope_for(a.get("write"))
        rec = agentjobs.spawn(task, provider=a.get("provider", ""), label=a.get("label", ""),
                              model=a.get("model", ""), base_url=a.get("baseUrl", ""),
                              timeout=a.get("timeout", 600), skills=True,   # may reuse skills; never memory
                              tools=tool_scope, cwd=config.WORKSPACE)
        spawned[a["id"]] = rec["id"]               # a later Await node can still see them
        return rec["id"]

    def join(running):
        deadline = time.time() + step_timeout
        done, failed = {}, {}
        while time.time() < deadline:
            beat()
            left = False
            for nid, aid in running.items():
                if nid in done or nid in failed:
                    continue
                r = agentjobs.status(aid) or {"state": "lost"}
                if r["state"] == "done":
                    done[nid] = r.get("output") or ""
                elif r["state"] in ("failed", "lost"):
                    failed[nid] = r.get("error") or r["state"]
                else:
                    left = True
            if not left:
                break
            time.sleep(2)
        for nid in running:                        # anything still running at the deadline
            if nid not in done and nid not in failed:
                failed[nid] = "timed out (still running)"
        return done, failed

    gathered, failures = [], []      # [(step, agent node, output)] · [(step, agent node, error)]
    step_records = []                # persisted per-agent history rows (see docstring)
    for step in sorted(steps):
        prior = ""
        if gathered:                 # sequence semantics: this step sees everything gathered so far
            prior = "\n\n(Context — results from earlier agents:)\n" + "\n\n".join(
                f"== {name(an)}\n{out[:2000]}" for _s, an, out in gathered)
        by_id = {a["id"]: a for a in steps[step]}
        tasks = {a["id"]: _persona_prefix(a.get("persona", "")) + _tmpl(a.get("task", ""), ctx) + prior
                 for a in steps[step]}
        running = {}
        for a in steps[step]:
            emit({"event": "tool", "id": node["id"],
                  "text": f"step {step} · spawning 🤖 {a.get('label') or a.get('task', '')[:40]}"})
            running[a["id"]] = spawn(a, tasks[a["id"]])
        done, failed = join(running)
        # salvage pass: retry each failed/timed-out agent ONCE, alone — a stalled endpoint (free-tier
        # concurrency caps, long no-token queues past the client read timeout) usually recovers when
        # the request runs serially. Only a failed retry fails the step.
        for nid in list(failed):
            a, first_err = by_id[nid], failed[nid]
            emit({"event": "tool", "id": node["id"],
                  "text": f"step {step} · ⟲ retrying {name(a)} alone (first attempt: {first_err[:80]})"})
            for _ in range(int(_SALVAGE_BACKOFF)):     # brief backoff — an overloaded endpoint
                beat()                                 # retried instantly tends to fail identically
                time.sleep(1)
            try:
                aid = spawn(a, tasks[nid])
            except Exception as ex:                # noqa: BLE001 — cap reached etc.
                failed[nid] = f"{first_err} · retry refused: {ex}"
                continue
            d2, f2 = join({nid: aid})
            if nid in d2:
                done[nid] = d2[nid]
                del failed[nid]
            else:
                failed[nid] = f"{f2[nid]} (on retry; first attempt: {first_err[:120]})"
        for a in steps[step]:                      # keep the plugged-in order deterministic
            nid = a["id"]
            if nid in done:
                ctx["nodes"][nid] = done[nid]      # {{node.ID}} of each agent = its result
                gathered.append((step, a, done[nid]))
                out = done[nid][:_OUT_CAP]
                emit({"event": "node_end", "id": nid, "ok": True, "label": _node_label(a), "output": out})
                step_records.append({"id": nid, "type": "agent", "label": _node_label(a), "ok": True,
                                     "branch": None, "output": out})
            else:
                failures.append((step, a, failed[nid]))
                out = failed[nid][:_OUT_CAP]
                emit({"event": "node_end", "id": nid, "ok": False, "label": _node_label(a), "output": out})
                step_records.append({"id": nid, "type": "agent", "label": _node_label(a), "ok": False,
                                     "branch": None, "output": out})
        if failed:                                 # later steps depend on this one — stop here
            break
    parts = [f"== {name(an)} (step {s})\n{out}" for s, an, out in gathered]
    parts += [f"== {name(an)} (step {s}) FAILED: {err}" for s, an, err in failures]
    compiled = "\n\n".join(parts)
    ok = not failures
    if ok and node.get("mode") == "summarize" and gathered:
        brief = (node.get("text") or "").strip() \
            or "Synthesize the agents' results below into one coherent, complete answer."
        # the compile turn follows the PRIMARY INTELLIGENCE like an un-pinned instruction node
        # (mind → CLI; else the shared agent's model loop) — a bare ag.run() booted the local
        # resident model mid-workflow whenever the mind was Claude/Codex
        from oceano import delegate
        mind = delegate.get_mind()
        prompt = brief + "\n\n" + compiled
        if mind == "claude" and delegate.available():
            compiled = ag.run_claude(prompt) or compiled
        elif mind == "codex" and delegate.codex_available():
            compiled = ag.run_codex(prompt) or compiled
        else:
            compiled = ag.run(prompt) or compiled
    if gathered:
        ag.messages.append({"role": "user", "content": f"(orchestrated agents → {compiled[:1500]})"})
    return ok, compiled, step_records


# ---------------- execution ----------------
def _node_label(n):
    t = n["type"]
    if t == "trigger":
        return "⚡ " + (n.get("kind") or "trigger")
    if t == "tool":
        return "🔧 " + (n.get("tool") or "tool")
    if t == "instruction":
        return (n.get("text", "")[:54] or "instruction")
    if t == "delegate":
        return "↗ " + (n.get("text", "")[:48] or "delegate") + _access_marker(n.get("write"))
    if t == "agent":
        return "🤖 " + (n.get("label") or n.get("task", "")[:44] or "agent") \
            + (f" ({n['provider']})" if n.get("provider") else "") \
            + _access_marker(n.get("write"))
    if t == "await":
        return "⏳ await agents"
    if t == "orchestrate":
        return "🕸 orchestrate agents"
    if t == "decision":
        return "◆ " + (n.get("question", "")[:48] or n.get("mode", "decision"))
    if t == "switch":
        return "⤳ switch"
    if t == "loop":
        return "↻ loop " + (n.get("over", "")[:30])
    if t == "merge":
        return "⧉ merge" + (" → json" if n.get("mode") == "json" else "")
    if t == "http":
        return "🌐 " + (n.get("method", "GET")) + " " + (n.get("url", "")[:36])
    if t == "subflow":
        return "▣ " + (n.get("workflow", "") or "sub-workflow")
    if t == "transform":
        return "ƒ " + (n.get("mode", "transform"))
    if t == "approval":
        return "✋ " + (n.get("prompt", "")[:40] or "approval")
    if t == "wait":
        return "⏲ wait " + (("until " + n["until"]) if n.get("until") else f"{n.get('minutes', 1)}m")
    return t


def _compact_event(kind, data):
    if kind == "tool_call":
        return "→ " + str(data.get("name"))
    if kind == "tool_result":
        r = (data.get("result") or "")
        return f"✓ {data.get('name')}" + (f" · {r[:80]}" if r else "")
    return ""


def _route(node, succ, branch):
    """Pick the next node id from `node`'s outgoing edges given a branch label.
    decision → yes/no edge; switch → the case-label edge (else 'default'); loop → handled by
    caller; everything else → its single edge (an 'error' edge is only taken on failure)."""
    outs = succ.get(node["id"], [])
    t = node["type"]
    if t == "decision":
        return next((to for (br, to) in outs if (br or "yes") == branch), None)
    if t == "switch":
        return (next((to for (br, to) in outs if br == branch), None)
                or next((to for (br, to) in outs if br == "default"), None)
                or next((to for (br, to) in outs if br is None), None))
    # plain nodes: prefer an unlabelled edge; never auto-follow an 'error' edge on success
    return (next((to for (br, to) in outs if br in (None, "next")), None)
            or next((to for (br, to) in outs if br not in ("error",)), None))


def _policy_mode(node):
    t = node.get("type")
    if t == "tool":
        cap = policies.capability_for_tool(node.get("tool", ""))
    elif t == "http":
        cap = "http_request"
    elif t in ("delegate", "agent") and node.get("write") in ("execute", "shell"):
        cap = "shell_exec"
    elif t in ("delegate", "agent") and node.get("write") == "write":
        cap = "workspace_write"
    else:
        cap = ""
    mode = policies.get().get(cap, "allow") if cap else "allow"
    return cap, mode


def _policy_prompt(node, capability):
    if node.get("type") == "tool":
        return (f"Policy approval required for tool `{node.get('tool', '')}` "
                f"(capability: {capability}). Allow this step to proceed?")
    return (f"Policy approval required for workflow node `{_node_label(node)}` "
            f"(capability: {capability}). Allow this step to proceed?")


def _restore_runtime(wf, state):
    graph = wf.get("graph") or {"nodes": [], "edges": []}
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    ctx = dict(state.get("ctx") or {})
    ctx.setdefault("input", "")
    ctx.setdefault("last", "")
    raw_nodes = ctx.get("nodes") or {}
    ctx["nodes"] = {int(k): v for k, v in raw_nodes.items()}
    loop_state = {int(k): v for k, v in (state.get("loop_state") or {}).items()}
    merge_got = {int(k): v for k, v in (state.get("merge_got") or {}).items()}
    merge_done = {int(x) for x in (state.get("merge_done") or [])}
    force_merge = {int(x) for x in (state.get("force_merge") or [])}
    branch_q = [(int(nid), blast) for nid, blast in (state.get("branch_q") or [])]
    spawned = {int(k): v for k, v in (state.get("spawned") or {}).items()}
    return {
        "ctx": ctx,
        "loop_state": loop_state,
        "branch_q": branch_q,
        "merge_got": merge_got,
        "merge_done": merge_done,
        "force_merge": force_merge,
        "spawned": spawned,
        "results": list(state.get("results") or []),
        "last_output": state.get("last_output", ""),
        "visits": int(state.get("visits") or 0),
        "cur": nodes.get(state.get("next_node_id")),
        "messages": list(state.get("agent_messages") or []),
        "run_id": state.get("run_id") or traces.new_run_id("wf"),
    }


def resume(wid, on_step=None):
    st = resume_state(wid)
    if not st:
        return None
    wf = get(wid) or copy.deepcopy(st.get("workflow"))
    if not wf:
        return None
    return run(wf, trigger=st.get("trigger", "resume"), on_step=on_step, inp=st.get("input", ""),
               _resume=st)


def run(wf, trigger="manual", on_step=None, _chain_seen=frozenset(), inp=None, _depth=0,
        nested=False, _resume=None):
    """Walk the workflow graph from its start node, executing nodes and branching at decision/switch
    nodes, iterating loop nodes, retrying failures and taking 'error' edges, and pausing at approval
    nodes. Shares one Agent so context accumulates. Returns the run record (incl. 'output' = last value).

    `inp` is this run's input value: nodes reference it (and any earlier node's output) via {{...}}
    templating, and it's seeded into the agent's context. Empty/None falls back to the stored default.
    `nested`/`_depth` are set when one workflow calls another via a sub-workflow node.
    `_resume` is an internal checkpoint payload created by a prior failed/cancelled run."""
    from oceano.agent import Agent
    wf_id = wf["id"]
    inp = "" if inp is None else str(inp)
    if not inp:
        inp = str((wf.get("input") or {}).get("default") or "")

    def beat():
        with _LIVE_LOCK:
            st = _LIVE.get(wf_id)
            if st is not None:
                st["beat"] = time.time()

    def emit(ev):
        e = ev.get("event")
        if e == "node_start":
            traces.record("workflow_node_start", workflow_id=wf_id, node_id=ev.get("id"),
                          node_type=ev.get("type"), label=ev.get("label"))
        elif e == "node_end":
            traces.record("workflow_node_end", workflow_id=wf_id, node_id=ev.get("id"),
                          ok=ev.get("ok"), branch=ev.get("branch"), output=(ev.get("output") or "")[:500])
        elif e == "done":
            traces.record("workflow_done", workflow_id=wf_id, status=ev.get("status"),
                          summary=((ev.get("run") or {}).get("summary") or ""))
        if not nested:
            with _LIVE_LOCK:
                st = _LIVE.get(wf_id)
                if st is not None:
                    st["beat"] = time.time()
                    if e == "node_start":
                        st["current"] = {"id": ev.get("id"), "label": ev.get("label")}
                    elif e == "node_end":
                        st["steps"].append({"id": ev.get("id"), "label": ev.get("label") or (st.get("current") or {}).get("label", ""),
                                            "ok": ev.get("ok"), "branch": ev.get("branch"), "output": ev.get("output", "")})
                    elif e == "done":
                        r = ev.get("run") or {}
                        st.update(status=ev.get("status", "ok"), current=None, finished=time.time(),
                                  summary=r.get("summary", ""), run_id=r.get("id"))
        if on_step:
            try:
                on_step(ev)
            except Exception:
                pass

    if not nested and _resume is None and (wf.get("overlap") or "skip") != "allow":
        with _LIVE_LOCK:
            _prune_live()
            st = _LIVE.get(wf_id)
            busy = bool(st and st.get("status") == "running")
        if busy:
            rec = _record_run(wf_id, trigger, "skipped", [],
                              "skipped — a run of this workflow is already in progress")
            rec["output"] = ""
            if on_step:
                try:
                    on_step({"event": "done", "status": "skipped", "run": rec})
                except Exception:
                    pass
            return rec

    graph = wf.get("graph") or {"nodes": [], "edges": []}
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    succ = {}
    inbound = {}
    attached = {}
    for e in graph.get("edges", []):
        src, dst = nodes.get(e["from"]), nodes.get(e["to"])
        if (src and dst and src["type"] == "agent" and dst["type"] == "orchestrate"
                and e.get("branch") in (None, "next")):
            attached.setdefault(dst["id"], []).append(src)
            continue
        succ.setdefault(e["from"], []).append((e.get("branch"), e["to"]))
        inbound[e["to"]] = inbound.get(e["to"], 0) + 1

    start_node = next((n for n in graph.get("nodes", []) if n["type"] in ("start", "trigger")), None)
    if not start_node:
        inbound_ids = {e["to"] for e in graph.get("edges", [])}
        start_node = next((n for n in graph.get("nodes", []) if n["id"] not in inbound_ids),
                          graph["nodes"][0] if graph.get("nodes") else None)

    restored = _restore_runtime(wf, _resume) if _resume else None
    run_id = (restored or {}).get("run_id") or traces.new_run_id("wf")
    with traces.scope(run_id=run_id, workflow_id=wf_id, trigger=trigger, nested=nested):
        traces.record("workflow_start", workflow_id=wf_id, name=wf.get("name", ""), resumed=bool(_resume))
        ag = Agent(learn=False, exclude_tools={"run_workflow"})
        if restored:
            ag.messages = list(restored["messages"] or ag.messages)
        elif inp:
            ag.messages.append({"role": "user", "content": f"(workflow input)\n{inp}"})
        ctx = (restored or {}).get("ctx") or {"input": inp, "last": "", "nodes": {}, "item": None, "index": None}
        loop_state = (restored or {}).get("loop_state") or {}
        branch_q = (restored or {}).get("branch_q") or []
        merge_got = (restored or {}).get("merge_got") or {}
        merge_done = (restored or {}).get("merge_done") or set()
        force_merge = (restored or {}).get("force_merge") or set()
        spawned = (restored or {}).get("spawned") or {}
        results = (restored or {}).get("results") or []
        last_output = (restored or {}).get("last_output") or ""
        visits = int((restored or {}).get("visits") or 0)
        cancelled = False
        cur = start_node if restored is None else restored.get("cur")

        def checkpoint(next_id):
            _save_checkpoint(wf_id, {
                "run_id": run_id,
                "workflow_id": wf_id,
                "workflow": copy.deepcopy(wf),
                "trigger": trigger,
                "input": inp,
                "next_node_id": next_id,
                "ctx": copy.deepcopy(ctx),
                "loop_state": copy.deepcopy(loop_state),
                "branch_q": list(branch_q),
                "merge_got": copy.deepcopy(merge_got),
                "merge_done": sorted(merge_done),
                "force_merge": sorted(force_merge),
                "spawned": dict(spawned),
                "results": list(results),
                "last_output": last_output,
                "visits": visits,
                "agent_messages": list(ag.messages),
                "ts": _now(),
            })

        import contextlib
        from oceano import jobs
        stack = contextlib.ExitStack()
        try:
            _jid = stack.enter_context(jobs.job("workflow", wf.get("name", ""), ref=f"workflow:{wf['id']}")) if not nested else None
            ce = jobs.cancel_event(_jid) if _jid is not None else None
            if not nested:
                stack.enter_context(tools.background())
            with stack:
                while visits < _VISIT_CAP:
                    if cur is None:
                        if branch_q:
                            nid, blast = branch_q.pop(0)
                            cur = nodes.get(nid)
                            if cur is not None:
                                ctx["last"] = blast
                            continue
                        mid = next((m for m, got in merge_got.items()
                                    if got and m not in merge_done and m in nodes), None)
                        if mid is None:
                            break
                        force_merge.add(mid)
                        cur = nodes[mid]
                        continue
                    visits += 1
                    if ce is not None and ce.is_set():
                        cancelled = True
                        break
                    t = cur["type"]
                    if t == "end":
                        cur = None
                        continue
                    if t == "loop":
                        ls = loop_state.get(cur["id"])
                        if ls is None:
                            raw = _tmpl(cur.get("over", "") or "{{last}}", ctx).strip()
                            items = None
                            try:
                                j = json.loads(raw)
                                if isinstance(j, list):
                                    items = [x if isinstance(x, str) else json.dumps(x) for x in j]
                            except Exception:
                                items = None
                            if items is None:
                                items = [ln for ln in raw.splitlines() if ln.strip()]
                            ls = loop_state[cur["id"]] = {"items": items[:_LOOP_CAP], "cursor": 0, "results": []}
                        else:
                            ls["results"].append(ctx["last"])
                        if ls["cursor"] < len(ls["items"]):
                            ctx["item"] = ls["items"][ls["cursor"]]
                            ctx["index"] = ls["cursor"]
                            ls["cursor"] += 1
                            emit({"event": "node_start", "id": cur["id"], "type": t,
                                  "label": f"↻ loop {ls['cursor']}/{len(ls['items'])}"})
                            emit({"event": "node_end", "id": cur["id"], "ok": True, "branch": "loop",
                                  "output": f"item {ls['cursor']}/{len(ls['items'])}: {str(ctx['item'])[:120]}"})
                            nxt = next((to for (br, to) in succ.get(cur["id"], []) if br == "loop"), None)
                            checkpoint(nxt)
                            cur = nodes.get(nxt) if nxt is not None else None
                            continue
                        ctx["item"] = ctx["index"] = None
                        agg = json.dumps(ls["results"])
                        ctx["nodes"][cur["id"]] = agg
                        ctx["last"] = agg
                        last_output = agg
                        label = f"↻ loop done · {len(ls['results'])} result{'s' if len(ls['results']) != 1 else ''}"
                        results.append({"id": cur["id"], "type": t, "label": label, "ok": True,
                                        "branch": "done", "output": agg[:_OUT_CAP]})
                        emit({"event": "node_end", "id": cur["id"], "ok": True, "branch": "done",
                              "label": label, "output": agg[:_OUT_CAP]})
                        loop_state.pop(cur["id"], None)
                        nxt = (next((to for (br, to) in succ.get(cur["id"], []) if br == "done"), None)
                               or next((to for (br, to) in succ.get(cur["id"], []) if br is None), None))
                        checkpoint(nxt)
                        cur = nodes.get(nxt) if nxt is not None else None
                        continue

                    if t == "merge":
                        got = merge_got.setdefault(cur["id"], [])
                        if cur["id"] not in force_merge:
                            got.append(ctx["last"])
                        need = inbound.get(cur["id"], 1)
                        if len(got) < need and branch_q and cur["id"] not in force_merge:
                            emit({"event": "node_start", "id": cur["id"], "type": t,
                                  "label": f"⧉ merge {len(got)}/{need}"})
                            emit({"event": "node_end", "id": cur["id"], "ok": True, "branch": None,
                                  "output": f"{len(got)}/{need} branches arrived — waiting for the rest"})
                            checkpoint(None)
                            cur = None
                            continue
                        label = _node_label(cur)
                        emit({"event": "node_start", "id": cur["id"], "type": t, "label": label})
                        if len(got) < need:
                            emit({"event": "tool", "id": cur["id"],
                                  "text": f"⧉ merged {len(got)}/{need} branches (the rest never arrived)"})
                        output = json.dumps(got) if cur.get("mode") == "json" else "\n\n".join(got)
                        merge_done.add(cur["id"])
                        merge_got.pop(cur["id"], None)
                        force_merge.discard(cur["id"])
                        ctx["nodes"][cur["id"]] = output
                        ctx["last"] = output
                        last_output = output
                        results.append({"id": cur["id"], "type": t, "label": label, "ok": True,
                                        "branch": None, "output": output[:_OUT_CAP]})
                        emit({"event": "node_end", "id": cur["id"], "ok": True, "branch": None,
                              "label": label, "output": output[:_OUT_CAP]})
                        nxt = _route(cur, succ, None)
                        checkpoint(nxt)
                        cur = nodes.get(nxt) if nxt is not None else None
                        continue

                    label = _node_label(cur)
                    emit({"event": "node_start", "id": cur["id"], "type": t, "label": label})
                    attempts = 1 + int(cur.get("retries", 0) or 0)
                    ok, output, branch = True, "", None
                    orch_step_records = []
                    for attempt in range(attempts):
                        ok, output, branch = True, "", None
                        orch_step_records = []
                        try:
                            capability, mode = _policy_mode(cur)
                            if mode == "block":
                                ok, output = False, f"blocked by policy ({capability})"
                            elif mode == "confirm":
                                approved, detail = _await_approval(
                                    wf_id, _policy_prompt(cur, capability), cur.get("timeout", 60), beat)
                                if not approved:
                                    ok, output = False, detail
                            if ok and t in ("start", "trigger"):
                                output = ""
                            elif ok and t == "tool":
                                name, args = cur.get("tool", ""), _tmpl(cur.get("args", {}), ctx)
                                if not tools.is_enabled(name):
                                    ok, output = False, f"tool '{name}' is disabled or unknown"
                                else:
                                    with policies.permit(capability):
                                        output = tools.run(name, json.dumps(args)) or ""
                                    if output.startswith("ERROR:"):
                                        ok = False
                                    ag.messages.append({"role": "user", "content": f"(ran tool `{name}` → {output[:1500]})"})
                            elif ok and t == "instruction":
                                ag.on_event = lambda kind, d, _i=cur["id"]: (
                                    emit({"event": "tool", "id": _i, "text": _compact_event(kind, d)})
                                    if kind in ("tool_call", "tool_result") else None)
                                text = _persona_prefix(cur.get("persona", "")) + _tmpl(cur.get("text", ""), ctx)
                                prov = cur.get("provider") or ""
                                from oceano import delegate
                                if cur.get("model"):
                                    output = _pinned_agent(cur, ag).run(text) or ""
                                elif prov == "claude" or (not prov and delegate.get_mind() == "claude"):
                                    if delegate.available():
                                        output = ag.run_claude(text) or ""
                                    else:
                                        ok, output = False, "this step is pinned to Claude, but the `claude` CLI isn't available on this host"
                                elif prov == "codex" or (not prov and delegate.get_mind() == "codex"):
                                    if delegate.codex_available():
                                        output = ag.run_codex(text) or ""
                                    else:
                                        ok, output = False, "this step is pinned to Codex, but the `codex` CLI isn't available on this host"
                                else:
                                    output = ag.run(text) or ""
                                ag.on_event = lambda kind, d: None
                            elif ok and t == "delegate":
                                from oceano import delegate
                                tool_scope = _tool_scope_for(cur.get("write"))
                                text = _persona_prefix(cur.get("persona", "")) + _tmpl(cur.get("text", ""), ctx)
                                r = delegate.run(text, cwd=config.WORKSPACE,
                                                 tools=tool_scope, timeout=cur.get("timeout") or None,
                                                 role=cur.get("role", "default"),
                                                 skills=True)
                                ok = bool(r.get("ok"))
                                output = (r.get("output") or "") if ok else f"delegate failed: {r.get('error', '')}"
                                ag.messages.append({"role": "user", "content": f"(delegated → {output[:1500]})"})
                            elif ok and t == "agent":
                                from oceano import agentjobs
                                tool_scope = _tool_scope_for(cur.get("write"))
                                task = _persona_prefix(cur.get("persona", "")) + _tmpl(cur.get("task", ""), ctx)
                                rec = agentjobs.spawn(task,
                                                      provider=cur.get("provider", ""),
                                                      model=cur.get("model", ""),
                                                      base_url=cur.get("baseUrl", ""),
                                                      label=cur.get("label", ""),
                                                      timeout=cur.get("timeout", 600),
                                                      tools=tool_scope, skills=True,
                                                      cwd=config.WORKSPACE)
                                spawned[cur["id"]] = rec["id"]
                                output = json.dumps({"agent_id": rec["id"], "label": rec["label"],
                                                     "provider": rec["provider"], "state": rec["state"]})
                            elif ok and t == "await":
                                from oceano import agentjobs
                                want = [w.strip() for w in (cur.get("agents") or "").split(",") if w.strip()]
                                targets = ({nid: aid for nid, aid in spawned.items() if nid in want or str(aid) in want}
                                           if want else dict(spawned))
                                if not targets:
                                    ok, output = False, "await: no agents were spawned in this run"
                                else:
                                    deadline = time.time() + cur.get("timeout", 900)
                                    done, failed = {}, {}
                                    while time.time() < deadline:
                                        beat()
                                        left = False
                                        for nid, aid in targets.items():
                                            if nid in done or nid in failed:
                                                continue
                                            r = agentjobs.status(aid) or {"state": "lost"}
                                            if r["state"] == "done":
                                                done[nid] = r.get("output") or ""
                                            elif r["state"] in ("failed", "lost"):
                                                failed[nid] = r.get("error") or r["state"]
                                            else:
                                                left = True
                                        if not left:
                                            break
                                        time.sleep(2)
                                    for nid, out in done.items():
                                        ctx["nodes"][nid] = out
                                    timed_out = [nid for nid in targets if nid not in done and nid not in failed]
                                    ok = not failed and not timed_out
                                    parts = [f"[{nid}] {out}" for nid, out in done.items()]
                                    parts += [f"[{nid}] FAILED: {err}" for nid, err in failed.items()]
                                    parts += [f"[{nid}] TIMED OUT (still running)" for nid in timed_out]
                                    output = "\n\n".join(parts)
                                    if done:
                                        ag.messages.append({"role": "user", "content": f"(agents finished → {output[:1500]})"})
                            elif ok and t == "orchestrate":
                                ok, output, orch_step_records = _run_orchestrate(
                                    cur, attached.get(cur["id"], []), ctx, ag, spawned, emit, beat)
                            elif ok and t == "decision":
                                fnode = {**cur, "question": _tmpl(cur.get("question", ""), ctx),
                                         "ruleValue": _tmpl(cur.get("ruleValue", ""), ctx)}
                                verdict, output = _decide(fnode, ctx["last"], ag)
                                branch = "yes" if verdict else "no"
                            elif ok and t == "switch":
                                branch, output = _run_switch(cur, ctx)
                            elif ok and t == "http":
                                ok, output = _run_http(cur, ctx)
                            elif ok and t == "transform":
                                ok, output = _run_transform(cur, ctx)
                            elif ok and t == "subflow":
                                ok, output = _run_subflow(cur, ctx, _depth)
                                ag.messages.append({"role": "user", "content": f"(sub-workflow → {output[:1500]})"})
                            elif ok and t == "approval":
                                approved, detail = _await_approval(wf_id, _tmpl(cur.get("prompt", ""), ctx) or "Approve this step?",
                                                                   cur.get("timeout", 60), beat)
                                ok = approved
                                branch = "approved" if approved else "rejected"
                                output = detail
                            elif ok and t == "wait":
                                secs = int(_wait_seconds(cur))
                                deadline = time.time() + secs
                                while time.time() < deadline:
                                    beat()
                                    if ce is not None and ce.is_set():
                                        break
                                    time.sleep(min(30.0, max(0.0, deadline - time.time())))
                                if ce is not None and ce.is_set():
                                    output = f"wait interrupted by cancel after {max(0, secs - int(deadline - time.time()))}s"
                                else:
                                    output = f"waited {secs}s" + (f" (until {cur['until']})" if cur.get("until") else "")
                        except Exception as ex:
                            ok, output = False, f"{type(ex).__name__}: {ex}"
                        if ok or attempt + 1 >= attempts:
                            break
                        emit({"event": "tool", "id": cur["id"], "text": f"retry {attempt + 1}/{attempts - 1}…"})
                        time.sleep(1)

                    ctx["nodes"][cur["id"]] = output
                    if t not in ("decision", "switch", "approval", "wait"):
                        ctx["last"] = output
                        last_output = output
                    results.extend(orch_step_records)
                    results.append({"id": cur["id"], "type": t, "label": label, "ok": ok,
                                    "branch": branch, "output": output[:_OUT_CAP]})
                    emit({"event": "node_end", "id": cur["id"], "ok": ok, "branch": branch, "label": label, "output": output[:_OUT_CAP]})

                    err_to = next((to for (br, to) in succ.get(cur["id"], []) if br == "error"), None)
                    if not ok and t != "approval" and err_to is None:
                        break
                    if not ok and err_to is not None:
                        nxt = err_to
                    elif t == "approval":
                        nxt = (next((to for (br, to) in succ.get(cur["id"], []) if br == branch), None)
                               or next((to for (br, to) in succ.get(cur["id"], []) if br in (None, "next")), None))
                    else:
                        plain = [to for (br, to) in succ.get(cur["id"], []) if br in (None, "next")]
                        if ok and len(plain) > 1 and t not in ("decision", "switch"):
                            for to in plain[1:]:
                                branch_q.append((to, ctx["last"]))
                            nxt = plain[0]
                        else:
                            nxt = _route(cur, succ, branch)
                    checkpoint(nxt)
                    cur = nodes.get(nxt) if nxt is not None else None

                status = "cancelled" if cancelled else (
                    "ok" if results and all(r["ok"] for r in results) else ("empty" if not results else "error"))
                done = sum(1 for r in results if r["ok"])
                summary = f"{done}/{len(results)} nodes ok" + ("" if status == "ok" else f" · {status}")
                rec = _record_run(wf["id"], trigger, status, results, summary)
                rec["output"] = last_output
                emit({"event": "done", "status": status, "run": rec})
                if status in ("ok", "empty", "skipped"):
                    _clear_checkpoint(wf_id)
                if not nested:
                    fire_chain(wf_id, status, frozenset(_chain_seen) | {wf_id}, out=last_output)
                    if _jid is not None:
                        jobs.set_result(_jid, summary)
                return rec
        finally:
            if not nested:
                with _LIVE_LOCK:
                    st = _LIVE.get(wf_id)
                    if st and st.get("status") == "running":
                        st.update(status="error", current=None, finished=time.time(), summary="(ended unexpectedly)")

def run_by_id(wid, trigger="manual", on_step=None, inp=None):
    wf = get(wid)
    if not wf:
        return {"status": "error", "summary": f"no workflow #{wid}"}
    return run(wf, trigger=trigger, on_step=on_step, inp=inp)
