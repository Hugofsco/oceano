"""Cross-encoder reranker client — talks to the dedicated llama.cpp --reranking server (:8084).

A reranker reads each (query, document) pair JOINTLY and scores relevance, which is more accurate
than the bi-encoder cosine used for first-stage recall — but too slow to run over the whole corpus.
So RAG uses it as a second stage: dense retrieves a small candidate pool, this re-orders it.

OPTIONAL: if the reranker model/server isn't there, `order()`/`rerank()` return None and callers
fall back to the dense order — RAG keeps working, just without the rerank step.
"""
import json
import os
import urllib.request

RERANK_URL = os.environ.get("OCEANO_RERANK_URL", "http://127.0.0.1:8084")


def rerank(query, docs, url=None):
    """Relevance scores aligned with `docs` (higher = more relevant), or None if the server is
    unreachable. [] for empty input."""
    if not docs:
        return []
    url = (url or RERANK_URL).rstrip("/")
    body = json.dumps({"query": query, "documents": list(docs)}).encode()
    req = urllib.request.Request(url + "/rerank", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
    except Exception:
        return None                                  # server down / model absent → caller uses dense order
    scores = [float("-inf")] * len(docs)
    for item in data.get("results", []):
        i = item.get("index")
        if isinstance(i, int) and 0 <= i < len(docs):
            scores[i] = item.get("relevance_score", item.get("score", 0.0))
    return scores


def order(query, docs):
    """Indices of `docs` reordered most-relevant-first, or None if the reranker is unavailable."""
    scores = rerank(query, docs)
    if scores is None:
        return None
    return sorted(range(len(docs)), key=lambda i: scores[i], reverse=True)
