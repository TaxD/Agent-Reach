"""The control socket, the CDP pipe and the watcher's commands, without a real Chrome."""

import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import time
from pathlib import Path

import pytest

from agent_reach.channels.reddit import RedditChannel
from agent_reach.channels.twitter import TwitterChannel
from agent_reach.runtime import browser, browser_watcher, cdp

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="unix sockets only")


@pytest.fixture
def short_home(monkeypatch):
    """macOS limits unix socket paths to 104 bytes; the pytest tmp_path is longer than that."""
    home = Path(os.path.realpath(tempfile.mkdtemp(prefix="ar", dir="/tmp")))
    monkeypatch.setenv("HOME", str(home))
    yield home
    shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def server(short_home):
    seen = []

    def echo(**kwargs):
        seen.append(kwargs)
        return {"ok": True, "got": kwargs}

    def boom(**kwargs):
        raise ValueError("secret detail")

    srv = cdp.ControlServer({"ping": lambda **_: {"ok": True}, "echo": echo, "boom": boom})
    yield srv, seen
    srv.close()


def _raw(line: bytes) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(str(cdp.socket_path()))
        sock.sendall(line)
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf)


# ── control socket ──


def test_request_round_trip(server):
    _, seen = server
    assert cdp.request("ping") == {"ok": True}
    assert cdp.request("echo", url="https://x.test/", n=2) == {"ok": True, "got": {"url": "https://x.test/", "n": 2}}
    assert seen == [{"url": "https://x.test/", "n": 2}]


def test_unknown_commands_are_refused_without_running_anything(server):
    _, seen = server
    for command in ("Storage.getCookies", "Runtime.evaluate", "cookies", "", "__init__"):
        assert cdp.request(command) == {"ok": False, "error": "unknown command"}
    assert _raw(b'{"url": "https://x.test/"}\n') == {"ok": False, "error": "unknown command"}
    assert _raw(b'["echo"]\n') == {"ok": False, "error": "unknown command"}
    assert seen == []


def test_bad_requests_get_an_error_without_details(server):
    assert _raw(b"not json\n") == {"ok": False, "error": "JSONDecodeError"}
    reply = cdp.request("boom")
    assert reply == {"ok": False, "error": "ValueError"} and "secret" not in json.dumps(reply)
    assert cdp.request("ping") == {"ok": True}, "one bad request does not stop the server"


def test_socket_is_private_to_this_user(server):
    path = cdp.socket_path()
    assert stat.S_ISSOCK(path.lstat().st_mode)
    assert path.lstat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.parent == browser.browser_root()


def test_request_without_a_watcher_returns_none(short_home):
    assert cdp.request("ping", timeout=1) is None
    srv = cdp.ControlServer({"ping": lambda **_: {"ok": True}})
    srv.close()
    assert not cdp.socket_path().exists()
    assert cdp.request("ping", timeout=1) is None


def test_a_stale_socket_file_is_replaced(short_home):
    first = cdp.ControlServer({"ping": lambda **_: {"ok": True, "n": 1}})
    first._sock.close()  # a watcher that died without cleaning up
    second = cdp.ControlServer({"ping": lambda **_: {"ok": True, "n": 2}})
    try:
        assert cdp.request("ping") == {"ok": True, "n": 2}
    finally:
        second.close()


def test_domain_match_is_exact_or_subdomain():
    assert cdp.domain_matches(".reddit.com", "reddit.com")
    assert cdp.domain_matches("old.REDDIT.com", "reddit.com")
    assert not cdp.domain_matches("notreddit.com", "reddit.com")
    assert not cdp.domain_matches("reddit.com.evil.test", "reddit.com")


# ── CDP pipe ──


