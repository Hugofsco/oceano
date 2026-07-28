"""Configurable tool-schema advertisement for Oceano agents.

Tools remain registered for the process lifetime. This module controls only which JSON schemas a
model sees on each call; Agent's execution allowlist remains the independent security boundary.
Hybrid mode bootstraps a budgeted set of capability bundles and exposes ``discover_tools`` so the
model can cumulatively load more allowed schemas during the turn.
"""
from dataclasses import dataclass, field, replace
from fnmatch import fnmatch
import json
import os
from pathlib import Path
import re
import time

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.11+ in supported installs
    tomllib = None


FALSE_VALUES = {"", "0", "false", "off", "no"}
VALID_MODES = {"full", "hybrid"}
VALID_FALLBACKS = {"none", "discover-once", "full-once"}
DEFAULT_SCHEMA_BUDGET = 5000
DEFAULT_MAX_SCHEMA_BUDGET = 9000
DEFAULT_LIMIT = 18                    # legacy count cap, only when explicitly configured
MIN_LIMIT = 8
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "tool-loading.toml"

# Compatibility constants retained for callers/tests. Runtime policy re-reads env/file settings.
ENABLED = os.environ.get("OCEANO_DYNAMIC_TOOLS", "0").strip().lower() not in FALSE_VALUES
try:
    MAX_TOOLS = max(MIN_LIMIT, int(os.environ.get("OCEANO_DYNAMIC_TOOL_LIMIT", str(DEFAULT_LIMIT))))
except ValueError:
    MAX_TOOLS = DEFAULT_LIMIT

_STOP = {
    "a", "an", "and", "are", "as", "at", "be", "can", "do", "for", "from", "i", "in",
    "is", "it", "me", "my", "of", "on", "or", "please", "that", "the", "this", "to",
    "use", "want", "with", "you", "available", "need", "help",
}


@dataclass(frozen=True)
class Bundle:
    name: str
    description: str
    tools: tuple
    aliases: tuple = ()
    core: tuple = ()


@dataclass(frozen=True)
class Policy:
    mode: str = "full"
    schema_budget: int = DEFAULT_SCHEMA_BUDGET
    max_schema_budget: int = DEFAULT_MAX_SCHEMA_BUDGET
    discovery: bool = True
    fallback: str = "full-once"
    telemetry: bool = True
    ambiguous: str = "discover"          # discover = core+meta-tool; full = complete catalog
    core_bundles: tuple = ("core",)
    count_limit: int | None = None         # legacy OCEANO_DYNAMIC_TOOL_LIMIT compatibility
    source: str = "defaults"


@dataclass(frozen=True)
class Route:
    schemas: list
    enabled: bool
    routed: bool
    reason: str
    total: int
    selected: int
    model: str = ""
    surface: str = "chat"
    domains: tuple = ()
    fallback: bool = False
    recovery_level: int = 0
    loaded_bundles: tuple = ()
    policy: Policy = field(default_factory=Policy)
    catalog_schema_tokens: int = 0

    @property
    def names(self):
        return tuple(s["function"]["name"] for s in self.schemas)

    @property
    def schema_tokens(self):
        return sum(schema_cost(s) for s in self.schemas)


DISCOVER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "discover_tools",
        "description": (
            "Search or load additional tools allowed in this conversation. Use when the current "
            "tools do not cover part of the task. Loaded tools become available on the next step."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Capability needed, in plain language"},
                "operation": {"type": "string", "enum": ["search", "load"], "default": "load"},
                "bundles": {"type": "array", "items": {"type": "string"},
                            "description": "Optional exact bundle names returned by a prior search"},
            },
            "required": ["query"],
        },
    },
}


