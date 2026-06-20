"""Interactive terminal — a PTY-backed login shell started in the workspace, bridged to xterm.js
over a WebSocket.

This is an UNGUARDED shell running as the daemon user: it deliberately bypasses the agent's
safety guards (safety.check_shell, the SSRF guard, workspace confinement) because a HUMAN is
driving, not the model. It is fenced only by the systemd sandbox the daemon already runs under
(NoNewPrivileges, ProtectHome=read-only, ReadWritePaths = workspace/data/skills). The WebSocket
route auth-gates the connection (valid session cookie, non-default password) before calling serve().

Protocol: client → server is JSON text — {"t":"i","d":<keystrokes>} for input, {"t":"r","c":cols,
"r":rows} for resize. Server → client is raw terminal bytes (binary frames).
"""
import asyncio
import fcntl
import json
import os
import pty
import signal
import struct
import termios

import config

_SHELL = os.environ.get("OCEANO_SHELL") or "/bin/bash"


def _set_winsize(fd, rows, cols):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0))
    except OSError:
        pass


async def serve(ws):
    """Run a shell PTY and pump bytes between it and the (already-accepted) WebSocket until either
    side closes, then kill + reap the shell."""
    from starlette.websockets import WebSocketDisconnect

    pid, fd = pty.fork()
    if pid == 0:                                    # child → become the login shell in the workspace
        try:
            os.chdir(str(config.WORKSPACE))
        except OSError:
            pass
        os.environ["TERM"] = "xterm-256color"
        os.environ.setdefault("LANG", "C.UTF-8")
        os.environ["OCEANO_TERMINAL"] = "1"
        try:
            os.execvp(_SHELL, [_SHELL, "-l"])
        except OSError:
            os._exit(127)
        os._exit(127)

    loop = asyncio.get_running_loop()
    os.set_blocking(fd, False)
    _set_winsize(fd, 24, 80)

    async def pty_to_ws():
        q = asyncio.Queue()

        def on_readable():
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                return
            except OSError:
                data = b""                          # shell gone
            q.put_nowait(data)

        loop.add_reader(fd, on_readable)
        try:
            while True:
                data = await q.get()
                if not data:                        # EOF → shell exited
                    return
                await ws.send_bytes(data)
        finally:
            try:
                loop.remove_reader(fd)
            except (OSError, ValueError):
                pass

    async def ws_to_pty():
        while True:
            try:
                txt = await ws.receive_text()
            except WebSocketDisconnect:
                return
            except Exception:
                return
            try:
                m = json.loads(txt)
                if m.get("t") == "i":
                    os.write(fd, m["d"].encode("utf-8"))
                elif m.get("t") == "r":
                    _set_winsize(fd, int(m.get("r", 24)), int(m.get("c", 80)))
            except (ValueError, KeyError, TypeError):
                continue                            # ignore a malformed frame, keep the session
            except OSError:
                return                              # the pty is gone

    t_out = asyncio.create_task(pty_to_ws())
    t_in = asyncio.create_task(ws_to_pty())
    try:
        await asyncio.wait({t_out, t_in}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (t_out, t_in):
            t.cancel()
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)                      # reap the child
        except OSError:
            pass


async def serve_host(ws, h, secret):
    """A LIVE interactive SSH session into a registered host h, bridged to the (already-accepted)
    WebSocket — reuses the keychain's host-key pinning + key/passphrase via hosts._open(). An
    interactive shell can't be command-filtered like ssh_run, so the caller gates it on the host's
    arming/trusted policy."""
    from oceano import hosts

    try:
        cli, _ = await asyncio.to_thread(hosts._open, h, secret, False)
    except Exception as e:                          # noqa: BLE001
        try:
            await ws.send_bytes(f"\r\n\x1b[31mconnect failed: {hosts._clean_err(e)}\x1b[0m\r\n".encode())
        except Exception:
            pass
        return
    loop = asyncio.get_running_loop()
    chan = cli.invoke_shell(term="xterm-256color", width=80, height=24)
    fd = chan.fileno()                              # readable when the channel has data (select-friendly)
    q = asyncio.Queue()

    def on_readable():
        try:
            while chan.recv_ready():
                d = chan.recv(65536)
                if not d:
                    break
                q.put_nowait(d)
            if chan.closed or chan.eof_received or chan.exit_status_ready():
                q.put_nowait(None)
        except Exception:                           # noqa: BLE001
            q.put_nowait(None)

    loop.add_reader(fd, on_readable)

    async def chan_to_ws():
        while True:
            d = await q.get()
            if d is None:
                return
            try:
                await ws.send_bytes(d)
            except Exception:
                return

    async def ws_to_chan():
        while True:
            try:
                txt = await ws.receive_text()
            except Exception:
                return
            try:
                m = json.loads(txt)
                if m.get("t") == "i":
                    chan.send(m["d"].encode("utf-8"))
                elif m.get("t") == "r":
                    chan.resize_pty(width=int(m.get("c", 80)), height=int(m.get("r", 24)))
            except (ValueError, KeyError, TypeError):
                continue
            except Exception:                       # channel dead
                return

    t_out = asyncio.create_task(chan_to_ws())
    t_in = asyncio.create_task(ws_to_chan())
    try:
        await asyncio.wait({t_out, t_in}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (t_out, t_in):
            t.cancel()
        try:
            loop.remove_reader(fd)
        except (OSError, ValueError):
            pass
        try:
            chan.close()
        except Exception:
            pass
        try:
            cli.close()
        except Exception:
            pass
