---
name: extract-to-csv
description: pull structured rows out of messy text/pages/docs into a clean CSV
---
# Extract to CSV

When the user wants structured data ("get all the prices into a table", "extract
the contacts") out of unstructured sources:

1. Gather the source(s): `fetch_url`, `read_file`, or `search_docs`.
2. Decide the columns with the user's goal in mind; keep them consistent.
3. Use `python_exec` with the `csv` module to write the file — don't hand-format
   CSV (it mishandles commas/quotes). Example shape:
   ```python
   import csv
   rows = [{"name": "...", "price": "..."}]
   with open("out.csv", "w", newline="") as f:
       w = csv.DictWriter(f, fieldnames=["name", "price"]); w.writeheader(); w.writerows(rows)
   ```
4. Leave a cell blank when a value is genuinely missing — never invent data.
5. Tell the user the path and the row count, and show the first few rows.
