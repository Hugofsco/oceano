# Oceano ≈

**A self-hosted, local-first AI agent — ChatGPT-style chat, real tools, and a workspace it actually works in.**

Oceano runs entirely on your own box. Models are served locally via `llama.cpp` /
`llama-swap`; web search goes through your own SearXNG; memory and document search
use a local embedding server. Nothing leaves the machine unless *you* add a remote
provider. The agent isn't sandboxed in a toy way — it reads, writes, and runs
commands inside a real `workspace/` folder, browses the web in a headless Chromium
you can watch and drive, remembers things across conversations, and can be reached
from a web UI or Telegram.

> Conceived as a workspace-based take on PewDiePie's *Odysseus*. The aesthetic is an
> "abyssal instrument console": dark water, bathymetric contours, bioluminescent cyan.

---

## Highlights

- **One daemon, whole stack.** A single `oceano.service` runs the web UI, the Telegram
  bot, the scheduled-task runner, and supervises the embedding server as a child.
- **Local models, swappable.** Chat models served by `llama-swap` (one resident at a
  time); pick the model per message in the UI. Bring your own remote endpoints
  (OpenAI/OpenRouter/Groq/…) too — keys stay on the box.
- **GPU-aware install.** `scripts/install.sh` detects your GPU/driver and builds
  `llama.cpp` with the matching backend (Vulkan / CUDA / ROCm / CPU).
- **29 built-in tools** + **MCP** — filesystem, shell, Python, web search, a real
  headless browser, long-term memory, document RAG, skills, scheduling, workflows, and
  delegation; plus any tools from MCP servers you connect.
- **Memory that learns.** Relevant memories are injected automatically each turn,
  durable facts are extracted in the background, and you control *how* each type of
  memory is used (pin / always / when-relevant / off). A weekly, Claude-reviewed
  maintenance job keeps the store deduped — and a graph view maps how memories relate.
