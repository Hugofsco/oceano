---
name: seo-technical-audit
description: audit a page or site for technical/on-page SEO issues (title, meta, headings, links, Core Web Vitals) and produce a prioritized fix list — use for "why am I not ranking", a traffic drop, or before a site migration/launch
status: published
notes: ported from claude-skills marketing-skill/seo-audit (MIT); scripts copied verbatim, stdlib-only
---
# SEO technical audit

1. **Score a page:**
   `python3 skills/seo-technical-audit/scripts/seo_checker.py --url <url>` (or `--file page.html`)
   → 0-100 on title/meta/headings/internal links/images, with what's missing.
2. **Roll up site health:** build a `checks.json` (array of check objects covering
   crawl/indexation/speed/content) and run
   `python3 skills/seo-technical-audit/scripts/seo_health_scorer.py --checks checks.json --industry <saas|ecommerce|local|publisher>`
   (`--demo` to see the expected shape first) → weighted 0-100 across 7 categories.

Core Web Vitals thresholds (75th percentile, real-user data): LCP good ≤2.5s / poor >4.0s,
INP good ≤200ms / poor >500ms, CLS good ≤0.1 / poor >0.25.

Report as: 3-5 bullet executive summary → findings (Issue / Impact / Evidence / Fix) →
a prioritized action plan (critical → high-impact → quick wins). For getting content
cited by LLMs rather than ranked by search, use [[aeo-content-citations]] instead —
they're complementary, not substitutes.
