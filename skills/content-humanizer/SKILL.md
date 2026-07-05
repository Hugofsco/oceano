---
name: content-humanizer
description: rewrite AI-sounding content (store copy, emails, social posts) so it reads like a person wrote it, not a committee — use when content feels robotic, generic, or reads like "delve into the landscape"
status: published
notes: ported from claude-skills marketing-skill/content-humanizer (MIT), heavily trimmed from a 3-mode consulting workflow down to the actual checklist + scorer
---
# Content humanizer

Score first, don't guess: `python3 skills/content-humanizer/scripts/humanizer_scorer.py <draft.md> --json`
→ 0-100. **80+** light polish only. **60-79** targeted pattern removal. **Below 60**
the AI fingerprint is too dense for a patch job — rewrite it, don't edit it. Re-score
after; the number has to move.

Kill on sight: "delve," "landscape," "crucial/vital/pivotal," "leverage," "furthermore/
moreover," "robust/comprehensive/holistic," "it's important to note that," any sentence
starting with a hedge. Replace, never just delete — "leverage" → "use," "robust" →
the actual number it handles.

Fix rhythm: AI writes uniform 18-22 word sentences. Vary it — short after long, a
one-word fragment for emphasis, a parenthetical aside.

Replace vague with specific: "many companies saw improvements" → name the company, the
year, the number. If you don't have one, say so honestly rather than inventing it —
"I haven't seen a controlled study on this, but in practice..." beats a fabricated stat.

If there's a brand voice on file, match it instead of writing generic-human. Otherwise
ask for one example of writing the user actually likes, then extract sentence length,
formality, and whether it uses humor — don't guess a voice and get it wrong.
