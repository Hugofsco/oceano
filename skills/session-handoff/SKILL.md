---
name: session-handoff
description: write a compact handoff note summarizing the current conversation/task so a fresh session (or a different mind — local, api, Claude, Codex) can pick it up cold — use when ending a long session, before delegating a subtask, or when the user asks to "hand off" or "continue this later"
status: published
notes: adapted from Matt Pocock's handoff skill (MIT)
---
# Session handoff

Write the note to `data/handoffs/<slug>-<date>.md` (create the dir if needed), not into chat
only — the next session needs to find it.

Sections, kept tight:
- **Goal** — what the next session should accomplish (from the user's framing, or inferred)
- **State** — what's actually done vs. still blocking, in plain facts
- **Open decisions** — anything the next session must still decide
- **Skills to load** — concrete skill names relevant to what's next
- **Artifacts** — paths to files/scripts/memory entries touched; reference them, never
  re-paste their content

Don't duplicate content that already lives somewhere durable (a memory entry, a file in the
workspace, a commit). Point at it instead. If the user said what the next session is for,
tailor the note to that; otherwise infer it from the last few turns.
