"""Self-improvement: learned skills, independent review, and delegation."""
from oceano import safety, skills
from oceano.tools.core import _TOOLS, _ws, emit_progress, tool

# --- self-improvement: learned skills + delegation ---------------------------
@tool({
    "type": "function",
    "function": {
        "name": "learn_skill",
        "description": "Save a NEW reusable skill you worked out during this task, for your "
                       "future self. It is stored as 'learning' and reviewed by an independent "
                       "model before being published into your active skills. Use only for "
                       "genuinely reusable know-how, not one-off details.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "short kebab-case name, e.g. scrape-paginated-site"},
            "description": {"type": "string", "description": "one line: when should this skill be used?"},
            "body": {"type": "string", "description": "the instructions: short imperative steps"},
        }, "required": ["name", "description", "body"]},
    },
})
def learn_skill(name, description, body):
    return skills.learn_skill(name, description, body)


@tool({
    "type": "function",
    "function": {
        "name": "evaluate_skill",
        "description": "Independently review a LEARNING skill and, if it's good, promote it to "
                       "STAGING. A stronger model (never the one that wrote it) checks it for "
                       "correctness/safety/usefulness/clarity, EDITS it to fix it if salvageable, "
                       "and ensures it doesn't duplicate or contradict an already-published skill; "
                       "conflicts or unfixable skills are rejected. It only stages — publishing "
                       "stays a separate step. Use right after learn_skill in a self-improvement "
                       "flow. Leave `name` empty to review the most recently learned skill.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "skill name or dir to review; empty = the most recently learned one"},
        }},
    },
})
def evaluate_skill(name=""):
    r = skills.review_one(name or None)
    if not r.get("ok"):
        return f"skill review failed: {r.get('error')}"
    if not r.get("reviewed"):
        return r.get("reason", "nothing to review")
    bits = [f"{r['name']} ({r['dir']}) → {r['result']}"]
    if r.get("edited"):
        bits.append("edited to fix")
    if r.get("conflicts_with"):
        bits.append(f"conflicts with {r['conflicts_with']}")
    if r.get("notes"):
        bits.append(r["notes"])
    return " · ".join(bits)


@tool({
    "type": "function",
    "function": {
        "name": "delegate",
        "description": "Hand a self-contained subtask to the configured delegate — a stronger "
                       "assistant running headless in the workspace. WHO that is, is set by the user "
                       "in Settings → Delegation (Claude Code by default, or a cloud model run as a "
                       "full agent); you don't choose — just delegate. Either way it can read, write, "
                       "and run things to complete the task. Give precise, complete instructions — "
                       "the relevant file paths and exactly what it must produce — because it cannot "
                       "ask you questions. Returns its final report. CALL THIS whenever the user asks "
                       "you to 'delegate' or 'have the strong model do it', or for a heavy subtask "
                       "beyond you. The capability is available — don't claim you can't.",
        "parameters": {"type": "object", "properties": {
            "instructions": {"type": "string"},
        }, "required": ["instructions"]},
    },
})
def delegate_tool(instructions):
    from oceano import delegate
    # `instructions` is a free-form string the model controls, handed to a full agentic CLI that can
    # read, write and run things. That is at least as powerful as ssh_run (which IS gated), so it
    # gets the same anti-laundering gate.
    refusal = safety.spawn_blocked()
    if refusal:
        return refusal

    def on_prog(ev):                         # surface the delegate's live work to the frontend
        emit_progress({"source": "delegate", **ev})

    r = delegate.run(instructions, cwd=_ws(), on_progress=on_prog)  # Settings → Delegation
    if r["ok"]:
        return r["output"][:8000] or "(the delegate finished but returned no text)"
    # Failed/stalled/capped. Hand back any partial work AND tell the local model NOT to attempt
    # the whole job itself — that's what overflows a small context window and produces garbage.
    partial = (r.get("output") or "").strip()
    msg = f"The delegate did not finish: {r.get('error')}."
    if partial:
        msg += f"\n\nPartial result it produced before stopping:\n{partial[:6000]}"
    msg += ("\n\nIMPORTANT: do NOT try to build or write this whole thing yourself — it is a "
            "large task meant for the delegate and will exceed your context. Tell the user the "
            "delegation didn't complete, summarize any partial progress above, and suggest they "
            "retry (delegation now streams and only stops if genuinely stalled, so a retry "
            "usually gets further) or break the request into smaller pieces.")
    return msg


