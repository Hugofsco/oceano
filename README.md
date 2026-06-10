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
- **24 built-in tools** + **MCP** — filesystem, shell, Python, web search, a real
  headless browser, long-term memory, document RAG, skills, and scheduling; plus any
  tools from MCP servers you connect.
- **Memory that learns.** Relevant memories are injected automatically each turn,
  durable facts are extracted in the background, and you control *how* each type of
  memory is used (pin / always / when-relevant / off).
- **Watch it browse.** A multi-tab live browser streams what the agent sees; a web
  search spins up a tab per source so you can see exactly what it read.
- **Rivers** — browse Hugging Face GGUF models, see which fit your GPU (auto-scored),
  download them, and one-click "serve" them into `llama-swap`.
- **Web UI** — floating windows, auth (login required), agent mode, live tool-call
  cards, streamed reasoning, file explorer, and a "Brain" for memory/skills/knowledge.

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

## The agent's tools (24)

| Group | Tools |
|-------|-------|
| **Workspace / shell** | `list_files`, `read_file`, `write_file`, `edit_file` (surgical patch), `make_folder`, `run_shell`, `python_exec` |
| **Web** | `web_search` (SearXNG), `fetch_url` (renders in the live browser) |
| **Browser** | `browser_open`, `browser_screenshot`, `browser_click`, `browser_scroll` |
| **Memory** | `remember`, `recall`, `update_memory`, `forget_memory` |
| **Documents (RAG)** | `index_docs`, `search_docs` |
| **Skills** | `list_skills`, `load_skill` |
| **Scheduling** | `schedule_task`, `list_tasks`, `notify` (ntfy push) |
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

---

## Skills

A skill is a reusable instruction packet at `skills/<name>/SKILL.md` (front-matter +
body). The catalog (names + descriptions) is surfaced to the agent every turn, and it
pulls the full body in with `load_skill` when a task matches. Ships with a starter
library: `research-report`, `summarize-document`, `code-review`, `daily-digest`,
`extract-to-csv`. Create/edit them in the UI (Brain → Skills) or by adding files.

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
  tool-call cards, a **Stop** button that aborts an in-flight query, and an **Agent**
  toggle (persists) that hands the model its tools.
- **Floating windows** — Settings, Brain (Memory / Knowledge / Skills / Rivers), Files
  explorer, Scheduler, and the **Live browser** (multi-tab; click to switch, watch the
  agent research source-by-source). Drag, resize, snap, minimize.
- **Multiple endpoints** — local `llama.cpp` plus remote providers; models from all of
  them appear in the composer's picker.

> Bound to localhost on purpose — the agent can run shell commands. Reach it over an
> SSH tunnel or Tailscale; do **not** expose `0.0.0.0` without additional auth.

---

## Telegram & scheduling

- **Telegram bot** — chat with Oceano from your phone. Enable it and set the token +
  allowed user IDs in **Settings → Telegram** (it runs inside the engine, no separate
  service). Only allow-listed user IDs are answered (the agent can run shell).
- **Scheduler** — cron tasks run by the agent autonomously; results pushed to your
  phone via [ntfy](https://ntfy.sh). Manage in the Scheduler window.

---

## MCP (Model Context Protocol)

Connect external tool servers in `data/mcp.json`; their tools appear to the agent
alongside the built-ins. Graceful no-op when none are configured.

```json
{
  "servers": [
    { "name": "fs", "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/Oceano/workspace"] }
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
  memory.py          long-term memory (SQLite + embeddings, policy, pinning)
  rag.py             document indexing + semantic search
  skills.py          skill loading + catalog
  scheduler.py       cron tasks + ntfy + heartbeat
  rivers.py          Hugging Face model catalog + hardware-fit + serve
  mcp_client.py      optional MCP server connections
  browser.py         agent browser surface (SSRF-guarded)
  livebrowser.py     persistent multi-tab headless Chromium (CDP screencast)
  embeddings.py      shared embedding client (:8082)
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

---

*Everything runs on your box. The deep is local.* ≈