class _FakeChrome:
    """The other end of --remote-debugging-pipe: records commands, answers like Chrome."""

    def __init__(self, attach_ok=True):
        self.to_r, self.to_w = os.pipe()
        self.from_r, self.from_w = os.pipe()
        self.seen = []
        self.attach_ok = attach_ok
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _reply(self, msg):
        method = msg["method"]
        if method == "Target.attachToTarget":
            result = {"sessionId": "S-" + msg["params"]["targetId"]} if self.attach_ok else {}
        elif method == "Target.getTargets":
            result = {"targetInfos": [{"targetId": "T1", "type": "page"}, "junk", 3]}
        else:
            result = {"method": method, "session": msg.get("sessionId")}
        return {"id": msg["id"], "result": result}

    def _run(self):
        buf = b""
        while True:
            chunk = os.read(self.to_r, 4096)
            if not chunk:
                break
            buf += chunk
            while b"\0" in buf:
                raw, buf = buf.split(b"\0", 1)
                msg = json.loads(raw)
                self.seen.append(msg)
                os.write(self.from_w, json.dumps({"method": "Target.targetCreated"}).encode() + b"\0")
                os.write(self.from_w, json.dumps(self._reply(msg)).encode() + b"\0")
        os.close(self.from_w)
        os.close(self.to_r)

    def connect(self):
        return cdp.PipeConnection(self.to_w, self.from_r)


def test_pipe_sends_session_id_only_when_given():
    chrome = _FakeChrome()
    conn = chrome.connect()
    try:
        assert conn.call("Browser.getVersion")["result"]["session"] is None
        assert conn.call("Runtime.evaluate", {"expression": "1"}, session_id="S9")["result"]["session"] == "S9"
        assert "sessionId" not in chrome.seen[0] and chrome.seen[1]["sessionId"] == "S9"
    finally:
        conn.close()
        chrome.thread.join(5)


def test_on_target_attaches_calls_and_detaches_in_order():
    chrome = _FakeChrome()
    conn = chrome.connect()
    try:
        reply = conn.on_target("T1", "Runtime.evaluate", {"expression": "1"})
        assert reply["result"] == {"method": "Runtime.evaluate", "session": "S-T1"}
        assert [m["method"] for m in chrome.seen] == ["Target.attachToTarget", "Runtime.evaluate",
                                                       "Target.detachFromTarget"]
        attach, call, detach = chrome.seen
        assert attach["params"] == {"targetId": "T1", "flatten": True} and "sessionId" not in attach
        assert call["sessionId"] == "S-T1"
        assert detach["params"] == {"sessionId": "S-T1"} and "sessionId" not in detach
        assert conn.targets() == [{"targetId": "T1", "type": "page"}]
    finally:
        conn.close()
        chrome.thread.join(5)


def test_on_target_without_a_session_runs_nothing_on_the_target():
    chrome = _FakeChrome(attach_ok=False)
    conn = chrome.connect()
    try:
        assert "error" in conn.on_target("T1", "Runtime.evaluate", {"expression": "1"})
        assert [m["method"] for m in chrome.seen] == ["Target.attachToTarget"]
    finally:
        conn.close()
        chrome.thread.join(5)


def test_pipe_notices_that_chrome_closed():
    chrome = _FakeChrome()
    conn = chrome.connect()
    assert conn.call("Browser.getVersion")["result"]["method"] == "Browser.getVersion"
    conn.close()
    chrome.thread.join(5)
    assert conn.closed.is_set()
    assert "error" in conn.call("Browser.close", timeout=1)


# ── watcher commands ──


class _FakeConn:
    """A browser with pages and a cookie jar, driven through the same calls the watcher uses."""

    def __init__(self, pages, cookies, keep_cookies=False):
        self.pages = list(pages)
        self.cookies = list(cookies)
        self.keep_cookies = keep_cookies
        self.calls = []
        self.on_targets = []
        self.closed = threading.Event()

    def targets(self):
        return [dict(p, type="page") for p in self.pages] + [{"targetId": "SW", "type": "service_worker"}]

    def call(self, method, params=None, timeout=30.0, session_id=None):
        self.calls.append((method, params))
        if method == "Target.createTarget":
            self.pages.append({"targetId": f"N{len(self.pages)}", "url": params["url"]})
            return {"result": {"targetId": self.pages[-1]["targetId"]}}
        if method == "Target.closeTarget":
            self.pages = [p for p in self.pages if p["targetId"] != params["targetId"]]
            return {"result": {"success": True}}
        if method == "Storage.getCookies":
            return {"result": {"cookies": [dict(c) for c in self.cookies]}}
        if method == "Storage.setCookies":
            if not self.keep_cookies:
                gone = {(c["name"], c["domain"]) for c in params["cookies"]}
                self.cookies = [c for c in self.cookies if (c["name"], c["domain"]) not in gone]
            return {"result": {}}
        return {"result": {}}

    def on_target(self, target_id, method, params=None, timeout=30.0):
        self.on_targets.append((target_id, method, params))
        return {"result": {}}

    def methods(self):
        return [m for m, _ in self.calls]


