---
name: code-review
description: review code in the workspace for bugs, then propose concrete fixes
---
# Code review

When the user asks you to review or improve code:

1. Find the files: `list_files`, then `read_file` each relevant one. For a large
   tree, `run_shell` with grep to locate the parts that matter.
2. Review for, in priority order:
   - **Correctness** — bugs, wrong logic, unhandled errors, edge cases, off-by-one.
   - **Security** — injection, unsafe input/paths, secrets in code.
   - **Clarity & reuse** — duplication, dead code, confusing names.
3. Report findings as a list. For each: `file:line`, what's wrong, why it matters,
   and the concrete fix.
4. If the user asks you to apply fixes, use `edit_file` (exact find/replace) so you
   change only the affected lines — don't rewrite whole files.
5. After editing, re-read or run the code (`run_shell` / `python_exec`) to confirm
   it still works.

Be specific and honest. Prefer a few high-confidence findings over a long list of
nitpicks. Say so when something looks fine.
