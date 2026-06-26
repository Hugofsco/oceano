"""Telegram frontend for Oceano — a face over Agent with per-chat controls.

Two ways to run it:
  • Folded into the web daemon (default) — enable it in Settings → Telegram.
    The web server drives the bot on its own event loop (see web/telegram_runtime).
  • Standalone:  python -m oceano.telegram_bot   (reads OCEANO_TELEGRAM_* from env)

Commands (per chat):
  /model    pick the model this chat talks to (inline keyboard, or /model <name>)
  /status   metrics: model, context size, last-turn tokens/speed, tools, memory
  /compact  summarize the conversation so far and shrink the context
  /context  show context size; /context <n> auto-compacts past n messages
  /reset    clear the conversation

SECURITY: the agent can run shell commands, so the bot ONLY responds to user IDs
in the allow-list (`ALLOWED`). Everyone else is refused (and told their own ID so
the owner can allowlist them).
"""
import asyncio
import contextlib
import os
import re
import tempfile
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

import config
from oceano import llm, voice
from oceano.agent import Agent

# Live allow-list of Telegram user IDs. Seeded from env/config; the web runtime
# overrides this from Settings so allow-list edits apply without a code change.
ALLOWED = set(config.TELEGRAM_ALLOWED)

_agents = {}           # chat_id -> Agent (one conversation/history per chat)
_started_at = {}       # chat_id -> epoch when this session's agent was created
_last_stats = {}       # chat_id -> {tokens, tok_s, steps, model} of the last turn
_ctx_cap = {}          # chat_id -> auto-compact threshold (messages), or absent
_model_menu = {}       # chat_id -> [{id, base_url, endpoint}] backing the /model keyboard
_voice_on = {}         # chat_id -> True if replies should also be spoken (TTS)
_compactions = {}      # chat_id -> how many times the context was compacted this session

# The command menu Telegram shows when you type "/" (registered via set_my_commands).
BOT_COMMANDS = [
    ("model", "🧠 Pick the model for this chat"),
    ("voice", "🔊 Toggle spoken replies on/off"),
    ("status", "📊 Model, context & live metrics"),
    ("compact", "🗜 Summarize & shrink the context"),
    ("context", "📜 Show context size (or set auto-compact)"),
    ("reset", "🧹 Clear this conversation"),
    ("help", "❓ Show what I can do"),
]


def _agent_for(chat_id):
    if chat_id not in _agents:
        _agents[chat_id] = Agent()
        _agents[chat_id].mind_session_key = f"telegram:{chat_id}"
        _started_at[chat_id] = time.time()
    return _agents[chat_id]


async def _keep_typing(ctx, chat_id, action=ChatAction.TYPING):
    """Re-send the chat action every few seconds — Telegram's 'typing…' bubble only
    lasts ~5s, so a long agent run would otherwise look frozen. Cancel to stop it."""
    try:
        while True:
            try:
                await ctx.bot.send_chat_action(chat_id, action)
            except Exception:
                pass
            await asyncio.sleep(4.5)
    except asyncio.CancelledError:
        pass


@contextlib.asynccontextmanager
async def _typing(ctx, chat_id, action=ChatAction.TYPING):
    task = asyncio.create_task(_keep_typing(ctx, chat_id, action))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(Exception):
            await task


def _denied(update: Update):
    """Return a refusal string if the sender isn't allowlisted, else None."""
    uid = update.effective_user.id if update.effective_user else None
    if uid in ALLOWED:
        return None
    return (f"⛔ Not authorized. Your Telegram ID is {uid}.\n"
            f"Ask the owner to add it to the allow-list (Settings → Telegram).")


# ---------------- model selection ----------------
def _models():
    """All models across configured endpoints (lazy import → no import cycle)."""
    from oceano.web import server
    return [m for m in server.list_models() if not m.get("error")]


def _apply_model(agent, m):
    """Apply a /model pick. The synthetic '🧠 Claude' entry switches Oceano's resident MIND
    to Claude Code (a global setting, like the web picker) rather than setting a per-chat model;
    any real model hands the mind back to the local/cloud provider. The mind is honoured by
    Agent.run_stream, so this turns Claude on/off for Telegram too."""
    from oceano import delegate
    if m.get("mind") in ("claude", "codex"):
        delegate.set_mind(m.get("mind"))
        return
    delegate.set_mind("local")
    from oceano.web import server
    agent.model = m["id"]
    agent.base_url = m["base_url"]
    agent.api_key = server.endpoint_key(m["base_url"])


