"""Workflows — visual, branching recipes the agent runs.

A workflow is a directed graph the user draws on a canvas (Drawflow in the UI):

  nodes : start · tool · instruction · delegate · decision · end
  edges : from -> to  (decision edges carry a branch label: "yes" / "no")

Execution walks the graph from the `start` node, following edges. Most nodes do their
work then follow their single outgoing edge; a `decision` node evaluates a condition and
follows its "yes" or "no" edge instead — that's the branching / decision-tree behaviour.
A decision can be judged three ways (the user picks per node):
  rule     — a deterministic test over the previous step's output (contains/equals/matches/gt/lt)
  model    — the local model answers YES/NO given the context (flexible, less predictable)
  delegate — Claude / a cloud model answers YES/NO (for judgments the local model shouldn't make)

The whole run shares ONE Agent, so context accumulates across nodes. A hard node-visit cap
stops any accidental loop from running forever. Runs are recorded so scheduled, unattended
runs stay observable. Storage is one JSON file (atomic); a workflow's cron schedule lives in
the scheduler as a managed task tagged `workflow:<id>`.
"""
import json
import re
import secrets
import threading
import time
from datetime import datetime, timezone

import config
from oceano import atomicio, tools

STORE = config.WORKSPACE.parent / "data" / "workflows.json"
SOURCE_PREFIX = "workflow:"
SCHED_PREFIX = "[ FLOW ] "
# start/end + the action nodes; "trigger" is a start that also declares HOW the flow fires (issue 8 C);
# switch=multi-branch, loop=foreach, http/subflow/transform=connectivity+data, approval=human-in-the-loop.
NODE_TYPES = ("start", "trigger", "tool", "instruction", "delegate", "decision",
              "switch", "loop", "http", "subflow", "transform", "approval", "end")
_TRIGGER_NODE_KINDS = ("manual", "schedule", "webhook", "keyword", "watch", "email")
_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")
_TRANSFORM_MODES = ("template", "regex", "jsonpath", "python")
_SWITCH_OPS = ("contains", "equals", "matches", "gt", "lt")
_MAX_RUNS = 60
_OUT_CAP = 4000
_VISIT_CAP = 400                     # max node executions per run — loop backstop (raised for foreach)
_LOOP_CAP = 200                      # max iterations a single loop node will run
_SUBFLOW_DEPTH = 5                   # how deep nested sub-workflows may go
_HTTP_CAP = 200000                   # cap an HTTP node's captured response body

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
    data.setdefault("runs", [])
    data["workflows"] = [_migrate(w) for w in data["workflows"]]
    return data


def _save(data):
    atomicio.write_text(STORE, json.dumps(data, indent=2))


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
        elif t == "delegate":
            node["text"] = str(n.get("text", ""))
            node["role"] = n.get("role") if n.get("role") in ("default", "improve") else "default"
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
        elif t == "http":
            node["method"] = n.get("method") if n.get("method") in _HTTP_METHODS else "GET"
            node["url"] = str(n.get("url", "")).strip()
            node["headers"] = n.get("headers") if isinstance(n.get("headers"), dict) else {}
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
# Unknown tokens render empty (never leak the literal braces or internal state).
_TMPL_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


def _resolve_token(expr, ctx):
    low = expr.strip().lower()
    if low == "input":
        return str(ctx.get("input", ""))
    if low in ("last", "prev", "previous", "output"):
        return str(ctx.get("last", ""))
    if low in ("item", "loop.item"):
        return str(ctx.get("item", ""))
    if low in ("index", "loop.index", "i"):
        return str(ctx.get("index", ""))
    m = re.match(r"(?:nodes?|step|steps)\.(\d+)(?:\.output)?$", low)
    if m:
        return str(ctx.get("nodes", {}).get(int(m.group(1)), ""))
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


# ---------------- CRUD ----------------
def list_all():
    return _load()["workflows"]


def get(wid):
    return next((w for w in _load()["workflows"] if w["id"] == wid), None)


def get_by_name(name):
    name = (name or "").strip().lower()
    return next((w for w in _load()["workflows"] if w["name"].strip().lower() == name), None)


