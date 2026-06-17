"""Oceano in the terminal — a rich, opencode-style chat client.

The simplest frontend, with the same comforts as the web UI / Telegram: streamed
reasoning + tool calls + answer, per-turn stats, and SESSIONS that persist to the shared
chat store (data/chats/) — so a conversation you start here shows up in the web UI too,
and vice-versa. Slash commands handle the session (type /help).

    python cli.py            # (use the venv: venv/bin/python cli.py)

The terminal layer is NOT hand-rolled: prompt_toolkit handles line editing, history and
the slash/@-path completion menu; rich handles markdown, the working spinner, diffs and
the live-streamed answer. Everything else runs through the same Agent.run_stream() the
other frontends use — frontends stay thin.
"""
import glob
import json
import os
import re
import secrets
import shutil
import sys
import time
from pathlib import Path

import config
from oceano import chats
from oceano.agent import Agent

# rich (output) — markdown, spinner, diffs, live stream. A hard dependency (requirements.txt).
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.text import Text

# prompt_toolkit (input) — line editing, history, completion. Soft import so a non-tty / a
# missing lib falls back to plain input().
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.styles import Style as PTStyle
    _HAS_PT = True
except Exception:                                   # pragma: no cover
    _HAS_PT = False

console = Console()
_isatty = sys.stdout.isatty()

_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_DATA = Path(__file__).resolve().parent / "data"
_HISTORY = _DATA / "cli_history"
_PREFS_PATH = _DATA / "cli_prefs.json"
_WEB_STORE = config.WORKSPACE.parent / "data" / "web.json"   # shared endpoint store (web UI)

# ---- themes: rich style strings (accent/user/dim/warn/err) + prompt_toolkit colors ----
THEMES = {
    "abyss":  dict(accent="cyan",         user="green",      dim="grey50",     warn="color(215)", err="red",
                   pt_arrow="ansigreen",  pt_rp="ansibrightblack"),
    "reef":   dict(accent="color(43)",    user="color(114)", dim="color(102)", warn="color(215)", err="color(203)",
                   pt_arrow="#87d7af",    pt_rp="#878787"),
    "aurora": dict(accent="color(141)",   user="color(121)", dim="color(103)", warn="color(222)", err="color(204)",
                   pt_arrow="#87ffaf",    pt_rp="#8787af"),
    "mono":   dict(accent="bright_white", user="white",      dim="grey50",     warn="white",      err="bright_red",
                   pt_arrow="ansiwhite",  pt_rp="ansibrightblack"),
}
THEME = "abyss"
ST = dict(THEMES["abyss"])           # current style strings (swapped by _apply_theme)
PREFS = {}                           # loaded in main(): theme · confirm_tools · bell

# Tools that can reach the OS OUTSIDE the workspace fence — gated behind a y/N prompt when
# confirm_tools is on (the default). File tools are already workspace-confined, so they're not
# gated (no value, just friction); these three are the real escape hatches.
RISKY_TOOLS = {"run_shell", "python_exec", "delegate"}

# block-letter wordmark (opencode-style banner), shown once at startup
LOGO = (
    "█████ █████ █████ █████ █   █ █████\n"
    "█   █ █     █     █   █ ██  █ █   █\n"
    "█   █ █     ████  █████ █ █ █ █   █\n"
    "█   █ █     █     █   █ █  ██ █   █\n"
    "█████ █████ █████ █   █ █   █ █████"
)

# Shown when no model is configured anywhere (no primary, no OCEANO_MODEL, nothing served).
_NO_MODEL_HINT = "no model configured — serve one in the web UI (Brain → Rivers)"


# ============================ tiny helpers ============================
def _termw():
    return max(36, min(shutil.get_terminal_size((80, 24)).columns, 100))


def _resolved_model():
    """What Oceano resolves the model to (primary / OCEANO_MODEL / a Rivers-served model);
    '' when nothing is configured. Never a hardcoded default."""
    try:
        from oceano import delegate
        return delegate.resolve_primary()["model"]
    except Exception:
        return config.MODEL


def banner():
    if _termw() >= 38:
        for ln in LOGO.splitlines():
            console.print(f"[bold {ST['accent']}]{ln}[/]")
    else:
        console.print(f"[bold {ST['accent']}]≈ Oceano[/]")
    m = _resolved_model()
    console.print(f"[{ST['dim']}]≈ a local agent in deep waters · model {escape(m or _NO_MODEL_HINT)}[/]")
    console.print(f"[{ST['dim']}]  /help · /palette · /chats to resume · Tab completes · Ctrl-C interrupts[/]")


def _uid():
    return "cli-" + secrets.token_hex(6)


def _hr(label=""):
    rule = "─" * 60
    console.print(f"[{ST['dim']}]{rule}[/]" if not label
                  else f"[{ST['dim']}]── {escape(label)} {('─' * max(0, 56 - len(label)))}[/]")


def _fmt_age(s):
    s = int(s); h, m = s // 3600, (s % 3600) // 60
    return (f"{h}h {m}m" if h else (f"{m}m" if m else f"{s}s"))


def _fmt_date(d):
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d or ""):
        return d or "—"
    from datetime import date
    today = date.today()
    try:
        delta = (today - date.fromisoformat(d)).days
    except ValueError:
        return d
    return "today" if delta == 0 else ("yesterday" if delta == 1 else d)