_BUILTIN_BUNDLES = {
    "core": Bundle("core", "Basic inspection, delegation, and reusable procedures",
                   ("list_files", "read_file", "code_search", "delegate", "list_skills", "load_skill"),
                   ("inspect", "read", "delegate", "skill")),
    "files-read": Bundle("files-read", "Read, list, and search files or source code",
                         ("list_files", "read_file", "code_search"),
                         ("file", "folder", "source", "repository", "repo", "inspect")),
    "files-write": Bundle("files-write", "Create and edit files and folders",
                          ("write_file", "edit_file", "make_folder"),
                          ("write", "edit", "create", "change", "modify", "implement", "build")),
    "code-execution": Bundle("code-execution", "Run commands, tests, Python, and Git",
                             ("run_shell", "python_exec", "run_tests", "git"),
                             ("code", "implement", "build", "debug", "test", "lint", "git", "fix"),
                             ("run_tests", "git")),
    "web-research": Bundle("web-research", "Search and retrieve public internet sources",
                           ("web_search", "fetch_url", "http_request", "rss"),
                           ("web", "online", "internet", "research", "latest", "source", "url"),
                           ("web_search", "fetch_url")),
    "browser": Bundle("browser", "Interact with a live website in a browser",
                      ("browser_open", "browser_snapshot", "browser_read", "browser_click",
                       "browser_fill", "browser_select", "browser_press", "browser_wait",
                       "browser_extract", "browser_screenshot", "browser_scroll", "browser_hover",
                       "browser_upload", "browser_dialog", "browser_tab", "browser_eval"),
                      ("browser", "website", "page", "click", "form", "screenshot"),
                      ("browser_open", "browser_snapshot", "browser_read")),
    "email-read": Bundle("email-read", "Inspect email accounts, folders, and messages",
                         ("mail_accounts", "mail_folders", "mail_list", "mail_read", "mail_save_attachment"),
                         ("email", "mail", "inbox", "sender", "message", "attachment"),
                         ("mail_list", "mail_read")),
    "email-write": Bundle("email-write", "Reply, send, move, flag, or delete email",
                          ("mail_send", "mail_reply", "mail_move", "mail_delete", "mail_flag", "mail_folder"),
                          ("email", "mail", "reply", "send", "move", "delete", "flag"),
                          ("mail_reply", "mail_send")),
    "calendar-read": Bundle("calendar-read", "Inspect calendar events and availability",
                            ("calendar_events", "find_free_slots"),
                            ("calendar", "meeting", "schedule", "availability", "appointment", "event", "tomorrow")),
    "calendar-write": Bundle("calendar-write", "Create, update, delete, or manage calendar events",
                             ("add_calendar_event", "add_calendar_events", "update_calendar_event",
                              "delete_calendar_event", "manage_calendar"),
                             ("calendar", "meeting", "schedule", "book", "create", "reschedule", "cancel")),
    "memory": Bundle("memory", "Recall and update long-term memory or chat history",
                     ("remember", "recall", "update_memory", "forget_memory", "search_chats"),
                     ("remember", "memory", "recall", "forget", "earlier", "conversation"),
                     ("recall", "remember")),
    "knowledge": Bundle("knowledge", "Index and search local documents and knowledge",
                        ("index_docs", "search_docs"), ("document", "docs", "knowledge", "pdf")),
    "workflows": Bundle("workflows", "List and run saved workflows",
                        ("run_workflow", "list_workflows"), ("workflow", "automation", "automate")),
    "scheduling": Bundle("scheduling", "Create and manage scheduled tasks and notifications",
                         ("schedule_task", "list_tasks", "update_task", "cancel_task", "notify"),
                         ("task", "schedule", "cron", "reminder", "notify")),
    "hosts-read": Bundle("hosts-read", "Inspect configured remote hosts",
                         ("list_hosts",), ("host", "server", "ssh", "remote")),
    "hosts-execute": Bundle("hosts-execute", "Run SSH or SFTP operations on configured hosts",
                            ("ssh_run", "sftp"), ("host", "server", "ssh", "sftp", "remote", "deploy")),
    "notes": Bundle("notes", "Search, read, create, and update notes or notebooks",
                    ("search_notebook", "get_note", "add_note", "update_note", "delete_note"),
                    ("note", "notebook", "journal"), ("search_notebook", "get_note")),
    "kanban": Bundle("kanban", "Read and update Kanban boards and cards",
                     ("kanban_board", "add_kanban_card", "update_kanban_card", "delete_kanban_card"),
                     ("kanban", "board", "card")),
    "desktop": Bundle("desktop", "Interact with the desktop, clipboard, files, and windows",
                      ("ui_open", "ui_close", "ui_arrange", "desktop_notify", "desktop_pick_file",
                       "desktop_save_file", "desktop_reveal_path", "desktop_open_path",
                       "desktop_clipboard_read", "desktop_clipboard_write", "desktop_screenshot"),
                      ("desktop", "window", "clipboard", "open", "save", "screenshot")),
    "media": Bundle("media", "Transcribe, speak, fetch, or convert media",
                    ("transcribe_media", "speak_to_file", "fetch_media", "convert"),
                    ("audio", "video", "media", "transcribe", "speech", "voice", "convert")),
    "data": Bundle("data", "Analyze structured data with Python or SQL",
                   ("sql_query", "python_exec", "read_file"),
                   ("data", "csv", "json", "sql", "analyze", "table", "spreadsheet"),
                   ("sql_query", "python_exec")),
    "agents": Bundle("agents", "Delegate work and manage spawned agents or jobs",
                     ("delegate", "spawn_agent", "agent_status", "spawn_job", "job_status"),
                     ("delegate", "agent", "parallel", "background", "job"), ("delegate",)),
}