def _turn_on(*names):
    from agent_reach.runtime import state

    state.mutate(lambda data: data["channels"].update({n: {"added_at": state.now()} for n in names}))


BAD_CHANNELS = ["hackernews", "github", "web", "no-such-channel", "", None, 7, ["reddit"], {"name": "reddit"},
                "https://www.reddit.com/login/", "reddit.com", "REDDIT", " reddit"]


@pytest.mark.parametrize("channel", BAD_CHANNELS + ["reddit"])
def test_open_refuses_anything_but_an_enabled_login_channel(monkeypatch, channel):
    monkeypatch.setattr(browser, "touch", lambda: None)
    _turn_on("hackernews", "github")  # turned on, but without a login
    conn = _FakeConn([{"targetId": "P0", "url": "about:blank"}], [])
    assert browser_watcher._open(conn, channel=channel) == {"ok": False, "error": "not an enabled login channel"}
    assert browser_watcher._open(conn, url=RedditChannel().login_url)["ok"] is False, "a URL is not accepted"
    assert conn.calls == []


def test_open_uses_the_channels_own_login_page(monkeypatch):
    monkeypatch.setattr(browser, "touch", lambda: None)
    _turn_on("reddit", "twitter")
    conn = _FakeConn([{"targetId": "P0", "url": "about:blank"}], [])
    for ch in (RedditChannel(), TwitterChannel()):
        assert browser_watcher._open(conn, channel=ch.name) == {"ok": True}
    assert [p for m, p in conn.calls if m == "Target.createTarget"] == [
        {"url": RedditChannel().login_url}, {"url": TwitterChannel().login_url}]


def test_open_reads_the_enabled_channels_fresh_each_time(monkeypatch):
    from agent_reach.runtime import state

    monkeypatch.setattr(browser, "touch", lambda: None)
    conn = _FakeConn([{"targetId": "P0", "url": "about:blank"}], [])
    _turn_on("reddit")
    assert browser_watcher._open(conn, channel="reddit")["ok"] is True
    state.mutate(lambda data: data["channels"].pop("reddit"))
    assert browser_watcher._open(conn, channel="reddit")["ok"] is False, "a removed channel is refused at once"


JAR = [
    {"name": "session", "value": "r1", "domain": ".reddit.com", "path": "/", "secure": True, "httpOnly": True},
    {"name": "token", "value": "r2", "domain": "www.reddit.com", "path": "/"},
    {"name": "sid", "value": "n1", "domain": "notreddit.com", "path": "/"},
    {"name": "auth", "value": "x1", "domain": ".x.com", "path": "/"},
    {"name": "keep", "value": "g1", "domain": ".google.com", "path": "/"},
]


@pytest.mark.parametrize("channel", BAD_CHANNELS + ["twitter"])
def test_clear_site_refuses_anything_but_an_enabled_login_channel(channel):
    _turn_on("reddit", "hackernews")
    conn = _FakeConn([{"targetId": "P0", "url": "https://www.reddit.com/"}], JAR)
    assert browser_watcher._clear_site(conn, channel=channel) == {"ok": False, "error": "not an enabled login channel"}
    assert browser_watcher._clear_site(conn, domains=["reddit.com"])["ok"] is False, "domains are not accepted"
    assert conn.calls == [] and conn.cookies == JAR


def test_clear_site_expires_only_that_sites_cookies_and_storage():
    _turn_on("reddit", "twitter")
    pages = [{"targetId": "P0", "url": "https://www.reddit.com/r/x"}, {"targetId": "P1", "url": "https://x.com/home"}]
    conn = _FakeConn(pages, JAR)
    assert browser_watcher._clear_site(conn, channel="reddit") == {"ok": True, "cleared": 2}
    assert [c["name"] for c in conn.cookies] == ["sid", "auth", "keep"]
    (expired,) = [p["cookies"] for m, p in conn.calls if m == "Storage.setCookies"]
    assert {(c["name"], c["domain"]) for c in expired} == {("session", ".reddit.com"), ("token", "www.reddit.com")}
    assert all(c["value"] == "" and c["expires"] == 1 for c in expired)
    assert expired[0]["secure"] is True and expired[0]["httpOnly"] is True, "attributes kept so the cookie matches"
    assert ("Target.closeTarget", {"targetId": "P0"}) in conn.calls
    assert [p["targetId"] for p in conn.pages] == ["P1"], "other sites' tabs stay open"
    origins = {params["origin"] for _, method, params in conn.on_targets if method == "Storage.clearDataForOrigin"}
    assert origins and all(o.split("://", 1)[1] in {"reddit.com", "www.reddit.com"} for o in origins)