def create(name, description="", graph=None, input_cfg=None):
    data = _load()
    wf = {"id": _next_id(data["workflows"]), "name": (name or "Untitled").strip(),
          "description": (description or "").strip(), "graph": _norm_graph(graph or {}),
          "input": _norm_input(input_cfg), "triggers": [], "created": _now()}
    _apply_graph_triggers(wf)
    cron = wf.pop("_graph_cron", None)
    data["workflows"].append(wf)
    _save(data)
    if cron is not None:                              # a schedule trigger node → register/clear the cron task
        set_schedule(wf["id"], cron)
    return wf


def update(wid, name=None, description=None, graph=None, input_cfg=None):
    data = _load()
    wf = next((w for w in data["workflows"] if w["id"] == wid), None)
    if not wf:
        return None
    if name is not None:
        wf["name"] = name.strip()
    if description is not None:
        wf["description"] = description.strip()
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
    return wf


def remove(wid):
    data = _load()
    before = len(data["workflows"])
    data["workflows"] = [w for w in data["workflows"] if w["id"] != wid]
    data["runs"] = [r for r in data["runs"] if r.get("workflow_id") != wid]
    _save(data)
    t = _task_for(wid)
    if t:
        from oceano import scheduler
        scheduler.delete_task(t["id"], allow_managed=True)
    return len(data["workflows"]) < before


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
    rs = _load()["runs"]
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
    return hash(tuple(items))


def poll_watch_triggers():
    """Run workflows whose watched folder changed since the last tick (called by the engine).
    First sight of a folder records a baseline only, so a restart can't spuriously fire."""
    fired = 0
    for wf in list_all():
        for tr in wf.get("triggers", []):
            if tr.get("type") != "watch" or not tr.get("enabled"):
                continue
            sig = _folder_sig(tr["folder"])
            if sig is None:
                continue
            key = (wf["id"], tr["folder"])
            prev = _WATCH_SIG.get(key, "__new__")
            _WATCH_SIG[key] = sig
            if prev != "__new__" and sig != prev:
                run_async(wf, trigger="watch"); fired += 1
    return fired


def poll_email_triggers():
    """Run workflows whose email trigger sees NEW mail since the last tick (called by the engine).
    First sight records a baseline (the current newest uid) so a restart can't replay old mail; each
    genuinely new message fires the workflow once, with a compact From/Subject/body as the run input."""
    from oceano import mail
    fired = 0
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
            _EMAIL_SEEN[key] = newest
            if prev is None:                           # baseline only on first sight
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
    wf = get(wid)
    if not wf:
        return None
    for tr in wf.get("triggers", []):
        if (tr.get("type") == "webhook" and tr.get("enabled")
                and secrets.compare_digest(str(tr.get("token", "")), str(token))):
            run_async(wf, trigger="webhook", inp=inp)
            return wf
    return None


