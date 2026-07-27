"""Scheduled tasks, self-improvement suggestions, workflows, and notifications."""
from oceano import scheduler
from oceano.tools.core import tool

# --- scheduled tasks + notifications ---------------------------------------
@tool({
    "type": "function",
    "function": {
        "name": "schedule_task",
        "description": "Schedule an instruction to run automatically — either REPEATING on a cron "
                       "schedule, or ONCE at a specific date/time. For recurring, pass `cron` (e.g. "
                       "'0 8 * * *' = every day at 08:00). For a one-off (\"remind me at 3pm "
                       "tomorrow\"), pass `at` as a local date/time like '2026-07-01 15:00' and leave "
                       "cron empty; it fires once then disables itself. Times are host-local.",
        "parameters": {"type": "object", "properties": {
            "instruction": {"type": "string"},
            "cron": {"type": "string", "description": "5-field cron for a REPEATING task"},
            "at": {"type": "string", "description": "local date/time for a ONE-OFF task, e.g. '2026-07-01 15:00'"},
        }, "required": ["instruction"]},
    },
})
def schedule_task(instruction, cron="", at=""):
    return scheduler.schedule_task(cron, instruction, run_once_at=(at or None))


@tool({
    "type": "function",
    "function": {
        "name": "list_tasks",
        "description": "List the user's scheduled tasks, each shown as '#id [cron] on/off: instruction' "
                       "(one-offs show 'once @ <time>'; a failed last run is flagged). Use the id with "
                       "update_task / cancel_task.",
        "parameters": {"type": "object", "properties": {}},
    },
})
def list_tasks():
    return scheduler.list_tasks()


def _managed_guard(tid):
    """The task's record if it is MANAGED (a `source`-tagged entry: the self-maintenance
    built-ins — reflection, skills review/distill, evals, memory hygiene, reindex — plus
    researcher- and workflow-owned schedules), else None. The agent's tools refuse to touch
    those: pausing the nightly reflection persists across restarts (the bootstrap only
    recreates MISSING tasks), so a single bad turn — or text injected into something the
    agent read — could silently switch off the whole self-improvement loop. The user manages
    them in the Scheduler window; these tools only manage plain agent tasks."""
    t = next((t for t in scheduler.all_tasks() if t["id"] == tid), None)
    return t if (t and t.get("managed")) else None


@tool({
    "type": "function",
    "function": {
        "name": "update_task",
        "description": "Edit an existing scheduled task by its id (from list_tasks). Pass only the "
                       "fields you want to change: a new cron schedule, a new instruction, or "
                       "enabled (false to PAUSE the task without deleting it, true to resume).",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "integer", "description": "the task id shown by list_tasks"},
            "cron": {"type": "string", "description": "new cron expression, e.g. '0 8 * * *'"},
            "instruction": {"type": "string", "description": "new instruction text"},
            "enabled": {"type": "boolean", "description": "false pauses the task, true resumes it"},
        }, "required": ["id"]},
    },
})
def update_task(id, cron=None, instruction=None, enabled=None):
    tid = int(id)
    m = _managed_guard(tid)
    if m:
        return (f"refused: task #{tid} ({(m.get('instruction') or '')[:70]!r}) is a managed entry "
                "(a built-in maintenance job, or a researcher/workflow schedule). Those are managed "
                "by the user in the Scheduler window — this tool only edits plain agent tasks.")
    ok = scheduler.update_task(tid, cron=cron, instruction=instruction, enabled=enabled)
    if not ok:
        return f"could not update task #{id} (no such task, or invalid cron expression)"
    return f"updated task #{id}"


@tool({
    "type": "function",
    "function": {
        "name": "cancel_task",
        "description": "Delete a scheduled task by its id (from list_tasks) so it stops running. "
                       "To pause a task but keep it, use update_task with enabled=false instead. "
                       "Managed entries (built-in maintenance jobs, researcher/workflow schedules) are "
                       "refused — only the user can change those, in the Scheduler window.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "integer", "description": "the task id shown by list_tasks"},
        }, "required": ["id"]},
    },
})
def cancel_task(id):
    tid = int(id)
    if tid not in {t["id"] for t in scheduler.all_tasks()}:
        return f"no task #{tid} — use list_tasks to see the current ids"
    m = _managed_guard(tid)
    if m:
        return (f"refused: task #{tid} ({(m.get('instruction') or '')[:70]!r}) is a managed entry "
                "(a built-in maintenance job, or a researcher/workflow schedule). Only the user can "
                "remove or pause those, in the Scheduler window.")
    scheduler.delete_task(tid)
    return f"cancelled task #{tid}"


@tool({
    "type": "function",
    "function": {
        "name": "list_suggestions",
        "description": "List Oceano's self-improvement suggestions — proposals nightly reflection filed for "
                       "the user to approve. Each shows '#id [kind] status: title'. Defaults to pending; "
                       "pass status='all' for every status. Accept one with accept_suggestion.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "description": "pending (default), accepted, dismissed, done, or all"},
        }},
    },
})
def list_suggestions(status="pending"):
    from oceano import suggestions
    items = suggestions.all_suggestions(status=(status or "pending"))
    if not items:
        return "(no suggestions)"
    return "\n".join(f"#{s['id']} [{s['kind']}] {s['status']}: {s['title']}"
                     + (f" — {s['detail']}" if s['detail'] else "") for s in items)


