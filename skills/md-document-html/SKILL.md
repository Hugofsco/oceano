---
name: md-document-html
description: convert a long markdown report/plan/spec (100+ lines) into a single-file interactive HTML document with a sticky TOC, search, scrollspy, and code-copy buttons — use before showing a long research-report/dossier/audit output in the Preview window, since plain markdown gets hard to navigate past ~100 lines
status: published
notes: ported from claude-skills markdown-html/md-document (MIT); all 3 scripts + config_loader copied, patched to be self-contained (no dependency on the separate design-system skill — sane built-in defaults, onboarding optional); tested end-to-end with real markdown, not just --sample
---
# Markdown to interactive HTML

For a long output (a [[research-report]], [[deep-research]] report, [[hypothesis-driven-dossier]],
[[ship-gate-check]] audit, etc.) that's going into Oceano's Preview window rather than
just chat, render it to HTML instead of leaving it as plain markdown — past ~100 lines
markdown loses navigability that HTML restores (TOC, search, jump links). For anything
shorter, plain markdown in chat is fine — don't bother with this.

Three-stage pipeline, one file in, one file out:
```
python3 skills/md-document-html/scripts/markdown_parser.py --input <doc.md> --output sections.json
python3 skills/md-document-html/scripts/html_renderer.py --sections sections.json --output doc.html
python3 skills/md-document-html/scripts/interactivity_injector.py --file doc.html --features search,copycode,smoothscroll,scrollspy
```
(`html_renderer.py --sample` alone renders a demo if you just want to see the shape.)

Single-file output — only externals are the Google Fonts CSS link and Prism.js CDN for
code highlighting, no JS framework, no build step. Ships with a neutral default brand
palette out of the box; if you want a custom one, hand-edit the palette constants in
`scripts/html_renderer.py` rather than chasing the original repo's onboarding wizard
(not ported — not worth the setup for single-user use).
