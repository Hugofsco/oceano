"""Conservative per-turn tool-schema routing.

All tools stay registered and execution-time allowlists remain authoritative. Routing only
reduces the schemas advertised to a model. It is feature-flagged off by default, supports
per-model patterns, falls back to the full allowed catalog when uncertain, and returns metadata
that the agent records without storing the user's prompt.
"""
from dataclasses import dataclass, replace
from fnmatch import fnmatch
import os
import re


FALSE_VALUES = {"", "0", "false", "off", "no"}
DEFAULT_LIMIT = 18
MIN_LIMIT = 8
# Compatibility constants; runtime decisions deliberately re-read the environment so an eval can
# compare modes without reloading the process.
ENABLED = os.environ.get("OCEANO_DYNAMIC_TOOLS", "0").strip().lower() not in FALSE_VALUES
try:
    MAX_TOOLS = max(MIN_LIMIT, int(os.environ.get("OCEANO_DYNAMIC_TOOL_LIMIT", str(DEFAULT_LIMIT))))
except ValueError:
    MAX_TOOLS = DEFAULT_LIMIT

_STOP = {
    "a", "an", "and", "are", "as", "at", "be", "can", "do", "for", "from", "i", "in",
    "is", "it", "me", "my", "of", "on", "or", "please", "that", "the", "this", "to",
    "use", "want", "with", "you",
}
_BASELINE = {
    "delegate", "list_skills", "load_skill", "list_files", "read_file", "code_search",
}
_DOMAINS = {
    "code": {
        "words": {"file", "folder", "code", "script", "project", "repository", "repo", "test",
                  "debug", "implement", "build", "fix", "lint"},
        "patterns": ("read_file", "write_file", "edit_file", "list_files", "make_folder",
                     "run_shell", "python_exec", "code_search", "run_tests", "git", "delegate"),
        "core": ("read_file", "code_search", "write_file", "run_tests"),
    },
    "web": {
        "words": {"web", "search", "online", "internet", "website", "url", "source", "research",
                  "latest", "browse"},
        "patterns": ("web_search", "fetch_url", "browser_", "http_request", "rss"),
        "core": ("web_search", "fetch_url"),
    },
    "calendar": {
        "words": {"calendar", "meeting", "schedule", "availability", "appointment", "event", "tomorrow"},
        "patterns": ("calendar_", "add_calendar_", "update_calendar_", "delete_calendar_",
                     "find_free_slots", "manage_calendar"),
        "core": ("calendar_events", "find_free_slots"),
    },
    "mail": {
        "words": {"email", "mail", "inbox", "message", "reply", "sender"},
        "patterns": ("mail_",),
        "core": ("mail_list", "mail_read", "mail_reply"),
    },
    "memory": {
        "words": {"remember", "memory", "recall", "forget"},
        "patterns": ("remember", "recall", "update_memory", "forget_memory", "search_chats"),
        "core": ("recall", "remember"),
    },
    "documents": {
        "words": {"document", "docs", "knowledge", "pdf"},
        "patterns": ("index_docs", "search_docs", "read_file"),
        "core": ("search_docs", "read_file"),
    },
    "workflow": {
        "words": {"workflow", "automation", "automate"},
        "patterns": ("run_workflow", "list_workflows", "schedule_task", "list_tasks"),
        "core": ("list_workflows", "run_workflow"),
    },
    "notes": {
        "words": {"note", "notebook", "kanban", "board"},
        "patterns": ("note", "notebook", "kanban"),
        "core": ("search_notebook", "get_note", "kanban_board"),
    },
    "hosts": {
        "words": {"host", "server", "ssh", "sftp", "remote"},
        "patterns": ("list_hosts", "ssh_run", "sftp"),
        "core": ("list_hosts", "ssh_run"),
    },
}
_INABILITY = re.compile(
    r"\b(?:i (?:cannot|can't|am unable to)|no (?:tool|access)|tool (?:is )?unavailable|"
    r"not available in this conversation|unable to (?:access|inspect|run|read|write|search))\b", re.I)


@dataclass(frozen=True)
class Route:
    schemas: list
    enabled: bool
    routed: bool
    reason: str
    total: int
    selected: int
    model: str = ""
    domains: tuple = ()
    fallback: bool = False

    @property
    def names(self):
        return tuple(s["function"]["name"] for s in self.schemas)


def _bool_env(name, default="0"):
    return os.environ.get(name, default).strip().lower() not in FALSE_VALUES


def _limit():
    try:
        return max(MIN_LIMIT, int(os.environ.get("OCEANO_DYNAMIC_TOOL_LIMIT", str(DEFAULT_LIMIT))))
    except ValueError:
        return DEFAULT_LIMIT


