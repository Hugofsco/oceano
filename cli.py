"""Oceano in the terminal — a rich, opencode-style chat client.

The simplest frontend, now with the same comforts as the web UI / Telegram: streamed
reasoning + tool calls + answer, per-turn stats, and SESSIONS that persist to the shared
chat store (data/chats/) — so a conversation you start here shows up in the web UI too,
and vice-versa. Slash commands handle the session (type /help).

    python cli.py            # (use the venv: venv/bin/python cli.py)

Everything runs through the same Agent.run_stream() the other frontends use — frontends
stay thin. No external deps: just ANSI + stdlib readline for line editing/history.
"""
import itertools
import json
import math
import os
import re
import secrets
import shutil
import sys
import threading
import time
from pathlib import Path

try:
    import readline  # arrow-key editing + history + Tab-completion (stdlib, optional)
except Exception:
    readline = None

import config
from oceano import chats
from oceano.agent import Agent

# ---- palette (abyssal console: bioluminescent cyan, warm user-green, dim depths) ----
R = "\033[0m"; B = "\033[1m"; DIM = "\033[2m"; IT = "\033[3m"; UL = "\033[4m"
CYAN = "\033[36m"; GREEN = "\033[32m"; BLUE = "\033[34m"; GREY = "\033[90m"
CORAL = "\033[38;5;210m"; BUOY = "\033[38;5;215m"; RED = "\033[31m"
_ANSI = re.compile(r"\033\[[0-9;]*m")
def _wrap(s):  # mark non-printing runs so readline measures the prompt width correctly
    return re.sub(r"(\033\[[0-9;]*m)", "\001\\1\002", s)
def _vlen(s):  # visible length, ignoring ANSI escapes
    return len(_ANSI.sub("", s))
OCEANO = f"{CYAN}{B}oceano ›{R} "
_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_DATA = Path(__file__).resolve().parent / "data"
_HISTORY = _DATA / "cli_history"
_PREFS_PATH = _DATA / "cli_prefs.json"
_isatty = sys.stdout.isatty()

# ---- themes (only the accent colors change; R/B/DIM/IT/UL stay structural) ----
THEMES = {
    "abyss":  dict(CYAN="\033[36m",       GREEN="\033[32m",       BLUE="\033[34m",      GREY="\033[90m",
                   CORAL="\033[38;5;210m", BUOY="\033[38;5;215m", RED="\033[31m"),
    "reef":   dict(CYAN="\033[38;5;43m",  GREEN="\033[38;5;114m", BLUE="\033[38;5;75m", GREY="\033[38;5;102m",
                   CORAL="\033[38;5;210m", BUOY="\033[38;5;215m", RED="\033[38;5;203m"),
    "aurora": dict(CYAN="\033[38;5;141m", GREEN="\033[38;5;121m", BLUE="\033[38;5;111m", GREY="\033[38;5;103m",
                   CORAL="\033[38;5;211m", BUOY="\033[38;5;222m", RED="\033[38;5;204m"),
    "mono":   dict(CYAN="\033[97m",       GREEN="\033[37m",       BLUE="\033[37m",      GREY="\033[90m",
                   CORAL="\033[37m",       BUOY="\033[37m",       RED="\033[91m"),
}
THEME = "abyss"
PREFS = {}                          # loaded in main(): theme · confirm_tools · bell
_HIST = []                          # input history (shared by the raw editor + readline fallback)

# side-effecting tools gated behind a y/n prompt when confirm_tools is on
RISKY_TOOLS = {"run_shell", "python_exec", "write_file", "edit_file", "make_folder",
               "forget_memory", "delegate", "schedule_task", "run_workflow"}

# block-letter wordmark (opencode-style banner), shown once at startup
LOGO = (
    "█████ █████ █████ █████ █   █ █████\n"
    "█   █ █     █     █   █ ██  █ █   █\n"
    "█   █ █     ████  █████ █ █ █ █   █\n"
    "█   █ █     █     █   █ █  ██ █   █\n"
    "█████ █████ █████ █   █ █   █ █████"
)


def _termw():
    return max(36, min(shutil.get_terminal_size((80, 24)).columns, 100))


def banner():
    if _termw() >= 38:
        for ln in LOGO.splitlines():
            print(f"{CYAN}{B}{ln}{R}")
    else:
        print(f"{CYAN}{B}≈ Oceano{R}")
    print(f"{GREY}≈ a local agent in deep waters · model {config.MODEL}{R}")
    print(f"{GREY}  /help · /palette · /chats to resume · Tab completes · Ctrl-C interrupts{R}")


# Claude-Code-style input box: straight rules top + bottom, BOTH drawn before you type
# (the bottom is visible while composing), with the cursor parked on the input line.
def _read_boxed(label="you"):
    w = _termw(); inner = w - 2; head = f"─ {label} "
    top = f"{GREY}┌{head}{'─' * max(0, inner - len(head))}┐{R}"
    bot = f"{GREY}└{'─' * inner}┘{R}"
    prompt = _wrap(f"{GREY}│{R} {GREEN}{B}›{R} ")
    sys.stdout.write("\n" + top + "\n\n" + bot)     # top · (empty input line) · bottom
    sys.stdout.write("\033[1A\r")                   # hop up onto the input line, column 0
    sys.stdout.flush()
    try:
        return input(prompt)
    finally:
        sys.stdout.write("\033[1B\r")               # drop below the box so the reply prints under it
        sys.stdout.flush()