def _menu_models():
    """The /model picker entries — external minds first when present, then every reachable
    endpoint model. Mind rows are sentinels ({mind:...}), not real model ids."""
    from oceano import delegate
    entries = []
    if delegate.available():
        entries.append({"id": "🧠 Claude", "endpoint": "your subscription", "mind": "claude"})
    if delegate.codex_available():
        entries.append({"id": "🧠 Codex", "endpoint": "your Codex auth", "mind": "codex"})
    return entries + _models()


async def model_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if _denied(update):
        await update.message.reply_text(_denied(update)); return
    chat_id = update.effective_chat.id
    agent = _agent_for(chat_id)
    from oceano import delegate
    entries = await asyncio.to_thread(_menu_models)
    if not entries:
        await update.message.reply_text("No reachable models. Add an endpoint in Settings → Endpoints "
                                        "(or install Claude Code / Codex CLI for the resident mind options)."); return

    if ctx.args:                                   # /model <name> → set directly by id/substring (or a mind)
        q = " ".join(ctx.args).strip().lower()
        if q in ("claude", "🧠 claude", "claude code", "mind"):
            if not await asyncio.to_thread(delegate.available):
                await update.message.reply_text("🧠 Claude isn't available — install Claude Code (the `claude` CLI) first."); return
            _apply_model(agent, {"mind": "claude"})
            await update.message.reply_text("✅ Mind set to *🧠 Claude* — your subscription, wearing "
                                            "Oceano's persona + memory.", parse_mode="Markdown"); return
        if q in ("codex", "🧠 codex"):
            if not await asyncio.to_thread(delegate.codex_available):
                await update.message.reply_text("🧠 Codex isn't available — install the `codex` CLI and run `codex login` first."); return
            _apply_model(agent, {"mind": "codex"})
            await update.message.reply_text("✅ Mind set to *🧠 Codex* — your Codex/OpenAI auth, wearing "
                                            "Oceano's persona + memory.", parse_mode="Markdown"); return
        match = next((m for m in entries if not m.get("mind") and m["id"].lower() == q), None) \
            or next((m for m in entries if not m.get("mind") and q in m["id"].lower()), None)
        if not match:
            await update.message.reply_text(f"No model matches {q!r}. Send /model to choose from a list."); return
        _apply_model(agent, match)
        await update.message.reply_text(f"✅ Model set to `{match['id']}` — via `{match['endpoint']}`",
                                        parse_mode="Markdown"); return

    mind = await asyncio.to_thread(delegate.get_mind)
    _model_menu[chat_id] = entries[:24]            # back the keyboard; cap so it stays tappable
    def _is_cur(m):                                # the current pick: the active mind, else the per-chat model
        return (m.get("mind") == mind) if mind in ("claude", "codex") else (not m.get("mind") and m["id"] == agent.model)
    rows = [[InlineKeyboardButton(("✅ " if _is_cur(m) else "🔹 ") + m["id"][:46],
                                  callback_data=f"tgmodel:{i}")]
            for i, m in enumerate(_model_menu[chat_id])]
    cur = ("🧠 Claude" if mind == "claude" else ("🧠 Codex" if mind == "codex" else f"`{agent.model}`"))
    await update.message.reply_text(f"🧠 Current: {cur}\nTap one for this chat:",
                                    parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))


async def model_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if _denied(update):
        await q.answer("not authorized", show_alert=True); return
    chat_id = update.effective_chat.id
    try:
        idx = int(q.data.split(":", 1)[1])
        m = _model_menu.get(chat_id, [])[idx]
    except (ValueError, IndexError):
        await q.answer("stale menu — send /model again"); return
    _apply_model(_agent_for(chat_id), m)
    if m.get("mind") == "claude":
        await q.answer("mind: 🧠 Claude")
        await q.edit_message_text("✅ Mind set to *🧠 Claude* — your subscription, wearing Oceano's "
                                  "persona + memory.", parse_mode="Markdown")
    elif m.get("mind") == "codex":
        await q.answer("mind: 🧠 Codex")
        await q.edit_message_text("✅ Mind set to *🧠 Codex* — your Codex/OpenAI auth, wearing Oceano's "
                                  "persona + memory.", parse_mode="Markdown")
    else:
        await q.answer(f"model: {m['id']}")
        await q.edit_message_text(f"✅ Model set to `{m['id']}` — via `{m['endpoint']}`", parse_mode="Markdown")


