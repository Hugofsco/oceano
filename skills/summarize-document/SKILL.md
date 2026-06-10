---
name: summarize-document
description: summarize a file or web page into key points, faithfully and concisely
---
# Summarize a document

When the user asks you to summarize a file, page, or set of docs:

1. Get the content: `read_file` for a workspace file, `fetch_url` for a web page,
   or `search_docs` if they mean their indexed documents.
2. If it's long, work in sections so nothing is dropped.
3. Produce, in markdown:
   - **TL;DR** — 2-3 sentences.
   - **Key points** — 5-8 bullets, each a concrete claim from the source.
   - **Notable details / numbers** — only if present.
   - **Open questions** — anything the source leaves unclear (omit if none).
4. Stay faithful: summarize only what's actually there; never invent. If the user
   asked for a target length, respect it.

If they want it saved, `write_file` it to the workspace as `summary.md`.
