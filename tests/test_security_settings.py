"""Settings → Security: the runtime toggles that arm/disarm each guard and taint gate.

The store is data/security.json (isolated per test by conftest). What must hold:

  * defaults are the protective posture — every guard/gate on, background remote hosts off
  * a toggle turned off opens exactly its own capability and nothing else
  * taint TRACKING is not togglable: with a gate off the turn still gets (and keeps) its taint,
    so re-enabling the gate protects immediately
  * the OCEANO_*_GUARD env kill-switches win over the toggles (a guard pinned off by env can't
    be re-armed from Settings)
  * unknown keys sent to set_security are dropped, and settings persist through the file
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import safety  # noqa: E402


def test_defaults_are_all_protective():
    s = safety.security_settings()
    for key, want in safety.SECURITY_DEFAULTS.items():
        assert s[key] is want, f"{key} must default to {want}"
    assert s["remote_hosts_background"] is False


def test_set_security_persists_and_ignores_unknown_keys():
    out = safety.set_security({"taint_egress": False, "definitely_not_a_key": True})
    assert out["taint_egress"] is False
    assert "definitely_not_a_key" not in out
    safety._sec_cache = None                     # force a re-read from the file
    assert safety.security_settings()["taint_egress"] is False


def test_shell_guard_toggle_disarms_and_rearms_check_shell():
    bad = "rm -rf --no-preserve-root /"
    assert safety.check_shell(bad) is not None
    safety.set_security({"shell_guard": False})
    assert safety.check_shell(bad) is None
    # string literal only, never executed — it's the payload check_python inspects
    assert safety.check_python("os.system('rm -rf --no-preserve-root /')") is None
    safety.set_security({"shell_guard": True})
    assert safety.check_shell(bad) is not None


def test_url_guard_toggle_disarms_check_url(monkeypatch):
    monkeypatch.setattr(safety.socket, "getaddrinfo",
                        lambda host, port: [(2, 1, 6, "", ("127.0.0.1", 0))])
    assert safety.check_url("http://evil.example/") is not None
    safety.set_security({"url_guard": False})
    assert safety.check_url("http://evil.example/") is None


def test_env_kill_switch_beats_the_toggle(monkeypatch):
    """OCEANO_SHELL_GUARD=0 pins the guard off; the Settings toggle can't re-arm it."""
    safety.set_security({"shell_guard": True})
    monkeypatch.setattr(safety, "SHELL_GUARD", False)
    assert safety.check_shell("rm -rf --no-preserve-root /") is None
    monkeypatch.setattr(safety, "URL_GUARD", False)
    assert safety.check_url("http://127.0.0.1/") is None


@pytest.mark.parametrize("gate, blocked", [
    ("spawn", safety.spawn_blocked),
    ("egress", safety.egress_blocked),
    ("persist", safety.persist_blocked),
])
def test_each_taint_gate_follows_its_own_toggle_only(gate, blocked):
    safety.mark_untrusted()
    assert blocked() is not None, f"{gate} gate must fire on a tainted turn by default"
    safety.set_security({f"taint_{gate}": False})
    assert blocked() is None, f"{gate} gate must open when ITS toggle is off"
    for other, fn in [("spawn", safety.spawn_blocked), ("egress", safety.egress_blocked),
                      ("persist", safety.persist_blocked)]:
        if other != gate:
            assert fn() is not None, f"disabling taint_{gate} must not open the {other} gate"
    safety.set_security({f"taint_{gate}": True})
    assert blocked() is not None, "re-enabling the gate protects immediately"


def test_disabling_a_gate_never_clears_the_taint_itself():
    safety.set_security({"taint_egress": False})
    safety.wrap_untrusted("web", "injected page")
    assert safety.egress_blocked() is None
    assert safety.injection_tainted() is True, "taint tracking must survive a disabled gate"
    safety.set_security({"taint_egress": True})
    assert safety.egress_blocked() is not None, "the still-tainted turn is re-gated at once"


def test_taint_active_covers_both_taint_sources():
    assert safety.taint_active("exec") is False
    safety.mark_bridge_untrusted()
    assert safety.taint_active("exec") is True, "the resident-mind bridge flag must count"
    safety.reset_bridge_untrusted()
    safety.mark_untrusted()
    assert safety.taint_active("exec") is True


def test_shell_and_dev_tools_follow_the_exec_toggle():
    from oceano.tools import dev, shell
    safety.mark_untrusted()
    assert shell._shell_blocked() is not None
    assert dev._exec_blocked() is not None
    safety.set_security({"taint_exec": False})
    assert shell._shell_blocked() is None
    assert dev._exec_blocked() is None
    assert safety.spawn_blocked() is not None, "exec toggle must not open the spawn gate"


def test_remote_hosts_master_switch_refuses_everything(monkeypatch):
    """remote_hosts_enabled=False beats every other allowance — clean turn, web channel,
    background permission on — for all three host tools."""
    from oceano.tools import hosts_tools
    monkeypatch.setattr(hosts_tools, "current_channel", lambda: "web")
    safety.set_security({"remote_hosts_enabled": False, "remote_hosts_background": True})
    assert "turned off" in hosts_tools.list_hosts()
    assert "turned off" in hosts_tools.ssh_run("box", ["uptime"])
    assert "turned off" in hosts_tools.sftp("list", "box", remote_path="/tmp")
    safety.set_security({"remote_hosts_enabled": True})
    assert "turned off" not in hosts_tools.ssh_run("box", ["uptime"])


def test_remote_hosts_background_toggle(monkeypatch):
    from oceano.tools import hosts_tools
    monkeypatch.setattr(hosts_tools, "current_channel", lambda: "telegram")
    assert "only usable from the web UI" in hosts_tools.list_hosts()
    out = hosts_tools.ssh_run("box", ["uptime"])
    assert "only runs in the web UI" in out
    safety.set_security({"remote_hosts_background": True})
    # channel gate opens; the call now proceeds to host resolution ("no host named …")
    assert "no host named" in hosts_tools.ssh_run("box", ["uptime"])
    assert "only usable from the web UI" not in hosts_tools.list_hosts()


def test_remote_taint_gate_follows_its_toggle(monkeypatch):
    from oceano.tools import hosts_tools
    monkeypatch.setattr(hosts_tools, "current_channel", lambda: "web")
    safety.mark_untrusted()
    assert "Blocked for safety" in hosts_tools.ssh_run("box", ["uptime"])
    safety.set_security({"taint_remote": False})
    assert "no host named" in hosts_tools.ssh_run("box", ["uptime"])
