---
name: hypothesis-driven-dossier
description: research a specific company or person by testing a stated hypothesis rather than writing a generic profile — use for meeting prep, diligence, or "tell me about X before I talk to them"
status: published
notes: distilled from claude-skills research/dossier (MIT) — kept the hypothesis-testing discipline and source-tiering as prose; dropped the DOCX/Node.js pipeline (use report.md like research-report) and the BYOK MCP source matrix (LinkedIn/Crunchbase/Pitchbook aren't wired up here) in favor of web_search/fetch_url
---
# Hypothesis-driven dossier

A profile that only confirms what the user already believes is worthless. Force the
hypothesis before researching.

1. **Ask for the hypothesis, not just the subject.** "What do you already believe about
   [X], and what do you want to verify or disprove?" If they don't have one, push back
   once: "commit to a guess you can update later." If still refused, fall back to "what's
   the most surprising thing I could find?" and say so in the output.
2. **Disambiguate the subject** — exact name + a second identifier (company domain,
   LinkedIn URL, employer+role). Don't proceed on an ambiguous name.
3. **Search both directions.** Spend real budget on disconfirming queries, not just
   supporting ones — aim for at least 30% of your searches trying to break the hypothesis,
   not confirm it. Use `web_search`/`fetch_url` (see [[research-report]] for the
   cross-check discipline).
4. **Tier every source** as you go: primary (official filings, court records, the
   company's own site), secondary (mainstream news/trade press), tertiary (blogs,
   forums, social). Weight the verdict accordingly.
5. **Write the verdict explicitly** — SUPPORTED / PARTIALLY SUPPORTED / DISPROVEN /
   INCONCLUSIVE — and say why, engaging with the disconfirming evidence you found, not
   just listing supporting facts.
6. **Give finding-tied hooks, not generic ones.** Not "ask about their roadmap" — "they
   shipped [X] three weeks ago (their blog); ask how it changes their Y plans."

Output as `report.md` per [[research-report]]'s shape, with an extra **Hypothesis test**
section (hypothesis stated verbatim, supporting evidence, disconfirming evidence,
verdict) ahead of the usual findings/sources.
