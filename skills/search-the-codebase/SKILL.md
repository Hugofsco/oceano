---
name: search-the-codebase
description: find code fast with recursive grep — skipping binaries, deps, and noise dirs
status: published
---
# Search the codebase

When you need to find where something is defined or used, search — don't guess paths
or read files at random. Use `run_shell` with grep (or ripgrep if present).

1. **Prefer ripgrep when available** — it's fast and auto-skips binaries + gitignored
   files: `command -v rg && rg -n "thing" .`
2. **Otherwise recursive grep, skipping binaries and noise:**
   ```
   grep -rnI --exclude-dir={.git,node_modules,venv,.venv,__pycache__,dist,build,.eval-runs} "thing" .
   ```
   - `-r` recursive, `-n` line numbers, **`-I` skips binary files** (don't dump
     gibberish from images/blobs).
   - `--exclude-dir=...` skips dependency/build/VCS clutter so results stay relevant.
3. **Narrow by file type** when you can: add `--include='*.py'` (or `*.js`, `*.ts`).
4. **If a query returns too many hits**, count first with `grep -rcI ... | grep -v ':0'`
   or make the pattern more specific before reading anything.
5. **Read context around a hit**: `grep -nI -C3 "thing" path/to/file`, or once you have
   a line number, open a window with `sed -n 'START,ENDp' path` (see [[read-large-files]]).
6. **Find definitions** with anchored patterns, e.g. `grep -rnI "^def name\|^class Name\|function name" .`.

This pairs with [[debug-systematically]] (locate the failing symbol) and
[[edit-file-precisely]] (change it once you've found it).