def test_clear_site_keeps_the_window_open_when_every_tab_was_that_site():
    _turn_on("twitter")
    conn = _FakeConn([{"targetId": "P0", "url": "https://x.com/home"}], JAR)
    assert browser_watcher._clear_site(conn, channel="twitter")["ok"] is True
    assert conn.methods().index("Target.createTarget") < conn.methods().index("Target.closeTarget")
    assert [p["url"] for p in conn.pages] == ["about:blank"]
    assert [c["name"] for c in conn.cookies] == ["session", "token", "sid", "keep"]


def test_clear_site_reports_failure_when_cookies_survive():
    _turn_on("reddit")
    conn = _FakeConn([{"targetId": "P0", "url": "about:blank"}], JAR, keep_cookies=True)
    assert browser_watcher._clear_site(conn, channel="reddit") == {
        "ok": False, "error": "cleanup could not be verified"}


def test_extension_is_loaded_only_when_it_matches_the_pinned_release(monkeypatch):
    conn = _FakeConn([], [])
    monkeypatch.setattr(browser, "extension_intact", lambda: False)
    browser_watcher._load_extension(conn)
    assert conn.calls == []
    monkeypatch.setattr(browser, "extension_intact", lambda: True)
    browser_watcher._load_extension(conn)
    assert conn.calls == [("Extensions.loadUnpacked", {"path": str(browser.extension_dir())})]


def test_watcher_serves_only_the_four_commands_and_cleans_up_its_own_children(monkeypatch):
    from agent_reach.runtime import bridge

    events = []

    class FakeServer:
        def __init__(self, handlers):
            events.append(("handlers", sorted(handlers)))
            self.handlers = handlers

        def close(self):
            events.append("server closed")

    class FakeProc:
        def poll(self):
            return None

        def wait(self, timeout=None):
            events.append("chrome waited")

        def kill(self):
            events.append("chrome killed")

    conn = _FakeConn([], [])
    conn.close = lambda: events.append("pipe closed")
    class FakeDaemon:
        def poll(self):
            return None

    daemon = FakeDaemon()
    monkeypatch.setattr(browser_watcher, "ControlServer", FakeServer)
    monkeypatch.setattr(browser_watcher, "_launch_with_pipe", lambda chrome: (FakeProc(), conn))
    monkeypatch.setattr(browser, "extension_intact", lambda: False)
    monkeypatch.setattr(bridge, "start", lambda: daemon)
    monkeypatch.setattr(bridge, "close", lambda proc: events.append(("daemon closed", proc is daemon)))
    started = time.monotonic()
    assert browser_watcher.watch("/fake/chrome", idle=0.3, poll=0.05) == "closed after idle"
    assert time.monotonic() - started < 5
    assert events[0] == ("handlers", ["clear_site", "close", "open", "ping"])
    assert ("Browser.close", None) in conn.calls
    assert events[1:] == ["server closed", "pipe closed", "chrome waited", ("daemon closed", True)]
    assert not browser.watcher_running(), "the watcher lock is released"


def test_a_second_watcher_does_not_start(monkeypatch):
    real_wait = browser.wait_for
    monkeypatch.setattr(browser, "wait_for", lambda check, seconds, interval=1.0: real_wait(check, 0.2, interval))
    monkeypatch.setattr(browser_watcher, "_launch_with_pipe", lambda chrome: pytest.fail("started a second Chrome"))
    fd = browser._open_lock(browser.watcher_lock_path())
    try:
        assert browser.try_lock(fd)
        assert browser_watcher.watch("/fake/chrome", idle=1) == "another watcher is running"
    finally:
        os.close(fd)
