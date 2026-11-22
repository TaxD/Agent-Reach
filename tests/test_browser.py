"""Dedicated Chrome browser, pinned extension and cookie lifecycle."""

import json
import os
import shutil
import subprocess
import threading
import zipfile

import pytest

from agent_reach.runtime import browser, cdp


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(browser, "is_running", lambda: False)
    return tmp_path


def _zip(path, files):
    with zipfile.ZipFile(path, "w") as zf:
        for name, (data, mode) in files.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o100000 | mode) << 16
            zf.writestr(info, data)
    return path


def test_domain_match_is_exact_or_subdomain():
    assert cdp.domain_matches(".reddit.com", "reddit.com")
    assert cdp.domain_matches("www.reddit.com", "reddit.com")
    assert not cdp.domain_matches("notreddit.com", "reddit.com")


def _pin(monkeypatch, archive):
    import hashlib
    import zipfile
    monkeypatch.setattr(browser, "OPENCLI_EXTENSION_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest())
    tree = hashlib.sha256()
    with zipfile.ZipFile(archive) as zf:
        for name in sorted(n for n in zf.namelist() if not n.endswith("/")):
            tree.update(name.encode() + b"\0" + hashlib.sha256(zf.read(name)).digest())
    monkeypatch.setattr(browser, "OPENCLI_EXTENSION_TREE_SHA256", tree.hexdigest())
    urls = []
    monkeypatch.setattr(browser, "_download", lambda url, dest, timeout=60.0: urls.append(url) or shutil.copy(archive, dest))
    return urls


def test_extension_installs_only_the_pinned_verified_asset(home, monkeypatch, tmp_path):
    manifest = (json.dumps({"manifest_version": 3, "version": "1.0.1"}), 0o644)
    archive = _zip(tmp_path / "v1.zip", {"manifest.json": manifest, "dist/background.js": ("//", 0o644)})
    urls = _pin(monkeypatch, archive)

    assert browser.refresh_extension() == {"version": "1.0.1", "updated": True}
    assert urls == [browser.OPENCLI_EXTENSION_URL]
    assert (browser.extension_dir() / "dist" / "background.js").is_file()
    token_file = browser.extension_dir() / browser.TOKEN_FILE_IN_EXTENSION
    token = browser.daemon_token_path().read_text().strip()
    assert json.loads(token_file.read_text()) == {"token": token}
    assert token_file.stat().st_mode & 0o777 == 0o600
    assert browser.extension_intact(), "the token file is not part of the pinned tree"
    # Already the pinned files: no second download.
    assert browser.refresh_extension() == {"version": "1.0.1", "updated": False}
    assert len(urls) == 1


def test_extension_whose_files_differ_from_the_pinned_tree_is_never_installed(home, monkeypatch, tmp_path):
    import hashlib

    from agent_reach.runtime.result import ReachError
    archive = _zip(tmp_path / "v1.zip", {"manifest.json": (json.dumps({"version": "1.0.1"}), 0o644)})
    _pin(monkeypatch, archive)
    monkeypatch.setattr(browser, "OPENCLI_EXTENSION_TREE_SHA256", hashlib.sha256(b"other files").hexdigest())
    with pytest.raises(ReachError, match="does not match the pinned files") as caught:
        browser.refresh_extension()
    assert caught.value.code == "UPSTREAM_BROKEN"
    assert not browser.extension_dir().exists()
    assert not list(browser.browser_root().glob(".extension-*")), "the work folder is cleaned up"


def test_tree_digest_ignores_only_the_token_file(home, monkeypatch, tmp_path):
    root = tmp_path / "ext"
    (root / "dist").mkdir(parents=True)
    (root / "manifest.json").write_text("{}")
    (root / "dist" / "background.js").write_text("//")
    clean = browser.extension_tree_digest(root)
    assert clean and len(clean) == 64
    (root / browser.TOKEN_FILE_IN_EXTENSION).write_text('{"token": "abc"}')
    assert browser.extension_tree_digest(root) == clean
    (root / "dist" / "background.js").write_text("// tampered")
    assert browser.extension_tree_digest(root) != clean
    (root / "dist" / "background.js").write_text("//")
    (root / "dist" / "extra.js").write_text("//")
    assert browser.extension_tree_digest(root) != clean, "an added file is a change"
    (root / "dist" / "extra.js").unlink()
    (root / "dist" / browser.TOKEN_FILE_IN_EXTENSION).write_text("{}")
    assert browser.extension_tree_digest(root) != clean, "only the top-level token file is skipped"


def test_tree_digest_refuses_symlinks(home, tmp_path):
    root = tmp_path / "ext"
    root.mkdir()
    (root / "manifest.json").write_text("{}")
    (root / "linked.js").symlink_to(tmp_path / "elsewhere.js")
    assert browser.extension_tree_digest(root) is None
    link = tmp_path / "ext-link"
    link.symlink_to(root, target_is_directory=True)
    assert browser.extension_tree_digest(link) is None


def test_extension_intact_is_false_for_a_modified_install(home, monkeypatch, tmp_path):
    archive = _zip(tmp_path / "v1.zip", {"manifest.json": (json.dumps({"version": "1.0.1"}), 0o644)})
    _pin(monkeypatch, archive)
    browser.refresh_extension()
    assert browser.extension_intact()
    (browser.extension_dir() / "manifest.json").write_text(json.dumps({"version": "1.0.1", "x": 1}))
    assert not browser.extension_intact()


def test_extension_with_a_wrong_checksum_is_never_installed(home, monkeypatch, tmp_path):
    from agent_reach.runtime.result import ReachError
    archive = _zip(tmp_path / "evil.zip", {"manifest.json": (json.dumps({"version": "9.9.9"}), 0o644)})
    monkeypatch.setattr(browser, "_download", lambda url, dest, timeout=60.0: shutil.copy(archive, dest))
    with pytest.raises(ReachError, match="checksum"):
        browser.refresh_extension()
    assert browser.extension_version() is None


def test_extension_archive_with_traversal_or_symlink_is_refused(home, monkeypatch, tmp_path):
    from agent_reach.runtime.result import ReachError
    for name, files in (("escape.zip", {"../escape.txt": ("x", 0o644)}),
                        ("link.zip", {"manifest.json": ("/etc/passwd", 0o120777 & 0o7777 | 0o20000)})):
        archive = tmp_path / name
        with zipfile.ZipFile(archive, "w") as zf:
            for member, (data, _mode) in files.items():
                info = zipfile.ZipInfo(member)
                info.external_attr = (0o120777 if name == "link.zip" else 0o100644) << 16
                zf.writestr(info, data)
        _pin(monkeypatch, archive)
        with pytest.raises(ReachError):
            browser.refresh_extension()
        assert browser.extension_version() is None
    assert not (browser.browser_root() / "escape.txt").exists()


def test_an_installed_extension_that_differs_from_the_pin_is_replaced(home, monkeypatch, tmp_path):
    browser.extension_dir().mkdir(parents=True)
    (browser.extension_dir() / "manifest.json").write_text(json.dumps({"version": "0.0.1"}))
    archive = _zip(tmp_path / "v1.zip", {"manifest.json": (json.dumps({"version": "1.0.1"}), 0o644)})
    urls = _pin(monkeypatch, archive)
    assert browser.refresh_extension() == {"version": "1.0.1", "updated": True}
    assert len(urls) == 1 and browser.extension_intact()


def test_ensure_chrome_uses_only_the_installed_system_chrome(home, monkeypatch):
    monkeypatch.setattr(browser, "_system_chrome", lambda: "/system/chrome")
    assert browser.ensure_chrome() == "/system/chrome"


def test_windows_has_no_login_browser(home, monkeypatch):
    from agent_reach.runtime.result import ReachError
    monkeypatch.setattr(browser.sys, "platform", "win32")
    monkeypatch.setattr(browser, "_system_chrome", lambda: "C:/chrome.exe")
    with pytest.raises(ReachError) as err:
        browser.ensure_chrome()
    assert err.value.code == "NEED_SETUP"
    assert browser.setup_problems()


def test_missing_chrome_asks_for_setup_and_downloads_nothing(home, monkeypatch):
    from agent_reach.runtime.result import ReachError
    monkeypatch.setattr(browser, "_system_chrome", lambda: None)
    monkeypatch.setattr(browser, "_download", lambda *a, **k: pytest.fail("downloaded a browser"))
    with pytest.raises(ReachError) as err:
        browser.ensure_chrome()
    assert err.value.code == "NEED_SETUP"


def test_chrome_is_driven_by_pipe_only_and_always_visible(monkeypatch):
    monkeypatch.delenv("AGENT_REACH_TEST_MOCK_KEYCHAIN", raising=False)
    argv = browser.chrome_argv("/chrome")
    assert argv[0] == "/chrome" and "--remote-debugging-pipe" in argv
    assert not any(a.startswith(("--remote-debugging-port", "--remote-debugging-address")) for a in argv)
    assert not any("headless" in a for a in argv)
    assert f"--user-data-dir={browser.profile_dir()}" in argv
    assert not any("mock-keychain" in a or "password-store" in a for a in argv), "test-only flags stay off"


def test_daemon_token_is_private_random_and_stable(home):
    path = browser.ensure_token()
    token = path.read_text().strip()
    assert len(token) == 64 and int(token, 16) >= 0
    assert path.stat().st_mode & 0o777 == 0o600
    assert browser.ensure_token() == path and path.read_text().strip() == token


def test_a_short_or_empty_token_is_replaced(home):
    path = browser.daemon_token_path()
    path.parent.mkdir(parents=True)
    path.write_text("short\n")
    browser.ensure_token()
    assert len(path.read_text().strip()) == 64


def test_clear_site_sends_only_the_channel_name(home, monkeypatch):
    from agent_reach.runtime.result import ReachError
    browser.profile_dir().mkdir(parents=True)
    monkeypatch.setattr(browser, "ensure_running", lambda: "dedicated")
    sent = []
    monkeypatch.setattr(cdp, "request", lambda command, timeout=15.0, **args: sent.append((command, args))
                        or {"ok": True, "cleared": 2})
    assert browser.clear_site("reddit") == 2
    assert sent == [("clear_site", {"channel": "reddit"})]
    monkeypatch.setattr(cdp, "request", lambda *a, **k: {"ok": False, "error": "cleanup could not be verified"})
    with pytest.raises(ReachError, match="retained"):
        browser.clear_site("reddit")
    monkeypatch.setattr(cdp, "request", lambda *a, **k: None)
    with pytest.raises(ReachError):
        browser.clear_site("reddit")


def test_clear_site_never_opens_a_browser_that_was_never_used(home, monkeypatch):
    monkeypatch.setattr(browser, "ensure_running", lambda: pytest.fail("opened the browser"))
    assert browser.clear_site("reddit") == 0


LOGIN_URL = "https://www.reddit.com/login/"


@pytest.fixture
def procs(home, monkeypatch):
    """A fake process table: pid -> command line, answered to `ps`. Signals are recorded, never sent."""
    table: dict = {}
    killed: list = []

    def run(argv, **kwargs):
        assert argv[:2] == ["ps", "-ww"], argv
        return subprocess.CompletedProcess(argv, 0, stdout=table.get(int(argv[-1]), "") + "\n", stderr="")

    def kill(pid, sig):
        killed.append((pid, sig))
        if table.get("dies_on_term", True):
            table.pop(pid, None)

    monkeypatch.setattr(browser.subprocess, "run", run)
    monkeypatch.setattr(browser.os, "kill", kill)
    return {"table": table, "killed": killed}


def _login_cmd(*extra):
    return " ".join(["/x/chrome", f"--user-data-dir={browser.profile_dir()}", "--no-first-run", *extra, LOGIN_URL])


def test_login_window_closes_the_automated_browser_first_and_attaches_nothing(procs, monkeypatch):
    from agent_reach.runtime.result import ReachError

    events, launched = [], []

    class Proc:
        pid = 4242

    def popen(argv, **kwargs):
        events.append("popen")
        launched.append((argv, kwargs))
        return Proc()

    monkeypatch.setattr(browser, "ensure_chrome", lambda: "/x/chrome")
    monkeypatch.setattr(browser, "stop", lambda *a, **k: events.append("stop") or True)
    monkeypatch.setattr(browser.subprocess, "Popen", popen)
    browser.login_window(LOGIN_URL)
    assert events == ["stop", "popen"], "the automated browser is closed before the login window opens"
    ((argv, kwargs),) = launched
    assert argv[0] == "/x/chrome" and argv[-1] == LOGIN_URL
    assert f"--user-data-dir={browser.profile_dir()}" in argv
    assert not [a for a in argv if a.startswith("--remote-debugging")]
    assert "--enable-unsafe-extension-debugging" not in argv
    assert not [a for a in argv if a.startswith("--load-extension")]
    assert kwargs.get("start_new_session") is True
    assert browser._load_state()["login_pid"] == 4242

    # The automated browser would not close: no login window opens.
    events.clear()
    browser.update_state(login_pid=None)
    monkeypatch.setattr(browser, "stop", lambda *a, **k: events.append("stop") or False)
    with pytest.raises(ReachError) as err:
        browser.login_window(LOGIN_URL)
    assert err.value.code == "UPSTREAM_BROKEN"
    assert events == ["stop"] and "login_pid" not in browser._load_state()


def test_login_window_pid_counts_only_our_plain_chrome(procs):
    procs["table"][4242] = _login_cmd("--new-window")
    browser.update_state(login_pid=4242)
    assert browser.login_window_pid() == 4242
    assert browser._load_state()["login_pid"] == 4242

    cases = {
        "pid reused by another program": "/usr/bin/python3 -m http.server",
        "another Chrome profile": "/x/chrome --user-data-dir=/tmp/elsewhere --new-window",
        "a Chrome under automation": _login_cmd("--remote-debugging-pipe"),
        "a Chrome with a debugging port": _login_cmd("--remote-debugging-port=9222"),
        "process gone": "",
    }
    for why, command in cases.items():
        procs["table"][4242] = command
        browser.update_state(login_pid=4242)
        assert browser.login_window_pid() is None, why
        assert "login_pid" not in browser._load_state(), f"{why}: the record is cleared"


@pytest.mark.parametrize("record", ["4242", 4242.0, True, 1, 0, -7, [4242], {"pid": 4242}])
def test_a_malformed_login_record_is_ignored_and_cleared(procs, record):
    procs["table"][4242] = _login_cmd()
    browser.update_state(login_pid=record)
    assert browser.login_window_pid() is None
    assert "login_pid" not in browser._load_state()


def test_queries_wait_while_the_login_window_is_open(procs, monkeypatch):
    from agent_reach.runtime import bridge
    from agent_reach.runtime.browser_binding import environment
    from agent_reach.runtime.result import ReachError

    procs["table"][4242] = _login_cmd()
    browser.update_state(login_pid=4242)
    for module, name in ((browser, "_launch"), (browser, "stop"), (browser, "close_login_window"),
                         (bridge, "start"), (bridge, "require_running"), (cdp, "request")):
        monkeypatch.setattr(module, name, lambda *a, _n=name, **k: pytest.fail(f"{_n} must not run"))
    for call in (browser.ensure_running, lambda: environment("reddit")):
        with pytest.raises(ReachError) as err:
            call()
        assert err.value.code == "BUSY"
    assert procs["killed"] == [], "a query never closes the login window"
    assert browser._load_state()["login_pid"] == 4242


def test_stop_closes_the_login_window_first(procs, monkeypatch):
    import signal

    procs["table"][4242] = _login_cmd()
    browser.update_state(login_pid=4242)
    monkeypatch.setattr(browser, "watcher_running", lambda: False)
    monkeypatch.setattr(cdp, "request", lambda *a, **k: pytest.fail("nothing else to close"))
    assert browser.stop(timeout=1) is True
    assert procs["killed"] == [(4242, signal.SIGTERM)]
    assert "login_pid" not in browser._load_state()
    monkeypatch.setattr(cdp, "request", lambda *a, **k: None)
    assert browser.status()["login_window_open"] is False


def test_stop_fails_when_the_login_window_does_not_close(home, monkeypatch):
    monkeypatch.setattr(browser, "close_login_window", lambda timeout=20.0: False)
    monkeypatch.setattr(browser, "watcher_running", lambda: True)
    monkeypatch.setattr(cdp, "request", lambda *a, **k: pytest.fail("the watcher is not asked while the window is open"))
    assert browser.stop(timeout=1) is False


def test_close_login_window_signals_only_the_recorded_login_chrome(procs):
    import signal

    assert browser.close_login_window(timeout=0.5) is True and procs["killed"] == [], "nothing recorded"
    procs["table"][4242] = "/usr/bin/vim notes.txt"
    browser.update_state(login_pid=4242)
    assert browser.close_login_window(timeout=0.5) is True and procs["killed"] == [], "a reused pid is never signalled"
    procs["table"][4242] = _login_cmd()
    procs["table"][5151] = _login_cmd()  # not the recorded one
    browser.update_state(login_pid=4242)
    procs["table"]["dies_on_term"] = False
    assert browser.close_login_window(timeout=0.5) is False, "still running after the wait"
    assert browser._load_state()["login_pid"] == 4242
    procs["table"]["dies_on_term"] = True
    assert browser.close_login_window(timeout=0.5) is True
    assert {pid for pid, _ in procs["killed"]} == {4242}
    assert all(sig == signal.SIGTERM for _, sig in procs["killed"])
    assert 5151 in procs["table"]


def test_stop_closes_through_the_control_socket(home, monkeypatch):
    running = {"on": True}
    sent = []

    def request(command, timeout=15.0, **args):
        sent.append(command)
        running["on"] = False
        return {"ok": True}

    monkeypatch.setattr(browser, "watcher_running", lambda: running["on"])
    monkeypatch.setattr(cdp, "request", request)
    assert browser.stop(timeout=1) is True and sent == ["close"]
    assert browser.stop(timeout=1) is True and sent == ["close"], "nothing running: nothing sent"


def test_status_reports_no_debugging_port_and_no_cookie_values(home, monkeypatch):
    monkeypatch.setattr(browser, "_system_chrome", lambda: None)
    out = browser.status()
    assert out["debugging_port"] is None and out["window"] == "visible" and out["running"] is False
    assert not (browser.browser_root() / "session.json").exists()


def test_delete_profile_removes_the_browser_folder(home, monkeypatch):
    monkeypatch.setattr(browser, "stop", lambda timeout=10.0: True)
    browser.profile_dir().mkdir(parents=True)
    browser.touch()
    from agent_reach.runtime import state
    for path in (browser.profile_dir(), browser._last_used_path()):
        state.remember_path(path, "test browser")
    assert browser.delete_profile() is True
    assert not browser.browser_root().exists()
    assert browser.delete_profile() is False


def test_pipe_connection_matches_replies_and_notices_close():
    to_chrome_r, to_chrome_w = os.pipe()
    from_chrome_r, from_chrome_w = os.pipe()

    def fake_chrome():  # echoes each NUL-delimited command back as a reply, with an event first
        buf = b""
        while True:
            chunk = os.read(to_chrome_r, 4096)
            if not chunk:
                break
            buf += chunk
            while b"\0" in buf:
                raw, buf = buf.split(b"\0", 1)
                msg = json.loads(raw)
                os.write(from_chrome_w, json.dumps({"method": "Some.event"}).encode() + b"\0")
                os.write(from_chrome_w, json.dumps({"id": msg["id"], "result": {"id": msg["params"]["path"]}}).encode() + b"\0")
        os.close(from_chrome_w)  # Chrome exits when its input pipe closes
        os.close(to_chrome_r)

    t = threading.Thread(target=fake_chrome)
    t.start()
    conn = cdp.PipeConnection(to_chrome_w, from_chrome_r)
    assert conn.call("Extensions.loadUnpacked", {"path": "/ext"}) == {"id": 1, "result": {"id": "/ext"}}
    assert conn.call("Extensions.loadUnpacked", {"path": "/two"})["result"] == {"id": "/two"}
    conn.close()
    t.join(5)
    assert conn.closed.is_set()
    assert "error" in conn.call("Browser.close", timeout=1)


def test_stop_without_flock_support_does_not_report_a_running_browser(home, monkeypatch):
    """Windows has no fcntl: nothing of ours can be running there, so stop (and uninstall) must not hang or fail."""
    import sys

    assert browser.stop(timeout=0.2) is True  # records the lock path while fcntl still imports
    monkeypatch.setitem(sys.modules, "fcntl", None)
    monkeypatch.setattr(cdp, "request", lambda *a, **k: None)
    assert browser.watcher_running() is False
    assert browser.stop(timeout=0.2) is True


def test_a_second_login_keeps_the_open_login_window(monkeypatch):
    """Chrome hands the new page to the running window, so it is neither closed nor re-recorded."""
    browser.update_state(login_pid=4242)
    monkeypatch.setattr(browser, "ensure_chrome", lambda: "/chrome")
    monkeypatch.setattr(browser, "login_window_pid", lambda: 4242)
    monkeypatch.setattr(browser, "stop", lambda *a, **k: pytest.fail("the open login window must stay"))
    started = []
    monkeypatch.setattr(browser.subprocess, "Popen",
                        lambda argv, **k: started.append(argv) or type("P", (), {"pid": 9999})())
    browser.login_window("https://x.com/i/flow/login")
    assert started[0][-1] == "https://x.com/i/flow/login"
    assert browser._load_state()["login_pid"] == 4242