# ---- raw-mode line editor with a live slash-command dropdown -------------------
# Activates for interactive ttys: type "/" to open a menu, ↑/↓ to move, Enter to pick
# (commands that take an argument stay editable so you can type it), or just keep typing
# to filter. Also gives ↑/↓ history, and basic emacs-style editing. Falls back to
# _read_boxed() when stdin isn't a tty / termios is unavailable / anything goes wrong.
def _supports_raw():
    if not _isatty or os.environ.get("OCEANO_CLI_PLAIN"):
        return False
    try:
        import termios  # noqa: F401
        import tty       # noqa: F401
        return sys.stdin.isatty()
    except Exception:
        return False


def _read_key(fd, select):
    """Read one keypress, returning a semantic token or a printable (UTF-8) string."""
    b = os.read(fd, 1)
    if not b:
        return "EOF"
    o = b[0]
    if b == b"\x1b":                                  # ESC — maybe the start of a sequence
        r, _, _ = select.select([fd], [], [], 0.0009)
        if not r:
            return "ESC"
        b2 = os.read(fd, 1)
        if b2 in (b"[", b"O"):
            b3 = os.read(fd, 1)
            simple = {b"A": "UP", b"B": "DOWN", b"C": "RIGHT", b"D": "LEFT", b"H": "HOME", b"F": "END"}
            if b3 in simple:
                return simple[b3]
            if b3.isdigit():                         # e.g. \x1b[3~ (Delete), \x1b[1~ (Home)
                seq = b3
                while not seq.endswith(b"~"):
                    nxt = os.read(fd, 1)
                    if not nxt:
                        break
                    seq += nxt
                return {b"1": "HOME", b"7": "HOME", b"4": "END", b"8": "END", b"3": "DEL"}.get(b3)
            return None
        return "ESC"
    if o == 3:
        return "C-c"
    if o == 4:
        return "C-d"
    if o in (1,):
        return "HOME"
    if o in (5,):
        return "END"
    if o == 21:
        return "C-u"
    if o == 11:
        return "C-k"
    if o == 23:
        return "C-w"
    if o in (13, 10):
        return "ENTER"
    if o == 9:
        return "TAB"
    if o in (127, 8):
        return "BACKSPACE"
    if o < 32:
        return None
    if o < 0x80:                                     # plain ASCII
        return b.decode("latin-1")
    extra = 1 if o >= 0xC0 else 0                    # rough UTF-8 continuation count
    extra = 1 if o < 0xE0 else (2 if o < 0xF0 else 3)
    buf = b + (os.read(fd, extra) if extra else b"")
    try:
        return buf.decode("utf-8")
    except Exception:
        return None


def _read_line_interactive(label="you"):
    import select
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    w = _termw(); inner = w - 2; head = f"─ {label} "
    top = f"{GREY}┌{head}{'─' * max(0, inner - len(head))}┐{R}"
    bot = f"{GREY}└{'─' * inner}┘{R}"
    tw = max(8, shutil.get_terminal_size((80, 24)).columns)
    start_col = 4                                     # visible width of "│ › "
    rows_cap = max(3, min(8, shutil.get_terminal_size((80, 24)).lines - 4))
    buf, pos, sel = "", 0, 0
    hist_idx, saved = None, None
    suppressed = False

    # initial frame (cooked mode: \n behaves) then drop to raw
    sys.stdout.write("\n" + top + "\n")
    sys.stdout.write(f"{GREY}│{R} {GREEN}{B}›{R} \0337")   # prompt + save cursor at input start
    sys.stdout.flush()

    def matches_for():
        if suppressed or not buf.startswith("/") or " " in buf:
            return []
        q = buf[1:].lower()
        if not q:
            return list(SLASH_CMDS)
        scored = [(c, _fuzzy(q, c)) for c in SLASH_CMDS]
        return [c for c, s in sorted((p for p in scored if p[1] is not None), key=lambda x: -x[1])]

    def render(matches):
        out = ["\0338\033[J", buf]                    # restore to input start, clear below, draw buffer
        out.append("\n" + bot)
        for i, c in enumerate(matches[:rows_cap]):
            desc = _CMD_DESC.get(c, "")[: max(0, tw - 8 - len(c))]
            if i == sel:
                out.append(f"\n {CYAN}{B}▸ {c}{R}  {GREY}{desc}{R}")
            else:
                out.append(f"\n {GREY}▸{R} {CYAN}{c}{R}  {GREY}{desc}{R}")
        total = start_col + pos                       # reposition to the edit cursor (wrap-aware)
        out.append("\0338")
        row, col = total // tw, total % tw
        if row:
            out.append(f"\033[{row}B")
        out.append("\r" + (f"\033[{col}C" if col else ""))
        sys.stdout.write("".join(out)); sys.stdout.flush()

    try:
        tty.setraw(fd)
        while True:
            matches = matches_for()
            if matches:
                sel = max(0, min(sel, len(matches) - 1))
            else:
                sel = 0
            render(matches)
            key = _read_key(fd, select)
            if key == "C-c":
                raise KeyboardInterrupt
            if key in ("EOF", "C-d") and not buf:
                raise EOFError
            if key == "ENTER":
                if matches:
                    chosen = matches[sel]
                    if chosen in ARG_CMDS:
                        buf, pos, suppressed = chosen + " ", len(chosen) + 1, False
                        continue
                    buf, pos = chosen, len(chosen)
                render([])                            # clear the menu, leave the line
                _remember_input(buf)
                return buf
            if key == "TAB":
                if matches:
                    buf, pos, suppressed = matches[sel] + " ", len(matches[sel]) + 1, True
                continue
            if key == "UP":
                if matches:
                    sel -= 1; sel %= len(matches)
                elif _HIST:
                    if hist_idx is None:
                        saved, hist_idx = buf, len(_HIST)
                    hist_idx = max(0, hist_idx - 1); buf = _HIST[hist_idx]; pos = len(buf)
                continue
            if key == "DOWN":
                if matches:
                    sel += 1; sel %= len(matches)
                elif hist_idx is not None:
                    hist_idx += 1
                    if hist_idx >= len(_HIST):
                        hist_idx, buf = None, (saved or "")
                    else:
                        buf = _HIST[hist_idx]
                    pos = len(buf)
                continue
            if key == "LEFT":
                pos = max(0, pos - 1); continue
            if key == "RIGHT":
                pos = min(len(buf), pos + 1); continue
            if key == "HOME":
                pos = 0; continue
            if key == "END":
                pos = len(buf); continue
            if key == "BACKSPACE":
                if pos > 0:
                    buf = buf[:pos - 1] + buf[pos:]; pos -= 1
                hist_idx, suppressed = None, False; continue
            if key in ("DEL", "C-d"):
                if pos < len(buf):
                    buf = buf[:pos] + buf[pos + 1:]
                continue
            if key == "C-u":
                buf, pos = buf[pos:], 0; suppressed = False; continue
            if key == "C-k":
                buf = buf[:pos]; continue
            if key == "C-w":
                i = pos
                while i > 0 and buf[i - 1] == " ":
                    i -= 1
                while i > 0 and buf[i - 1] != " ":
                    i -= 1
                buf, pos = buf[:i] + buf[pos:], i; suppressed = False; continue
            if key == "ESC":
                if matches:
                    suppressed = True
                continue
            if isinstance(key, str) and key and ord(key[0]) >= 32:
                buf = buf[:pos] + key + buf[pos:]; pos += len(key)
                hist_idx, suppressed = None, False
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\n")
        sys.stdout.flush()


