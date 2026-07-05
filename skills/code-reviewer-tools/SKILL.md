---
name: code-reviewer-tools
description: deterministic PR/code-quality scan (complexity, SOLID violations, hardcoded secrets, code smells) across 14 languages — a quick mechanical pre-check, not a replacement for Claude Code's native /code-review
status: published
notes: ported from claude-skills engineering-team/code-reviewer (MIT); scripts copied verbatim, stdlib-only. Explicitly supplementary — Claude Code already ships a native /code-review skill for the real review; this is the fast deterministic scan to run first or in CI
---
# Code reviewer tools (supplementary)

Run this for a fast mechanical pass before or alongside the real review — it catches
the boring stuff deterministically so the harder review can focus on logic and design.

1. **Diff-level risk scan:** `python3 skills/code-reviewer-tools/scripts/pr_analyzer.py <repo> --base main --head <branch>`
   → complexity score 1-10, risk tier, files to prioritize; flags hardcoded secrets,
   injection patterns, leftover debug statements, TODO/FIXME.
2. **Structural quality scan:** `python3 skills/code-reviewer-tools/scripts/code_quality_checker.py <path> --language <lang>`
   → long functions (>50 lines), god classes (>20 methods), deep nesting (>4 levels),
   too many params (>5). Supports python/typescript/go/swift/kotlin/csharp/java/c/cpp/
   rust/ruby/php/dart.
3. **Combined report:** `python3 skills/code-reviewer-tools/scripts/review_report_generator.py <repo> --format markdown --output review.md`
   → verdict: 90+/no-high = approve, 75+/≤2 high = approve with suggestions, 50-74 =
   request changes, <50 or any critical = block.

Use this to triage before spending real review effort, or as a CI gate — not as the
review itself.
