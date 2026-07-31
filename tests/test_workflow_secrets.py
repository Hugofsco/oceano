"""Workflow secrets ({{secret.NAME}}): encrypted-at-rest storage, write-only API surface,
resolution ONLY inside the HTTP node (with redaction of resolved values from recorded
output), and http-header encryption at rest in the workflow store.
"""
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import secretcrypto, workflows  # noqa: E402 - after the sys.path bootstrap


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(workflows, "STORE", tmp_path / "workflows.json")
    monkeypatch.setattr(workflows, "RUNS_STORE", tmp_path / "workflow_runs.json")
    monkeypatch.setattr(workflows, "SECRETS_STORE", tmp_path / "wf_secrets.json")
    monkeypatch.setattr(workflows, "TRIG_STATE", tmp_path / "trigger_state.json")
    monkeypatch.setattr(workflows, "_LIVE", {})
    monkeypatch.setattr(secretcrypto, "_KEY_FILE", tmp_path / "oceano.env.key")
    monkeypatch.setattr(secretcrypto, "_key_cache", None)
    monkeypatch.delenv("OCEANO_DATA_KEY", raising=False)
    monkeypatch.setattr("oceano.logs.log_run", lambda *a, **k: None)
    yield


# ---------------- the store ----------------
def test_secret_crud_and_encryption_at_rest():
    assert workflows.set_secret("GITHUB_TOKEN", "ghp_hunter2")
    assert workflows.list_secrets() == ["GITHUB_TOKEN"]
    on_disk = json.loads(workflows.SECRETS_STORE.read_text())["secrets"]["GITHUB_TOKEN"]
    assert on_disk != "ghp_hunter2" and on_disk.startswith("enc:v1:")
    assert workflows.delete_secret("GITHUB_TOKEN") is True
    assert workflows.list_secrets() == []
    assert workflows.delete_secret("GITHUB_TOKEN") is False


def test_secret_names_and_empty_values_are_validated():
    assert not workflows.set_secret("", "x")
    assert not workflows.set_secret("9starts-with-digit", "x")
    assert not workflows.set_secret("has space", "x")
    assert not workflows.set_secret("no-value", "")
    assert workflows.set_secret("ok.Name_1-x", "v")


# ---------------- resolution: HTTP node only, redacted output ----------------
class _FakeResp:
    is_redirect = False
    ok = True
    status_code = 200
    headers = {}

    def __init__(self, text):
        self.text = text


def _stub_requests(monkeypatch, seen, reply_text):
    """Stub the GUARDED request path. The http node used to call requests.request directly, which
    re-resolved the host after check_url had already resolved it — reopening the DNS-rebinding window
    the pinned adapter exists to close. Patching safety.guarded_request (rather than the requests
    module) both keeps these tests working and pins that the node goes through the guard."""
    def request(method, url, headers=None, data=None, timeout=0, allow_redirects=True):
        seen.update(method=method, url=url, headers=headers or {}, data=data)
        return _FakeResp(reply_text)
    monkeypatch.setattr("oceano.safety.guarded_request", request)
    monkeypatch.setitem(sys.modules, "requests",
                        types.SimpleNamespace(request=request,
                                              compat=types.SimpleNamespace(urljoin=lambda a, b: b)))


def test_http_node_resolves_secrets_and_redacts_them_from_output(monkeypatch):
    workflows.set_secret("API_KEY", "sk-veryhush")
    monkeypatch.setattr("oceano.safety.check_url", lambda u: None)
    seen = {}
    _stub_requests(monkeypatch, seen, reply_text="you sent sk-veryhush back")   # an echoing API
    node = {"method": "POST", "url": "https://api.example.com/q?key={{secret.API_KEY}}",
            "headers": {"Authorization": "Bearer {{secret.API_KEY}}"}, "body": "k={{secret.API_KEY}}"}
    ok, out = workflows._run_http(node, {"input": "", "last": "", "nodes": {}})
    assert ok is True
    # the real value reached the wire in all three places…
    assert seen["url"].endswith("key=sk-veryhush")
    assert seen["headers"]["Authorization"] == "Bearer sk-veryhush"
    assert seen["data"] == b"k=sk-veryhush"
    # …but never the recorded output, even when the API echoes it
    assert "sk-veryhush" not in out and "•••" in out


def test_unknown_secret_renders_empty_and_templating_cannot_reach_secrets():
    workflows.set_secret("API_KEY", "sk-veryhush")
    # the general templating engine (instruction/agent/transform text) must NOT resolve secrets —
    # otherwise a prompt-injected step could exfiltrate one
    assert workflows._tmpl("{{secret.API_KEY}}", {"input": "", "last": "", "nodes": {}}) == ""
    used = []
    assert workflows._fill_secrets("x={{secret.NOPE}}", used) == "x="
    assert used == []


def test_http_error_paths_are_redacted_too(monkeypatch):
    workflows.set_secret("TOK", "hush-tok")
    monkeypatch.setattr("oceano.safety.check_url", lambda u: f"refused: {u}")
    node = {"method": "GET", "url": "https://x.example/{{secret.TOK}}", "headers": {}, "body": ""}
    ok, out = workflows._run_http(node, {"input": "", "last": "", "nodes": {}})
    assert ok is False
    assert "hush-tok" not in out and "•••" in out


# ---------------- http headers encrypted at rest in the workflow store ----------------
def test_http_headers_encrypted_at_rest_plaintext_on_read():
    graph = {"nodes": [{"id": 1, "type": "start"},
                       {"id": 2, "type": "http", "method": "GET", "url": "https://x.example",
                        "headers": {"Authorization": "Bearer raw-token"}},
                       {"id": 3, "type": "end"}],
             "edges": [{"from": 1, "to": 2}, {"from": 2, "to": 3}]}
    wf = workflows.create("h", graph=graph)
    stored = json.loads(workflows.STORE.read_text())["workflows"][0]
    stored_val = next(n for n in stored["graph"]["nodes"] if n["type"] == "http")["headers"]["Authorization"]
    assert stored_val != "Bearer raw-token" and stored_val.startswith("enc:v1:")
    # every read path hands plaintext back (create's return, get, list_all)
    assert wf["graph"]["nodes"][1]["headers"]["Authorization"] == "Bearer raw-token"
    assert workflows.get(wf["id"])["graph"]["nodes"][1]["headers"]["Authorization"] == "Bearer raw-token"
    assert workflows.list_all()[0]["graph"]["nodes"][1]["headers"]["Authorization"] == "Bearer raw-token"


def test_http_headers_run_with_plaintext_after_encryption(monkeypatch):
    monkeypatch.setattr("oceano.safety.check_url", lambda u: None)
    seen = {}
    _stub_requests(monkeypatch, seen, reply_text="fine")
    graph = {"nodes": [{"id": 1, "type": "start"},
                       {"id": 2, "type": "http", "method": "GET", "url": "https://x.example",
                        "headers": {"X-Auth": "topsecret"}},
                       {"id": 3, "type": "end"}],
             "edges": [{"from": 1, "to": 2}, {"from": 2, "to": 3}]}
    created = workflows.create("h2", graph=graph)
    rec = workflows.run(workflows.get(created["id"]), trigger="manual", nested=True)
    assert rec["status"] == "ok"
    assert seen["headers"]["X-Auth"] == "topsecret"           # decrypt-at-use, not ciphertext