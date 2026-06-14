"""Oceano in the terminal — a rich, opencode-style chat client.

The simplest frontend, now with the same comforts as the web UI / Telegram: streamed
reasoning + tool calls + answer, per-turn stats, and SESSIONS that persist to the shared
chat store (data/chats/) — so a conversation you start here shows up in the web UI too,
and vice-versa. Slash commands handle the session (type /help).

    python cli.py            # (use the venv: venv/bin/python cli.py)

Everything runs through the same Agent.run_stream() the other frontends use — frontends
stay thin. No external deps: just ANSI + stdlib readline for line editing/history.
"""
import re
import secrets
import shutil
import sys
import time

try:
    import readline  # noqa: F401 — arrow-key editing + in-session history (stdlib, optional)
except Exception:
    pass

import config
from oceano import chats
from oceano.agent import Agent

# ---- palette (abyssal console: bioluminescent cyan, warm user-green, dim depths) ----
R = "\033[0m"; B = "\033[1m"; DIM = "\033[2m"; IT = "\033[3m"
CYAN = "\033[36m"; GREEN = "\033[32m"; BLUE = "\033[34m"; GREY = "\033[90m"; CORAL = "\033[38;5;210m"; BUOY = "\033[38;5;215m"
def _wrap(s):  # mark non-printing runs so readline measures the prompt width correctly
    return re.sub(r"(\033\[[0-9;]*m)", "\001\\1\002", s)
OCEANO = f"{CYAN}{B}oceano ›{R} "

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
    print(f"{GREY}  /help · /chats to resume · Ctrl-C interrupts a reply{R}")


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
            print(f"{CYAN}{B}oceano ›{R} {m.get('content', '')}")
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
    """Run one streamed turn, render it, fold it into the transcript, and persist."""
    s.messages.append({"role": "user", "content": text})
    if s.title in ("", "New voyage"):
        s.title = text[:60]
    if s.cap and len(s.agent.messages) > s.cap:           # auto-compact like Telegram/web
        dropped = s.agent.compact()
        if dropped:
            s.compactions += 1
            print(f"{GREY}🗜 auto-compacted {dropped} messages (over {s.cap}){R}")

    asst, think, in_think, printed_prefix = "", "", False, False

    def flush_think():
        nonlocal think, in_think
        if think.strip():
            s.messages.append({"role": "thinking", "text": think})
        think, in_think = "", False

    try:
        for ev in s.agent.run_stream(text):
            t = ev.get("type")
            if t == "reasoning":
                if not in_think:
                    sys.stdout.write(f"\n{GREY}{IT}🤔 "); in_think = True
                sys.stdout.write(ev["text"]); sys.stdout.flush(); think += ev["text"]
            elif t == "token":
                if in_think:
                    sys.stdout.write(R + "\n"); flush_think()
                if not printed_prefix:
                    sys.stdout.write("\n" + OCEANO); printed_prefix = True
                sys.stdout.write(ev["text"]); sys.stdout.flush(); asst += ev["text"]
            elif t == "tool_call":
                if in_think:
                    sys.stdout.write(R + "\n"); flush_think()
                print(f"\n{CYAN}  → {ev['name']}{R}{GREY}({(ev.get('args') or '')[:60]}){R}")
            elif t == "tool_result":
                s.messages.append({"role": "tool", "name": ev["name"], "args": "", "result": ev["result"]})
                prev = (ev.get("result") or "").replace("\n", " ")[:120]
                if prev:
                    print(f"{GREY}    {prev}{R}")
            elif t == "answer_done":
                pass
            elif t == "stats":
                line = _stats_line(ev)
                if line:
                    print("\n" + line)
    except KeyboardInterrupt:
        print(f"\n{CORAL}⏹ interrupted{R}")
    if in_think:
        sys.stdout.write(R + "\n"); flush_think()
    if asst.strip():
        s.messages.append({"role": "assistant", "content": asst})
    print()
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


HELP = f"""{B}Commands{R}
  {CYAN}/chats{R}            list & open saved conversations
  {CYAN}/new{R} {GREY}(/reset){R}     start a fresh conversation
  {CYAN}/compact{R}          summarize & shrink the context now
  {CYAN}/context{R} [n|off]  show context size, or set/clear auto-compact
  {CYAN}/status{R}           model · context · session info
  {CYAN}/model{R} [name]     show or switch the chat model
  {CYAN}/help{R}             this list
  {CYAN}/quit{R} {GREY}(/q){R}        leave (the conversation is already saved)
{GREY}Anything else is a message. Sessions persist to data/chats/ — reachable from the web UI too.{R}"""


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
    elif cmd in ("help", "h", "?"):
        print(HELP)
    else:
        print(f"{GREY}unknown command /{cmd} — /help for the list{R}")
    return False


# ============================ main loop ============================
def main():
    print()
    banner()
    s = Session()
    while True:
        try:
            line = _read_boxed("you").strip()
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
