---
name: author-a-skill
description: how to write a new Oceano skill (skills/<slug>/SKILL.md) correctly — use when the user asks to create, write, or save a new skill, or when learn_skill/save_skill is about to be called
status: published
notes: Oceano's skill format differs from generic agent-skill conventions (flat, single-file, small-model-budget) — this documents Oceano's own shape, not a generic one
---
# Author a skill

An Oceano skill is exactly one file: `skills/<slug>/SKILL.md`. No subfolders are read by the
loader — only this file's frontmatter + body reach the model, so keep it short.

Frontmatter (plain `key: value` lines, no nested YAML, no multi-line block scalars — the
parser is line-based):
```
---
name: <slug-ish name>
description: <one line: what it's for + when to use it>
status: published
---
```
`description` is what semantic retrieval matches against — make it specific (the situation,
the trigger words), not generic ("helps with X" tells the agent nothing).

Body: short, imperative, numbered steps a small model can follow without re-deriving intent.
Aim for **under ~30 lines** — this gets injected into context alongside other skills, and
budget matters most for the local model. Link related skills with `[[skill-name]]` instead
of re-explaining what they already cover.

Before saving:
- [ ] Description states the trigger, not just the topic
- [ ] Body is steps, not a survey/essay
- [ ] No time-sensitive facts (they go stale silently)
- [ ] Doesn't duplicate an existing published skill — check `list_skills`/`catalog` first
- [ ] If it needs a deterministic script, keep it in `skills/<slug>/scripts/` and invoke it
      by path with `run_shell`/`python_exec` (relative to the workspace) — SKILL.md stays
      the short instructions, the script does the heavy lifting

If you (the model) are proposing this skill for yourself rather than the user authoring it
directly, use `learn_skill`, not `save_skill` — it goes through independent review before
publishing, so it never trusts its own judgment alone.