def _prompt(label="you"):
    if not _supports_raw():
        return _read_boxed(label)
    try:
        return _read_line_interactive(label)
    except (KeyboardInterrupt, EOFError):
        raise
    except Exception:
        return _read_boxed(label)


def _uid():
    return "cli-" + secrets.token_hex(6)


def _hr(label=""):
    line = "─" * 60
    print(f"{GREY}{line}{R}" if not label else f"{GREY}── {label} {('─' * max(0, 56 - len(label)))}{R}")


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


# ============================ markdown rendering ============================
# A tiny dependency-free markdown → ANSI renderer for model answers and resumed
# transcripts. Handles fenced code, headers, lists, quotes, rules + inline styles.
def _md_inline(s):
    # Links FIRST, on raw text — the `[` in later-introduced ANSI escapes (\x1b[36m …) would
    # otherwise be swallowed by the link bracket; \x1b is also excluded as a belt-and-braces.
    s = re.sub(r"\[([^\]\n\x1b]+)\]\(([^)\n\x1b]+)\)",
               lambda m: f"{UL}{CYAN}{m.group(1)}{R}{GREY}({m.group(2)}){R}", s)
    s = re.sub(r"`([^`]+)`", lambda m: f"{CYAN}{m.group(1)}{R}", s)
    s = re.sub(r"\*\*([^*]+)\*\*", lambda m: f"{B}{m.group(1)}{R}", s)
    s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", lambda m: f"{IT}{m.group(1)}{R}", s)
    s = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", lambda m: f"{IT}{m.group(1)}{R}", s)
    return s


def _wrap_text(s, width, prefix="", cont=""):
    width = max(width, 12)
    out, line, vis, started = [], prefix, _vlen(prefix), False
    for word in s.split(" "):
        wv = _vlen(word)
        if started and vis + 1 + wv > width:
            out.append(line); line, vis, started = cont + word, _vlen(cont) + wv, True
        else:
            line += ((" " if started else "") + word); vis += (1 if started else 0) + wv; started = True
    out.append(line if started else prefix.rstrip())
    return "\n".join(out)


def _md_block(ln, width):
    raw = ln.rstrip()
    if not raw.strip():
        return ""
    if re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", raw):                 # --- horizontal rule
        return f"{GREY}{'─' * min(width, 56)}{R}"
    h = re.match(r"^(#{1,6})\s+(.*)$", raw)                          # # headers
    if h:
        lvl, txt = len(h.group(1)), _md_inline(h.group(2).strip())
        return f"{B}{CYAN}{txt}{R}" if lvl == 1 else (f"{B}{txt}{R}" if lvl == 2 else f"{B}{GREY}{txt}{R}")
    q = re.match(r"^\s*>\s?(.*)$", raw)                              # > blockquote
    if q:
        return _wrap_text(_md_inline(q.group(1)), width, f"{GREY}▎ {IT}", f"{GREY}▎ {IT}") + R
    li = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", raw)              # - / 1. list item
    if li:
        indent, mark = li.group(1), li.group(2)
        bullet = "•" if mark in "-*+" else mark
        pad = " " * len(indent)
        return _wrap_text(_md_inline(li.group(3)), width,
                          f"{pad}{CYAN}{bullet}{R} ", " " * (len(indent) + len(bullet) + 1))
    return _wrap_text(_md_inline(raw), width)


def _md_code(lines, lang):
    head = f"{GREY}{IT}{lang}{R}\n" if lang else ""
    body = "\n".join(f"{GREY}│{R} {ln}" for ln in lines) if lines else f"{GREY}│{R}"
    return head + body


