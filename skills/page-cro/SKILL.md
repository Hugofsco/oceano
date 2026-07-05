---
name: page-cro
description: audit a store/landing/pricing page for conversion problems and recommend fixes ranked by impact — use for "this page isn't converting" or before launching a new product/checkout page
status: published
notes: ported from claude-skills marketing-skill/page-cro (MIT), trimmed from a full consulting framework down to the working checklist + scorer
---
# Page conversion audit

```
python3 skills/page-cro/scripts/conversion_audit.py --file <page.html>
```
(or `--url`, `--json`) — mechanical scan for CTA presence/count, form weight, social
proof, trust signals, with a score. Run this first; it anchors the manual pass.

Then check, in order of impact: value prop clear in 5 seconds → headline specific (use
[[copywriting]]'s scorer for candidates) → one obvious primary CTA above the fold with
outcome-focused copy (not "Submit"/"Learn More") → visual hierarchy/scannability → trust
signals near CTAs (reviews, logos, specific numbers) → objections addressed (price,
"will this work for me", guarantee) → friction (too many form fields, slow load,
confusing mobile layout).

Report as **Quick Wins** (do now) → **High-Impact** (bigger effort, bigger payoff) →
**Test Ideas** (hypotheses worth A/B testing, not assumptions). Never recommend testing
as a substitute for an obvious fix — say what to just fix vs. what's genuinely worth
testing.
