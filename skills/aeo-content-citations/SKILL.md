---
name: aeo-content-citations
description: audit and optimize written content so LLMs (ChatGPT, Perplexity, Claude, Gemini) cite it as a source, and track which pages get cited — use for brand/store content, blog posts, or "get cited by ChatGPT"/"AEO audit" requests
status: published
notes: ported from claude-skills marketing-skill/aeo (MIT); scripts copied verbatim, stdlib-only
---
# AEO — get content cited by LLMs

Distinct from SEO: SEO optimizes for click-through search rank; this optimizes for being
cited as the source inside an LLM's answer (E-E-A-T signals, structured facts, schema
markup). Both can coexist on the same piece.

Workflow:
1. **Audit** — `python3 skills/aeo-content-citations/scripts/aeo_audit.py --input <file.md> --industry <saas|ecommerce|b2b|media|...>`
   (or `--url <live-url>`, stdlib-only, no deps). Gives a 0-100 composite + Experience/
   Expertise/Authoritativeness/Trustworthiness breakdown + top fixes.
2. **Optimize** — `python3 skills/aeo-content-citations/scripts/aeo_optimizer.py --input <file.md> --mode conservative|balanced|aggressive --output <out.md>`.
   Rewrites structure, adds citations, injects FAQ/HowTo schema.
3. **Track** — `python3 skills/aeo-content-citations/scripts/citation_tracker.py --action add --url <url> --llm <perplexity|chatgpt|claude|gemini> --query "<query>" --date <YYYY-MM-DD>`,
   then `--action report --url <url>` for citation count/velocity. Stores locally at
   `~/.aeo-data/citations.json`, no telemetry.

Before any of this: a blocked `GPTBot`/`PerplexityBot`/`ClaudeBot`/`Google-Extended` in
robots.txt zeroes that platform regardless of content quality — check that first.

Run any script with `--sample` for a demo, `--output json` for machine-readable results.