def _md(text, width=None):
    width = width or _termw()
    out, in_code, lang, buf = [], False, "", []
    for ln in text.split("\n"):
        fence = re.match(r"^\s*```(\w*)\s*$", ln)
        if fence:
            if in_code:
                out.append(_md_code(buf, lang)); in_code, buf = False, []
            else:
                in_code, lang, buf = True, fence.group(1), []
            continue
        (buf.append(ln) if in_code else out.append(_md_block(ln, width)))
    if in_code:                                                      # unterminated fence
        out.append(_md_code(buf, lang))
    return "\n".join(out)


_MD_HINT = re.compile(r"(^|\n)\s{0,3}(#{1,6}\s|[-*+]\s|\d+\.\s|>\s|```)|`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\)")
def _looks_md(s):
    return bool(_MD_HINT.search(s))


def _screen_rows(seg, width, first_col):
    rows = 0
    for i, ln in enumerate(seg.split("\n")):
        vis = (first_col if i == 0 else 0) + len(ln)
        rows += max(1, math.ceil(vis / width)) if vis else 1
    return rows


# ============================ diffs for file edits ============================
def _capture_diff(name, args_json, width):
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


def _render_diff(d, width, limit=60):
    import difflib
    a, b = d["before"].splitlines(), d["after"].splitlines()
    diff = list(difflib.unified_diff(a, b, lineterm="", n=2))[2:]   # drop the ---/+++ header
    if not diff:
        return f"{GREY}    · no content change{R}"
    adds = sum(1 for l in diff if l.startswith("+"))
    dels = sum(1 for l in diff if l.startswith("-"))
    lines = [f"{GREY}    ✎ {d['path']}{R}  {GREEN}+{adds}{R} {RED}-{dels}{R}"]
    for ln in diff[:limit]:
        body = ln[: width - 4]
        if ln.startswith("@@"):
            lines.append(f"{CYAN}    {body}{R}")
        elif ln.startswith("+"):
            lines.append(f"{GREEN}    {body}{R}")
        elif ln.startswith("-"):
            lines.append(f"{RED}    {body}{R}")
        else:
            lines.append(f"{GREY}    {body}{R}")
    if len(diff) > limit:
        lines.append(f"{GREY}    … {len(diff) - limit} more diff lines{R}")
    return "\n".join(lines)


# ============================ working spinner ============================
class Spinner:
    """A braille spinner shown only during genuine waits (before the first token, while a
    tool runs). Runs in a daemon thread and erases its line on stop. No-op when not a tty."""
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self):
        self._t = self._stop = None

    def start(self, msg=""):
        if not _isatty:
            return
        self.stop()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, args=(msg,), daemon=True)
        self._t.start()

    def _run(self, msg):
        for ch in itertools.cycle(self.FRAMES):
            if self._stop.is_set():
                break
            sys.stdout.write(f"\r{CYAN}{ch}{R} {GREY}{msg}{R}\033[K")
            sys.stdout.flush()
            self._stop.wait(0.09)

    def stop(self):
        if self._stop:
            self._stop.set()
        if self._t:
            self._t.join(timeout=0.3)
        self._t = self._stop = None
        if _isatty:
            sys.stdout.write("\r\033[K"); sys.stdout.flush()


# ============================ @file attachments + $EDITOR ============================
def _expand_attachments(text):
    """Resolve `@path` tokens in a typed line into inline context the local (text-only)
    model can use: text files are inlined; images are described by the vision target.
    Returns the augmented message (original text + attachment blocks)."""
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
            print(f"{GREY}  · @{ref}: no such file — skipped{R}")
            continue
        if p.suffix.lower() in _IMG_EXT:
            try:
                from oceano import delegate
                print(f"{GREY}  · 🖼 describing {p.name} with the vision target…{R}")
                r = delegate.describe_image(str(p), text, role="vision")
                desc = (r.get("output") or "").strip() if r.get("ok") else f"(couldn't analyze: {r.get('error')})"
                blocks.append(f"[Attached image “{p.name}” — what the vision target sees:]\n{desc}")
            except Exception as e:
                print(f"{GREY}  · vision failed for {p.name}: {e}{R}")
        else:
            try:
                from oceano import rag
                content = rag._read(p)
            except Exception:
                content = p.read_text(encoding="utf-8", errors="replace")
            if content.strip():
                blocks.append(f"[Attached file “{p.name}”:]\n{content[:6000]}")
                print(f"{GREY}  · 📎 attached {p.name} ({len(content)} chars){R}")
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
        print(f"{GREY}  · editor failed ({e}){R}")
        return ""
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ============================ readline: history + Tab-complete ============================
SLASH_CMDS = ["/chats", "/new", "/fork", "/rename", "/delete", "/palette", "/compact",
              "/context", "/status", "/model", "/theme", "/editor", "/copy",
              "/permission", "/bell", "/help", "/quit"]
# commands that take an argument — picking one from the dropdown keeps the line editable
ARG_CMDS = {"/model", "/theme", "/rename", "/delete", "/context", "/copy",
            "/palette", "/permission", "/bell"}


def _path_complete(frag):
    import glob
    out = []
    for m in glob.glob(os.path.expanduser(frag) + "*")[:50]:
        out.append("@" + m + ("/" if os.path.isdir(m) else " "))
    return out


def _completer(text, state):
    try:
        buf = (readline.get_line_buffer() if readline else "").lstrip()
        if buf.startswith("/theme ") and not text.startswith("/"):
            opts = [n + " " for n in THEMES if n.startswith(text.lower())]
        elif text.startswith("@"):
            opts = _path_complete(text[1:])
        elif text.startswith("/") or buf.startswith("/"):
            opts = [c + " " for c in SLASH_CMDS if c.startswith(text)]
        else:
            opts = []
        return opts[state] if state < len(opts) else None
    except Exception:
        return None