def _record_run(workflow_id, trigger, status, steps, summary):
    data = _load()
    rec = {"id": _next_id(data["runs"]), "workflow_id": workflow_id, "ts": _now(),
           "trigger": trigger, "status": status, "summary": summary, "steps": steps}
    data["runs"].append(rec)
    data["runs"] = data["runs"][-_MAX_RUNS:]
    _save(data)
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
    # mode == "model"
    from oceano import llm
    msg = llm.chat([{"role": "system", "content": "You are a decision gate in a workflow. "
                     "Read the question and the latest output, then answer with exactly one word: YES or NO."},
                    {"role": "user", "content": f"{q}\n\nLatest step output:\n{last_output[:2000]}"}],
                   tools=None)
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
    url = _tmpl(node.get("url", ""), ctx).strip()
    if not url:
        return False, "no URL"
    headers = {str(k): _tmpl(str(v), ctx) for k, v in (node.get("headers") or {}).items()}
    body = _tmpl(node.get("body", ""), ctx)
    import requests
    # SSRF guard: the URL may be templated from untrusted upstream data ({{last}}, an email body…),
    # so a public URL that redirects to a loopback/metadata address would slip past a one-time check.
    # Follow redirects MANUALLY and re-validate every hop before issuing it.
    cur = url
    try:
        for _ in range(6):
            refusal = safety.check_url(cur)
            if refusal:
                return False, refusal
            resp = requests.request(method, cur, headers=headers or None,
                                    data=body.encode("utf-8") if body else None,
                                    timeout=30, allow_redirects=False)
            if resp.is_redirect and resp.headers.get("Location"):
                cur = requests.compat.urljoin(cur, resp.headers["Location"])
                continue
            return resp.ok, f"HTTP {resp.status_code}\n{resp.text[:_HTTP_CAP]}"
        return False, "too many redirects"
    except Exception as e:                           # noqa: BLE001
        return False, f"request failed: {e}"


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
        for part in [p for p in re.split(r"[.\[\]]", _tmpl(text, ctx).strip()) if p]:
            try:
                cur = cur[int(part)] if part.lstrip("-").isdigit() else cur[part]
            except (KeyError, IndexError, TypeError):
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
        return "↗ " + (n.get("text", "")[:48] or "delegate")
    if t == "decision":
        return "◆ " + (n.get("question", "")[:48] or n.get("mode", "decision"))
    if t == "switch":
        return "⤳ switch"
    if t == "loop":
        return "↻ loop " + (n.get("over", "")[:30])
    if t == "http":
        return "🌐 " + (n.get("method", "GET")) + " " + (n.get("url", "")[:36])
    if t == "subflow":
        return "▣ " + (n.get("workflow", "") or "sub-workflow")
    if t == "transform":
        return "ƒ " + (n.get("mode", "transform"))
    if t == "approval":
        return "✋ " + (n.get("prompt", "")[:40] or "approval")
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


