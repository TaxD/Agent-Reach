"""The signed protocol with the hardened OpenCLI daemon, without a daemon or network."""

import hashlib
import hmac

import pytest

from agent_reach.runtime import bridge, browser
from agent_reach.runtime.result import ReachError

# conftest replaces these for every test so nothing reaches port 19825; keep the real ones here.
REAL_STATUS = bridge.status
REAL_START = bridge.start
TOKEN = "f" * 16 + "0123456789abcdef" * 3


@pytest.fixture
def token():
    path = browser.daemon_token_path()
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text(TOKEN + "\n")
    path.chmod(0o600)
    assert browser.ensure_token() == path and path.read_text().strip() == TOKEN
    return TOKEN.encode()


def _proof(key: bytes, nonce: str) -> str:
    return hmac.new(key, ("opencli-daemon-proof\n" + nonce).encode(), hashlib.sha256).hexdigest()


class FakeDaemon:
    """Answers bridge._open like a daemon on the port; records every request it receives."""

    def __init__(self, key=None, status=None):
        self.key, self.requests = key, []
        self.reply_status = status or {"ok": True, "daemonVersion": "1.8.7", "profiles": []}

    def __call__(self, path, headers):
        self.requests.append((path, dict(headers)))
        if path.startswith("/ping?nonce="):
            nonce = path.split("=", 1)[1]
            return {"ok": True, **({"proof": _proof(self.key, nonce)} if self.key else {})}
        if path == "/ping":
            return {"ok": True}
        if path == "/status":
            return self.reply_status
        raise AssertionError(path)


def test_signature_matches_the_documented_hmac(token, monkeypatch):
    monkeypatch.setattr(bridge.time, "time", lambda: 1700000000.1234)
    monkeypatch.setattr(bridge.secrets, "token_hex", lambda n: "ab" * n)
    body = b'{"action":"exec"}'
    header = bridge.signature("post", "/command", body)
    nonce = "ab" * 16
    message = "\n".join(["POST", "/command", "1700000000123", nonce, hashlib.sha256(body).hexdigest()])
    expected = hmac.new(token, message.encode(), hashlib.sha256).hexdigest()
    assert header == f"OpenCLI-HMAC ts=1700000000123;nonce={nonce};sig={expected}"
    assert TOKEN not in header


def test_signature_covers_method_path_and_body(token):
    def sig(*args):
        return bridge.signature(*args).rsplit("sig=", 1)[1]

    assert bridge.signature("GET", "/status").startswith("OpenCLI-HMAC ts=")
    first, second = bridge.signature("GET", "/status"), bridge.signature("GET", "/status")
    assert first.split(";")[1] != second.split(";")[1], "every request has a fresh nonce"
    assert len({sig("GET", "/status"), sig("POST", "/status"), sig("GET", "/command"),
                sig("GET", "/status", b"x")}) == 4


def test_daemon_is_ours_only_with_the_right_proof(token, monkeypatch):
    daemon = FakeDaemon(key=token)
    monkeypatch.setattr(bridge, "_open", daemon)
    assert bridge.daemon_is_ours() is True
    path, headers = daemon.requests[0]
    assert path.startswith("/ping?nonce=") and len(path.split("=", 1)[1]) == 32
    assert "Authorization" not in headers and TOKEN not in path

    monkeypatch.setattr(bridge, "_open", FakeDaemon(key=b"another machine's token"))
    assert bridge.daemon_is_ours() is False
    monkeypatch.setattr(bridge, "_open", FakeDaemon(key=None))
    assert bridge.daemon_is_ours() is False
    for reply in ({"proof": 123}, {"proof": ""}, ["proof"], "proof", None):
        monkeypatch.setattr(bridge, "_open", lambda path, headers, r=reply: r)
        assert bridge.daemon_is_ours() is False

    replayed = {}

    def replay(path, headers):  # a squatter that replays a proof it saw for another nonce
        replayed.setdefault("proof", _proof(token, "0" * 32))
        return {"proof": replayed["proof"]}

    monkeypatch.setattr(bridge, "_open", replay)
    assert bridge.daemon_is_ours() is False

    def down(path, headers):
        raise OSError("connection refused")

    monkeypatch.setattr(bridge, "_open", down)
    assert bridge.daemon_is_ours() is False


def test_a_listener_that_cannot_prove_itself_gets_nothing(token, monkeypatch):
    monkeypatch.setattr(bridge, "status", REAL_STATUS)
    squatter = FakeDaemon(key=None)
    monkeypatch.setattr(bridge, "_open", squatter)
    with pytest.raises(ReachError) as caught:
        bridge.status()
    assert caught.value.code == "NEED_SETUP"
    assert all("Authorization" not in headers for _, headers in squatter.requests)
    assert [p for p, _ in squatter.requests if not p.startswith("/ping")] == [], "no command was sent to it"


class _Answer:
    def __init__(self, code, body, headers):
        self.status, self._body, self.headers = code, body, headers

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_opener(monkeypatch, answer_for):
    seen = []

    class Opener:
        def open(self, request, timeout=None):
            seen.append(request)
            return answer_for(request)

    monkeypatch.setattr(bridge.urllib.request, "build_opener", lambda *a: Opener())
    return seen


def _response_proof(key, challenge, code, body):
    import hashlib
    import hmac

    message = "\n".join(["opencli-response", challenge, str(code), hashlib.sha256(body).hexdigest()])
    raw = key if isinstance(key, bytes) else key.encode()
    return hmac.new(raw, message.encode(), hashlib.sha256).hexdigest()


