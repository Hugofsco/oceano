---
name: research-report
description: how to produce a structured, cited research report on a topic
---
# Research report

When the user asks you to research a topic in depth, follow these steps:

1. `web_search` the topic to find 4-6 relevant sources.
2. Read the most promising ones with `fetch_url` (or `browser_open` for JS-heavy
   pages). Don't trust a single source.
3. Cross-check every important claim against at least two sources.
4. Write the result to the workspace as `report.md` with this shape:
   - **Summary** — 2-3 sentences.
   - **Key findings** — bullet points, each with the source URL in parentheses.
   - **Sources** — the list of URLs you actually read.
5. `remember` any durable fact worth keeping for later.

Be factual. Cite every non-obvious claim. If sources disagree, say so.
