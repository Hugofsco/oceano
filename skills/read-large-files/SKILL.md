---
name: read-large-files
description: read and navigate big files in targeted windows instead of choking on the read_file size limit
status: published
---
# Read large files

`read_file` returns only the **first ~20,000 characters** of a file. For anything
bigger, that gives you the top and silently hides the rest — so work in targeted
windows with `run_shell` instead of assuming you've seen the whole thing.

1. **Size it up first:** `wc -l path` for line count; if it's small, `read_file` is fine.
2. **Get an outline** instead of reading top-to-bottom:
   `grep -nI "^def \|^class \|^function \|^##\|^export " path` — gives you the map of
   functions/classes/sections with line numbers.
3. **Locate what you need** with [[search-the-codebase]] (`grep -nI "symbol" path`), then
4. **Open just that window:** `sed -n '120,180p' path` (lines 120–180). Widen as needed.
5. **Editing inside a big file:** copy the exact lines from your `sed` window into
   `edit_file`'s `find`, with enough surrounding context to be unique — see
   [[edit-file-precisely]]. Don't `write_file` a huge file from memory; you'll lose the
   parts you never read.
6. **Never paste a whole large file back to the user** — quote the relevant lines with
   their `file:line` references.