def _init_readline():
    """Load history into _HIST (used by the raw editor) and set up readline's Tab-complete
    for the fallback reader. Persist on exit through whichever backend is available."""
    global _HIST
    import atexit
    try:
        _HISTORY.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    if readline:
        try:
            if _HISTORY.exists():
                readline.read_history_file(str(_HISTORY))
        except Exception:
            pass
        readline.set_history_length(2000)
        n = readline.get_current_history_length()
        _HIST = [h for i in range(1, n + 1) if (h := readline.get_history_item(i))]
        try:
            readline.set_completer(_completer)
            readline.set_completer_delims(" \t\n")
            readline.parse_and_bind("tab: complete")
        except Exception:
            pass
        atexit.register(_save_history)
    else:
        try:
            _HIST = _HISTORY.read_text(encoding="utf-8").splitlines()
        except Exception:
            _HIST = []
        atexit.register(_save_history)


def _remember_input(line):
    """Record a submitted line in history (both backends), de-duping consecutive repeats."""
    if not line.strip():
        return
    if _HIST and _HIST[-1] == line:
        return
    _HIST.append(line)
    if readline:
        try:
            readline.add_history(line)
        except Exception:
            pass


def _save_history():
    try:
        if readline:
            readline.write_history_file(str(_HISTORY))
        else:
            _HISTORY.write_text("\n".join(_HIST[-2000:]), encoding="utf-8")
    except Exception:
        pass


# ============================ prefs + themes ============================
def _load_prefs():
    global PREFS
    try:
        PREFS = json.loads(_PREFS_PATH.read_text(encoding="utf-8"))
    except Exception:
        PREFS = {}
    PREFS.setdefault("theme", "abyss")
    PREFS.setdefault("confirm_tools", False)
    PREFS.setdefault("bell", True)
    return PREFS


def _save_prefs():
    try:
        _PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PREFS_PATH.write_text(json.dumps(PREFS, indent=2), encoding="utf-8")
    except Exception:
        pass


def _apply_theme(name):
    """Swap the accent palette at runtime and rebuild the colored derived strings."""
    global CYAN, GREEN, BLUE, GREY, CORAL, BUOY, RED, OCEANO, HELP, THEME
    t = THEMES.get(name)
    if not t:
        return False
    CYAN, GREEN, BLUE = t["CYAN"], t["GREEN"], t["BLUE"]
    GREY, CORAL, BUOY, RED = t["GREY"], t["CORAL"], t["BUOY"], t["RED"]
    THEME = name
    OCEANO = f"{CYAN}{B}oceano ›{R} "
    HELP = _build_help()
    return True


