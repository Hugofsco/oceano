---
name: mcp-server-builder
description: scaffold and validate a new MCP server from an OpenAPI spec instead of hand-writing tool wrappers — use when exposing an API as tools for Claude/Codex/any MCP client, or when adding a new MCP server to Oceano's own connections
status: published
notes: ported from claude-skills engineering/mcp-server-builder (MIT); scripts copied verbatim, stdlib-only
---
# MCP server builder

Relevant to Oceano itself: the Claude/Codex-as-mind bridge (`oceano/mindbridge.py`) IS an
MCP server exposing Oceano's own tools — this skill is for building *additional* ones
(e.g. wrapping a third-party REST API you want any mind to call as a tool).

1. **Scaffold from an OpenAPI spec:**
   `python3 skills/mcp-server-builder/scripts/openapi_to_mcp.py --input openapi.json --server-name <name> --language python --output-dir ./out`
   (accepts stdin too; `--language typescript` for a JS/TS host)
2. **Validate the tool manifest before wiring it up:**
   `python3 skills/mcp-server-builder/scripts/mcp_validator.py --input out/tool_manifest.json --strict`
   Catches duplicate names, bad schema shape, missing descriptions, empty required fields.

Before shipping: secrets stay in env vars (never in the tool schema/description),
prefer an outbound-host allowlist over an open proxy, and treat tool names as additive-
only — renaming one in place breaks every client that already called it.