_INABILITY = re.compile(
    r"\b(?:i (?:cannot|can't|am unable to)|no (?:tool|access)|tool (?:is )?unavailable|"
    r"not available in this conversation|unable to (?:access|inspect|run|read|write|search))\b", re.I)
_CACHE = {"path": None, "mtime": None, "data": {}}


def _bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in FALSE_VALUES


def _int(value, default, minimum=0):
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _tokens(text):
    return {t[:-1] if len(t) > 4 and t.endswith("s") else t
            for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in _STOP}


def schema_cost(schema):
    """Stable approximation of prompt tokens consumed by one serialized tool schema."""
    return max(1, (len(json.dumps(schema, ensure_ascii=True, separators=(",", ":"))) + 3) // 4)


def _config_path():
    raw = os.environ.get("OCEANO_TOOL_CONFIG", "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_CONFIG_PATH


def _load_config():
    path = _config_path()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {}
    if _CACHE["path"] == path and _CACHE["mtime"] == mtime:
        return _CACHE["data"]
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8")) if tomllib else {}
    except (OSError, ValueError):
        data = {}
    _CACHE.update({"path": path, "mtime": mtime, "data": data if isinstance(data, dict) else {}})
    return _CACHE["data"]


def _apply_policy(base, values, source):
    if not isinstance(values, dict):
        return base
    mode = values.get("mode", base.mode)
    fallback = values.get("fallback", base.fallback)
    ambiguous = values.get("ambiguous", base.ambiguous)
    return replace(
        base,
        mode=mode if mode in VALID_MODES else base.mode,
        schema_budget=_int(values.get("schema_budget"), base.schema_budget, 500),
        max_schema_budget=_int(values.get("max_schema_budget"), base.max_schema_budget, 500),
        discovery=_bool(values.get("discovery"), base.discovery),
        fallback=fallback if fallback in VALID_FALLBACKS else base.fallback,
        telemetry=_bool(values.get("telemetry"), base.telemetry),
        ambiguous=ambiguous if ambiguous in ("discover", "full") else base.ambiguous,
        core_bundles=tuple(values.get("core_bundles") or base.core_bundles),
        source=source,
    )


def resolve_policy(model="", surface="chat", force=None):
    """Resolve global → model → surface policy. Explicit scopes are handled by Agent first."""
    legacy_enabled = _bool(os.environ.get("OCEANO_DYNAMIC_TOOLS"), False)
    mode = "hybrid" if legacy_enabled else "full"
    base = Policy(mode=mode, source="legacy-env" if legacy_enabled else "defaults")
    cfg = _load_config()
    base = _apply_policy(base, cfg.get("default"), "config:default")

    # Environment variables are global scalar defaults. More specific model and surface rules
    # below intentionally win, matching explicit scope → surface → model → global.
    env_mode = os.environ.get("OCEANO_TOOL_LOADING_MODE", "").strip().lower()
    if env_mode in VALID_MODES:
        base = replace(base, mode=env_mode, source="environment")
    if os.environ.get("OCEANO_TOOL_SCHEMA_BUDGET"):
        base = replace(base, schema_budget=_int(os.environ.get("OCEANO_TOOL_SCHEMA_BUDGET"),
                                                base.schema_budget, 500), source="environment")
    if os.environ.get("OCEANO_TOOL_MAX_SCHEMA_BUDGET"):
        base = replace(base, max_schema_budget=_int(os.environ.get("OCEANO_TOOL_MAX_SCHEMA_BUDGET"),
                                                    base.max_schema_budget, 500), source="environment")
    if os.environ.get("OCEANO_TOOL_DISCOVERY") is not None:
        base = replace(base, discovery=_bool(os.environ.get("OCEANO_TOOL_DISCOVERY")), source="environment")
    if os.environ.get("OCEANO_TOOL_FALLBACK") in VALID_FALLBACKS:
        base = replace(base, fallback=os.environ["OCEANO_TOOL_FALLBACK"], source="environment")
    if os.environ.get("OCEANO_DYNAMIC_TOOL_TELEMETRY") is not None:
        base = replace(base, telemetry=_bool(os.environ.get("OCEANO_DYNAMIC_TOOL_TELEMETRY")),
                       source="environment")
    if os.environ.get("OCEANO_DYNAMIC_TOOL_LIMIT"):
        base = replace(base, count_limit=_int(os.environ.get("OCEANO_DYNAMIC_TOOL_LIMIT"),
                                              DEFAULT_LIMIT, MIN_LIMIT))

    model_l = (model or "").lower()
    # Legacy model filters are global eligibility defaults. Structured model/surface rules may
    # deliberately override them, preserving the documented specificity order.
    if legacy_enabled and not env_mode:
        allow = tuple(p.strip().lower()
                      for p in os.environ.get("OCEANO_DYNAMIC_TOOL_MODELS", "").split(",")
                      if p.strip())
        deny = tuple(p.strip().lower()
                     for p in os.environ.get("OCEANO_DYNAMIC_TOOL_EXCLUDE_MODELS", "").split(",")
                     if p.strip())
        if ((allow and not any(fnmatch(model_l, p) for p in allow))
                or any(fnmatch(model_l, p) for p in deny)):
            base = replace(base, mode="full", source="legacy-model-filter")

    for item in cfg.get("models", []) if isinstance(cfg.get("models"), list) else []:
        if isinstance(item, dict) and fnmatch(model_l, str(item.get("pattern", "")).lower()):
            base = _apply_policy(base, item, f"config:model:{item.get('pattern')}")
            break
    surfaces = cfg.get("surfaces") if isinstance(cfg.get("surfaces"), dict) else {}
    base = _apply_policy(base, surfaces.get(surface), f"config:surface:{surface}")

    if force is not None:
        base = replace(base, mode="hybrid" if force else "full", source="forced-eval")
    if base.max_schema_budget < base.schema_budget:
        base = replace(base, max_schema_budget=base.schema_budget)
    return base


def enabled_for(model="", force=None, surface="chat"):
    return resolve_policy(model, surface, force).mode == "hybrid"


def bundles():
    """Built-in bundles plus optional TOML overrides/additions."""
    out = dict(_BUILTIN_BUNDLES)
    raw = _load_config().get("bundles")
    if not isinstance(raw, dict):
        return out
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        prior = out.get(name, Bundle(name, name.replace("-", " "), ()))
        out[name] = Bundle(
            name=name,
            description=str(spec.get("description") or prior.description),
            tools=tuple(spec.get("tools") or prior.tools),
            aliases=tuple(spec.get("aliases") or prior.aliases),
            core=tuple(spec.get("core") or prior.core),
        )
    return out


_GENERIC_ACTION_TERMS = {
    "create", "delete", "inspect", "manage", "move", "open", "read", "run", "save",
    "search", "send", "update", "use", "write",
}


def _bundle_score(bundle, query_tokens):
    # Generic verbs occur in nearly every domain ("create" previously loaded calendar,
    # notes, mail, desktop, and files together). Require at least one domain/distinctive
    # term, then use all overlap only to rank bundles within that domain.
    terms = _tokens(" ".join(bundle.aliases) + " " + bundle.name)
    overlap = query_tokens & terms
    specific = overlap - _GENERIC_ACTION_TERMS
    return 3 * len(specific) + len(overlap) if specific else 0


def _matched_bundles(query, all_bundles):
    q = _tokens(query)
    ranked = [(score, name) for name, bundle in all_bundles.items()
              if name != "core" and (score := _bundle_score(bundle, q)) > 0]
    return [name for _, name in sorted(ranked, key=lambda x: (-x[0], x[1]))]


def _add(chosen, schema_by_name, name, budget, count_limit=None):
    if name in chosen or name not in schema_by_name:
        return
    if count_limit is not None and len(chosen) >= count_limit:
        return
    candidate = schema_by_name[name]
    if sum(schema_cost(s) for s in chosen.values()) + schema_cost(candidate) <= budget:
        chosen[name] = candidate


def route(schemas, query, model="", limit=None, force=None, surface="chat"):
    """Return a budgeted bootstrap catalog plus routing metadata."""
    schemas = list(schemas)
    policy = resolve_policy(model, surface, force)
    total_cost = sum(schema_cost(s) for s in schemas)
    if limit is not None:
        policy = replace(policy, count_limit=max(MIN_LIMIT, int(limit)))
    if policy.mode != "hybrid":
        return Route(schemas, False, False, "full-policy", len(schemas), len(schemas), model,
                     surface=surface, policy=policy, catalog_schema_tokens=total_cost)
    if total_cost <= policy.schema_budget:
        return Route(schemas, True, False, "catalog-within-budget", len(schemas), len(schemas), model,
                     surface=surface, policy=policy, catalog_schema_tokens=total_cost)

    all_bundles = bundles()
    matched = _matched_bundles(query, all_bundles)
    if not matched and policy.ambiguous == "full":
        return Route(schemas, True, False, "ambiguous-full", len(schemas), len(schemas), model,
                     surface=surface, policy=policy, catalog_schema_tokens=total_cost)
    by_name = {s["function"]["name"]: s for s in schemas}
    chosen = {}
    cap = policy.count_limit
    for bundle_name in policy.core_bundles:
        for name in all_bundles.get(bundle_name, Bundle(bundle_name, "", ())).tools:
            _add(chosen, by_name, name, policy.schema_budget, cap)
    if policy.discovery:
        by_name["discover_tools"] = DISCOVER_SCHEMA
        _add(chosen, by_name, "discover_tools", policy.schema_budget, cap)
    for bundle_name in matched:
        bundle = all_bundles[bundle_name]
        for name in bundle.core:
            _add(chosen, by_name, name, policy.schema_budget, cap)
    for bundle_name in matched:
        for name in all_bundles[bundle_name].tools:
            _add(chosen, by_name, name, policy.schema_budget, cap)
    # Only unbundled/custom tools use lexical fallback. Built-ins are selected through
    # domain bundles above; scoring every built-in description reintroduced the same broad
    # catalog whenever a prompt contained generic verbs such as "read" or "create".
    q = _tokens(query)
    bundled_names = {name for bundle in all_bundles.values() for name in bundle.tools}
    scored = []
    for pos, schema in enumerate(schemas):
        fn = schema["function"]
        if fn["name"] in bundled_names:
            continue
        name_overlap = q & _tokens(fn["name"].replace("_", " "))
        description_overlap = q & _tokens(fn.get("description", ""))
        specific_description = description_overlap - _GENERIC_ACTION_TERMS
        score = 5 * len(name_overlap) + len(specific_description)
        if name_overlap or len(specific_description) >= 2:
            scored.append((score, -pos, fn["name"]))
    for _, _, name in sorted(scored, reverse=True):
        _add(chosen, by_name, name, policy.schema_budget, cap)
    selected = list(chosen.values())
    reason = "routed" if matched else "ambiguous-discovery"
    return Route(selected, True, True, reason, len(schemas), len(selected), model, surface,
                 tuple(matched), loaded_bundles=tuple(policy.core_bundles) + tuple(matched), policy=policy,
                 catalog_schema_tokens=total_cost)


def select(schemas, query, limit=None, model="", force=None, surface="chat"):
    return route(schemas, query, model=model, limit=limit, force=force, surface=surface).schemas


def _parse_args(args):
    if isinstance(args, dict):
        return args
    try:
        value = json.loads(args or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def discover(route_info, catalog, allowed_names, args):
    """Search/load allowed bundles and return (updated route, JSON tool result)."""
    data = _parse_args(args)
    query = str(data.get("query") or "").strip()
    operation = data.get("operation") if data.get("operation") in ("search", "load") else "load"
    requested = [str(x) for x in (data.get("bundles") or []) if isinstance(x, str)]
    all_bundles = bundles()
    ranked = []
    for name in requested + _matched_bundles(query, all_bundles):
        if name in all_bundles and name not in ranked:
            ranked.append(name)
    if not ranked:
        # Compact catalog for the model to choose from; descriptions contain no user data.
        candidates = [{"bundle": b.name, "description": b.description}
                      for b in all_bundles.values() if any(t in allowed_names for t in b.tools)]
        return route_info, json.dumps({"loaded": [], "candidates": candidates[:20],
                                      "message": "No close match; choose a bundle and call load."})
    candidates = []
    for name in ranked:
        bundle = all_bundles[name]
        available = [t for t in bundle.tools if t in allowed_names]
        if available:
            candidates.append({"bundle": name, "description": bundle.description, "tools": available})
    if operation == "search":
        return route_info, json.dumps({"loaded": [], "candidates": candidates[:12]})

    schema_by_name = {s["function"]["name"]: s for s in catalog
                      if s["function"]["name"] in allowed_names}
    chosen = {s["function"]["name"]: s for s in route_info.schemas}
    chosen["discover_tools"] = DISCOVER_SCHEMA
    loaded = []
    for candidate in candidates:
        before = set(chosen)
        for name in candidate["tools"]:
            _add(chosen, schema_by_name, name, route_info.policy.max_schema_budget,
                 route_info.policy.count_limit)
        added = sorted(set(chosen) - before)
        if added:
            loaded.append({"bundle": candidate["bundle"], "tools": added})
    updated = replace(route_info, schemas=list(chosen.values()), selected=len(chosen),
                      reason="discovered", loaded_bundles=tuple(dict.fromkeys(
                          (*route_info.loaded_bundles, *(x["bundle"] for x in loaded)))))
    return updated, json.dumps({"loaded": loaded, "advertised_tools": updated.selected,
                               "schema_tokens": updated.schema_tokens})


def should_expand(route_info, content="", issues=None, tool_events=None):
    if not route_info or not route_info.enabled or route_info.fallback:
        return False
    if _INABILITY.search(content or ""):
        return True
    if any("no action tool was used" in issue for issue in (issues or ())):
        return True
    return any("not available in this conversation" in (result or "").lower()
               for _, result in (tool_events or ()))


def recover(route_info, catalog, allowed_names, query):
    """Tiered one-shot discovery expansion, then optional full-catalog recovery."""
    if route_info.recovery_level == 0 and route_info.policy.fallback in ("discover-once", "full-once"):
        # Load every allowed tool in the bundles inferred from the original request, using the
        # larger cumulative budget. This is deterministic and precedes the expensive full fallback.
        data = {"query": query, "operation": "load", "bundles": list(route_info.domains)}
        updated, result = discover(route_info, catalog, allowed_names, data)
        if updated.selected > route_info.selected:
            return replace(updated, recovery_level=1, reason="discovery-retry"), "discovery", result
        route_info = replace(route_info, recovery_level=1)
    if route_info.policy.fallback == "full-once" and route_info.recovery_level <= 1:
        allowed_catalog = [s for s in catalog if s["function"]["name"] in allowed_names]
        updated = replace(route_info, schemas=allowed_catalog, routed=False, reason="full-catalog-retry",
                          selected=len(allowed_catalog), fallback=True, recovery_level=2)
        return updated, "full", json.dumps({"loaded": "full allowed catalog",
                                             "advertised_tools": len(allowed_catalog)})
    return route_info, None, json.dumps({"loaded": [], "message": "No further recovery configured."})


def expanded(route_info, schemas):
    """Compatibility helper for callers/tests that explicitly request a full fallback."""
    return replace(route_info, schemas=list(schemas), routed=False, reason="full-catalog-retry",
                   selected=len(schemas), fallback=True, recovery_level=2)


def telemetry(route_info, event="selected", used_tools=(), errors=0, **extra):
    """Record routing metrics without prompts, arguments, results, or answers."""
    if not route_info.policy.telemetry or (not route_info.enabled and not route_info.fallback):
        return None
    from oceano import traces
    return traces.record_global(
        "tool_routing", phase=event, model=route_info.model, surface=route_info.surface,
        enabled=route_info.enabled, routed=route_info.routed, reason=route_info.reason,
        policy_source=route_info.policy.source, catalog_tools=route_info.total,
        advertised_tools=route_info.selected, schema_tokens=route_info.schema_tokens,
        catalog_schema_tokens=route_info.catalog_schema_tokens,
        schema_tokens_saved=max(0, route_info.catalog_schema_tokens - route_info.schema_tokens),
        schema_budget=route_info.policy.schema_budget, selected_tools=list(route_info.names),
        bundles=list(route_info.loaded_bundles), fallback=route_info.fallback,
        recovery_level=route_info.recovery_level, used_tools=sorted(set(used_tools)),
        tool_errors=int(errors or 0), ts_monotonic=time.monotonic(), **extra,
    )