def _patterns(name):
    return tuple(p.strip().lower() for p in os.environ.get(name, "").split(",") if p.strip())


def enabled_for(model="", force=None):
    """Whether routing applies to this model. ``force`` is reserved for controlled evals/tests."""
    if force is not None:
        return bool(force)
    if not _bool_env("OCEANO_DYNAMIC_TOOLS", "0"):
        return False
    model_l = (model or "").lower()
    allow = _patterns("OCEANO_DYNAMIC_TOOL_MODELS")
    deny = _patterns("OCEANO_DYNAMIC_TOOL_EXCLUDE_MODELS")
    if allow and not any(fnmatch(model_l, p) for p in allow):
        return False
    if deny and any(fnmatch(model_l, p) for p in deny):
        return False
    return True


def _tokens(text):
    return {t[:-1] if len(t) > 4 and t.endswith("s") else t
            for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in _STOP}


def _matches(name, patterns):
    return any(name == p or name.startswith(p) or p in name for p in patterns)


def route(schemas, query, model="", limit=None, force=None):
    """Return a routed catalog plus decision metadata; never expands the supplied catalog."""
    schemas = list(schemas)
    cap = max(MIN_LIMIT, int(limit or _limit()))
    if not enabled_for(model, force=force):
        return Route(schemas, False, False, "disabled", len(schemas), len(schemas), model)
    if len(schemas) <= cap:
        return Route(schemas, True, False, "catalog-within-limit", len(schemas), len(schemas), model)
    q = _tokens(query)
    if not q:
        return Route(schemas, True, False, "ambiguous", len(schemas), len(schemas), model)

    available = {s["function"]["name"] for s in schemas}
    matched = [(name, spec) for name, spec in _DOMAINS.items() if q & spec["words"]]
    hinted = {s["function"]["name"] for _, spec in matched for s in schemas
              if _matches(s["function"]["name"], spec["patterns"])}
    scored = []
    for pos, schema in enumerate(schemas):
        fn = schema["function"]
        name = fn["name"]
        score = 5 * len(q & _tokens(name.replace("_", " "))) + len(q & _tokens(fn.get("description", "")))
        if name in hinted:
            score += 8
        if name in _BASELINE:
            score += 3
        if score:
            scored.append((score, -pos, name))
    # Strong-looking lexical overlap without a recognized domain is not enough evidence to hide
    # capabilities. This is deliberately biased toward recall rather than schema reduction.
    if not matched or not scored or max(s[0] for s in scored) < 3:
        return Route(schemas, True, False, "ambiguous", len(schemas), len(schemas), model)

    chosen = {n for n in _BASELINE if n in available}
    # Guarantee each recognized domain at least its core tools before filling by score. This keeps
    # multi-domain prompts (e.g. "email tomorrow's calendar") from being crowded into one family.
    for _, spec in matched:
        for name in spec["core"]:
            if name in available and len(chosen) < cap:
                chosen.add(name)
    for _, _, name in sorted(scored, reverse=True):
        if len(chosen) >= cap:
            break
        chosen.add(name)
    selected = [s for s in schemas if s["function"]["name"] in chosen]
    return Route(selected, True, True, "routed", len(schemas), len(selected), model,
                 tuple(name for name, _ in matched))


def select(schemas, query, limit=None, model="", force=None):
    """Compatibility wrapper returning only schemas."""
    return route(schemas, query, model=model, limit=limit, force=force).schemas


def should_expand(route_info, content="", issues=None, tool_events=None):
    """One-shot recovery signal when a reduced catalog appears to have blocked completion."""
    if not route_info or not route_info.routed or route_info.fallback:
        return False
    if _INABILITY.search(content or ""):
        return True
    if any("no action tool was used" in issue for issue in (issues or ())):
        return True
    return any("not available in this conversation" in (result or "").lower()
               for _, result in (tool_events or ()))


def expanded(route_info, schemas):
    return replace(route_info, schemas=list(schemas), routed=False, reason="full-catalog-retry",
                   selected=len(schemas), fallback=True)


def telemetry(route_info, event="selected", used_tools=(), errors=0):
    """Record routing metrics without prompts, arguments, results, or other user content."""
    if not _bool_env("OCEANO_DYNAMIC_TOOL_TELEMETRY", "1"):
        return None
    if not route_info.enabled and not route_info.fallback:
        return None
    from oceano import traces
    return traces.record_global(
        "tool_routing", phase=event, model=route_info.model, enabled=route_info.enabled,
        routed=route_info.routed, reason=route_info.reason, catalog_tools=route_info.total,
        advertised_tools=route_info.selected, selected_tools=list(route_info.names),
        domains=list(route_info.domains), fallback=route_info.fallback,
        used_tools=sorted(set(used_tools)), tool_errors=int(errors or 0),
    )