- **Visual workflows.** Draw branching, multi-step recipes on a node canvas
  (tool · instruction · delegate · decision), run them on demand or on a schedule, and
  watch each node execute live. See [Workflows](#workflows).
- **Configurable delegation.** Hand a heavy subtask to a stronger assistant — Claude
  Code (no API key, via the `claude` CLI) or a cloud model run as a full agent — with
  *who* chosen in Settings, and a separate target for the self-improving jobs.
- **Watch it browse.** A multi-tab live browser streams what the agent sees; a web
  search spins up a tab per source so you can see exactly what it read.
- **Run-aware + optional queue.** A live indicator shows every background job
  (workflows, scheduled tasks, research, …); an optional setting serializes them — and,
  if you want, chat — so the single local model isn't hit in parallel.
- **Rivers** — browse Hugging Face GGUF models, see which fit your GPU (auto-scored),
  download them, and one-click "serve" them into `llama-swap`.
- **A desktop of apps.** Floating windows: chat with dated history folders, a "Brain"
  (memory · knowledge · skills · rivers · evals), Workflows, file explorer + editor,
  Scheduler, Calendar, Researcher, semantic Search, a Kanban Notes board, a
  System-health dashboard, a Memory graph, a Voice console, and a sandboxed Preview that
  renders web apps, markdown, Mermaid, charts, and slide decks.

---

## Architecture

```
                          ┌──────────────────────── oceano.service (oceano/engine.py) ───────────────────────┐
   browser / Telegram ──► │  FastAPI web UI :8800   ·   Telegram bot   ·   scheduler loop                    │
                          │        │                                                                          │
                          │   Agent core (oceano/agent.py)  ──► tools (oceano/tools.py)                       │
                          │        │                              │                                           │
                          │        ▼                              ├─► llama-swap :8081  (chat models)         │
                          │   per-turn context:                   ├─► SearXNG :8080     (web search)          │
                          │   date · workspace · memory · skills  ├─► livebrowser       (headless Chromium)   │
                          │                                       ├─► memory / RAG  ──► embeddings :8082 ◄─────┤ (spawned + supervised
                          │                                       └─► MCP servers (optional, data/mcp.json)   │  as a child process)
                          └───────────────────────────────────────────────────────────────────────────────────┘
```

- **`oceano/engine.py`** — the single entry point. Runs `uvicorn` (web), starts the
  Telegram bot via the app lifespan, runs the scheduler as a background task, and
  spawns/supervises the `llama.cpp` embedding server (auto-restart, unified logs).
- **`oceano/agent.py`** — the agent loop. Each turn it rebuilds a context block
  (current date, the workspace path, relevant memories, the skills catalog), calls
  the model with tools, executes tool calls, and streams the result. After the turn
  it extracts durable facts in the background (self-learning memory).
- **Frontends are thin** — web, Telegram, CLI, and the scheduler all just call
  `Agent.run()` / `run_stream()`.

### Ports

| Port | Service | Notes |
|------|---------|-------|
| `8800` | Oceano web UI | localhost-only; reach via SSH tunnel / Tailscale |
| `8081` | `llama-swap` | OpenAI-compatible; chat models, one resident at a time |
| `8082` | embedding server | `nomic-embed-text` (CPU), used by memory + RAG |
| `8080` | SearXNG | web search backend (`?format=json`) |

---

## The agent's tools (29)

| Group | Tools |
|-------|-------|
| **Workspace / shell** | `list_files`, `read_file`, `write_file`, `edit_file` (surgical patch), `make_folder`, `run_shell`, `python_exec` |
| **Web** | `web_search` (SearXNG), `fetch_url` (renders in the live browser) |
| **Browser** | `browser_open`, `browser_screenshot`, `browser_click`, `browser_scroll` |
| **Memory** | `remember`, `recall`, `update_memory`, `forget_memory` |
| **Documents (RAG)** | `index_docs`, `search_docs` |
| **Skills** | `list_skills`, `load_skill`, `learn_skill` |
| **Scheduling** | `schedule_task`, `list_tasks`, `notify` (ntfy push) |
| **Workflows** | `run_workflow`, `list_workflows` (trigger saved workflows; authored in the UI) |
| **Delegation** | `delegate` (hand a subtask to the configured stronger assistant) |
| **Calendar** | `calendar_events` (read the synced local copy) |
| **MCP** | any tools exposed by connected MCP servers (`mcp__<server>__<tool>`) |

File/shell operations are fenced to `workspace/` by default (`OCEANO_CONFINE=1`).

---

## Memory

SQLite-backed (`data/memory.db`), semantic via the embedding server with a keyword
fallback. It's designed to feel like the agent actually *remembers* you:

- **Passive recall** — each turn, the memories relevant to your message are injected
  into context automatically (no need for the model to call `recall`).
- **Self-learning** — after each turn a background pass reads *your* message and
  extracts durable, first-person facts, saving the new ones (deduped). It never
  attributes facts about people/things you merely researched to you.
- **Pinning** — pin core facts (Brain → Memory, the 📌) so they're always injected.
- **Typed injection policy** — every memory has a category (identity / preference /
  project / fact / task), and **Settings → Memory** controls how each type reaches
  the model: **Always**, **When relevant**, or **Off**. Pinned memories override.
- **Self-correction** — the agent can `update_memory` / `forget_memory` when something
  becomes wrong or outdated.
- **Maintenance + graph** — a locked weekly job hands the whole store to Claude Code to
  dedupe, merge, and re-file (pinned memories are never deleted, and a run that would gut
  the store is refused). A **graph view** (Brain → Memory → ❄ Graph) maps memories by
  semantic similarity and shared tags, colored by category.

---

## Skills

A skill is a reusable instruction packet at `skills/<name>/SKILL.md` (front-matter +
body). The catalog (names + descriptions) is surfaced to the agent every turn, and it
pulls the full body in with `load_skill` when a task matches. Ships with a starter
library: `research-report`, `summarize-document`, `code-review`, `daily-digest`,
`extract-to-csv`. Create/edit them in the UI (Brain → Skills) or by adding files.

---

## Workflows

Named, **branching** recipes you draw on a node canvas (the Workflows window). A workflow
is a directed graph; execution walks it from a **start** node, following edges:

- **tool** — a chosen tool fired with preset arguments (a real form per tool, with
  searchable pickers for skills / saved workflows / workspace files — no JSON to hand-write)
- **instruction** — a free-form step run through the agent loop (it may use any tool)
- **delegate** — hand the step to the configured delegate (Claude Code / a cloud model)
- **decision** — routes **yes / no** down different edges, judged by a **rule** over the
  previous step's output, the **local model**, or a **delegate**
- **start / end**

All steps share one agent, so context accumulates across nodes; a hard visit-cap stops
runaway loops. Run a workflow on demand (live, node-by-node progress over SSE) or on a
cron (managed in the Scheduler); every run is recorded. The agent can *trigger* saved
workflows with `run_workflow`, but you author them in the UI. Stored in
`data/workflows.json`; the canvas is a vendored [Drawflow](https://github.com/jerosoler/Drawflow).

---

## Delegation

Oceano can hand a self-contained subtask to a stronger assistant via the `delegate` tool.
**Who** that is, is set in **Settings → Delegation** — and the default path needs no
Anthropic API key:

- **Claude Code** (default) — runs headless via the `claude` CLI inside the workspace,
  with its own tools (uses your existing CLI login, no key passed by Oceano).
- **A cloud model** — any configured OpenAI-compatible endpoint, run through Oceano's
  *own* agent loop with *our* tools, so it can read, write, and run things — not just reason.

Two independent **roles** let you point different work at different models: **default**
(the agent's `delegate` tool) and **improve** (the self-improving jobs — skills review,
eval judging, memory maintenance). The local model never grades its own work; that's
what the `improve` delegate is for. Live readiness + a one-click test sit in the panel.

---

## Rivers — the model "cookbook"

Browse and provision local models from the UI (Brain → Rivers):

- **Recommended for your machine** — a curated catalog auto-scored against your VRAM
  (fits / partial / won't-fit, with a 0–100 score), best-capable-that-runs first.
- **Hugging Face search** — find any GGUF repo, expand to see each quant with a
  hardware-fit badge and size.
- **Download** with a progress bar, **serve** with one click (appends a model block to
  `llama-swap.yaml`, which hot-reloads), and **search your on-device models**.

---

## Web UI

Served at `http://127.0.0.1:8800` (login required — default **admin / admin**, change
it in Settings → Account). It's a single-page app with:

- **Auth** — cookie session, password hashed (PBKDF2) in `data/web.json`; all `/api`
  routes gated.
- **Chat** — SSE streaming, streamed reasoning (collapsible, auto-scrolling), inline
  tool-call cards, a **Stop** button that aborts an in-flight query, an **Agent** toggle
  (persists) that hands the model its tools, and Telegram-style **slash commands**
  (`/context`, `/compact`, `/status`, …) with autocomplete. The sidebar slides between
  the app menu and dated **chat-history folders**.
- **Floating windows** — Settings, **Brain** (Memory · Knowledge · Skills · Rivers ·
  Evals), **Workflows** (node canvas), Files explorer + editor, Scheduler, Calendar,
  Researcher, semantic **Search**, **Notes** (Kanban), **Health** (live system
  dashboard), **Memory graph**, **Voice** (push-to-talk in / spoken replies out), the
  **Live browser** (multi-tab — watch the agent research source-by-source), and a
  sandboxed **Preview**. Drag, resize, snap, minimize.
- **Preview / artifacts** — when the agent writes an `.html` app, markdown, a Mermaid
  diagram, a Chart.js spec, or a `.slides` deck, a chip opens it rendered in an
  origin-isolated sandbox iframe (device presets + live reload).
- **Multiple endpoints** — local `llama.cpp` plus remote providers; models from all of
  them appear in the composer's picker.

> Bound to localhost on purpose — the agent can run shell commands. Reach it over an
> SSH tunnel or Tailscale; do **not** expose `0.0.0.0` without additional auth.

---

## Telegram & scheduling

- **Telegram bot** — chat with Oceano from your phone. Enable it and set the token +
  allowed user IDs in **Settings → Telegram** (it runs inside the engine, no separate
  service). Only allow-listed user IDs are answered (the agent can run shell).
- **Scheduler** — cron tasks run by the agent autonomously; results pushed to your phone
  via [ntfy](https://ntfy.sh). Manage in the Scheduler window, or hit **▶ Run** to fire
  any job on demand (locked jobs and workflows included).
- **Background jobs & the queue** — every unattended job (workflows, scheduled tasks,
  research, evals, memory upkeep) registers in a live registry shown by a topbar
  indicator. **Settings → Tools → Execution** can *serialize* them through one gate —
  optionally including chat — so the single local model isn't hit in parallel.

---

## MCP (Model Context Protocol)

Connect external tool servers in `data/mcp.json`; their tools appear to the agent
alongside the built-ins. Graceful no-op when none are configured.

```json
{
  "servers": [
    { "name": "fs", "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/Oceano/workspace"] }
  ]
}
```

---

## Install

Designed for a fresh Ubuntu host. The installer detects the GPU and builds `llama.cpp`
with the right backend, installs dependencies, fetches the embedding model, sets up the
Python venv + Playwright, brings up SearXNG + `llama-swap`, and installs the systemd
unit templated to your user/path.

```bash
git clone https://github.com/Hugofsco/oceano.git ~/Oceano
cd ~/Oceano
cp oceano.env.example oceano.env   # fill in secrets when ready (chmod 600)

scripts/install.sh --check         # detect + probe only, change nothing
scripts/install.sh                 # full install (idempotent; safe to re-run)
scripts/install.sh --with-models   # also download the chat models (several GB)
```

Backends: **NVIDIA → CUDA**, **AMD/Intel → Vulkan**, **ROCm**, or **CPU**. The script
installs the NVIDIA driver if absent (reboot, then re-run for the CUDA build).

Once installed:

```bash
systemctl status oceano        # health
journalctl -u oceano -f        # unified logs (web · telegram · scheduler · embeddings)
sudo systemctl restart oceano  # restart everything
```

Then open `http://127.0.0.1:8800` and log in with **admin / admin**.

---

## Configuration

Everything is overridable via `OCEANO_*` environment variables (see `config.py`).
Secrets live in `oceano.env` (loaded by systemd; `chmod 600`, never committed).

| Variable | Default | Purpose |
|----------|---------|---------|
| `OCEANO_LLM_URL` | `http://127.0.0.1:8081/v1` | chat model endpoint (llama-swap) |
| `OCEANO_MODEL` | `qwen3-4b` | default model |
| `OCEANO_WORKSPACE` | `./workspace` | the agent's working folder |
| `OCEANO_SEARXNG` | `http://127.0.0.1:8080` | web search |
| `OCEANO_MAX_STEPS` | `25` | tool-call loop cap per turn |
| `OCEANO_CONFINE` | `1` | fence file ops to the workspace |
| `OCEANO_AUTO_LEARN` | `1` | background self-learning memory |
| `OCEANO_SHELL_GUARD` / `OCEANO_URL_GUARD` | `1` | safety guards |
| `OCEANO_TELEGRAM_TOKEN` / `_ALLOWED` | — | Telegram (or set in Settings) |
| `HF_TOKEN` | — | optional, for gated Hugging Face repos |

---

## Security posture

Oceano runs powerful tools (shell, file writes, a browser) for one trusted local user
— it is **hardened, not sandboxed**:

- **`oceano/safety.py`** — `check_shell` (refuses catastrophic commands), `check_url`
  (SSRF guard: blocks loopback/private/link-local/metadata so injections can't reach
  your DBs/LLM/cloud metadata), and `wrap_untrusted` (fences web/doc/email text as
  data so the model never obeys instructions hidden inside it).
- **Workspace confinement** — file tools resolve relative to `workspace/` and refuse
  to escape it.
- **systemd hardening** — `NoNewPrivileges`, `ProtectHome=read-only` with
  `ReadWritePaths` limited to `workspace/`, `data/`, `skills/`, `PrivateTmp`.
- **Localhost binding** + **login auth** on the web UI.

For true isolation, run it in a container or under bubblewrap/firejail.

---

## Project layout

```
oceano/
  engine.py          the single daemon (web + telegram + scheduler + embed supervisor)
  agent.py           the agent loop, context building, self-learning
  llm.py             OpenAI-compatible client (streaming, tools)
  tools.py           the tool registry + built-in tools
  safety.py          shell/SSRF guards + untrusted-content fencing
  memory.py          long-term memory (SQLite + embeddings, policy, pinning, graph, maintenance)
  rag.py             document indexing + semantic search (incremental)
  skills.py          skill loading + catalog + Claude-judged evaluation
  scheduler.py       cron tasks + on-demand run + ntfy + heartbeat
  workflows.py       visual branching workflows (graph engine + run history)
  jobs.py            background-job registry + optional serialization gate (queue)
  delegate.py        delegation to Claude Code / a cloud model (per-role config)
  notes.py           Kanban scratchpad (JSON-persisted)
  evals.py           model eval suite (cases, leaderboard, scheduled runs)
  researcher.py      scheduled deep-dives → living docs → RAG
  calsync.py         read-only calendar sync (ICS feeds)
  voice.py           speech-in (faster-whisper) / speech-out (Piper) for web + Telegram
  rivers.py          Hugging Face model catalog + hardware-fit + serve
  mcp_client.py      optional MCP server connections
  browser.py         agent browser surface (SSRF-guarded)
  livebrowser.py     persistent multi-tab headless Chromium (CDP screencast)
  embeddings.py      shared embedding client (:8082)
  atomicio.py        atomic writes for the small JSON stores
  telegram_bot.py    Telegram frontend
  web/
    server.py        FastAPI backend + all /api routes + auth
    static/          the SPA (index.html, app.js, style.css)
config.py            central, env-overridable config
scripts/
  install.sh         host bootstrapper (GPU detect → build → services)
  serve-embeddings.sh  the embedding server launcher
systemd/             oceano.service + oceano-llama-swap.service
deploy/searxng/      bundled SearXNG compose + settings
skills/              skill library (one folder per skill)
cli.py               run the agent from the terminal
```

Runtime data (`data/`, `workspace/`), the virtualenv, and `oceano.env` are gitignored.

## License

MIT — see [LICENSE](LICENSE). Bundled third-party libraries (CodeMirror, marked,
DOMPurify, highlight.js, Mermaid, Chart.js, Drawflow) are credited with their own
licenses in [NOTICE](NOTICE).

---

*Everything runs on your box. The deep is local.* ≈
