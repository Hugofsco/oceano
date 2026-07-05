---
name: a11y-audit
description: scan React/Vue/Next.js/plain-HTML code for WCAG 2.2 violations and check color contrast — use before shipping a new component or page, or when auditing the WooCommerce storefront
status: published
notes: ported from claude-skills engineering-team/a11y-audit (MIT); scripts copied verbatim, stdlib-only
---
# Accessibility audit

```
python3 skills/a11y-audit/scripts/a11y_scanner.py <path> --format table
python3 skills/a11y-audit/scripts/contrast_checker.py --fg "#767676" --bg "#ffffff"
python3 skills/a11y-audit/scripts/contrast_checker.py --file styles.css --suggest
```
Scanner auto-detects framework (React/Vue/Angular/Svelte/HTML); severity is
Critical (blocks a user group — missing alt text, no keyboard access) / Major
(insufficient contrast, missing form labels) / Minor (redundant ARIA, heading skips).
Fix Critical before shipping; Major within the same work session; Minor when convenient.

Common mistakes worth checking for by eye even without the scanner: `<div onClick>`
instead of `<button>` (loses keyboard support for free), `outline: none` with no
replacement focus style, placeholder text used as the only label, color alone conveying
state (add an icon/text too), `display: none` for screen-reader hiding instead of
`.sr-only`.

Re-run the scanner with `--baseline <prior-scan.json>` after fixing, to confirm no
regression. Pairs with [[react-frontend-tools]] at component-creation time rather than
as an afterthought.
