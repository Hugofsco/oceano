"""Locked maintenance job: re-sync every embedding index to what's actually present.

Documents (RAG), memories, skills, and chats each keep their own embedding store. Over
time files get deleted, memories forgotten, skills removed, chats trashed — and stale
vectors linger. This job re-syncs all four to disk reality: vanished files / deleted
items are PRUNED, changed items are re-embedded, and anything missing an embedding is
backfilled. Only what's present is kept.

Runs as a locked scheduler entry (source `reindex:all`) — schedulable + toggleable in the
Scheduler, but not deletable — mirroring the other maintenance jobs.
"""
SOURCE = "reindex:all"
PREFIX = "[ INDEX ] "
CRON = "0 4 * * *"          # nightly at 04:00


def reindex_all():
    """Reindex documents · memories · skills · chats. Returns a one-line summary. Each piece
    degrades independently; uses the embedding server (not the chat model)."""
    from oceano import jobs
    parts = []
    with jobs.job("reindex", "reindex docs · memories · skills · chats", ref=SOURCE, gate=False):
        try:
            from oceano import rag
            parts.append("docs: " + rag.reindex())
        except Exception as e:
            parts.append(f"docs: error ({type(e).__name__}: {e})")
        try:
            from oceano import memory
            parts.append("memories: " + memory.reindex())
        except Exception as e:
            parts.append(f"memories: error ({type(e).__name__}: {e})")
        try:
            from oceano import skills
            parts.append("skills: " + skills.reindex())
        except Exception as e:
            parts.append(f"skills: error ({type(e).__name__}: {e})")
        try:
            from oceano import chats
            parts.append(f"chats: {chats.reindex()} (re)embedded")
        except Exception as e:
            parts.append(f"chats: error ({type(e).__name__}: {e})")
    return " · ".join(parts)


def rebuild_embeddings():
    """Re-embed every stored vector (memories · docs · chats) IN PLACE — for after an embedding
    MODEL or CONVENTION change (e.g. adding nomic's search_query/search_document prefixes), where
    the source text is unchanged but the vectors must be recomputed so queries and documents share
    one space again. Skills embed lazily into an in-memory cache, so they refresh on the next
    restart / use — nothing to rebuild there. Returns a one-line summary."""
    from oceano import memory, rag, chats
    parts = []
    try:
        parts.append("memories: " + memory.reindex(force=True))
    except Exception as e:
        parts.append(f"memories: error ({type(e).__name__}: {e})")
    try:
        parts.append("docs: " + rag.reembed_all())
    except Exception as e:
        parts.append(f"docs: error ({type(e).__name__}: {e})")
    try:
        parts.append(f"chats: {chats.reindex(force=True)} re-embedded")
    except Exception as e:
        parts.append(f"chats: error ({type(e).__name__}: {e})")
    return " · ".join(parts)


def ensure_task():
    """Make sure the locked '[ INDEX ]' reindex schedule exists (visible + retimable +
    toggleable in the Scheduler, but not deletable)."""
    from oceano import scheduler
    if any(t.get("source") == SOURCE for t in scheduler.all_tasks()):
        return
    scheduler.add_task(CRON, PREFIX + "Reindex documents · memories · skills · chats (prune absent, refresh changed)",
                       source=SOURCE)
