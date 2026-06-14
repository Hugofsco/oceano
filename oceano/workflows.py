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
NODE_TYPES = ("start", "tool", "instruction", "delegate", "decision", "end")
_MAX_RUNS = 60
_OUT_CAP = 4000
_VISIT_CAP = 60                      # max node executions per run — loop backstop

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
        if t == "tool":
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
            node["ruleOp"] = n.get("ruleOp") if n.get("ruleOp") in ("contains", "equals", "matches", "gt", "lt") else "contains"
            node["ruleValue"] = str(n.get("ruleValue", ""))
            node["role"] = n.get("role") if n.get("role") in ("default", "improve") else "default"
        nodes.append(node)
    ids = {n["id"] for n in nodes}
    edges = []
    for e in graph.get("edges", []) or []:
        if isinstance(e, dict) and e.get("from") in ids and e.get("to") in ids:
            edges.append({"from": e["from"], "to": e["to"],
                          "branch": e["branch"] if e.get("branch") in ("yes", "no") else None})
    return {"nodes": nodes, "edges": edges}


# ---------------- CRUD ----------------
def list_all():
    return _load()["workflows"]


def get(wid):
    return next((w for w in _load()["workflows"] if w["id"] == wid), None)


def get_by_name(name):
    name = (name or "").strip().lower()
    return next((w for w in _load()["workflows"] if w["name"].strip().lower() == name), None)


def create(name, description="", graph=None):
    data = _load()
    wf = {"id": _next_id(data["workflows"]), "name": (name or "Untitled").strip(),
          "description": (description or "").strip(), "graph": _norm_graph(graph or {}),
          "triggers": [], "created": _now()}
    data["workflows"].append(wf)
    _save(data)
    return wf


def update(wid, name=None, description=None, graph=None):
    data = _load()
    wf = next((w for w in data["workflows"] if w["id"] == wid), None)
    if not wf:
        return None
    if name is not None:
        wf["name"] = name.strip()
    if description is not None:
        wf["description"] = description.strip()
    if graph is not None:
        wf["graph"] = _norm_graph(graph)
    _save(data)
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


# ---------------- triggers (event-based runs: watch · webhook · keyword · chain) ----------------
_TRIGGER_TYPES = ("watch", "webhook", "keyword", "chain")
_WATCH_SIG = {}                      # (wid, folder) -> last signature; baseline on first sight


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
        out.append(n)
    return out


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


def run_async(wf, trigger="trigger", chain_seen=frozenset()):
    """Fire-and-forget a run in a daemon thread (used by every event trigger)."""
    threading.Thread(target=lambda: run(wf, trigger=trigger, _chain_seen=chain_seen), daemon=True).start()


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


def fire_keyword(message, channel="web"):
    """Run workflows whose keyword trigger matches a chat message. Returns the names fired."""
    msg = (message or "").strip().lower()
    fired = []
    if not msg:
        return fired
    for wf in list_all():
        for tr in wf.get("triggers", []):
            if tr.get("type") != "keyword" or not tr.get("enabled") or tr.get("channel") not in ("any", channel):
                continue
            pat = (tr.get("pattern") or "").strip().lower()
            if pat and pat in msg:
                run_async(wf, trigger="keyword"); fired.append(wf["name"]); break
    return fired


def fire_chain(after_wid, status, seen=frozenset()):
    """When a workflow finishes, run any workflow chained after it (loop-guarded by `seen`)."""
    for wf in list_all():
        if wf["id"] in seen:
            continue
        for tr in wf.get("triggers", []):
            if (tr.get("type") == "chain" and tr.get("enabled") and tr.get("after") == after_wid
                    and (tr.get("on") == "any" or status == "ok")):
                run_async(wf, trigger="chain", chain_seen=seen)
                break


def webhook_run(wid, token):
    """Run a workflow if `token` matches one of its enabled webhook triggers."""
    wf = get(wid)
    if not wf:
        return None
    for tr in wf.get("triggers", []):
        if (tr.get("type") == "webhook" and tr.get("enabled")
                and secrets.compare_digest(str(tr.get("token", "")), str(token))):
            run_async(wf, trigger="webhook")
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


# ---------------- execution ----------------
def _node_label(n):
    t = n["type"]
    if t == "tool":
        return "🔧 " + (n.get("tool") or "tool")
    if t == "instruction":
        return (n.get("text", "")[:54] or "instruction")
    if t == "delegate":
        return "↗ " + (n.get("text", "")[:48] or "delegate")
    if t == "decision":
        return "◆ " + (n.get("question", "")[:48] or n.get("mode", "decision"))
    return t


def _compact_event(kind, data):
    if kind == "tool_call":
        return "→ " + str(data.get("name"))
    if kind == "tool_result":
        r = (data.get("result") or "")
        return f"✓ {data.get('name')}" + (f" · {r[:80]}" if r else "")
    return ""