def _short(s, n=60):
    return (s or "").replace("\n", " ")[:n]


# ============================ diffs for file edits ============================
def _capture_diff(name, args_json):
    """At tool_call time (before the tool runs) read the current file so we can show a
    real before/after diff for write_file / edit_file once the result comes back."""
    if name not in ("write_file", "edit_file"):
        return None
    try:
        a = json.loads(args_json or "{}")
        from oceano.tools import _resolve
        p = _resolve(a.get("path", ""))
        before = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
        if name == "write_file":
            after = a.get("content", "")
        else:
            find, repl = a.get("find", ""), a.get("replace", "")
            after = before.replace(find, repl) if find and find in before else before
        return {"path": a.get("path", ""), "before": before, "after": after}
    except Exception:
        return None


def _print_diff(d, limit=60):
    import difflib
    a, b = d["before"].splitlines(), d["after"].splitlines()
    diff = list(difflib.unified_diff(a, b, lineterm="", n=2))[2:]   # drop the ---/+++ header
    if not diff:
        console.print(f"[{ST['dim']}]    · no content change[/]")
        return
    adds = sum(1 for l in diff if l.startswith("+"))
    dels = sum(1 for l in diff if l.startswith("-"))
    console.print(f"[{ST['dim']}]    ✎ {escape(d['path'])}[/]  [green]+{adds}[/] [red]-{dels}[/]")
    for ln in diff[:limit]:
        body = escape(ln)
        if ln.startswith("@@"):
            console.print(f"    [{ST['accent']}]{body}[/]")
        elif ln.startswith("+"):
            console.print(f"    [green]{body}[/]")
        elif ln.startswith("-"):
            console.print(f"    [red]{body}[/]")
        else:
            console.print(f"    [{ST['dim']}]{body}[/]")
    if len(diff) > limit:
        console.print(f"[{ST['dim']}]    … {len(diff) - limit} more diff lines[/]")


# ============================ @file attachments + $EDITOR ============================
def _expand_attachments(text):
    """Resolve `@path` tokens in a typed line into inline context the local (text-only)
    model can use: text files are inlined; images are described by the vision target."""
    refs = re.findall(r"(?:^|\s)@(\S+)", text)
    if not refs:
        return text
    blocks = []
    for ref in refs:
        p = Path(ref).expanduser()
        if not p.is_absolute() and not p.exists():
            cand = config.WORKSPACE / ref
            if cand.exists():
                p = cand
        if not p.is_file():
            console.print(f"[{ST['dim']}]  · @{escape(ref)}: no such file — skipped[/]")
            continue
        if p.suffix.lower() in _IMG_EXT:
            try:
                from oceano import delegate
                console.print(f"[{ST['dim']}]  · 🖼 describing {escape(p.name)} with the vision target…[/]")
                r = delegate.describe_image(str(p), text, role="vision")
                desc = (r.get("output") or "").strip() if r.get("ok") else f"(couldn't analyze: {r.get('error')})"
                blocks.append(f"[Attached image “{p.name}” — what the vision target sees:]\n{desc}")
            except Exception as e:
                console.print(f"[{ST['dim']}]  · vision failed for {escape(p.name)}: {escape(str(e))}[/]")
        else:
            try:
                from oceano import rag
                content = rag._read(p)
            except Exception:
                content = p.read_text(encoding="utf-8", errors="replace")
            if content.strip():
                blocks.append(f"[Attached file “{p.name}”:]\n{content[:6000]}")
                console.print(f"[{ST['dim']}]  · 📎 attached {escape(p.name)} ({len(content)} chars)[/]")
    return (text + "\n\n" + "\n\n".join(blocks)) if blocks else text


