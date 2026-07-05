---
name: academic-literature-search
description: search real academic literature (PubMed + OpenAlex) with structured, citable metadata instead of generic web search — use when the topic is scientific/medical/academic and you need actual papers, authors, and citation counts
status: published
notes: distilled from claude-skills research/litreview (MIT) — kept the free keyless-API search lane and its script; dropped the DOCX/Node.js generation pipeline and multi-phase grill-me intake as a mismatch for this agent (no docx lib, no multi-turn forcing-question apparatus needed for a quick lookup)
---
# Academic literature search

For a scientific/academic question, plain `web_search` misses structured bibliographic
data. Use the free, keyless APIs instead:

```
python3 skills/academic-literature-search/scripts/free_search.py --query "<topic>" --source both --max 15 --mailto <your-email>
```
`--source pubmed` for clinical/biomedical, `--source openalex` for broader academic
coverage (also returns `cited_by_count`, useful for spotting seminal papers). `--json`
for structured output. Stdlib-only `urllib`, exits 2 with a clear message if offline.

Rate-limit etiquette: PubMed E-utilities ≤3 req/s; OpenAlex is faster with `--mailto`
(the "polite pool"). Don't parallelize calls — go sequential.

Write findings the same way [[research-report]] does: summary, key findings each with
their source URL, and a sources list — cite only papers this search actually returned,
never fill gaps from training knowledge. If a query returns nothing, say so explicitly
rather than padding the answer.
