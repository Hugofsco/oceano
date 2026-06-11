---
name: edit-file-precisely
description: change code/text safely with exact find-and-replace instead of rewriting whole files
status: published
---
# Edit a file precisely

Goal: change exactly what needs changing, and nothing else. Rewriting a whole file
with `write_file` risks dropping code you didn't mean to touch — prefer `edit_file`.

1. **Read before you edit.** `read_file` the file first (for a big file, see the
   [[read-large-files]] skill). Never edit a file you haven't looked at this turn.
2. **Use `edit_file(path, find, replace)`** for changes to an existing file. `find`
   must match the file **byte-for-byte**, including indentation and surrounding
   whitespace — copy it straight from what you just read.
3. **Make `find` unique.** `edit_file` replaces *every* occurrence of `find`. If the
   snippet appears more than once, widen `find` to include a distinctive line above
   or below so it matches only the spot you mean.
4. **One change per call.** Smaller, targeted edits are easier to get right and to
   undo than one giant replacement.
5. **If it says the text wasn't found**, you copied it inexactly (a space, a tab, a
   trailing comma). Re-read the exact region and copy it again — don't switch to
   `write_file` to brute-force it.
6. **Only `write_file`** for brand-new files, or when you truly intend to replace the
   whole file's contents.
7. **Confirm.** Re-read the changed region (or run it — see [[verify-by-running]]) so
   you know the edit landed as intended.
