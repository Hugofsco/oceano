---
name: self-eval
description: honestly score the quality of work just completed using a two-axis model (ambition x execution) instead of defaulting every task to "pretty good" — use after finishing a nontrivial task, especially before reporting it as done
status: published
notes: ported from claude-skills engineering/self-eval (MIT); prose-only, adapted score-persistence path from a cwd dotfile to workspace/self-eval-scores.jsonl
---
# Self-eval

The failure mode this exists to prevent: every task quietly scoring "4/5" because
difficulty and execution quality get conflated into one number. Score them separately,
then read the matrix — don't pick a number and rationalize it.

**Axis 1 — Ambition** (what was attempted, not how well): Low (routine, no real risk of
failure) / Medium (real novelty or challenge, partial failure was possible) / High
(genuinely unfamiliar or high-stakes, real risk of total failure).

**Axis 2 — Execution** (quality of the actual output, independent of difficulty): Poor
(doesn't meet its own stated goal) / Adequate (done but with gaps or shortcuts) / Strong
(thorough, no obvious improvement left on the table).

**Composite matrix** (read it, don't override it):
| | Poor | Adequate | Strong |
|---|---|---|---|
| Low ambition | 1 | 2 | 2 |
| Medium ambition | 2 | 3 | 4 |
| High ambition | 2 | 4 | 5 |

Low ambition caps at 2 no matter how well it was done. A 5 needs both high ambition AND
strong execution — it should be rare. The honest score for solid ordinary work is 3.

**Before finalizing, argue both sides in ≥3 sentences total:** a case for scoring lower
(what was actually easy or avoided), a case for higher (what was genuinely hard or
exceeded plan), then resolve — re-rate an axis if either case reveals you misjudged it.

**Check for score inflation:** read `workspace/self-eval-scores.jsonl` if it exists; if
4+ of the last 5 scores are identical, say so explicitly before giving this one.

Append one line after presenting the evaluation:
`{"date":"YYYY-MM-DD","score":N,"ambition":"...","execution":"...","task":"..."}`
to `workspace/self-eval-scores.jsonl` (create it if missing).
