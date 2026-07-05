---
name: deep-research
description: disciplined multi-source investigation for a high-stakes question where a wrong answer is expensive — use for strategy decisions, comparing N options, or validating a hypothesis against real data; for a quick topic overview use research-report instead
status: published
notes: distilled from claude-skills research/deep-research (MIT) — kept the core discipline (falsifiable hypotheses, parallel fan-out, triangulation, adversarial pass, per-source files); dropped the full 9-phase/plan.md/sources.csv/refresh_targets.md apparatus as heavier bookkeeping than a lightweight skill needs
---
# Deep research

This is the heavy end of research — reach for [[research-report]] instead when a fast
answer is fine and being wrong isn't costly.

1. **Reframe first.** Rewrite the question as 2-4 *falsifiable* hypotheses before
   searching — "X is the better choice because Y" you can actually confirm or refute,
   not a vague topic.
2. **Fan out in parallel, not sequentially.** If there's real breadth to cover (several
   subtopics, several competing sources), use `spawn_agent` to run searches
   concurrently instead of one at a time — that's the whole point of paying for depth.
3. **Save one file per source**, not one blob: `sources/01-<slug>.md` with the URL,
   date, and verbatim quotes — never paraphrase-then-forget where a claim came from.
   An empty fetch is an empty claim; never invent a plausible-sounding citation.
4. **Triangulate.** Every hypothesis needs ≥3 independent sources of *different types*
   (primary/official, academic, industry press, discussion/forum) before you call it
   confirmed. Fewer than that: label it "insufficient evidence," not fact.
5. **Run an adversarial pass before writing the conclusion.** Actively search for
   counter-evidence to each hypothesis, not just supporting evidence — see
   [[hypothesis-driven-dossier]] for the same discipline applied to entities.
6. **Persist the output** as `report.md` + the `sources/` folder in the workspace, not
   chat-only — the reuse value is in the files. List which hypotheses were confirmed,
   refuted, or left undetermined, each with its source count.

Don't skip checking whether you (or a past session) already researched this — check
memory and prior workspace files first.