@tool({
    "type": "function",
    "function": {
        "name": "accept_suggestion",
        "description": "Accept a pending suggestion by id (from list_suggestions) and ACT on it: a "
                       "'research' suggestion creates a scheduled research topic, 'workflow' a workflow "
                       "draft, 'memory' a saved memory; other kinds are marked for manual follow-up. "
                       "This changes Oceano's setup, so do it when the user approves.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "integer", "description": "the suggestion id shown by list_suggestions"},
        }, "required": ["id"]},
    },
})
def accept_suggestion(id):
    from oceano import suggestions
    r = suggestions.accept(int(id))
    return r.get("result") if r.get("ok") else f"could not accept #{id}: {r.get('error')}"


@tool({
    "type": "function",
    "function": {
        "name": "dismiss_suggestion",
        "description": "Dismiss a self-improvement suggestion by id (from list_suggestions) so it's no "
                       "longer pending.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "integer", "description": "the suggestion id shown by list_suggestions"},
        }, "required": ["id"]},
    },
})
def dismiss_suggestion(id):
    from oceano import suggestions
    r = suggestions.dismiss(int(id))
    return f"dismissed suggestion #{id}" if r.get("ok") else f"could not dismiss #{id}: {r.get('error')}"


def _run_one_workflow(name, inp=""):
    from oceano import workflows
    name = str(name or "").strip()
    wf = workflows.get_by_name(name)
    if not wf and name.isdigit():
        wf = workflows.get(int(name))
    if not wf:
        avail = ", ".join(w["name"] for w in workflows.list_all()) or "(none defined)"
        return f"no workflow named {name!r}. Available: {avail}"
    decl = wf.get("input") or {}
    if decl.get("enabled") and decl.get("required") and not (inp or decl.get("default")):
        return (f"workflow '{wf['name']}' needs an input"
                + (f" ({decl['label']})" if decl.get("label") else "") + " — call run_workflow again with `input`.")
    rec = workflows.run(wf, trigger="agent", inp=inp)
    lines = [f"Workflow '{wf['name']}' — {rec['summary']}"]
    for s in rec.get("steps", []):
        mark = "✓" if s["ok"] else "✗"
        lines.append(f"  {mark} {s['label']}: {(s['output'] or '').strip()[:240]}")
    return "\n".join(lines)


@tool({
    "type": "function",
    "function": {
        "name": "run_workflow",
        "description": "Run one of the user's saved workflows (a named, multi-step recipe) "
                       "right now, by name or id. Use this when the user asks to run a workflow, "
                       "or when a task matches a workflow they've defined. Some workflows take an "
                       "INPUT value (a workflow that processes whatever you pass it) — call "
                       "list_workflows to see which, and pass the value as `input`. You can RUN "
                       "workflows but not create them — the user authors workflows in the UI. Run "
                       "several in sequence by passing a comma-separated list of names (the same "
                       "input, if any, is given to each).",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "a workflow name or numeric id, or several comma-separated"},
            "input": {"type": "string", "description": "the input value to feed the workflow, if it takes one"},
        }, "required": ["name"]},
    },
})
def run_workflow(name, input=""):
    """Run one workflow, or several in sequence: pass a comma-separated list of names. `input`
    is the workflow's argument (used by workflows that declare they take one)."""
    inp = str(input or "")
    names = [n.strip() for n in str(name or "").split(",") if n.strip()]
    if len(names) > 1:
        return "\n\n".join(_run_one_workflow(n, inp) for n in names)
    return _run_one_workflow(names[0] if names else str(name or ""), inp)


@tool({
    "type": "function",
    "function": {
        "name": "list_workflows",
        "description": "List the user's saved workflows (name, description, step count) so you "
                       "know which ones can be run with run_workflow.",
        "parameters": {"type": "object", "properties": {}},
    },
})
def list_workflows():
    from oceano import workflows
    wfs = workflows.list_all()
    if not wfs:
        return "(no workflows defined yet — the user can create them in the Workflows window)"
    def _nodes(w):
        return len([n for n in w.get("graph", {}).get("nodes", []) if n.get("type") not in ("start", "end")])
    def _inp(w):
        d = w.get("input") or {}
        if not d.get("enabled"):
            return ""
        lab = d.get("label") or "a value"
        return f" · takes input: {lab}" + ("" if d.get("required") else " (optional)")
    return "\n".join(f"- {w['name']} ({_nodes(w)} nodes){_inp(w)}"
                     + (f": {w['description']}" if w.get("description") else "") for w in wfs)


@tool({
    "type": "function",
    "function": {
        "name": "notify",
        "description": "Send the user a push notification on the channels they enabled (ntfy on "
                       "their phone, and/or their Telegram). Use to report when a long or background "
                       "task is finished, or anything they asked to be told about.",
        "parameters": {"type": "object", "properties": {
            "message": {"type": "string"},
            "title": {"type": "string"},
        }, "required": ["message"]},
    },
})
def notify(message, title="Oceano"):
    return scheduler.notify(message, title)