# back-compat: the tool was once 'delegate_to_claude'. Keep the old name callable (not shown
# to the model) so any saved reference still routes to the generalized delegate.
_TOOLS["delegate_to_claude"] = delegate_tool


def _chat_agent_tool_scope(access):
    """Translate the user-owned chat spawn permission into every provider's tool vocabulary."""
    if access == "shell":
        return "Read,Glob,Grep,Write,Edit,Bash"
    if access == "write":
        return "Read,Glob,Grep,Write,Edit"
    return "Read,Glob,Grep"


@tool({
    "type": "function",
    "function": {
        "name": "spawn_agent",
        "description": (
            "Start a contained sub-agent on a task IN THE BACKGROUND and return immediately — "
            "like delegate, but you keep working while it runs. Oceano's daemon owns the run, "
            "tracks it, notifies the user when it finishes, and delivers the result back into "
            "this conversation. Give precise, self-contained instructions (file paths, exactly "
            "what to produce) — it cannot ask questions. provider: omit for the configured "
            "delegation default, or pick 'claude' | 'codex' | 'api' | 'local' ('local' shares "
            "the one resident model: serialized and weak — avoid for heavy work). Check on it "
            "with agent_status. Use for parallel subtasks; for one blocking subtask whose answer "
            "you need right now, use delegate instead."
        ),
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "description": "complete, self-contained instructions"},
            "provider": {"type": "string", "enum": ["", "claude", "codex", "api", "local"],
                         "description": "who runs it; empty = the configured delegation default"},
            "label": {"type": "string", "description": "short name, e.g. 'summarize logs'"},
            "timeout": {"type": "integer", "description": "seconds before the agent is stopped (default 600)"},
        }, "required": ["task"]},
    },
})
def spawn_agent(task, provider="", label="", timeout=0):
    refusal = safety.spawn_blocked()           # an unattended sub-agent must not start from a tainted turn
    if refusal:
        return refusal
    from oceano import agentjobs, mindbridge   # lazy: mindbridge imports tools (avoid an import cycle)
    from oceano.web import state as web_state
    access = web_state.load().get("prefs", {}).get("chat_agent_access", "read")
    try:
        rec = agentjobs.spawn(task, provider=provider, label=label, timeout=timeout,
                              tools=_chat_agent_tool_scope(access), cwd=_ws(),
                              sid=mindbridge.active_session())
    except RuntimeError as e:                  # cap / unknown provider → relay the reason verbatim
        return f"could not spawn the agent: {e}"
    out = (f"started agent #{rec['id']} ({rec['provider']}) {rec['label']} - running in the "
           f"background; check it with agent_status(agent_id={rec['id']}). The user is notified "
           f"and the result is delivered here when it finishes. This asynchronous start does "
           "not complete the parent turn: continue any independent work, start any other "
           "requested agents, and give the user a proper progress response. Do not wait or poll "
           "unless the user explicitly asked you to.")
    if rec.get("warning"):
        out += "\n" + rec["warning"]
    return out


@tool({
    "type": "function",
    "function": {
        "name": "agent_status",
        "description": "Check a background sub-agent started with spawn_agent: state (running/"
                       "done/failed/lost), its result or error, and a tail of its progress. "
                       "Omit agent_id to list every agent Oceano is tracking.",
        "parameters": {"type": "object", "properties": {
            "agent_id": {"type": "integer", "description": "id from spawn_agent; omit to list all"},
        }},
    },
})
def agent_status(agent_id=None):
    from oceano import agentjobs
    if not agent_id:
        js = agentjobs.status()
        return "\n".join(f"#{j['id']} [{j['state']}] ({j['provider']}) {j['label']}" for j in js) \
            or "no background agents"
    rec = agentjobs.status(agent_id)
    if rec is None:
        return f"ERROR: no agent #{agent_id}"
    out = f"#{rec['id']} \"{rec['label']}\" ({rec['provider']}) — {rec['state']}"
    if rec["state"] == "done" and rec.get("output"):
        out += "\n--- result ---\n" + rec["output"]
    elif rec["state"] == "failed":
        out += f"\nerror: {rec.get('error') or 'unknown'}"
    if rec.get("tail") and rec["state"] == "running":
        out += "\n--- progress tail ---\n" + rec["tail"]
    return out