# ---------------- context / compaction ----------------
def _ctx_metrics(agent):
    return agent.context_metrics()                  # (#messages, ~tokens) — shared with the web


def _compact(agent):
    """Summarize everything but the system message into one note, shrinking context.
    Returns the number of messages dropped. Delegates to Agent.compact (shared logic)."""
    return agent.compact()


async def compact_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if _denied(update):
        await update.message.reply_text(_denied(update)); return
    chat_id = update.effective_chat.id
    agent = _agent_for(chat_id)
    if len(agent.messages) <= 2:
        await update.message.reply_text("✨ Nothing to compact yet — the context is already small."); return
    async with _typing(ctx, chat_id):
        dropped = await asyncio.to_thread(_compact, agent)
    _compactions[chat_id] = _compactions.get(chat_id, 0) + 1
    n, approx = _ctx_metrics(agent)
    await update.message.reply_text(
        f"🗜 *Compacted* — folded {dropped} messages into a summary.\n"
        f"📜 Context now *{n}* msgs · ~*{approx}* tok  ·  🔁 {_compactions[chat_id]} this session",
        parse_mode="Markdown")


async def context_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if _denied(update):
        await update.message.reply_text(_denied(update)); return
    chat_id = update.effective_chat.id
    agent = _agent_for(chat_id)
    if ctx.args:                                   # /context <n> → set auto-compact threshold
        arg = ctx.args[0].lower()
        if arg in ("off", "0", "none"):
            _ctx_cap.pop(chat_id, None)
            await update.message.reply_text("🔕 Auto-compact off."); return
        try:
            n = max(4, int(arg))
        except ValueError:
            await update.message.reply_text("Usage: `/context <n>` (messages before auto-compact), or `/context off`",
                                            parse_mode="Markdown"); return
        _ctx_cap[chat_id] = n
        await update.message.reply_text(f"✅ Auto-compact set — I'll summarize once the chat passes *{n}* messages.",
                                        parse_mode="Markdown")
        return
    n, approx = _ctx_metrics(agent)
    st = _last_stats.get(chat_id, {})
    real_ctx = st.get("ctx")
    size = f"*{real_ctx:,}* tokens" if real_ctx else f"~*{approx:,}* tokens (estimate)"
    cap = _ctx_cap.get(chat_id)
    await update.message.reply_text(
        f"📜 *Context*\n• {n} messages\n• {size}\n• 🗜 {_compactions.get(chat_id, 0)} compactions this session\n"
        + (f"• ⏳ auto-compact at *{cap}* messages" if cap
           else "• auto-compact: off — `/context <n>` to set, `/compact` to shrink now"),
        parse_mode="Markdown")


# ---------------- status ----------------
def _fmt_age(secs):
    secs = int(secs)
    if secs < 90:
        return f"{secs}s"
    if secs < 5400:
        return f"{secs // 60}m"
    return f"{secs // 3600}h{(secs % 3600) // 60}m"


