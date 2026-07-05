---
name: copywriting
description: write or rewrite marketing/store copy — headlines, CTAs, product pages, landing pages — use for "write copy for", "improve this copy", headline help, or CTA copy
status: published
notes: ported from claude-skills marketing-skill/copywriting (MIT), trimmed from a full consulting-page framework down to the working principles + scorer
---
# Copywriting

Before writing, know: who's the audience, what's the ONE action you want them to take,
what makes this different from the alternative, and any real proof points (numbers,
reviews). Ask if missing — don't invent proof.

Principles, in order of importance: clear beats clever; benefits (what it means for
them) beat features (what it does); specific beats vague ("cuts setup from 20 minutes
to 2" beats "saves time"); customer's own words beat company jargon; active voice;
never fabricate a stat or testimonial.

Weak CTA verbs to avoid: Submit, Sign Up, Learn More, Click Here. Strong pattern:
[action] + [specific thing they get] — "Get the Complete Checklist," not "Download."

**Score every headline candidate before picking one** — write 5-10, score them all,
present the top 2-3 with scores:
```
python3 skills/copywriting/scripts/headline_scorer.py "<headline>"
python3 skills/copywriting/scripts/headline_scorer.py --file headlines.txt --json
```
0-100 across power words / emotional trigger / numbers / length / specificity /
clarity. Never present a sub-60 headline as the primary recommendation.

Always give 2-3 alternatives for a headline or CTA with a one-line reason each — never
ship a single option and call it done. For AI-sounding drafts, run [[content-humanizer]]
after this, before an SEO pass.
