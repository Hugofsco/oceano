# Oceano ≈

**A self-hosted, local-first AI agent — ChatGPT-style chat, real tools, and a workspace it actually works in.**

Oceano runs entirely on your own box. Models are served locally via `llama.cpp` /
`llama-swap`; web search goes through your own SearXNG; memory and document search
use a local embedding server. Nothing leaves the machine unless *you* add a remote
provider. The agent isn't sandboxed in a toy way — it reads, writes, and runs
commands inside a real `workspace/` folder, browses the web in a headless Chromium
you can watch and drive, remembers things across conversations, and can be reached
from a web UI or Telegram.

> Inspired by PewDiePie's *Odysseus*, reimagined as a workspace-based local agent. The
> aesthetic is an "abyssal instrument console": dark water, bathymetric contours,
> bioluminescent cyan.

---

## Highlights

- **One daemon, whole stack.** A single `oceano.service` runs the web UI, the Telegram
  bot, the scheduled-task runner, and supervises the embedding server as a child.
- **Local models, swappable.** Chat models served by `llama-swap` (one resident at a
  time); pick the model per message in the UI. Bring your own remote endpoints
  (OpenAI/OpenRouter/Groq/…) too — keys stay on the box.
- **GPU-aware install.** `scripts/install.sh` detects your GPU/driver and builds
  `llama.cpp` with the matching backend (Vulkan / CUDA / ROCm / CPU).
- **50 built-in tools** + **MCP** — filesystem, shell, Python, dev (git · ripgrep · run
  tests), media (transcribe · speak · fetch · convert), web search, a real headless browser,
  HTTP/REST + RSS, local data analysis (DuckDB), long-term memory, document RAG, skills,
  scheduling, workflows, an agent-managed calendar (schedule a whole conflict-aware plan in
  one shot), agent-driven UI control (it opens & arranges your windows), and delegation; plus
  any tools from MCP servers you connect.
- **Memory that learns.** Relevant memories are injected automatically each turn,
  durable facts are extracted in the background, and you control *how* each type of
  memory is used (pin / always / when-relevant / off). A weekly maintenance job (run by
  the configured delegate) keeps the store deduped, a graph view maps how memories relate,
  and you can semantically search your **past conversations** too.
- **Drop in files & images.** Attach files to a chat message (drag · paste · 📎):
  documents are read inline, and images are understood by a configurable vision target
  (Claude Code or a cloud vision model) since the local chat model is text-only. Or bulk-load
  data: the Files explorer takes **drag-and-drop (or pick) of whole files and folders**
  straight into the workspace.
