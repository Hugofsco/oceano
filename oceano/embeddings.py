"""Shared embedding client — talks to the dedicated llama.cpp embed server (:8082).
Used by BOTH long-term memory and document RAG, so they stay consistent."""
import json
import math
import os

from openai import OpenAI

EMBED_URL   = os.environ.get("OCEANO_EMBED_URL", "http://127.0.0.1:8082/v1")
EMBED_MODEL = os.environ.get("OCEANO_EMBED_MODEL", "nomic-embed-text")

_client = OpenAI(base_url=EMBED_URL, api_key="sk-no-key-needed")


# nomic-embed-text-v1.5 is trained with task-instruction prefixes; matching them on the two sides is
# what makes query↔document retrieval work (measured in oceano/reval.py). 'document' for anything we
# STORE/index, 'query' for a SEARCH query. Both stores and queries MUST use the same convention, so a
# convention change means re-embedding the stores — see reindex.rebuild_embeddings().
_PREFIX = {"query": "search_query: ", "document": "search_document: "}


def embed(text, kind="document"):
    """text -> embedding vector, or None if the embed server is down. `kind` is 'document' (stored/
    indexed content, the default) or 'query' (a search query) — it selects the nomic prefix above."""
    try:
        r = _client.embeddings.create(model=EMBED_MODEL, input=_PREFIX.get(kind, _PREFIX["document"]) + text)
        return r.data[0].embedding
    except Exception:
        return None


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def loads_vec(blob):
    """Decode a stored embedding (JSON text) back into a vector, or None if it's missing
    or corrupt. Used on the hot read paths (memory + RAG search) so ONE bad/truncated row
    can't take down the whole query with a JSONDecodeError — the row is simply skipped."""
    if not blob:
        return None
    try:
        v = json.loads(blob)
    except (ValueError, TypeError):
        return None
    return v if isinstance(v, list) else None