def _status_text(chat_id):
    from oceano import tools, memory, rag, delegate
    agent = _agent_for(chat_id)
    mind = delegate.get_mind()
    claude_mind = mind == "claude" and delegate.available()
    codex_mind = mind == "codex" and delegate.codex_available()
    n, approx = _ctx_metrics(agent)
    st = _last_stats.get(chat_id, {})
    try:
        docs = rag.stats().get("files", 0)
    except Exception:
        docs = 0
    # actual context size = the prompt tokens the model processed last turn (from the API);
    # fall back to the rough estimate until the first reply gives us a real number.
    real_ctx = st.get("ctx")
    ctx_size = f"*{real_ctx:,}* tok" if real_ctx else f"~*{approx:,}* tok"
    cap = f"  ·  auto-compact > {_ctx_cap[chat_id]} msgs" if chat_id in _ctx_cap else ""
    last = "—"
    if st:
        last = f"{st.get('tokens', 0)} tok"
        if st.get("tok_s"):
            last += f" · {st['tok_s']} tok/s"
        last += f" · {st.get('steps', 0)} tool-steps"
    vs = voice.status()
    spoken = "🔊 on" if _voice_on.get(chat_id) else "🔇 off"
    # Model/voice names can carry Markdown specials (Piper's en_GB-..._male-medium has underscores) —
    # wrap every dynamic identifier in backticks so legacy-Markdown parsing can't choke on it. An odd
    # '_' or '*' otherwise makes Telegram reject the WHOLE message with a 400, so /status sends nothing.
    stt_name = f" (`{vs['stt_model']}`)" if vs.get("stt") and vs.get("stt_model") else ""
    tts_name = f" (`{vs['tts_voice']}`)" if vs.get("tts") and vs.get("tts_voice") else ""
    lines = [
        "🌊 *Oceano — status*",
        ("🧠 *mind* · Claude (your subscription)" if claude_mind else ("🧠 *mind* · Codex (your auth)" if codex_mind else f"🧠 *model* · `{agent.model}`")),
        f"📜 *context* · {n} msgs · {ctx_size}{cap}",
        f"🗜 *compactions* · {_compactions.get(chat_id, 0)} this session",
        f"⚡ *last turn* · {last}",
        f"🛠 *tools* · {len(tools.schemas())} available",
        f"💾 *memory* · {memory.count()} facts · {docs} docs indexed",
        f"🎙️ *voice* · in {'✓' if vs['stt'] else '✗'}{stt_name} · out {spoken}{tts_name}",
        f"⏱ *session* · {_fmt_age(time.time() - _started_at.get(chat_id, time.time()))}",
    ]
    return "\n".join(lines)