- **Visual workflows + triggers.** Draw branching, multi-step recipes on a node canvas
  (tool · instruction · delegate · decision); fire them manually, on a cron, or on an
  **event** — a watched folder changing, a webhook, a chat keyword, or another workflow
  finishing. Watch each node execute live. See [Workflows](#workflows).
- **Survives a refresh.** Open app windows reopen where you left them, and a chat reply
  (or workflow) still generating when you reload **reconnects** instead of being lost.
- **Configurable delegation + any model as primary.** Hand a heavy subtask to a stronger
  assistant — Claude Code (no API key) or a cloud model run as a full agent — *who* chosen
  in Settings, with separate targets for the self-improving jobs. Pick **any model from any
  endpoint** as Oceano's primary (local-first is opt-in), or turn delegation **fully off**.
- **Oceano as body, Claude as mind (optional).** Pick **🧠 Claude** in the model picker and the
  whole conversation is driven by Claude Code (your subscription, no API key) wearing Oceano's
  persona, memory, and history — and reaching for *Oceano's own tools* (memory, calendar, windows,
  notify) over an in-process **MCP bridge**, so it acts as the resident mind of the local body
  (its tool use shows as chips in the chat, just like the local model). Flip back to the local
  model for fully-offline. See [Claude as the mind](#claude-as-the-mind).
- **Watch it browse.** A multi-tab live browser streams what the agent sees; a web
  search spins up a tab per source so you can see exactly what it read.
- **Run-aware + optional queue.** A live indicator shows every background job
  (workflows, scheduled tasks, research, …); an optional setting serializes them — and,
  if you want, chat — so the single local model isn't hit in parallel.
- **Rivers** — browse Hugging Face GGUF models, see which fit your GPU (auto-scored),
  download them, one-click "serve" them into `llama-swap`, and **✨ Recommend settings for your
  hardware** (it reads the model's GGUF + your VRAM/RAM/cores and fills in context, GPU layers,
  KV dtype, threads, and MoE-offload, each with a reason).
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

## The agent's tools (50)

| Group | Tools |
|-------|-------|
| **Workspace / shell** | `list_files`, `read_file`, `write_file`, `edit_file` (surgical patch), `make_folder`, `run_shell`, `python_exec` |
| **Dev** | `git` (status/diff/commit/blame in the workspace; push refused), `code_search` (ripgrep), `run_tests` (auto-detect pytest/npm/cargo/make) |
| **Media** | `transcribe_media` (audio/video → text, faster-whisper), `speak_to_file` (text → spoken `.ogg`, natural Kokoro voice), `fetch_media` (download via yt-dlp), `convert` (ffmpeg / pandoc / ImageMagick) |
| **Web / data** | `http_request` (authenticated REST + webhooks + Home Assistant; SSRF-guarded with an opt-in `OCEANO_HTTP_ALLOW` allowlist for local hosts), `rss` (read RSS/Atom feeds), `sql_query` (read-only DuckDB over CSV/TSV/Parquet/JSON) |
| **UI** (web only) | `ui_open` (pop a window or a file/folder — Preview, Calendar, Files…), `ui_close`, `ui_arrange` (tile · cascade · focus · center · minimize) — the agent drives the floating-window desktop, so it can *show* you what it made, not just describe it |
| **Web** | `web_search` (SearXNG), `fetch_url` (renders in the live browser) |
| **Browser** | `browser_open`, `browser_screenshot`, `browser_click`, `browser_scroll` |
| **Memory** | `remember`, `recall`, `update_memory`, `forget_memory`, `search_chats` (recall past conversations) |
| **Documents (RAG)** | `index_docs`, `search_docs` |
| **Skills** | `list_skills`, `load_skill` (one or several), `learn_skill`, `evaluate_skill` (independent review → staging) |
| **Scheduling** | `schedule_task`, `list_tasks`, `notify` (ntfy push) |
| **Workflows** | `run_workflow` (one or several), `list_workflows` (trigger saved workflows; authored in the UI) |
| **Delegation** | `delegate` (hand a subtask to the configured stronger assistant) |
| **Calendar** | `calendar_events` (read schedule), `find_free_slots` (open slots), `add_calendar_event`, `add_calendar_events` (a whole plan in one call — exact or auto-placed), `manage_calendar` (create · move · delete in one atomic, conflict-aware call), `update_calendar_event`, `delete_calendar_event` (synced feeds stay read-only) |
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
- **Maintenance + graph** — a locked weekly job hands the whole store to the configured
  delegate to dedupe, merge, and re-file (pinned memories are never deleted, and a run that
  would gut the store is refused). A **graph view** (Brain → Memory → ❄ Graph) maps memories
  by semantic similarity and shared tags, colored by category.
- **Conversation recall** — past chats are embedded incrementally, so semantic
  **Search → Conversations** and the agent's `search_chats` tool can surface what you
  discussed in earlier sessions, not just stored facts.

---

## Skills

A skill is a reusable instruction packet at `skills/<name>/SKILL.md` (front-matter +
body). The catalog (names + descriptions) is surfaced to the agent every turn, and it
pulls the full body in with `load_skill` when a task matches. Ships with a starter library
(`research-report`, `code-review`, `daily-digest`, `debug-systematically`,
`read-large-files`, `verify-by-running`, …). Create/edit them in the UI (Brain → Skills),
add files directly, or let the agent **learn** them:

- **`learn_skill`** — the agent distills a reusable procedure it just worked out. **`/skill`**
  in the chat box does the same for the *current conversation*.
- A learned skill enters as `learning` and is reviewed by an **independent** model (the
  `improve` delegate) before it goes live: `learning` → `staged` → `published`. Only
  published skills ever reach the agent — the model that wrote a skill never validates it.
- The reviewer doesn't just approve/reject — it can **edit a salvageable skill to fix it** and
  **conflict-checks** it against the published library before promoting it to `staged`. Brain →
  Skills has **Published / Staged / Learning** tabs so you can see what's queued and **publish a
  staged skill yourself**. A workflow can close the loop with the `evaluate_skill` tool
  (research → `learn_skill` → `evaluate_skill` → staged).

---

## Workflows

Named, **branching** recipes you draw on a node canvas (the Workflows window). A workflow
is a directed graph; execution walks it from a **start** node, following edges:

- **tool** — a chosen tool fired with preset arguments (a real form per tool, with
  searchable pickers for skills / saved workflows / workspace files — and **multi-select**
  on the capability pickers, e.g. load several skills or run several workflows at once — no
  JSON to hand-write)
- **instruction** — a free-form step run through the agent loop (it may use any tool)
- **delegate** — hand the step to the configured delegate (Claude Code / a cloud model)
- **decision** — routes **yes / no** down different edges, judged by a **rule** over the
  previous step's output, the **local model**, or a **delegate**
- **start / end**

All steps share one agent, so context accumulates across nodes; a hard visit-cap stops
runaway loops. **Triggers** (the ⚡ panel) decide *when* a workflow fires: manually (▶ Run),
on a **cron** (managed in the Scheduler), or on an **event** — a watched workspace folder
changing, an incoming **webhook** (a secret-token URL), a **chat keyword** (web / Telegram),
or **another workflow finishing** (chaining, loop-guarded). Every run is recorded (live,
node-by-node over SSE), and a run still in progress when you **refresh the browser reconnects**
to its live state. The agent can also trigger saved workflows with `run_workflow`, but you
author them in the UI. Stored in `data/workflows.json`; the canvas is a vendored
[Drawflow](https://github.com/jerosoler/Drawflow).

---

## Delegation

Oceano can hand a self-contained subtask to a stronger assistant via the `delegate` tool.
**Who** that is, is set in **Settings → Delegation** — and the default path needs no
Anthropic API key:

- **Claude Code** (default) — runs headless via the `claude` CLI inside the workspace,
  with its own tools (uses your existing CLI login, no key passed by Oceano).
- **A cloud model** — any configured OpenAI-compatible endpoint, run through Oceano's
  *own* agent loop with *our* tools, so it can read, write, and run things — not just reason.

Three independent **roles** let you point different work at different models: **default**
(the agent's `delegate` tool), **improve** (the self-improving jobs — skills review, eval
judging, memory maintenance), and **vision** (image recognition — the local chat model is
text-only, so files dropped into chat get routed here; Claude Code reads the image file
directly, or point it at a cloud vision model). The local model never grades its own work,
nor sees images itself. Live readiness + a one-click test sit in each section.

Delegation **streams**: the delegate's live work (its narration and tool uses) surfaces under
the `delegate` tool card in chat (and dim in the CLI), so a long build shows progress instead
of a frozen spinner. It uses an **idle** timeout that resets on every event — an actively
working delegate is never killed for "taking too long", only a genuinely stalled one — with a
generous absolute cap as a backstop. If a delegation doesn't finish it returns any partial
work and tells the local model *not* to attempt the whole job itself (which would overflow a
small context). Tune with `OCEANO_DELEGATE_IDLE` (default 300s), `OCEANO_DELEGATE_MAXTOTAL`
(3600s), `OCEANO_DELEGATE_MAXTURNS` (60).

The same panel also sets Oceano's **primary model** — **any model from any configured
endpoint** (local-first is opt-in; a cloud model can be your default, and it's carried to
chat, Telegram, the CLI, and background jobs). A master toggle turns **delegation fully off**
(withholding the `delegate` tool and stopping the delegated jobs) for a purely local setup.

---

## Claude as the mind

Delegation hands *subtasks* to Claude. The inverse is also possible: make **Claude the resident
mind** of the whole assistant. Pick **🧠 Claude** in the chat model picker (or Settings → Delegation
→ *Primary intelligence*) and every turn — chat **and** voice — is driven by the `claude` CLI (your
Claude subscription, **no API key**), while **Oceano stays the body**:

- It wears Oceano's **persona**, your **memory**, and the **conversation history** — so it knows you
  and the thread — and its reply streams into the chat as usual.
- It reaches for **Oceano's own tools** — memory (`remember`/`recall`/…), the calendar, the floating
  **windows** (`ui_open`/`ui_arrange`), `notify` — over an in-process, token-gated **MCP bridge**, so
  the mind drives the real body: it pops your Calendar, saves to *Oceano's* memory (not its own), and
  so on. Its tool use shows as **chips in the chat**, and its strong native tools (files, shell, web)
  stay available too.
- Memory is the continuity: Claude's intelligence **+** Oceano's memory = a presence that remembers you.

The bridge is **localhost-only and token-gated** (a header token, constant-time compared), the mind
can't delegate to itself, and tool calls execute *inside* the daemon (so windows actually open). Flip
back to a **local model** anytime for fully-offline operation — that's the trade-off: Claude is
sharper, the local model keeps Oceano sovereign and offline. A common setup is Claude as the
interactive mind with the local model still running the background/scheduled work.

---

## Rivers — the model "cookbook"

Browse and provision local models from the UI (Brain → Rivers):

- **Recommended for your machine** — a curated catalog auto-scored against your VRAM
  (fits / partial / won't-fit, with a 0–100 score), best-capable-that-runs first.
- **Hugging Face search** — find any GGUF repo, expand to see each quant with a
  hardware-fit badge and size.
- **Download** with a progress bar, **serve** with one click (appends a model block to
  `llama-swap.yaml`, which hot-reloads), and **search your on-device models**.
- **✨ Recommend settings for your hardware** — one click reads the model's GGUF metadata and your
  VRAM/RAM/cores and fills in context, GPU layers, KV dtype, threads, and MoE→CPU offload (each with
  a one-line reason): full offload when it fits, the largest context that fits, q8 KV only when it
  helps, expert-offload for MoE models too big for VRAM, partial/CPU otherwise — always with VRAM
  headroom so it shows "fits".
- **Tune serving fully** — context, GPU layers, KV-cache dtype (K & V), flash-attention,
  threads, batch/ubatch, MoE-offload, TTL, and free-form extra flags — with **preset chips**
  (context 8k/16k/32k…, an "all-GPU ↔ CPU" layers slider) and a **live VRAM estimate** (weights +
  KV-cache read straight from the GGUF) that updates as you change them, plus a **live "VRAM used"
  monitor** in the header.
- **Edit, unserve, or delete** an already-served model from the Installed list: re-tune its
  parameters, drop it from `llama-swap`, or remove its `.gguf` from disk. Edits are surgical text
  splices, so your hand-written comments and custom flags are preserved.

---

## Web UI

Served at `http://127.0.0.1:8800` (login required — default **admin / admin**, change
it in Settings → Account). It's a single-page app with:

- **Auth** — cookie session, password hashed (PBKDF2) in `data/web.json`; all `/api`
  routes gated. **Optional TOTP 2FA** (Settings → Account): scan a QR with any authenticator
  app and a 6-digit code is required at login. Off by default.
- **Chat** — SSE streaming, streamed reasoning (collapsible, auto-scrolling), inline
  tool-call cards, a **Stop** button, an **Agent** toggle (persists) that hands the model
  its tools, Telegram-style **slash commands** (`/context`, `/compact`, `/status`,
  `/skill`, …) with autocomplete, and **file/image attachments** (drag · paste · 📎). A reply
  still being generated when you reload **reconnects** to it (the turn keeps running
  server-side). The sidebar slides between the app menu and dated **chat-history folders**.
- **Hands-free voice** — a 🎙 **Converse** toggle in the composer turns chat into a spoken
  conversation: it listens (browser voice-activity detection), transcribes locally
  (faster-whisper), runs the *same* agent turn (so it uses tools and **opens/arranges windows
  as it works**), and speaks the reply back in a natural **Kokoro** voice (markdown/emoji stripped
  so it reads cleanly). Half-duplex, with an optional **wake word** ("Oceano …"). All local; the
  installer provisions the stack.
- **Floating windows** — Settings, **Brain** (Memory · Knowledge · Skills · Rivers ·
  Evals), **Workflows** (node canvas), Files explorer + editor (drag-and-drop **file/folder
  upload** into the workspace), Scheduler, Calendar, Researcher, semantic **Search**
  (memories · documents · conversations), **Notes** (Kanban), **Health** (live system
  dashboard), **Memory graph**, **Voice** (push-to-talk in / spoken replies out — natural local
  **Kokoro** neural voice, falling back to Piper), **Logs** (an **Activity** record of every
  unattended run — scheduled tasks, workflows, research — *with the agent's actual result*, plus a
  **System** tab tailing the `oceano` and `llama-swap` systemd journals so you can see if it's
  healthy without SSH), the
  **Live browser** (multi-tab — watch the agent research source-by-source), and a
  sandboxed **Preview**. Drag, resize, snap, minimize — and the set of open windows
  **reopens after a reload**.
- **Preview / artifacts** — when the agent writes an `.html` app, markdown, a Mermaid
  diagram, a Chart.js spec, or a `.slides` deck, a chip opens it rendered in an
  origin-isolated sandbox iframe (device presets + live reload).
- **Multiple endpoints** — local `llama.cpp` plus remote providers; models from all of
  them appear in the composer's picker — alongside **🧠 Claude** (when the CLI is present), which
  makes Claude the mind ([above](#claude-as-the-mind)).
- **Settings, deepened** — a **Voice** tab (pick the speak-out engine — Kokoro / Piper / auto —
  plus voice, speed, and wake word, and **browse & download Piper voices** from the Hugging Face
  catalog straight into `assets/voice/`), and a **Services** panel listing every piece (chat
  models · embeddings · SearXNG · voice TTS/STT · scheduler · Telegram) with a **per-service
  restart** where it's safe — reload a voice model, respawn the embedding child, restart Telegram,
  or restart `llama-swap` (via a scoped polkit rule, no password).

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
- **Locked maintenance jobs** — schedulable/toggleable (but not deletable) entries keep
  Oceano healthy: a skills review, the eval suite, memory hygiene, and a nightly
  **`[ INDEX ]` reindex** that re-syncs the doc / memory / skill / chat embeddings to disk
  (pruning what's gone, re-embedding what changed). The self-improving jobs are judged by
  the configured `improve` delegate, never the local model.
- **Background jobs & the queue** — every unattended job (workflows, scheduled tasks,
  research, evals, memory & index upkeep) registers in a live registry shown by a topbar
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

One script, two modes — **baremetal** (default, systemd) or **Docker** (containerized).
Both auto-detect the GPU and pick the matching `llama.cpp` backend
(**NVIDIA → CUDA**, **AMD/Intel → Vulkan**, **ROCm**, or **CPU**).

### Baremetal (default)

The installer detects the GPU and builds `llama.cpp` with the right backend, installs
dependencies, fetches the embedding model, sets up the Python venv + Playwright, brings
up SearXNG + `llama-swap`, and installs the systemd unit templated to your user/path.

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

The install also drops an **`oceano`** terminal client on your PATH — the rich, streamed
`cli.py` with rendered markdown + colored diffs, a slash-command **palette** (type `/`),
themes, and a tool-confirmation gate (on by default for OS-reaching tools); its sessions
persist to the same chat store as the web UI (`/chats` to resume). Just run `oceano`. Install/remove it on its own with `scripts/install-cli.sh`
(`--system` for `/usr/local/bin`, `--uninstall` to remove). In Docker, get the same client
with `docker compose exec oceano /app/venv/bin/python cli.py`.

### Docker (containerized)

`--docker` builds **one image** (`oceano:local`) with the detected GPU backend and brings
up the whole stack via `docker compose` — four services: `oceano` (engine, :8800),
`embeddings` (:8082, CPU), `llama-swap` (:8081, **GPU**), and `searxng`. Everything the
build needs is in the repo's `Dockerfile` (llama.cpp, llama-swap, Python deps, Chromium,
ffmpeg, espeak-ng); only the GPU models live outside it, in a host-mounted `./models`.

```bash
cp oceano.env.example oceano.env             # secrets (mounted at runtime, never baked in)
scripts/install.sh --docker                  # detect GPU → build image → compose up
scripts/install.sh --docker --with-models    # …and fetch the chat model into ./models
```

For an **NVIDIA** GPU it installs the NVIDIA Container Toolkit and applies
`deploy/docker/docker-compose.nvidia.yml`; for **Vulkan/ROCm** it passes the DRI/KFD
device nodes through (`docker-compose.vulkan.yml` / `.rocm.yml`); **CPU** needs no
override. The compose lives in `deploy/docker/`; manage it the usual way:

```bash
cd deploy/docker
docker compose -f docker-compose.yml -f docker-compose.nvidia.yml ps      # status
docker compose -f docker-compose.yml -f docker-compose.nvidia.yml logs -f # logs
```

Either way, open `http://127.0.0.1:8800` (published localhost-only — same posture as
baremetal; the other services stay on the internal network).

---

## Configuration

Everything is overridable via `OCEANO_*` environment variables (see `config.py`).
Secrets live in `oceano.env` (loaded by systemd; `chmod 600`, never committed).

| Variable | Default | Purpose |
|----------|---------|---------|
| `OCEANO_LLM_URL` | `http://127.0.0.1:8081/v1` | chat model endpoint (llama-swap) |
| `OCEANO_MODEL` | _(unset)_ | pin a model; unset → Oceano uses your primary (Settings → Delegation) or a model served in Brain → Rivers |
| `OCEANO_WORKSPACE` | `./workspace` | the agent's working folder |
| `OCEANO_SEARXNG` | `http://127.0.0.1:8080` | web search |
| `OCEANO_MAX_STEPS` | `25` | tool-call loop cap per turn |
| `OCEANO_DELEGATE_IDLE` / `_MAXTOTAL` / `_MAXTURNS` | `300` / `3600` / `60` | delegation idle timeout (s), absolute cap (s), max turns |
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
  your DBs/LLM/cloud metadata — re-validated on *every* browser navigation, so a fetched
  page can't 302/redirect its way to an internal address), and `wrap_untrusted` (fences web / doc / email text — and
  the passive research-note auto-injection — as data so the model never obeys instructions
  hidden inside it).
- **Workspace confinement** — file tools resolve relative to `workspace/` and refuse
  to escape it.
- **systemd hardening** — `NoNewPrivileges`, `ProtectHome=read-only` with `ReadWritePaths`
  limited to `workspace/`, `data/`, `skills/`, `assets/voice/`, and the `llama.cpp/` model dir,
  plus `PrivateTmp`. A **scoped polkit rule** lets the daemon restart only the `oceano-llama-swap`
  unit from the UI — `NoNewPrivileges` stays intact (no escalation; systemd does the work over D-Bus).
  The installer also offers to add the service user to the `systemd-journal` group so the Logs window's
  **System** tab can read the journal (read-only; skipped if already in `systemd-journal`/`adm`).
- **Localhost binding** + **login auth** on the web UI, with **optional TOTP 2FA** (RFC 6238 —
  authenticator app + QR; secret stays in the hardened `data/web.json`).
- **Secrets & tokens** — `data/web.json` (password hash, cookie-signing secret, endpoint API
  keys) is written atomically, so a crash can't corrupt it and lock you out; session cookies and
  the sandboxed-preview capability tokens are HMAC domain-separated, so one can't be replayed as
  the other; and destructive file ops refuse to act on the workspace root itself. When **Claude is
  the mind**, its tool bridge is localhost-only behind a header token (constant-time compared,
  persisted in a gitignored `data/.mind-token`), so only the launched `claude` process can reach it.

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
  rag.py             document indexing + semantic search (incremental, self-pruning)
  chats.py           chat persistence (dated folders) + conversation search (embeddings)
  skills.py          skill loading + catalog + independent review + learn-from-chat
  scheduler.py       cron tasks + on-demand run + ntfy + heartbeat
  reindex.py         locked job: re-sync doc / memory / skill / chat indexes to disk
  workflows.py       visual branching workflows (graph engine + run history)
  jobs.py            background-job registry + optional serialization gate (queue)
  delegate.py        delegation to Claude Code / a cloud model (per-role config) + the "mind" toggle
  mindbridge.py      Claude-as-mind: Oceano's tools exposed to the mind, executed in the daemon
  mcp_bridge_server.py  stdio MCP proxy Claude Code launches to reach those tools (token-gated)
  notes.py           Kanban scratchpad (JSON-persisted)
  evals.py           model eval suite (cases, leaderboard, scheduled runs)
  researcher.py      scheduled deep-dives → living docs → RAG
  calsync.py         calendar — agent-managed local events + read-only ICS feed sync
  voice.py           speech-in (faster-whisper) / speech-out (Kokoro → Piper → espeak) for web + Telegram
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
  install.sh         host bootstrapper (GPU detect → build → services; --docker for containers)
  install-cli.sh     installs the `oceano` terminal command (a cli.py launcher)
  serve-embeddings.sh  the embedding server launcher
systemd/             oceano.service + oceano-llama-swap.service + oceano-polkit.rules
deploy/searxng/      bundled SearXNG compose + settings
skills/              skill library (one folder per skill)
cli.py               rich terminal client (streamed; sessions persist to data/chats/; installed as `oceano`)
```

Runtime data (`data/`, `workspace/`), the virtualenv, and `oceano.env` are gitignored.

## License

MIT — see [LICENSE](LICENSE). Bundled third-party libraries (CodeMirror, marked,
DOMPurify, highlight.js, Mermaid, Chart.js, Drawflow) are credited with their own
licenses in [NOTICE](NOTICE).

---

*Everything runs on your box. The deep is local.* ≈
