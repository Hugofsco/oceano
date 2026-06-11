---
name: verify-by-running
description: after changing code, actually run it and read the output before claiming it works
status: published
---
# Verify by running

A change you only *read* is a change you only *hope* works. Before you tell the user
it's done, make the code prove it.

1. **Pick the cheapest real check:**
   - Tests present? Run them — `run_shell` `pytest -q` (Python) or `npm test` / `node`
     (JS). Run the specific test file first, then the suite.
   - A script/module? Execute it with `python_exec` or `run_shell` and read stdout/stderr.
   - A web page/UI change? `fetch_url` the page or take a `browser_screenshot` to see it.
2. **Read the output, don't skim it.** Exit code, last lines, any traceback. A non-zero
   exit or an exception means it's *not* done — that output is your next debugging input
   (see [[debug-systematically]]).
3. **Check you didn't break the neighbors** — run the surrounding tests, not just the one
   you touched.
4. **Quick syntax gate for code you edited** before a full run: `python -m py_compile file.py`,
   or `node --check file.js`.
5. **Report honestly.** Say exactly what you ran and the result. If something still fails
   or you couldn't run it, say so plainly — never claim "works" on faith.
