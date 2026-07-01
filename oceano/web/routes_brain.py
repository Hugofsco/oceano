"""Brain routes: embedding-engine stats + semantic search, Rivers (HF model
catalog → hwfit → download → serve), memories, the memory graph, and the
memory-injection policy."""
import asyncio

from fastapi import APIRouter, Request

from oceano import chats, embeddings, memory, rag, rivers
from oceano.web.state import _embed_reachable

router = APIRouter()


# ---------------- brain: embedding-engine stats + semantic search ------------
@router.get("/api/brain/stats")
def brain_stats():
    docs = rag.stats()
    return {"memories": memory.count(),
            "docs": docs,
            "embed": {"ok": _embed_reachable(), "model": embeddings.EMBED_MODEL,
                      "url": embeddings.EMBED_URL, "dims": docs.get("dims")}}


@router.post("/api/brain/search")
async def brain_search(request: Request):
    """Semantic search over memories, indexed docs, or past conversations."""
    b = await request.json()
    query = (b.get("query") or "").strip()
    scope = b.get("scope", "memory")
    if not query:
        return {"results": []}
    fn = {"memory": memory.search, "docs": rag.search, "chats": chats.search}.get(scope, memory.search)
    return {"results": await asyncio.to_thread(fn, query)}   # cosine scan off the event loop


@router.post("/api/brain/index")
async def brain_index(request: Request):
    """Index a folder of documents into the RAG store (embeds each chunk)."""
    folder = ((await request.json()).get("folder") or "").strip()
    if not folder:
        return {"ok": False, "result": "no folder given"}
    result = await asyncio.to_thread(rag.index_docs, folder)
    return {"ok": not result.startswith(("ERROR", "(no such")), "result": result}


# ---------------- rivers: HF model catalog → hwfit → download → serve -------
@router.get("/api/rivers/hw")
def rivers_hw():
    return rivers.hw()


@router.get("/api/rivers/recommended")
def rivers_recommended():
    return rivers.recommended()


@router.get("/api/rivers/search")
async def rivers_search(q: str = ""):
    try:
        return {"results": await asyncio.to_thread(rivers.search, q)}
    except Exception as e:
        return {"results": [], "error": f"{type(e).__name__}: {e}"}


@router.get("/api/rivers/files")
async def rivers_files(repo: str):
    try:
        return await asyncio.to_thread(rivers.files, repo)
    except Exception as e:
        return {"files": [], "error": f"{type(e).__name__}: {e}"}


@router.get("/api/rivers/installed")
def rivers_installed():
    return {"models": rivers.installed()}


@router.post("/api/rivers/download")
async def rivers_download(request: Request):
    b = await request.json()
    try:
        return rivers.start_download(b.get("repo", ""), b.get("filename", ""))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@router.get("/api/rivers/jobs")
def rivers_jobs():
    return {"jobs": rivers.jobs()}


def _serve_params(b):
    """Pull the serving params out of a request body (shared by serve + update)."""
    return dict(ngl=b.get("ngl", 99), ctx=b.get("ctx", 8192), fa=b.get("fa", True),
                kv=b.get("kv", "f16"), kv_v=b.get("kv_v"), ttl=b.get("ttl", 600),
                threads=b.get("threads"), batch=b.get("batch"), ubatch=b.get("ubatch"),
                n_cpu_moe=b.get("n_cpu_moe"), parallel=b.get("parallel", 1), extra=b.get("extra", ""))


@router.post("/api/rivers/serve")
async def rivers_serve(request: Request):
    b = await request.json()
    return rivers.serve(b.get("filename", ""), b.get("name"), **_serve_params(b))


@router.get("/api/rivers/served")
def rivers_served():
    """Models currently wired into llama-swap, as structured params (for edit/unserve)."""
    return rivers.served()


@router.get("/api/rivers/estimate")
async def rivers_estimate(filename: str, ctx: int = 8192, kv: str = "f16",
                          kv_v: str = "", ngl: int = 99):
    """VRAM estimate for a serve config (reads the GGUF header off the event loop)."""
    return await asyncio.to_thread(rivers.estimate, filename, ctx, kv, ngl, kv_v or None)


@router.get("/api/rivers/recommend")
async def rivers_recommend(filename: str):
    """Suggested serving config for this model on this box (ngl/ctx/KV/threads/MoE), with reasons."""
    return await asyncio.to_thread(rivers.recommend, filename)


@router.post("/api/rivers/update")
async def rivers_update(request: Request):
    """Re-tune an already-served model in place (preserves comments + unknown flags)."""
    b = await request.json()
    return rivers.update_served(b.get("name", ""), **_serve_params(b))


@router.post("/api/rivers/unserve")
async def rivers_unserve(request: Request):
    """Remove a model from llama-swap.yaml."""
    return rivers.unserve((await request.json()).get("name", ""))


@router.post("/api/rivers/delete")
async def rivers_delete(request: Request):
    """Delete a model's .gguf from disk (refuses if served, or if it's the embedding model)."""
    return rivers.delete_model((await request.json()).get("filename", ""))


# ---------------- memories ----------------
@router.get("/api/memories")
def get_memories():
    return memory.list_all()


@router.post("/api/memories")
async def post_memory(req: Request):
    b = await req.json()
    memory.remember(b["text"], b.get("tags", ""), b.get("category", "fact"),
                    bool(b.get("pinned")), b.get("source", ""))
    return {"ok": True}


@router.patch("/api/memories/{mid}")
async def patch_memory(mid: int, req: Request):
    b = await req.json()
    if "pinned" in b:
        memory.set_pinned(mid, bool(b["pinned"]))
    if "category" in b:
        memory.set_category(mid, b["category"])
    return {"ok": True}


@router.delete("/api/memories/{mid}")
def remove_memory(mid: int):
    return {"ok": memory.forget(mid)}


# ---------------- memory graph (Memory Graph window) ----------------
@router.get("/api/memory/graph")
async def memory_graph(threshold: float = 0.62):
    """Memories as a similarity graph for the Memory Graph window. The cosine scan runs
    off the event loop so a large store can't stall the request."""
    th = min(max(threshold, 0.3), 0.95)
    return await asyncio.to_thread(memory.graph, th)


# ---------------- memory injection policy (Settings → Memory) ----------------
@router.get("/api/memory/policy")
def get_memory_policy():
    return {"policy": memory.get_policy(), "categories": memory.CATEGORIES}


@router.post("/api/memory/policy")
async def set_memory_policy(req: Request):
    return {"ok": True, "policy": memory.set_policy(await req.json())}
