"""Self-improvement: learned skills, independent review, and delegation."""
import config
from oceano import skills
from oceano.tools.core import _TOOLS, emit_progress, tool

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

    def on_prog(ev):                         # surface the delegate's live work to the frontend
        emit_progress({"source": "delegate", **ev})

    r = delegate.run(instructions, cwd=config.WORKSPACE, on_progress=on_prog)  # Settings → Delegation
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