def _editor_compose(initial=""):
    """Open $EDITOR on a temp file for composing a long / multi-line prompt; return its text."""
    import subprocess
    import tempfile
    editor = os.environ.get("OCEANO_EDITOR") or os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
    fd, path = tempfile.mkstemp(suffix=".md", prefix="oceano-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(initial)
        subprocess.call([*editor.split(), path])
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except Exception as e:
        console.print(f"[{ST['dim']}]  · editor failed ({escape(str(e))})[/]")
        return ""
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ============================ prompt_toolkit: input + completion ============================
SLASH_CMDS = ["/chats", "/new", "/fork", "/rename", "/delete", "/palette", "/compact",
              "/context", "/status", "/model", "/theme", "/editor", "/copy",
              "/permission", "/bell", "/help", "/quit"]
ARG_CMDS = {"/model", "/theme", "/rename", "/delete", "/context", "/copy",
            "/palette", "/permission", "/bell"}
_CMD_DESC = {
    "/chats": "open saved conversations", "/new": "start a fresh conversation",
    "/compact": "shrink the context now", "/context": "context size · auto-compact",
    "/status": "model · context · session", "/model": "pick endpoint + model",
    "/editor": "compose in $EDITOR", "/palette": "fuzzy command launcher",
    "/theme": "switch color theme", "/rename": "rename this conversation",
    "/delete": "delete a conversation", "/fork": "branch into a new conversation",
    "/copy": "copy last reply (or code)", "/permission": "toggle tool confirmations",
    "/bell": "toggle the completion beep", "/help": "this list", "/quit": "leave",
}


def _path_complete(frag):
    out = []
    for m in glob.glob(os.path.expanduser(frag) + "*")[:50]:
        out.append(m + ("/" if os.path.isdir(m) else ""))
    return out


if _HAS_PT:
    class _OceanoCompleter(Completer):
        """Slash commands at the line start, theme names after `/theme `, and @path tokens
        anywhere — shown in prompt_toolkit's own popup menu (no hand-rolled dropdown)."""

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            at = re.search(r"(?:^|\s)@(\S*)$", text)
            if at is not None:
                frag = at.group(1)
                for p in _path_complete(frag):
                    yield Completion(p, start_position=-len(frag),
                                     display=p, display_meta="file" if not p.endswith("/") else "dir")
                return
            if text.startswith("/theme "):
                frag = text[len("/theme "):]
                for n in THEMES:
                    if n.startswith(frag.lower()):
                        yield Completion(n, start_position=-len(frag), display=n)
                return
            if text.startswith("/") and " " not in text:
                for c in SLASH_CMDS:
                    if c.startswith(text):
                        yield Completion(c, start_position=-len(text),
                                         display=c, display_meta=_CMD_DESC.get(c, ""))

_SESSION = None


def _session():
    global _SESSION
    if _SESSION is None and _HAS_PT:
        try:
            _HISTORY.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        _SESSION = PromptSession(history=FileHistory(str(_HISTORY)),
                                 completer=_OceanoCompleter(),
                                 complete_while_typing=True,
                                 enable_open_in_editor=True)
    return _SESSION


def _pt_style():
    return PTStyle.from_dict({"arrow": f"{ST['pt_arrow']} bold", "rprompt": ST["pt_rp"]})


def _prompt(model=""):
    """Read one line. prompt_toolkit on a tty (editing/history/completion + a dim model
    rprompt); plain input() otherwise. Ctrl-C → KeyboardInterrupt, Ctrl-D → EOFError."""
    if not (_isatty and _HAS_PT):
        return input("› ")
    rp = FormattedText([("class:rprompt", f" {model} " if model else "")])
    return _session().prompt(FormattedText([("class:arrow", "❯ ")]),
                             rprompt=rp, style=_pt_style())


# ============================ prefs + themes ============================
def _load_prefs():
    global PREFS
    try:
        PREFS = json.loads(_PREFS_PATH.read_text(encoding="utf-8"))
    except Exception:
        PREFS = {}
    PREFS.setdefault("theme", "abyss")
    PREFS.setdefault("confirm_tools", True)          # default ON: confirm OS-reaching tools (CLI has no OS sandbox)
    PREFS.setdefault("bell", True)
    return PREFS


def _save_prefs():
    try:
        _PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PREFS_PATH.write_text(json.dumps(PREFS, indent=2), encoding="utf-8")
    except Exception:
        pass


def _apply_theme(name):
    global ST, THEME, HELP
    t = THEMES.get(name)
    if not t:
        return False
    ST = dict(t)
    THEME = name
    HELP = _build_help()
    return True


# ============================ fuzzy matcher (command palette) ============================
def _fuzzy(query, text):
    q, t = query.lower(), text.lower()
    if not q:
        return 0.0
    score, ti, last = 0.0, 0, -2
    for ch in q:
        idx = t.find(ch, ti)
        if idx == -1:
            return None
        score += 6 if idx == last + 1 else 1
        if idx == 0 or t[idx - 1] in " /_-.:":
            score += 4
        last, ti = idx, idx + 1
    return score - len(t) * 0.05


# ============================ clipboard ============================
def _clip_cmd():
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"], ["pbcopy"]):
        if shutil.which(cmd[0]):
            return cmd
    return None


def _copy_to_clipboard(text):
    cmd = _clip_cmd()
    if cmd:
        import subprocess
        try:
            subprocess.run(cmd, input=text.encode("utf-8"), check=True)
            return True
        except Exception:
            pass                                            # fall through to OSC 52
    if _isatty:                                             # works over SSH / headless in modern terminals
        import base64
        b = base64.b64encode(text.encode("utf-8")).decode("ascii")
        sys.stdout.write(f"\033]52;c;{b}\a"); sys.stdout.flush()
        return True
    console.print(f"[{ST['dim']}]no clipboard available — install wl-copy, xclip, or xsel.[/]")
    return False


# ============================ endpoints + models (the /model picker) ============================
def _cli_endpoints():
    """Configured endpoints (name, base_url, api_key) from the shared store, else the local
    config endpoint as a fallback so /model always has at least one thing to pick."""
    try:
        d = json.loads(_WEB_STORE.read_text(encoding="utf-8"))
        eps = [e for e in d.get("endpoints", []) if e.get("base_url")]
        if eps:
            return eps
    except Exception:
        pass
    return [{"name": "Local (llama.cpp)", "base_url": config.LLM_BASE_URL, "api_key": ""}]


def _models_for(ep):
    """Model ids an endpoint serves (GET base_url/models). [] if unreachable / none."""
    import requests
    try:
        h = {"Authorization": f"Bearer {ep['api_key']}"} if ep.get("api_key") else {}
        r = requests.get(ep["base_url"].rstrip("/") + "/models", headers=h, timeout=8)
        r.raise_for_status()
        ids = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
        return sorted(ids, key=str.lower)
    except Exception:
        return []


def _pick(prompt_label):
    """Read a 1-based numeric choice (Enter / Ctrl-C cancels). Returns the int, or None."""
    try:
        sel = input(prompt_label).strip()
    except (EOFError, KeyboardInterrupt):
        console.print()
        return None
    return int(sel) if sel.isdigit() else None


# ============================ tool permission gate ============================
def _confirm_tool(name, args, s):
    """Ask before a side-effecting tool runs (when confirm_tools is on). y / N / a=always."""
    if s.trust:
        return True
    try:
        a = json.loads(args or "{}")
        detail = a.get("path") or a.get("cmd") or a.get("command") or a.get("code") or a.get("query") or ""
    except Exception:
        detail = (args or "")[:60]
    detail = _short(str(detail), 70)
    console.print(f"[{ST['warn']}]  ⚠ allow [bold]{escape(name)}[/]?[/]"
                  + (f" [{ST['dim']}]{escape(detail)}[/]" if detail else "")
                  + f"  [{ST['dim']}]\\[y / N / a=always][/]")
    try:
        ans = input("    › ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print()
        return False
    if ans in ("a", "always"):
        s.trust = True
        return True
    return ans in ("y", "yes")


# ============================ a session ============================
class Session:
    """One conversation: an Agent (the live LLM context) + the display transcript we persist
    to data/chats/ in the SAME shape the web UI saves, so they interoperate."""

    def __init__(self):
        self.new()

    def new(self):
        self.id = _uid()
        self.title = "New voyage"
        self.messages = []
        self.agent = Agent()
        self.compactions = 0
        self.cap = None
        self.started = time.time()
        self.trust = False

    def load(self, sid):
        rec = chats.get(sid)
        if not rec:
            return False
        self.id = sid
        self.title = rec.get("title") or "Voyage"
        self.messages = rec.get("messages") or []
        self.agent = Agent()
        for m in self.messages:
            if m.get("role") == "user":
                self.agent.messages.append({"role": "user", "content": m.get("content", "")})
            elif m.get("role") == "assistant" and m.get("content"):
                self.agent.messages.append({"role": "assistant", "content": m.get("content", "")})
        self.compactions = 0
        self.cap = None
        self.started = time.time()
        self.trust = False
        return True

    def save(self):
        if self.messages:
            chats.save(self.id, self.title, self.messages)


# ============================ rendering ============================
def _render_transcript(messages):
    for m in messages:
        role = m.get("role")
        if role == "user":
            console.print(f"[bold {ST['user']}]you ›[/] {escape(m.get('content', ''))}")
        elif role == "thinking":
            txt = " ".join((m.get("text") or "").split())[:280]
            console.print(f"[{ST['dim']} italic]🤔 {escape(txt)}[/]")
        elif role == "tool":
            console.print(f"  [{ST['accent']}]→ {escape(m.get('name') or '')}[/]"
                          f"[{ST['dim']}]({escape(_short(m.get('args') or ''))})[/]")
            prev = _short(m.get("result") or "", 120)
            if prev:
                console.print(f"[{ST['dim']}]    {escape(prev)}[/]")
        elif role == "assistant":
            body = m.get("content", "")
            console.print(f"[bold {ST['accent']}]oceano ›[/]")
            console.print(Markdown(body) if body.strip() else Text(""))
    console.print()


def _stats_line(ev):
    bits = []
    if ev.get("ctx"):
        bits.append(f"{round(ev['ctx'] / 1000, 1)}k ctx")
    if ev.get("tokens"):
        bits.append(f"{ev['tokens']} tok")
    if ev.get("tok_s"):
        bits.append(f"{ev['tok_s']} tok/s")
    return f"[{ST['dim']}]  · {' · '.join(bits)}[/]" if bits else ""


def _turn(s, text):
    """Run one streamed turn and render it with rich: a spinner during genuine waits, dim
    reasoning, tool calls + diffs, and the final answer streamed live as markdown (rich owns
    the redraw — no cursor math). Folds the turn into the transcript and persists it."""
    s.messages.append({"role": "user", "content": text})
    if s.title in ("", "New voyage"):
        s.title = text[:60]
    if s.cap and len(s.agent.messages) > s.cap:           # auto-compact like Telegram/web
        dropped = s.agent.compact()
        if dropped:
            s.compactions += 1
            console.print(f"[{ST['dim']}]🗜 auto-compacted {dropped} messages (over {s.cap})[/]")

    augmented = _expand_attachments(text)
    t0 = time.time()
    asst = answer_buf = ""
    status = answer_live = None
    reasoning_open = False
    pending_diff = None

    def stop_status():
        nonlocal status
        if status is not None:
            status.stop(); status = None

    def start_status(msg):
        nonlocal status
        stop_status()
        status = console.status(f"[{ST['dim']}]{escape(msg)}[/]", spinner="dots")
        status.start()

    def close_reasoning():
        nonlocal reasoning_open
        if reasoning_open:
            console.print()
            reasoning_open = False

    def open_answer():
        # Stream the answer as PLAIN text in a transient Live (no per-token markdown re-parse,
        # so no flicker on half-typed **bold**/```fences). On close the transient region is
        # erased and the whole answer is rendered ONCE as markdown — clean, no duplication.
        nonlocal answer_live
        if answer_live is None:
            console.print(f"[bold {ST['accent']}]oceano ›[/]")
            answer_live = Live(Text(""), console=console, refresh_per_second=12,
                               transient=True, vertical_overflow="visible")
            answer_live.start()

    def close_answer():
        nonlocal answer_live, answer_buf
        if answer_live is not None:
            answer_live.stop()                       # transient → erases the live plain-text region
            answer_live = None
            if answer_buf.strip():
                console.print(Markdown(answer_buf))  # final answer, rendered once
            answer_buf = ""

    gen = s.agent.run_stream(augmented)
    try:
        start_status("thinking…")
        for ev in gen:
            t = ev.get("type")
            if t == "reasoning":
                stop_status(); close_answer()
                if not reasoning_open:
                    console.print(Text("🤔 ", style=f"{ST['dim']} italic"), end="", soft_wrap=True)
                    reasoning_open = True
                console.print(Text(ev["text"], style=f"{ST['dim']} italic"), end="", soft_wrap=True)
            elif t == "token":
                stop_status(); close_reasoning()
                open_answer()
                asst += ev["text"]; answer_buf += ev["text"]
                answer_live.update(Text(answer_buf))
            elif t == "tool_call":
                stop_status(); close_reasoning(); close_answer()
                name, args = ev["name"], ev.get("args") or ""
                console.print(f"  [{ST['accent']}]→ {escape(name)}[/] [{ST['dim']}]({escape(_short(args))})[/]")
                if PREFS.get("confirm_tools") and _isatty and name in RISKY_TOOLS:
                    if not _confirm_tool(name, args, s):
                        gen.close()                          # GeneratorExit unwinds before the tool runs
                        console.print(f"[{ST['warn']}]  ✋ denied — stopping this turn.[/]")
                        break
                pending_diff = _capture_diff(name, args)
                start_status(f"running {name}…")
            elif t == "tool_progress":
                # live updates from a streaming tool (the delegate) — show what it's doing
                stop_status(); close_reasoning(); close_answer()
                if ev.get("kind") == "text" and (ev.get("text") or "").strip():
                    console.print(Text("    " + ev["text"].strip(), style=f"{ST['dim']} italic"),
                                  soft_wrap=True)
                elif ev.get("kind") == "tool":
                    d = ev.get("detail") or ""
                    console.print(f"    [{ST['dim']}]↳ {escape(ev.get('tool', 'tool'))}"
                                  f"{(' · ' + escape(d)) if d else ''}[/]")
                start_status("delegating…")
            elif t == "tool_result":
                stop_status(); close_reasoning(); close_answer()
                res = ev.get("result") or ""
                s.messages.append({"role": "tool", "name": ev["name"], "args": "", "result": res})
                if pending_diff and not res.startswith(("ERROR", "(no such")):
                    _print_diff(pending_diff)
                else:
                    prev = _short(res, 120)
                    if prev:
                        console.print(f"[{ST['dim']}]    {escape(prev)}[/]")
                pending_diff = None
                start_status("thinking…")
            elif t == "answer_done":
                close_answer()
            elif t == "stats":
                stop_status(); close_reasoning(); close_answer()
                line = _stats_line(ev)
                if line:
                    console.print(line)
            elif t == "error":
                stop_status(); close_reasoning(); close_answer()
                console.print(f"[{ST['err']}]⚠ {escape(ev.get('error') or ev.get('message') or 'error')}[/]")
        stop_status(); close_reasoning(); close_answer()
    except KeyboardInterrupt:
        stop_status(); close_reasoning(); close_answer()
        console.print(f"[{ST['warn']}]⏹ interrupted[/]")
    if asst.strip():
        s.messages.append({"role": "assistant", "content": asst})
    console.print()
    if PREFS.get("bell") and _isatty and (time.time() - t0) > 8:
        sys.stdout.write("\a"); sys.stdout.flush()          # ping when a long turn finishes
    s.save()


# ============================ slash commands ============================
def _cmd_chats(s):
    items = chats.list_all()
    if not items:
        console.print(f"[{ST['dim']}]no saved conversations yet — they appear here once you chat.[/]")
        return
    console.print(f"\n[bold]Conversations[/] [{ST['dim']}](newest first)[/]")
    for i, c in enumerate(items[:40], 1):
        marker = f"[{ST['accent']}]●[/]" if c["id"] == s.id else " "
        console.print(f" {marker} [bold]{i:2}[/]  [{ST['dim']}]{_fmt_date(c.get('date')):<9}[/] "
                      f"{escape(c.get('title', 'Untitled')[:54])} [{ST['dim']}]· {c.get('count', 0)} msgs[/]")
    n = _pick(f"open # (Enter to cancel): ")
    if n and 1 <= n <= min(len(items), 40):
        sid = items[n - 1]["id"]
        if sid == s.id:
            console.print(f"[{ST['dim']}]already in this conversation.[/]"); return
        s.save()
        if s.load(sid):
            console.print(f"\n[{ST['accent']}]≈ resumed “{escape(s.title)}”[/]\n")
            _hr(); _render_transcript(s.messages); _hr()
    else:
        console.print(f"[{ST['dim']}]cancelled.[/]")


def _cmd_status(s):
    n, approx = s.agent.context_metrics()
    console.print(
        f"\n[bold]🌊 Oceano — status[/]\n"
        f"  [{ST['dim']}]model[/]     {escape(s.agent.model or _NO_MODEL_HINT)}\n"
        f"  [{ST['dim']}]session[/]   {escape(s.title)}  [{ST['dim']}]({s.id})[/]\n"
        f"  [{ST['dim']}]context[/]   {n} messages · ~{approx} tok"
        + (f"  · auto-compact > {s.cap}" if s.cap else "") + "\n"
        f"  [{ST['dim']}]compacted[/] {s.compactions}× this session\n"
        f"  [{ST['dim']}]uptime[/]    {_fmt_age(time.time() - s.started)}\n"
        f"  [{ST['dim']}]prefs[/]     theme {THEME} · confirm-tools "
        f"{'on' if PREFS.get('confirm_tools') else 'off'} · bell "
        f"{'on' if PREFS.get('bell') else 'off'}")


def _cmd_model(s, arg):
    """Switch the chat model. `/model <name>` is the quick path (set by name on the current
    endpoint); `/model` with no arg opens an interactive picker: choose an endpoint, then a
    model on it — applied to THIS session (model + endpoint + key, so remote models route)."""
    if arg:
        s.agent.model = arg
        console.print(f"[{ST['dim']}]model → {escape(arg)}[/]")
        return

    eps = _cli_endpoints()
    cur_base = s.agent.base_url or config.LLM_BASE_URL
    console.print(f"\n[bold]Model[/]  [{ST['dim']}]current: {escape(s.agent.model or _NO_MODEL_HINT)}[/]")

    if len(eps) == 1:
        ep = eps[0]
        console.print(f"[{ST['dim']}]endpoint: {escape(ep['name'])} ({escape(ep['base_url'])})[/]")
    else:
        console.print(f"[{ST['dim']}]Choose an endpoint:[/]")
        for i, e in enumerate(eps, 1):
            mark = f"[{ST['accent']}]●[/]" if e["base_url"] == cur_base else " "
            console.print(f" {mark} [bold]{i:2}[/]  {escape(e['name'])}  [{ST['dim']}]{escape(e['base_url'])}[/]")
        n = _pick("endpoint # (Enter to cancel): ")
        if not (n and 1 <= n <= len(eps)):
            console.print(f"[{ST['dim']}]cancelled.[/]"); return
        ep = eps[n - 1]

    with console.status(f"[{ST['dim']}]listing {escape(ep['name'])}…[/]", spinner="dots"):
        models = _models_for(ep)
    if not models:
        console.print(f"[{ST['dim']}]no models from {escape(ep['name'])} — unreachable, or none served "
                      f"(check the endpoint/key in the web UI).[/]")
        return
    console.print(f"[{ST['dim']}]Models on {escape(ep['name'])}:[/]")
    for i, m in enumerate(models, 1):
        mark = f"[{ST['accent']}]●[/]" if (m == s.agent.model and ep["base_url"] == cur_base) else " "
        console.print(f" {mark} [bold]{i:2}[/]  {escape(m)}")
    n = _pick("model # (Enter to cancel): ")
    if not (n and 1 <= n <= len(models)):
        console.print(f"[{ST['dim']}]cancelled.[/]"); return

    chosen = models[n - 1]
    s.agent.model = chosen
    s.agent.base_url = ep["base_url"]
    s.agent.api_key = ep.get("api_key") or None
    console.print(f"[{ST['accent']}]🌊 model → {escape(chosen)}[/] [{ST['dim']}]@ {escape(ep['name'])} · this session[/]")


def _cmd_context(s, arg):
    if arg:
        if arg.lower() in ("off", "0", "none"):
            s.cap = None; console.print(f"[{ST['dim']}]🔕 auto-compact off.[/]"); return
        if arg.isdigit() and int(arg) > 0:
            s.cap = int(arg); console.print(f"[{ST['dim']}]✅ auto-compact at > {s.cap} messages.[/]"); return
        console.print(f"[{ST['dim']}]usage: /context <n> | off[/]"); return
    n, approx = s.agent.context_metrics()
    console.print(f"[{ST['dim']}]📜 {n} messages · ~{approx} tok"
                  + (f" · auto-compact > {s.cap}" if s.cap else " · auto-compact off") + "[/]")


def _cmd_compact(s):
    dropped = s.agent.compact()
    if dropped:
        s.compactions += 1
        n, approx = s.agent.context_metrics()
        console.print(f"[{ST['dim']}]🗜 folded {dropped} messages into a summary · now {n} msgs · ~{approx} tok[/]")
    else:
        console.print(f"[{ST['dim']}]✨ nothing to compact — the context is already small.[/]")


def _cmd_palette(s, query):
    """Fuzzy command palette — filter every command + recent chat, then pick by number."""
    cands = [("cmd", c, f"[{ST['accent']}]{c}[/]", _CMD_DESC.get(c, "")) for c in SLASH_CMDS]
    for c in chats.list_all()[:30]:
        if c["id"] != s.id:
            cands.append(("chat", c["id"],
                          f"[{ST['dim']}]{_fmt_date(c.get('date'))}[/] {escape(c.get('title', 'Untitled')[:46])}",
                          f"{c.get('count', 0)} msgs"))
    if query:
        scored = []
        for kind, val, label, desc in cands:
            plain = re.sub(r"\[[^\]]*\]", "", label)
            sc = _fuzzy(query, plain + " " + desc)
            if sc is not None:
                scored.append((sc, kind, val, label, desc))
        scored.sort(key=lambda x: -x[0])
        rows = [(k, v, l, d) for _, k, v, l, d in scored[:15]]
    else:
        rows = [(k, v, l, d) for k, v, l, d in cands[:15]]
    if not rows:
        console.print(f"[{ST['dim']}]no matches for “{escape(query)}”.[/]"); return
    console.print(f"\n[bold]Palette[/]" + (f" [{ST['dim']}]· {escape(query)}[/]" if query else ""))
    for i, (kind, val, label, desc) in enumerate(rows, 1):
        tag = f"[{ST['dim']}]cmd [/]" if kind == "cmd" else f"[{ST['accent']}]chat[/]"
        console.print(f"  [bold]{i:2}[/] {tag}  {label}  [{ST['dim']}]{escape(desc)}[/]")
    n = _pick("pick # (Enter to cancel): ")
    if not (n and 1 <= n <= len(rows)):
        console.print(f"[{ST['dim']}]cancelled.[/]"); return
    kind, val, _, _ = rows[n - 1]
    if kind == "cmd":
        _handle(val, s)
    elif s.id != val:
        s.save()
        if s.load(val):
            console.print(f"\n[{ST['accent']}]≈ resumed “{escape(s.title)}”[/]\n")
            _hr(); _render_transcript(s.messages); _hr()


def _cmd_theme(arg):
    if not arg:
        names = "  ".join((f"[bold]{n}[/]" if n == THEME else n) for n in THEMES)
        console.print(f"[{ST['dim']}]theme: {THEME} · available:[/] {names} [{ST['dim']}]· /theme <name>[/]")
        return
    if _apply_theme(arg.lower()):
        PREFS["theme"] = THEME; _save_prefs()
        console.print(f"[{ST['accent']}]🎨 theme → {THEME}[/]")
    else:
        console.print(f"[{ST['dim']}]unknown theme “{escape(arg)}” — try: {', '.join(THEMES)}[/]")


def _cmd_rename(s, arg):
    if not arg:
        console.print(f"[{ST['dim']}]usage: /rename <new title>[/]"); return
    s.title = arg[:80]
    if s.messages:
        rec = chats.get(s.id) or {}
        chats.save(s.id, s.title, s.messages, created=rec.get("created"))
    console.print(f"[{ST['dim']}]✎ renamed → {escape(s.title)}[/]")


def _cmd_delete(s, arg):
    items = chats.list_all()
    if arg.isdigit():
        i = int(arg)
        if not (1 <= i <= len(items)):
            console.print(f"[{ST['dim']}]no #{i} in the list.[/]"); return
        target = items[i - 1]
        if target["id"] != s.id:
            try:
                ok = input(f"delete “{target.get('title', '')[:40]}”? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                console.print(); return
            if ok in ("y", "yes"):
                chats.delete(target["id"]); console.print(f"[{ST['dim']}]🗑 deleted.[/]")
            else:
                console.print(f"[{ST['dim']}]cancelled.[/]")
            return
    try:
        ok = input(f"delete THIS conversation “{s.title[:40]}”? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print(); return
    if ok in ("y", "yes"):
        chats.delete(s.id); s.new(); console.print(f"[{ST['dim']}]🗑 deleted — started a fresh voyage.[/]")
    else:
        console.print(f"[{ST['dim']}]cancelled.[/]")


def _cmd_fork(s):
    if not s.messages:
        console.print(f"[{ST['dim']}]nothing to fork yet — say something first.[/]"); return
    s.save()
    s.id = _uid()
    s.title = f"{s.title} (fork)"[:80]
    s.trust = False
    s.save()
    console.print(f"[{ST['accent']}]⑂ forked — continuing in a new conversation: {escape(s.title)}[/]")


def _cmd_copy(s, arg):
    last = next((m.get("content", "") for m in reversed(s.messages) if m.get("role") == "assistant"), "")
    if not last:
        console.print(f"[{ST['dim']}]no assistant reply to copy yet.[/]"); return
    if arg.lower() in ("code", "```"):
        blocks = re.findall(r"```[\w-]*\n(.*?)```", last, flags=re.DOTALL)
        if not blocks:
            console.print(f"[{ST['dim']}]no code block in the last reply.[/]"); return
        payload, what = blocks[-1].rstrip("\n"), "last code block"
    else:
        payload, what = last, "last reply"
    if _copy_to_clipboard(payload):
        console.print(f"[{ST['dim']}]📋 copied {what} ({len(payload)} chars).[/]")


def _cmd_toggle(s, key, arg, label):
    a = arg.lower()
    if a in ("on", "true", "1", "yes"):
        PREFS[key] = True
    elif a in ("off", "false", "0", "no"):
        PREFS[key] = False
    else:
        PREFS[key] = not PREFS.get(key)
    if key == "confirm_tools" and PREFS[key]:
        s.trust = False
    _save_prefs()
    console.print(f"[{ST['dim']}]{label}: {'on' if PREFS[key] else 'off'}[/]")


def _build_help():
    # NB: literal square-bracket hints (\[#], \[name], …) MUST escape the opening bracket as
    # \[ — rich would otherwise read them as markup tags and silently drop them.
    a, d = ST["accent"], ST["dim"]
    return (
        f"[bold]Commands[/]\n"
        f"  [{a}]/chats[/]            list & open saved conversations\n"
        f"  [{a}]/new[/] [{d}](/reset)[/]     start a fresh conversation\n"
        f"  [{a}]/fork[/]             branch the current chat into a new one\n"
        f"  [{a}]/rename[/] <title>   rename this conversation\n"
        f"  [{a}]/delete[/] \\[#]       delete this conversation (or # from the list)\n"
        f"  [{a}]/palette[/] \\[query]  fuzzy launcher over commands & chats\n"
        f"  [{a}]/compact[/]          summarize & shrink the context now\n"
        f"  [{a}]/context[/] \\[n|off]  show context size, or set/clear auto-compact\n"
        f"  [{a}]/status[/]           model · context · session info\n"
        f"  [{a}]/model[/] \\[name]     pick endpoint → model (or /model <name> to set by name)\n"
        f"  [{a}]/theme[/] \\[name]     color theme: {', '.join(THEMES)}\n"
        f"  [{a}]/editor[/]           compose a long prompt in $EDITOR\n"
        f"  [{a}]/copy[/] \\[code]      copy the last reply (or its last code block)\n"
        f"  [{a}]/permission[/] \\[on|off]  confirm before side-effecting tools run\n"
        f"  [{a}]/bell[/] \\[on|off]    beep when a long turn finishes\n"
        f"  [{a}]/help[/]             this list\n"
        f"  [{a}]/quit[/] [{d}](/q)[/]        leave (the conversation is already saved)\n"
        f"[{d}]Type [/][{a}]@path[/][{d}] to attach a file/image · [/][{a}]Tab[/][{d}] completes commands, paths & themes.[/]\n"
        f"[{d}]Anything else is a message. Sessions persist to data/chats/ — reachable from the web UI too.[/]")


HELP = _build_help()


def _handle(line, s):
    """Returns False to keep looping; raises SystemExit on /quit."""
    parts = line[1:].split()
    cmd = (parts[0] if parts else "").lower()
    arg = " ".join(parts[1:]).strip()
    if cmd in ("quit", "exit", "q"):
        s.save(); console.print(f"[{ST['accent']}]≈ bye[/]"); raise SystemExit(0)
    elif cmd in ("new", "reset", "clear"):
        s.save(); s.new(); console.print(f"[{ST['accent']}]≈ new voyage[/]")
    elif cmd == "chats":
        _cmd_chats(s)
    elif cmd == "status":
        _cmd_status(s)
    elif cmd == "context":
        _cmd_context(s, arg)
    elif cmd == "compact":
        _cmd_compact(s)
    elif cmd == "model":
        _cmd_model(s, arg)
    elif cmd == "editor":
        body = _editor_compose(arg)
        if body:
            console.print(f"[bold {ST['user']}]you ›[/] [{ST['dim']}](from $EDITOR, {len(body)} chars)[/]")
            _turn(s, body)
        else:
            console.print(f"[{ST['dim']}]nothing to send.[/]")
    elif cmd in ("palette", "p"):
        _cmd_palette(s, arg)
    elif cmd == "theme":
        _cmd_theme(arg)
    elif cmd == "rename":
        _cmd_rename(s, arg)
    elif cmd in ("delete", "rm"):
        _cmd_delete(s, arg)
    elif cmd == "fork":
        _cmd_fork(s)
    elif cmd in ("copy", "yank"):
        _cmd_copy(s, arg)
    elif cmd in ("permission", "permissions", "confirm"):
        _cmd_toggle(s, "confirm_tools", arg, "tool confirmations")
    elif cmd == "bell":
        _cmd_toggle(s, "bell", arg, "completion beep")
    elif cmd in ("help", "h", "?"):
        console.print(HELP)
    else:
        console.print(f"[{ST['dim']}]unknown command /{escape(cmd)} — /help for the list[/]")
    return False


# ============================ main loop ============================
def main():
    _load_prefs()
    _apply_theme(PREFS.get("theme", "abyss"))
    console.print()
    banner()
    s = Session()
    while True:
        try:
            console.print()
            line = _prompt(model=s.agent.model).strip()
        except EOFError:
            s.save(); console.print(f"[{ST['accent']}]≈ bye[/]"); break
        except KeyboardInterrupt:
            console.print(f"[{ST['dim']}](/quit to leave)[/]"); continue
        if not line:
            continue
        if line.startswith("/"):
            try:
                _handle(line, s)
            except SystemExit:
                break
            continue
        _turn(s, line)


if __name__ == "__main__":
    main()
