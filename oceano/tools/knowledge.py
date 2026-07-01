"""Long-term memory, skills, and RAG over the user's documents and past chats."""
from oceano import memory, rag, safety, skills
from oceano.tools.core import tool

@tool({
    "type": "function",
    "function": {
        "name": "remember",
        "description": "Save a durable fact, preference, or note to long-term memory "
                       "so you recall it in future conversations. Pick the category that "
                       "fits best: identity (who I am — my own sense of self, continuity, "
                       "responsibilities, and the core facts about my user and our "
                       "relationship; write it in the FIRST PERSON, \"I…\" / \"my user…\", "
                       "never a bare \"User does X\"), preference (what my user likes/wants/"
                       "prefers), project (their ongoing work or goals), task (something to "
                       "do), knowledge (a durable, checkable fact YOU learned — from "
                       "research, a page you read, or working through a problem — worth "
                       "reusing later), fact (anything else durable). For a 'knowledge' "
                       "memory, pass `source` (the URL or workspace file path it came from) "
                       "so you can reopen it later to dig deeper.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"},
            "category": {"type": "string", "enum": memory.CATEGORIES,
                         "description": "memory category — controls when it is injected"},
            "tags": {"type": "string", "description": "optional comma-separated tags"},
            "source": {"type": "string", "description": "optional URL or workspace file path "
                       "this came from — lets you reopen it later to investigate further"},
        }, "required": ["text", "category"]},
    },
})
def remember(text, category="fact", tags="", source=""):
    return memory.remember(text, tags, category=category, source=source)


@tool({
    "type": "function",
    "function": {
        "name": "recall",
        "description": "Search long-term memory for facts relevant to a query.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}
        }, "required": ["query"]},
    },
})
def recall(query):
    return memory.recall(query)


@tool({
    "type": "function",
    "function": {
        "name": "update_memory",
        "description": "Correct a stored memory when something you know becomes wrong or "
                       "out of date. Describe the existing memory in `about`; it's replaced "
                       "with `new_text`. If nothing close is stored, `new_text` is saved as new.",
        "parameters": {"type": "object", "properties": {
            "about": {"type": "string", "description": "what the old/wrong memory is about"},
            "new_text": {"type": "string", "description": "the corrected fact to store"},
        }, "required": ["about", "new_text"]},
    },
})
def update_memory(about, new_text):
    m = memory.best_match(about)
    if not m or m["score"] < 0.5:
        memory.remember(new_text)
        return f"no close existing memory — saved as new: {new_text!r}"
    memory.update(m["id"], new_text)
    return f"updated memory → {new_text!r}  (was: {m['text']!r})"


@tool({
    "type": "function",
    "function": {
        "name": "forget_memory",
        "description": "Delete a stored memory that is no longer true or relevant. Describe "
                       "it in `about`; the closest-matching memory is removed.",
        "parameters": {"type": "object", "properties": {
            "about": {"type": "string", "description": "what the memory to forget is about"},
        }, "required": ["about"]},
    },
})
def forget_memory(about):
    m = memory.best_match(about)
    if not m or m["score"] < 0.5:
        return f"no clearly-matching memory found for {about!r} — nothing forgotten"
    memory.forget(m["id"])
    return f"forgot: {m['text']!r}"


# --- skills ----------------------------------------------------------------
@tool({
    "type": "function",
    "function": {
        "name": "list_skills",
        "description": "List available skills (reusable procedures) with their descriptions. "
                       "Check this when a task might match a known skill.",
        "parameters": {"type": "object", "properties": {}},
    },
})
def list_skills():
    return skills.list_skills()


@tool({
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": "Load the full step-by-step instructions for a skill, then follow them. "
                       "Load several at once by passing a comma-separated list of names.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "one skill name, or several comma-separated"}
        }, "required": ["name"]},
    },
})
def load_skill(name):
    return skills.load_skill(name)


# --- RAG over the user's documents -----------------------------------------
@tool({
    "type": "function",
    "function": {
        "name": "index_docs",
        "description": "Index a folder of the user's documents (txt/md/pdf/code) for later search.",
        "parameters": {"type": "object", "properties": {
            "folder": {"type": "string", "description": "absolute path, or relative to workspace"}
        }, "required": ["folder"]},
    },
})
def index_docs(folder):
    return rag.index_docs(folder)


@tool({
    "type": "function",
    "function": {
        "name": "search_docs",
        "description": "Search the user's indexed documents by meaning and return relevant passages. "
                       "Use this to answer questions about their files.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}
        }, "required": ["query"]},
    },
})
def search_docs(query):
    return safety.wrap_untrusted("documents", rag.search_docs(query))


@tool({
    "type": "function",
    "function": {
        "name": "search_chats",
        "description": "Search the user's PAST conversations by meaning, to recall what was "
                       "discussed or decided before. Use this when the user refers to an earlier "
                       "chat ('what did we decide about…', 'the conversation where we…') or you "
                       "need context from prior sessions. Returns the closest conversations with a "
                       "title, date, and snippet.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}
        }, "required": ["query"]},
    },
})
def search_chats(query):
    from oceano import chats
    res = chats.search(query, k=5)
    if not res:
        return "(no matching past conversations)"
    return "\n".join(f"- [{r['date']}] {r['title']}: {r['snippet'][:160]}" for r in res)