def test_status_is_signed_and_its_answer_must_be_authenticated(token, monkeypatch):
    monkeypatch.setattr(bridge, "status", REAL_STATUS)
    monkeypatch.setattr(bridge, "daemon_is_ours", lambda: True)
    body = b'{"ok": true, "daemonVersion": "1.8.7", "pid": 4242}'

    def honest(request):
        challenge = request.get_header("X-opencli-challenge")
        return _Answer(200, body, {"X-OpenCLI-Proof": _response_proof(token, challenge, 200, body)})

    seen = _fake_opener(monkeypatch, honest)
    assert bridge.status()["daemonVersion"] == "1.8.7"
    auth = seen[0].get_header("Authorization")
    assert auth.startswith("OpenCLI-HMAC ") and TOKEN not in auth

    # A listener that took the port after the proof check: its answer (and the pid in it) is never used.
    for forged in (lambda r: _Answer(200, body, {}),
                   lambda r: _Answer(200, body, {"X-OpenCLI-Proof": "0" * 64}),
                   lambda r: _Answer(200, b'{"ok": true, "pid": 1}', {"X-OpenCLI-Proof": _response_proof(
                       token, r.get_header("X-opencli-challenge"), 200, body)}),
                   lambda r: _Answer(200, body, {"X-OpenCLI-Proof": _response_proof(token, "f" * 32, 200, body)})):
        _fake_opener(monkeypatch, forged)
        assert bridge.status() == {}


def test_status_when_nothing_listens_is_empty(token, monkeypatch):
    monkeypatch.setattr(bridge, "status", REAL_STATUS)

    def down(path, headers):
        raise OSError("connection refused")

    monkeypatch.setattr(bridge, "_open", down)
    assert bridge.status() == {}


def test_env_carries_the_token_path_never_the_token(token):
    env = bridge.env()
    home = str(browser.browser_root() / "opencli-home")
    assert env == {"OPENCLI_DAEMON_TOKEN_FILE": str(browser.daemon_token_path()), "OPENCLI_DAEMON_MANAGED": "1",
                   "HOME": home, "USERPROFILE": home}
    assert home != str(browser.home_dir()), "OpenCLI must never see the user's real ~/.opencli"
    assert not any(TOKEN in value for value in env.values())


class FakeProc:
    pid = 4242

    def __init__(self):
        self.events = []

    def poll(self):
        return None

    def terminate(self):
        self.events.append("terminate")

    def wait(self, timeout=None):
        self.events.append("wait")

    def kill(self):
        self.events.append("kill")


def test_start_stops_its_own_orphan_before_starting_and_fails_if_it_stays(token, monkeypatch):
    """A daemon that proves our token while no watcher runs is our own leftover: ask it to stop, never adopt it."""
    monkeypatch.setattr(bridge, "status", lambda: {"ok": True, "daemonVersion": "1.8.7", "pid": 1})
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda *a, **k: pytest.fail("started a daemon beside the old one"))
    sent = []

    class Opener:
        def open(self, request, timeout=None):
            sent.append((request.get_method(), request.full_url, request.get_header("Authorization")))
            raise OSError("gone")

    monkeypatch.setattr(bridge.urllib.request, "build_opener", lambda *a: Opener())
    monkeypatch.setattr(bridge, "_something_listens", lambda: True)
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)
    clock = iter(range(0, 1000, 3))
    monkeypatch.setattr(bridge.time, "monotonic", lambda: next(clock))
    with pytest.raises(ReachError) as caught:
        REAL_START()
    assert caught.value.code == "NEED_SETUP"
    method, url, auth = sent[0]
    assert (method, url) == ("POST", bridge.URL + "/shutdown") and auth.startswith("OpenCLI-HMAC ") and TOKEN not in auth


def test_start_runs_our_daemon_with_the_token_file_and_never_adopts_another(token, monkeypatch):
    proc = FakeProc()
    launched = []
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda argv, **kw: launched.append((argv, kw)) or proc)
    replies = iter([{}, {"ok": True, "daemonVersion": "1.8.7", "pid": proc.pid}])
    monkeypatch.setattr(bridge, "status", lambda: next(replies))
    assert REAL_START() is proc
    argv, kwargs = launched[0]
    assert argv == ["node", "daemon.js"]
    assert kwargs["env"]["OPENCLI_DAEMON_TOKEN_FILE"] == str(browser.daemon_token_path())
    assert not any(TOKEN in str(v) for v in list(kwargs["env"].values()) + argv)

    other = FakeProc()
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda argv, **kw: other)
    replies = iter([{}, {"ok": True, "daemonVersion": "1.8.7", "pid": 1}])
    monkeypatch.setattr(bridge, "status", lambda: next(replies))
    with pytest.raises(ReachError):
        REAL_START()
    assert other.events[0] == "terminate", "the child we started is stopped, the other daemon left alone"


def test_require_running_rejects_a_daemon_of_another_version(token, monkeypatch):
    monkeypatch.setattr(bridge, "status", lambda: {"ok": True, "daemonVersion": "1.0.0"})
    with pytest.raises(ReachError) as caught:
        bridge.require_running()
    assert caught.value.code == "NEED_SETUP"
    monkeypatch.setattr(bridge, "status", lambda: {})
    with pytest.raises(ReachError):
        bridge.require_running()


def test_node_children_never_inherit_inspector_or_plugin_settings(token, monkeypatch):
    for name in ("NODE_OPTIONS", "NODE_PATH", "OPENCLI_DAEMON_PORT", "npm_config_userconfig"):
        monkeypatch.setenv(name, "--inspect=0.0.0.0:9229")
    monkeypatch.setenv("PATH_KEEP", "1")
    cleaned = bridge.clean_environ()
    assert "PATH_KEEP" in cleaned
    assert not [k for k in cleaned if k.startswith(bridge.UNSAFE_ENV_PREFIXES)]
