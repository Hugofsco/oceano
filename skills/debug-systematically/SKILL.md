---
name: debug-systematically
description: find and fix bugs by reproducing, reading the error, locating the cause, then making the smallest fix
status: published
---
# Debug systematically

Don't guess-edit. Bugs are found by evidence, not by hope.

1. **Reproduce it first.** Run the failing command/test/script with `run_shell` or
   `python_exec` and read the *actual* error and full traceback. If you can't make it
   fail on demand, you can't know you've fixed it.
2. **Read the traceback bottom-up.** The last frame is usually where it broke — note
   the `file:line`. The message names the error type (KeyError, TypeError, …) — take it
   literally.
3. **Go to that line.** Open a window there (`sed -n` / `read_file`; see
   [[read-large-files]]) and read the surrounding code to understand intent.
4. **Trace it.** Use [[search-the-codebase]] to find where the failing function/variable
   is defined and who calls it — the cause is often a caller passing the wrong thing.
5. **Form ONE hypothesis**, then make the **smallest** fix with [[edit-file-precisely]].
   Resist rewriting; change the specific cause.
6. **Re-run** to confirm the error is gone (step 1's command), and run nearby tests so
   the fix didn't break a neighbor — see [[verify-by-running]].
7. **When stuck after ~2 tries:** add a temporary `print(...)`/log near the suspect line,
   run, inspect the real values, then remove it. Narrow a big input to a minimal failing
   case.
8. **For a genuinely hard or unfamiliar bug**, hand it to `delegate_to_claude` with the
   exact error, the relevant file paths, and what "fixed" looks like.

Skip binary files and generated/vendored code while hunting — the bug is almost always
in source you (or the user) wrote.