def run(wf, trigger="manual", on_step=None, _chain_seen=frozenset()):
    """Walk the workflow graph from its start node, executing nodes and branching at
    decision nodes. Shares one Agent so context accumulates. Returns the run record."""
    from oceano.agent import Agent
    wf_id = wf["id"]

    def emit(ev):
        e = ev.get("event")
        with _LIVE_LOCK:                                # mirror progress into the live registry
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

    start = next((n for n in graph.get("nodes", []) if n["type"] == "start"), None)
    if not start:                                      # tolerate a missing start: first node with no inbound edge
        inbound = {e["to"] for e in graph.get("edges", [])}
        start = next((n for n in graph.get("nodes", []) if n["id"] not in inbound),
                     graph["nodes"][0] if graph.get("nodes") else None)

    ag = Agent(learn=False, exclude_tools={"run_workflow"})
    results, last_output, visits = [], "", 0
    cur = start
    with _LIVE_LOCK:
        _prune_live()
        _LIVE[wf_id] = {"workflow_id": wf_id, "name": wf.get("name", ""), "trigger": trigger,
                        "started": _now(), "beat": time.time(), "status": "running",
                        "current": None, "steps": [], "summary": "", "finished": None, "run_id": None}
    from oceano import jobs
    try:
        with jobs.job("workflow", wf.get("name", ""), ref=f"workflow:{wf['id']}"), tools.background():
            while cur and visits < _VISIT_CAP:
                visits += 1
                t = cur["type"]
                if t == "end":
                    break
                label = _node_label(cur)
                emit({"event": "node_start", "id": cur["id"], "type": t, "label": label})
                ok, output, branch = True, "", None
                try:
                    if t == "start":
                        output = ""
                    elif t == "tool":
                        name, args = cur.get("tool", ""), cur.get("args", {})
                        if not tools.is_enabled(name):
                            ok, output = False, f"tool '{name}' is disabled or unknown"
                        else:
                            output = tools.run(name, json.dumps(args)) or ""
                            last_output = output
                            ag.messages.append({"role": "user",
                                "content": f"(ran tool `{name}` → {output[:1500]})"})
                    elif t == "instruction":
                        ag.on_event = lambda kind, d, _i=cur["id"]: (
                            emit({"event": "tool", "id": _i, "text": _compact_event(kind, d)})
                            if kind in ("tool_call", "tool_result") else None)
                        output = ag.run(cur.get("text", "")) or ""
                        ag.on_event = lambda kind, d: None
                        last_output = output
                    elif t == "delegate":
                        from oceano import delegate
                        r = delegate.run(cur.get("text", ""), cwd=config.WORKSPACE,
                                         tools="Read,Glob,Grep", timeout=600, role=cur.get("role", "default"))
                        ok = bool(r.get("ok"))
                        output = (r.get("output") or "") if ok else f"delegate failed: {r.get('error', '')}"
                        last_output = output
                        ag.messages.append({"role": "user", "content": f"(delegated → {output[:1500]})"})
                    elif t == "decision":
                        verdict, detail = _decide(cur, last_output, ag)
                        branch = "yes" if verdict else "no"
                        output = detail
                except Exception as ex:
                    ok, output = False, f"{type(ex).__name__}: {ex}"
                results.append({"id": cur["id"], "type": t, "label": label, "ok": ok,
                                "branch": branch, "output": output[:_OUT_CAP]})
                emit({"event": "node_end", "id": cur["id"], "ok": ok, "branch": branch, "output": output[:_OUT_CAP]})

                outs = succ.get(cur["id"], [])
                if t == "decision":
                    nxt = next((to for (br, to) in outs if (br or "yes") == branch), None)
                else:
                    nxt = outs[0][1] if outs else None
                cur = nodes.get(nxt) if nxt is not None else None

        status = "ok" if results and all(r["ok"] for r in results) else ("empty" if not results else "error")
        done = sum(1 for r in results if r["ok"])
        summary = f"{done}/{len(results)} nodes ok" + ("" if status == "ok" else f" · {status}")
        rec = _record_run(wf["id"], trigger, status, results, summary)
        emit({"event": "done", "status": status, "run": rec})
        fire_chain(wf_id, status, frozenset(_chain_seen) | {wf_id})   # chain-trigger any followers
        return rec
    finally:
        with _LIVE_LOCK:                                # never leave a 'running' entry stranded
            st = _LIVE.get(wf_id)
            if st and st.get("status") == "running":
                st.update(status="error", current=None, finished=time.time(), summary="(ended unexpectedly)")


def run_by_id(wid, trigger="manual", on_step=None):
    wf = get(wid)
    if not wf:
        return {"status": "error", "summary": f"no workflow #{wid}"}
    return run(wf, trigger=trigger, on_step=on_step)