def run(wf, trigger="manual", on_step=None, _chain_seen=frozenset(), inp=None, _depth=0, nested=False):
    """Walk the workflow graph from its start node, executing nodes and branching at decision/switch
    nodes, iterating loop nodes, retrying failures and taking 'error' edges, and pausing at approval
    nodes. Shares one Agent so context accumulates. Returns the run record (incl. 'output' = last value).

    `inp` is this run's input value: nodes reference it (and any earlier node's output) via {{...}}
    templating, and it's seeded into the agent's context. Empty/None falls back to the stored default.
    `nested`/`_depth` are set when one workflow calls another via a sub-workflow node."""
    from oceano.agent import Agent
    wf_id = wf["id"]
    inp = "" if inp is None else str(inp)
    if not inp:                                        # no explicit value → the workflow's default
        inp = str((wf.get("input") or {}).get("default") or "")

    def beat():
        with _LIVE_LOCK:
            st = _LIVE.get(wf_id)
            if st is not None:
                st["beat"] = time.time()

    def emit(ev):
        e = ev.get("event")
        if not nested:
            with _LIVE_LOCK:                            # mirror progress into the live registry
                st = _LIVE.get(wf_id)
                if st is not None:
                    st["beat"] = time.time()
                    if e == "node_start":
                        st["current"] = {"id": ev.get("id"), "label": ev.get("label")}
                    elif e == "node_end":
                        st["steps"].append({"id": ev.get("id"), "label": (st.get("current") or {}).get("label", ""),
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

    graph = wf.get("graph") or {"nodes": [], "edges": []}
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    succ = {}                                          # id -> [(branch, to_id)]
    for e in graph.get("edges", []):
        succ.setdefault(e["from"], []).append((e.get("branch"), e["to"]))

    start = next((n for n in graph.get("nodes", []) if n["type"] in ("start", "trigger")), None)
    if not start:                                      # tolerate a missing start: first node with no inbound edge
        inbound = {e["to"] for e in graph.get("edges", [])}
        start = next((n for n in graph.get("nodes", []) if n["id"] not in inbound),
                     graph["nodes"][0] if graph.get("nodes") else None)

    ag = Agent(learn=False, exclude_tools={"run_workflow"})
    if inp:                                            # make the input visible to instruction nodes
        ag.messages.append({"role": "user", "content": f"(workflow input)\n{inp}"})
    # ctx powers {{...}} templating between nodes (issue 8 A)
    ctx = {"input": inp, "last": "", "nodes": {}, "item": None, "index": None}
    loop_state = {}                                    # loop node id -> {items, cursor}
    results, last_output, visits = [], "", 0
    cur = start
    if not nested:
        with _LIVE_LOCK:
            _prune_live()
            _LIVE[wf_id] = {"workflow_id": wf_id, "name": wf.get("name", ""), "trigger": trigger,
                            "started": _now(), "beat": time.time(), "status": "running", "current": None,
                            "steps": [], "summary": "", "finished": None, "run_id": None, "awaiting": None}
    import contextlib
    from oceano import jobs
    stack = contextlib.ExitStack()
    try:
        _jid = stack.enter_context(jobs.job("workflow", wf.get("name", ""), ref=f"workflow:{wf['id']}")) if not nested else None
        if not nested:
            stack.enter_context(tools.background())
        with stack:
            while cur and visits < _VISIT_CAP:
                visits += 1
                t = cur["type"]
                if t == "end":
                    break
                # ---- loop node: foreach over a list, with a "loop" body edge and a "done" exit edge ----
                if t == "loop":
                    ls = loop_state.get(cur["id"])
                    if ls is None:                     # first entry: evaluate the list
                        raw = _tmpl(cur.get("over", "") or "{{last}}", ctx).strip()
                        items = None
                        try:
                            j = json.loads(raw)
                            if isinstance(j, list):
                                items = [x if isinstance(x, str) else json.dumps(x) for x in j]
                        except Exception:              # noqa: BLE001
                            items = None
                        if items is None:
                            items = [ln for ln in raw.splitlines() if ln.strip()]
                        ls = loop_state[cur["id"]] = {"items": items[:_LOOP_CAP], "cursor": 0}
                    if ls["cursor"] < len(ls["items"]):
                        ctx["item"] = ls["items"][ls["cursor"]]
                        ctx["index"] = ls["cursor"]
                        ls["cursor"] += 1
                        emit({"event": "node_start", "id": cur["id"], "type": t,
                              "label": f"↻ loop {ls['cursor']}/{len(ls['items'])}"})
                        emit({"event": "node_end", "id": cur["id"], "ok": True, "branch": "loop",
                              "output": f"item {ls['cursor']}/{len(ls['items'])}: {str(ctx['item'])[:120]}"})
                        nxt = (next((to for (br, to) in succ.get(cur["id"], []) if br == "loop"), None))
                        cur = nodes.get(nxt) if nxt is not None else None
                        continue
                    else:                              # loop exhausted → take the 'done' edge
                        ctx["item"] = ctx["index"] = None
                        nxt = (next((to for (br, to) in succ.get(cur["id"], []) if br == "done"), None)
                               or next((to for (br, to) in succ.get(cur["id"], []) if br is None), None))
                        cur = nodes.get(nxt) if nxt is not None else None
                        continue

                label = _node_label(cur)
                emit({"event": "node_start", "id": cur["id"], "type": t, "label": label})
                attempts = 1 + int(cur.get("retries", 0) or 0)
                ok, output, branch = True, "", None
                for attempt in range(attempts):
                    ok, output, branch = True, "", None
                    try:
                        if t in ("start", "trigger"):
                            output = ""
                        elif t == "tool":
                            name, args = cur.get("tool", ""), _tmpl(cur.get("args", {}), ctx)
                            if not tools.is_enabled(name):
                                ok, output = False, f"tool '{name}' is disabled or unknown"
                            else:
                                output = tools.run(name, json.dumps(args)) or ""
                                ag.messages.append({"role": "user", "content": f"(ran tool `{name}` → {output[:1500]})"})
                        elif t == "instruction":
                            ag.on_event = lambda kind, d, _i=cur["id"]: (
                                emit({"event": "tool", "id": _i, "text": _compact_event(kind, d)})
                                if kind in ("tool_call", "tool_result") else None)
                            output = ag.run(_tmpl(cur.get("text", ""), ctx)) or ""
                            ag.on_event = lambda kind, d: None
                        elif t == "delegate":
                            from oceano import delegate
                            r = delegate.run(_tmpl(cur.get("text", ""), ctx), cwd=config.WORKSPACE,
                                             tools="Read,Glob,Grep", timeout=600, role=cur.get("role", "default"))
                            ok = bool(r.get("ok"))
                            output = (r.get("output") or "") if ok else f"delegate failed: {r.get('error', '')}"
                            ag.messages.append({"role": "user", "content": f"(delegated → {output[:1500]})"})
                        elif t == "decision":
                            fnode = {**cur, "question": _tmpl(cur.get("question", ""), ctx),
                                     "ruleValue": _tmpl(cur.get("ruleValue", ""), ctx)}
                            verdict, output = _decide(fnode, ctx["last"], ag)
                            branch = "yes" if verdict else "no"
                        elif t == "switch":
                            branch, output = _run_switch(cur, ctx)
                        elif t == "http":
                            ok, output = _run_http(cur, ctx)
                        elif t == "transform":
                            ok, output = _run_transform(cur, ctx)
                        elif t == "subflow":
                            ok, output = _run_subflow(cur, ctx, _depth)
                            ag.messages.append({"role": "user", "content": f"(sub-workflow → {output[:1500]})"})
                        elif t == "approval":
                            approved, detail = _await_approval(wf_id, _tmpl(cur.get("prompt", ""), ctx) or "Approve this step?",
                                                               cur.get("timeout", 60), beat)
                            ok = approved
                            branch = "approved" if approved else "rejected"
                            output = detail
                    except Exception as ex:            # noqa: BLE001
                        ok, output = False, f"{type(ex).__name__}: {ex}"
                    if ok or attempt + 1 >= attempts:
                        break
                    emit({"event": "tool", "id": cur["id"], "text": f"retry {attempt + 1}/{attempts - 1}…"})
                    time.sleep(1)

                # record output as this node's value, and as 'last' for plain (non-branching) nodes
                ctx["nodes"][cur["id"]] = output
                if t not in ("decision", "switch", "approval"):
                    ctx["last"] = output
                    last_output = output
                results.append({"id": cur["id"], "type": t, "label": label, "ok": ok,
                                "branch": branch, "output": output[:_OUT_CAP]})
                emit({"event": "node_end", "id": cur["id"], "ok": ok, "branch": branch, "output": output[:_OUT_CAP]})

                # routing: a failed node with an 'error' edge takes it; decision/switch/approval branch
                err_to = next((to for (br, to) in succ.get(cur["id"], []) if br == "error"), None)
                if not ok and err_to is not None:
                    nxt = err_to
                elif t == "approval":
                    nxt = (next((to for (br, to) in succ.get(cur["id"], []) if br == branch), None)
                           or next((to for (br, to) in succ.get(cur["id"], []) if br in (None, "next")), None))
                else:
                    nxt = _route(cur, succ, branch)
                cur = nodes.get(nxt) if nxt is not None else None

            status = "ok" if results and all(r["ok"] for r in results) else ("empty" if not results else "error")
            done = sum(1 for r in results if r["ok"])
            summary = f"{done}/{len(results)} nodes ok" + ("" if status == "ok" else f" · {status}")
            rec = _record_run(wf["id"], trigger, status, results, summary)
            rec["output"] = last_output                # so a sub-workflow / chain can use the final value
            emit({"event": "done", "status": status, "run": rec})
            if not nested:                             # chain-trigger any followers with this run's final output
                fire_chain(wf_id, status, frozenset(_chain_seen) | {wf_id}, out=last_output)
                if _jid is not None:
                    jobs.set_result(_jid, summary)     # surface the workflow outcome in the activity log
            return rec
    finally:
        if not nested:
            with _LIVE_LOCK:                            # never leave a 'running' entry stranded
                st = _LIVE.get(wf_id)
                if st and st.get("status") == "running":
                    st.update(status="error", current=None, finished=time.time(), summary="(ended unexpectedly)")


def run_by_id(wid, trigger="manual", on_step=None, inp=None):
    wf = get(wid)
    if not wf:
        return {"status": "error", "summary": f"no workflow #{wid}"}
    return run(wf, trigger=trigger, on_step=on_step, inp=inp)