async def status_cmd(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    if _denied(update):
        await update.message.reply_text(_denied(update)); return
    text = await asyncio.to_thread(_status_text, update.effective_chat.id)
    await update.message.reply_text(text, parse_mode="Markdown")


async def voice_cmd(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    """Toggle spoken (TTS) replies for this chat. Voice INPUT always works if STT is up."""
    if _denied(update):
        return
    chat_id = update.effective_chat.id
    st = voice.status()
    if not st["tts"]:
        await update.message.reply_text("🔇 No TTS engine available (install a Piper voice or espeak-ng)."); return
    on = not _voice_on.get(chat_id, False)
    _voice_on[chat_id] = on
    await update.message.reply_text(
        (f"🔊 Spoken replies ON — I'll talk back using `{st['tts_voice']}`."
         if on else "🔇 Spoken replies OFF — text only.")
        + ("\n(You can always send me a voice note and I'll transcribe it.)" if st["stt"] else ""),
        parse_mode="Markdown")


# ---------------- lifecycle commands ----------------
_HELP = ("🌊 *Oceano* online — send me a task, or a 🎙️ voice note and I'll transcribe it.\n\n"
         "🧠 /model — pick the model (or 🧠 Claude as the mind) for this chat\n"
         "🔊 /voice — toggle spoken replies on/off\n"
         "📊 /status — model, context & live metrics\n"
         "🗜 /compact — summarize & shrink the context\n"
         "📜 /context — context size (or set auto-compact)\n"
         "🧹 /reset — clear our conversation\n\n"
         "_Tip: type / to see the command menu._")


async def start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    denied = _denied(update)
    if denied:
        await update.message.reply_text(denied)
    else:
        await update.message.reply_text(_HELP, parse_mode="Markdown")


async def reset(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    if _denied(update):
        return
    cid = update.effective_chat.id
    for d in (_agents, _started_at, _last_stats, _model_menu, _compactions):
        d.pop(cid, None)                           # _voice_on persists — it's a preference, not history
    await update.message.reply_text("🧹 Conversation cleared — fresh start. 🌊")


def _run_collect(agent, text):
    """Drive the agent (tools + streaming) to completion, collecting the answer,
    tool names, and final stats — so the Telegram footer + /status can show metrics.
    Runs on the 'telegram' channel: web fetches use plain HTTP and the shared live
    browser is left untouched (the Telegram user can't see it — it's the web UI's)."""
    from oceano import tools
    answer, tools_used, stats = "", [], {}
    with tools.channel("telegram"):
        for ev in agent.run_stream(text):
            k = ev.get("type")
            if k == "token":
                answer += ev.get("text", "")
            elif k == "answer":
                answer = ev.get("text", answer)
            elif k == "tool_call":
                tools_used.append(ev.get("name", ""))
            elif k == "stats":
                stats = ev
    return answer.strip(), tools_used, stats


_MD_IMG = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
_BARE_IMG = re.compile(r"[\w./\-]+\.(?:png|jpe?g|gif|webp)", re.IGNORECASE)


def _extract_images(answer):
    """Pull image references out of the agent's answer → (text_without_md_images,
    [Path]). Catches markdown ![](path) and bare workspace image filenames that
    actually exist on disk, so charts and screenshots arrive as Telegram photos."""
    answer = answer or ""
    paths = _MD_IMG.findall(answer)
    text = _MD_IMG.sub("", answer)
    paths += [m.group(0) for m in _BARE_IMG.finditer(text)]
    out, seen = [], set()
    for p in paths:
        rp = p.strip().lstrip("./")
        if rp.startswith(("http://", "https://", "data:")):
            continue
        try:
            fp = (config.WORKSPACE / rp).resolve()
            if fp.is_file() and fp.is_relative_to(config.WORKSPACE) and fp not in seen:
                seen.add(fp)
                out.append(fp)
        except (OSError, ValueError):
            pass
    return text.strip(), out


def _chunks(text, limit=4000):
    """Split a long reply into pieces no larger than `limit`, preferring to cut at a paragraph,
    then a line, then a sentence, then a word boundary (hard-cut only as a last resort). Telegram
    caps a message at 4096 chars and the TTS engine at TTS_MAX_CHARS — without this a big response
    is silently truncated. Returns a list (possibly one element, or empty)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    out, rest, half = [], text, limit // 2
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"))           # paragraph / line break
        if cut < half:
            cut = max((window.rfind(s) for s in (". ", "! ", "? ", "。", "… ")), default=-1)
            if cut != -1:
                cut += 1                                              # keep the sentence terminator
        if cut < half:
            cut = window.rfind(" ")                                   # fall back to a word boundary
        if cut <= 0:
            cut = limit                                              # no boundary at all → hard cut
        out.append(rest[:cut].strip())
        rest = rest[cut:].lstrip()
    if rest.strip():
        out.append(rest.strip())
    return out


async def _send_voice(update, ctx, text):
    """Speak `text` as Telegram voice note(s). A long reply is split into sentence-aligned chunks
    (each under the TTS cap) and sent as sequential notes, so nothing is dropped (best-effort;
    silent if TTS is down). Capped to a sane number of notes to avoid flooding on a huge reply."""
    for part in _chunks(text, config.TTS_MAX_CHARS)[:8]:
        ogg = await asyncio.to_thread(voice.synthesize, part)
        if not ogg:
            continue
        try:
            with open(ogg, "rb") as fh:
                await ctx.bot.send_voice(update.effective_chat.id, voice=fh)
        except Exception:
            pass
        finally:
            try:
                os.remove(ogg)
            except OSError:
                pass


async def _respond(update, ctx, text):
    """Run the agent on `text` (telegram channel) and reply with text + any images,
    plus a spoken reply if /voice is on for this chat. Shared by typed and voice input.
    The 'typing…' bubble is kept alive for the whole run and stops once text is sent."""
    chat_id = update.effective_chat.id
    agent = _agent_for(chat_id)
    spoken, images = "", []
    async with _typing(ctx, chat_id):              # keep 'typing…' alive until we reply
        cap = _ctx_cap.get(chat_id)                # auto-compact if the context outgrew the cap
        if cap and len(agent.messages) > cap:
            await asyncio.to_thread(_compact, agent)
            _compactions[chat_id] = _compactions.get(chat_id, 0) + 1
        try:
            answer, used, stats = await asyncio.to_thread(_run_collect, agent, text)
        except Exception as e:
            await update.message.reply_text(f"⚠️ Something went wrong: {e}")
            return
        _last_stats[chat_id] = {"tokens": stats.get("tokens", 0), "tok_s": stats.get("tok_s"),
                                "steps": len(used), "model": agent.model, "ctx": stats.get("ctx")}
        spoken, images = _extract_images(answer)   # spoken = the clean text (no image markup)
        foot = []
        if used:
            foot.append("🛠 " + ", ".join(dict.fromkeys(used)))
        if stats.get("tokens"):
            foot.append(f"⚡ {stats['tokens']} tok" + (f" · {stats['tok_s']} tok/s" if stats.get("tok_s") else ""))
        body = spoken
        if foot:
            body = (body + "\n\n— " + "  ·  ".join(foot)).strip()
        if not body and not images:
            body = "🤔 (no response)"
        if body:
            for part in _chunks(body, 4000):               # deliver a long reply in full, not truncated
                await update.message.reply_text(part)      # ← typing stops when the last lands
    for fp in images[:6]:                          # deliver charts/screenshots as photos
        try:
            await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
            with open(fp, "rb") as fh:
                await ctx.bot.send_photo(chat_id, photo=fh)
        except Exception as e:
            await update.message.reply_text(f"⚠️ couldn't send image {fp.name}: {e}")
    if _voice_on.get(chat_id) and spoken.strip():  # spoken reply if the chat asked for it
        async with _typing(ctx, chat_id, ChatAction.RECORD_VOICE):
            await _send_voice(update, ctx, spoken)


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    denied = _denied(update)
    if denied:
        await update.message.reply_text(denied)
        return
    try:
        from oceano import workflows
        workflows.fire_keyword(update.message.text, "telegram")    # keyword-trigger workflows
    except Exception:
        pass
    await _respond(update, ctx, update.message.text)


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """A voice note (or audio clip) → transcribe → run it like a typed command."""
    denied = _denied(update)
    if denied:
        await update.message.reply_text(denied)
        return
    if not voice.stt_available():
        await update.message.reply_text("🎙️ Voice input isn't set up (faster-whisper missing)."); return
    media = update.message.voice or update.message.audio
    if not media:
        return
    chat_id = update.effective_chat.id
    tmp = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False).name
    async with _typing(ctx, chat_id):              # keep 'typing…' alive through transcription
        try:
            tgfile = await media.get_file()
            await tgfile.download_to_drive(custom_path=tmp)
            transcript = await asyncio.to_thread(voice.transcribe, tmp)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if not transcript:
            await update.message.reply_text("🎙️ Sorry, I couldn't make out that audio."); return
        await update.message.reply_text(f"🎙️ heard: {transcript}")   # plain text — ASR may contain * or _
    await _respond(update, ctx, transcript)


async def _post_init(app):
    """Register the slash-command menu so typing '/' shows the options + descriptions
    (Telegram's native autocomplete). Kept in sync with BOT_COMMANDS on every startup."""
    from telegram import BotCommand
    try:
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in BOT_COMMANDS])
    except Exception:
        pass


async def unknown_cmd(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    """A /command that isn't one of ours — nudge the user to the menu instead of going
    silent (typing '/' shows the same list as autocomplete)."""
    if _denied(update):
        await update.message.reply_text(_denied(update)); return
    await update.message.reply_text(
        "🤔 I don't know that command. I can do:\n"
        + "\n".join(f"/{c} — {d}" for c, d in BOT_COMMANDS)
        + "\n\n_Type / to see the menu._", parse_mode="Markdown")


def build_application(token):
    """Wire the handlers onto a fresh Application. Shared by standalone + web."""
    app = Application.builder().token(token).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("model", model_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("compact", compact_cmd))
    app.add_handler(CommandHandler("context", context_cmd))
    app.add_handler(CommandHandler("voice", voice_cmd))
    app.add_handler(CallbackQueryHandler(model_callback, pattern=r"^tgmodel:"))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))   # any unregistered /command (after the real ones)
    return app


def main():
    if not config.TELEGRAM_TOKEN:
        raise SystemExit("Set OCEANO_TELEGRAM_TOKEN (put it in oceano.env).")
    if not ALLOWED:
        print("WARNING: allow-list is empty — the bot will refuse everyone. Send "
              "it /start to learn your ID, then add it in Settings → Telegram.")
    app = build_application(config.TELEGRAM_TOKEN)
    print("Oceano Telegram bot running (polling).")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