# ============================ fuzzy matcher (command palette) ============================
def _fuzzy(query, text):
    """Subsequence fuzzy score (à la fuzzysort): consecutive hits and word-boundary hits
    score higher; shorter targets win ties. Returns None if `query` isn't a subsequence."""
    q, t = query.lower(), text.lower()
    if not q:
        return 0.0
    score, ti, last = 0.0, 0, -2
    for ch in q:
        idx = t.find(ch, ti)
        if idx == -1:
            return None
        score += 6 if idx == last + 1 else 1                # consecutive-run bonus
        if idx == 0 or t[idx - 1] in " /_-.:":
            score += 4                                       # word-boundary bonus
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
    print(f"{GREY}no clipboard available — install wl-copy, xclip, or xsel.{R}")
    return False


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
    detail = str(detail).replace("\n", " ")[:70]
    print(f"{BUOY}  ⚠ allow {B}{name}{R}{BUOY}?{R}" + (f" {GREY}{detail}{R}" if detail else "")
          + f"  {GREY}[y / N / a=always]{R}")
    try:
        ans = input("    › ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
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
        self.messages = []                 # display transcript (user/assistant/thinking/tool)
        self.agent = Agent()
        self.compactions = 0
        self.cap = None                    # auto-compact threshold (messages), or None
        self.started = time.time()
        self.trust = False                 # "always allow tools" for this session (set via prompt)

    def load(self, sid):
        rec = chats.get(sid)
        if not rec:
            return False
        self.id = sid
        self.title = rec.get("title") or "Voyage"
        self.messages = rec.get("messages") or []
        self.agent = Agent()
        for m in self.messages:            # rebuild the LLM context from the dialogue turns
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
            print(f"{GREEN}{B}you ›{R} {m.get('content', '')}")
        elif role == "thinking":
            print(f"{GREY}{IT}🤔 {(' '.join((m.get('text') or '').split()))[:280]}{R}")
        elif role == "tool":
            print(f"{CYAN}  → {m.get('name')}{R}{GREY}({(m.get('args') or '')[:60]}){R}")
            prev = (m.get("result") or "").replace("\n", " ")[:120]
            if prev:
                print(f"{GREY}    {prev}{R}")
        elif role == "assistant":
            body = m.get("content", "")
            print(f"{CYAN}{B}oceano ›{R}")
            print(_md(body) if _looks_md(body) else body)
    print()


def _stats_line(ev):
    bits = []
    if ev.get("ctx"):
        bits.append(f"{round(ev['ctx'] / 1000, 1)}k ctx")
    if ev.get("tokens"):
        bits.append(f"{ev['tokens']} tok")
    if ev.get("tok_s"):
        bits.append(f"{ev['tok_s']} tok/s")
    return f"{GREY}  · {' · '.join(bits)}{R}" if bits else ""


def _turn(s, text):
    """Run one streamed turn, render it, fold it into the transcript, and persist.
    Reasoning + the answer stream live; once an answer segment completes it is re-rendered
    in place as markdown (when it's a tty and fits the screen). File edits show a diff."""
    s.messages.append({"role": "user", "content": text})
    if s.title in ("", "New voyage"):
        s.title = text[:60]
    if s.cap and len(s.agent.messages) > s.cap:           # auto-compact like Telegram/web
        dropped = s.agent.compact()
        if dropped:
            s.compactions += 1
            print(f"{GREY}🗜 auto-compacted {dropped} messages (over {s.cap}){R}")

    augmented = _expand_attachments(text)
    w = _termw()
    sp = Spinner()
    t0 = time.time()
    asst = think = seg = ""
    in_think = seg_active = False
    pending = None

    def flush_think():
        nonlocal think, in_think
        if think.strip():
            s.messages.append({"role": "thinking", "text": think})
        think, in_think = "", False

    def finalize_seg():
        """Close the current answer segment — re-render it as markdown in place when safe."""
        nonlocal seg, seg_active
        seg_active = False
        if not seg:
            return
        if _isatty and _looks_md(seg):
            rows = _screen_rows(seg, w, _vlen(OCEANO))
            term_lines = shutil.get_terminal_size((80, 24)).lines
            if rows < term_lines - 1:
                sys.stdout.write("\r")
                if rows > 1:
                    sys.stdout.write(f"\033[{rows - 1}A")
                sys.stdout.write("\033[J" + OCEANO + "\n" + _md(seg, w) + "\n")
                sys.stdout.flush(); seg = ""; return
        sys.stdout.write("\n")                            # raw stream stays; just close the line
        seg = ""

    gen = s.agent.run_stream(augmented)
    try:
        sp.start("thinking…")
        for ev in gen:
            sp.stop()
            t = ev.get("type")
            if t != "token" and seg_active:
                finalize_seg()
            if t == "reasoning":
                if not in_think:
                    sys.stdout.write(f"\n{GREY}{IT}🤔 "); in_think = True
                sys.stdout.write(ev["text"]); sys.stdout.flush(); think += ev["text"]
            elif t == "token":
                if in_think:
                    sys.stdout.write(R + "\n"); flush_think()
                if not seg_active:
                    sys.stdout.write("\n" + OCEANO); seg_active = True; seg = ""
                sys.stdout.write(ev["text"]); sys.stdout.flush(); asst += ev["text"]; seg += ev["text"]
            elif t == "tool_call":
                if in_think:
                    sys.stdout.write(R + "\n"); flush_think()
                print(f"\n{CYAN}  → {ev['name']}{R}{GREY}({(ev.get('args') or '')[:60]}){R}")
                if PREFS.get("confirm_tools") and _isatty and ev["name"] in RISKY_TOOLS:
                    if not _confirm_tool(ev["name"], ev.get("args"), s):
                        gen.close()                         # GeneratorExit unwinds before the tool runs
                        print(f"{CORAL}  ✋ denied — stopping this turn.{R}")
                        break
                pending = _capture_diff(ev["name"], ev.get("args"), w)
                sp.start(f"running {ev['name']}…")
            elif t == "tool_result":
                s.messages.append({"role": "tool", "name": ev["name"], "args": "", "result": ev["result"]})
                res = ev.get("result") or ""
                if pending and not res.startswith(("ERROR", "(no such")):
                    print(_render_diff(pending, w))
                else:
                    prev = res.replace("\n", " ")[:120]
                    if prev:
                        print(f"{GREY}    {prev}{R}")
                pending = None
                sp.start("thinking…")
            elif t == "answer_done":
                pass
            elif t == "stats":
                line = _stats_line(ev)
                if line:
                    print("\n" + line)
        sp.stop()
        if in_think:
            sys.stdout.write(R + "\n"); flush_think()
        if seg_active:
            finalize_seg()
    except KeyboardInterrupt:
        sp.stop()
        if in_think:
            sys.stdout.write(R + "\n"); flush_think()
        if seg_active:
            sys.stdout.write("\n")
        print(f"{CORAL}⏹ interrupted{R}")
    if asst.strip():
        s.messages.append({"role": "assistant", "content": asst})
    print()
    if PREFS.get("bell") and _isatty and (time.time() - t0) > 8:
        sys.stdout.write("\a"); sys.stdout.flush()          # ping when a long turn finishes
    s.save()


# ============================ slash commands ============================
def _cmd_chats(s):
    items = chats.list_all()
    if not items:
        print(f"{GREY}no saved conversations yet — they appear here once you chat.{R}")
        return
    print(f"\n{B}Conversations{R} {GREY}(newest first){R}")
    for i, c in enumerate(items[:40], 1):
        marker = f"{CYAN}●{R}" if c["id"] == s.id else " "
        print(f" {marker} {B}{i:2}{R}  {GREY}{_fmt_date(c.get('date')):<9}{R} {c.get('title', 'Untitled')[:54]} {GREY}· {c.get('count', 0)} msgs{R}")
    try:
        sel = input(f"{GREY}open # (Enter to cancel): {R}").strip()
    except (EOFError, KeyboardInterrupt):
        print(); return
    if sel.isdigit() and 1 <= int(sel) <= min(len(items), 40):
        sid = items[int(sel) - 1]["id"]
        if sid == s.id:
            print(f"{GREY}already in this conversation.{R}"); return
        s.save()
        if s.load(sid):
            print(f"\n{CYAN}≈ resumed “{s.title}”{R}\n")
            _hr(); _render_transcript(s.messages); _hr()
    else:
        print(f"{GREY}cancelled.{R}")


def _cmd_status(s):
    n, approx = s.agent.context_metrics()
    print(
        f"\n{B}🌊 Oceano — status{R}\n"
        f"  {GREY}model{R}     {s.agent.model}\n"
        f"  {GREY}session{R}   {s.title}  {GREY}({s.id}){R}\n"
        f"  {GREY}context{R}   {n} messages · ~{approx} tok"
        + (f"  · auto-compact > {s.cap}" if s.cap else "") + "\n"
        f"  {GREY}compacted{R} {s.compactions}× this session\n"
        f"  {GREY}uptime{R}    {_fmt_age(time.time() - s.started)}\n"
        f"  {GREY}prefs{R}     theme {THEME} · confirm-tools "
        f"{'on' if PREFS.get('confirm_tools') else 'off'} · bell "
        f"{'on' if PREFS.get('bell') else 'off'}\n"
    )


def _cmd_context(s, arg):
    if arg:
        if arg.lower() in ("off", "0", "none"):
            s.cap = None; print(f"{GREY}🔕 auto-compact off.{R}"); return
        if arg.isdigit() and int(arg) > 0:
            s.cap = int(arg); print(f"{GREY}✅ auto-compact at > {s.cap} messages.{R}"); return
        print(f"{GREY}usage: /context <n> | off{R}"); return
    n, approx = s.agent.context_metrics()
    print(f"{GREY}📜 {n} messages · ~{approx} tok"
          + (f" · auto-compact > {s.cap}" if s.cap else " · auto-compact off") + f"{R}")


def _cmd_compact(s):
    dropped = s.agent.compact()
    if dropped:
        s.compactions += 1
        n, approx = s.agent.context_metrics()
        print(f"{GREY}🗜 folded {dropped} messages into a summary · now {n} msgs · ~{approx} tok{R}")
    else:
        print(f"{GREY}✨ nothing to compact — the context is already small.{R}")


def _cmd_palette(s, query):
    """Fuzzy command palette — filter every command + recent chat, then pick by number."""
    cands = [("cmd", c, f"{CYAN}{c}{R}", _CMD_DESC.get(c, "")) for c in SLASH_CMDS]
    for c in chats.list_all()[:30]:
        if c["id"] != s.id:
            cands.append(("chat", c["id"],
                          f"{GREY}{_fmt_date(c.get('date'))}{R} {c.get('title', 'Untitled')[:46]}",
                          f"{c.get('count', 0)} msgs"))
    if query:
        scored = []
        for kind, val, label, desc in cands:
            sc = _fuzzy(query, _ANSI.sub("", label) + " " + desc)
            if sc is not None:
                scored.append((sc, kind, val, label, desc))
        scored.sort(key=lambda x: -x[0])
        rows = [(k, v, l, d) for _, k, v, l, d in scored[:15]]
    else:
        rows = [(k, v, l, d) for k, v, l, d in cands[:15]]
    if not rows:
        print(f"{GREY}no matches for “{query}”.{R}"); return
    print(f"\n{B}Palette{R}" + (f" {GREY}· {query}{R}" if query else ""))
    for i, (kind, val, label, desc) in enumerate(rows, 1):
        tag = f"{GREY}cmd {R}" if kind == "cmd" else f"{CYAN}chat{R}"
        print(f"  {B}{i:2}{R} {tag}  {label}  {GREY}{desc}{R}")
    try:
        sel = input(f"{GREY}pick # (Enter to cancel): {R}").strip()
    except (EOFError, KeyboardInterrupt):
        print(); return
    if not (sel.isdigit() and 1 <= int(sel) <= len(rows)):
        print(f"{GREY}cancelled.{R}"); return
    kind, val, _, _ = rows[int(sel) - 1]
    if kind == "cmd":
        _handle(val, s)
    elif s.id != val:
        s.save()
        if s.load(val):
            print(f"\n{CYAN}≈ resumed “{s.title}”{R}\n")
            _hr(); _render_transcript(s.messages); _hr()


def _cmd_theme(arg):
    if not arg:
        names = "  ".join((f"{B}{n}{R}" if n == THEME else n) for n in THEMES)
        print(f"{GREY}theme: {THEME} · available:{R} {names} {GREY}· /theme <name>{R}")
        return
    if _apply_theme(arg.lower()):
        PREFS["theme"] = THEME; _save_prefs()
        print(f"{CYAN}🎨 theme → {THEME}{R}")
    else:
        print(f"{GREY}unknown theme “{arg}” — try: {', '.join(THEMES)}{R}")


def _cmd_rename(s, arg):
    if not arg:
        print(f"{GREY}usage: /rename <new title>{R}"); return
    s.title = arg[:80]
    if s.messages:
        rec = chats.get(s.id) or {}
        chats.save(s.id, s.title, s.messages, created=rec.get("created"))
    print(f"{GREY}✎ renamed → {s.title}{R}")


def _cmd_delete(s, arg):
    items = chats.list_all()
    if arg.isdigit():
        i = int(arg)
        if not (1 <= i <= len(items)):
            print(f"{GREY}no #{i} in the list.{R}"); return
        target = items[i - 1]
        if target["id"] != s.id:
            try:
                ok = input(f"{CORAL}delete “{target.get('title', '')[:40]}”? [y/N] {R}").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print(); return
            if ok in ("y", "yes"):
                chats.delete(target["id"]); print(f"{GREY}🗑 deleted.{R}")
            else:
                print(f"{GREY}cancelled.{R}")
            return
    try:
        ok = input(f"{CORAL}delete THIS conversation “{s.title[:40]}”? [y/N] {R}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(); return
    if ok in ("y", "yes"):
        chats.delete(s.id); s.new(); print(f"{GREY}🗑 deleted — started a fresh voyage.{R}")
    else:
        print(f"{GREY}cancelled.{R}")


def _cmd_fork(s):
    if not s.messages:
        print(f"{GREY}nothing to fork yet — say something first.{R}"); return
    s.save()
    s.id = _uid()
    s.title = f"{s.title} (fork)"[:80]
    s.trust = False
    s.save()
    print(f"{CYAN}⑂ forked — continuing in a new conversation: {s.title}{R}")


def _cmd_copy(s, arg):
    last = next((m.get("content", "") for m in reversed(s.messages) if m.get("role") == "assistant"), "")
    if not last:
        print(f"{GREY}no assistant reply to copy yet.{R}"); return
    if arg.lower() in ("code", "```"):
        blocks = re.findall(r"```[\w-]*\n(.*?)```", last, flags=re.DOTALL)
        if not blocks:
            print(f"{GREY}no code block in the last reply.{R}"); return
        payload, what = blocks[-1].rstrip("\n"), "last code block"
    else:
        payload, what = last, "last reply"
    if _copy_to_clipboard(payload):
        print(f"{GREY}📋 copied {what} ({len(payload)} chars).{R}")


def _cmd_toggle(s, key, arg, label):
    a = arg.lower()
    if a in ("on", "true", "1", "yes"):
        PREFS[key] = True
    elif a in ("off", "false", "0", "no"):
        PREFS[key] = False
    else:
        PREFS[key] = not PREFS.get(key)
    if key == "confirm_tools" and PREFS[key]:
        s.trust = False                         # re-arm prompting for this session
    _save_prefs()
    print(f"{GREY}{label}: {'on' if PREFS[key] else 'off'}{R}")


_CMD_DESC = {
    "/chats": "open saved conversations", "/new": "start a fresh conversation",
    "/compact": "shrink the context now", "/context": "context size · auto-compact",
    "/status": "model · context · session", "/model": "show / switch the model",
    "/editor": "compose in $EDITOR", "/palette": "fuzzy command launcher",
    "/theme": "switch color theme", "/rename": "rename this conversation",
    "/delete": "delete a conversation", "/fork": "branch into a new conversation",
    "/copy": "copy last reply (or code)", "/permission": "toggle tool confirmations",
    "/bell": "toggle the completion beep", "/help": "this list", "/quit": "leave",
}


def _build_help():
    return f"""{B}Commands{R}
  {CYAN}/chats{R}            list & open saved conversations
  {CYAN}/new{R} {GREY}(/reset){R}     start a fresh conversation
  {CYAN}/fork{R}             branch the current chat into a new one
  {CYAN}/rename{R} <title>   rename this conversation
  {CYAN}/delete{R} [#]       delete this conversation (or # from the list)
  {CYAN}/palette{R} [query]  fuzzy launcher over commands & chats
  {CYAN}/compact{R}          summarize & shrink the context now
  {CYAN}/context{R} [n|off]  show context size, or set/clear auto-compact
  {CYAN}/status{R}           model · context · session info
  {CYAN}/model{R} [name]     show or switch the chat model
  {CYAN}/theme{R} [name]     color theme: {', '.join(THEMES)}
  {CYAN}/editor{R}           compose a long prompt in $EDITOR
  {CYAN}/copy{R} [code]      copy the last reply (or its last code block)
  {CYAN}/permission{R} [on|off]  confirm before side-effecting tools run
  {CYAN}/bell{R} [on|off]    beep when a long turn finishes
  {CYAN}/help{R}             this list
  {CYAN}/quit{R} {GREY}(/q){R}        leave (the conversation is already saved)
{GREY}Type {R}{CYAN}@path{GREY} to attach a file/image · {R}{CYAN}Tab{GREY} completes commands, paths & themes.{R}
{GREY}Anything else is a message. Sessions persist to data/chats/ — reachable from the web UI too.{R}"""


HELP = _build_help()


def _handle(line, s):
    """Returns False to keep looping; raises SystemExit on /quit."""
    parts = line[1:].split()
    cmd = (parts[0] if parts else "").lower()
    arg = " ".join(parts[1:]).strip()
    if cmd in ("quit", "exit", "q"):
        s.save(); print(f"{CYAN}≈ bye{R}"); raise SystemExit(0)
    elif cmd in ("new", "reset", "clear"):
        s.save(); s.new(); print(f"{CYAN}≈ new voyage{R}")
    elif cmd == "chats":
        _cmd_chats(s)
    elif cmd == "status":
        _cmd_status(s)
    elif cmd == "context":
        _cmd_context(s, arg)
    elif cmd == "compact":
        _cmd_compact(s)
    elif cmd == "model":
        if arg:
            s.agent.model = arg; print(f"{GREY}model → {arg}{R}")
        else:
            print(f"{GREY}model: {s.agent.model}  ·  /model <name> to switch{R}")
    elif cmd == "editor":
        body = _editor_compose(arg)
        if body:
            print(f"{GREEN}{B}you ›{R} {GREY}(from $EDITOR, {len(body)} chars){R}")
            _turn(s, body)
        else:
            print(f"{GREY}nothing to send.{R}")
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
        print(HELP)
    else:
        print(f"{GREY}unknown command /{cmd} — /help for the list{R}")
    return False


# ============================ main loop ============================
def main():
    _load_prefs()
    _apply_theme(PREFS.get("theme", "abyss"))
    _init_readline()
    print()
    banner()
    s = Session()
    while True:
        try:
            line = _prompt("you").strip()
        except EOFError:
            s.save(); print(f"{CYAN}≈ bye{R}"); break
        except KeyboardInterrupt:
            print(f"{GREY}(/quit to leave){R}"); continue
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
