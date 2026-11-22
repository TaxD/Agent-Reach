"""Dedicated Chrome browser, pinned extension and cookie lifecycle."""

import json
import os
import shutil
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
    monkeypatch.setattr(browser, "OPENCLI_EXTENSION_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest())
    urls = []
    monkeypatch.setattr(browser, "_download", lambda url, dest, timeout=60.0: urls.append(url) or shutil.copy(archive, dest))
    return urls


def test_extension_installs_only_the_pinned_verified_asset(home, monkeypatch, tmp_path):
    manifest = (json.dumps({"manifest_version": 3, "version": "1.0.1"}), 0o644)
    archive = _zip(tmp_path / "v1.zip", {"manifest.json": manifest, "dist/background.js": ("//", 0o644)})
    urls = _pin(monkeypatch, archive)

    assert browser.refresh_extension() == {"version": "1.0.1", "updated": True}
    assert urls == [browser.OPENCLI_EXTENSION_URL_PINNED]
    assert (browser.extension_dir() / "dist" / "background.js").is_file()
    assert browser._load_state()["extension_sha256"] == browser.OPENCLI_EXTENSION_SHA256
    # Already the pinned asset: neither a plain call nor an upgrade downloads again.
    assert browser.refresh_extension() == {"version": "1.0.1", "updated": False}
    assert browser.refresh_extension(upgrade=True) == {"version": "1.0.1", "updated": False}
    assert len(urls) == 1


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


def test_extension_refresh_keeps_an_installed_copy_without_network(home, monkeypatch):
    browser.extension_dir().mkdir(parents=True)
    (browser.extension_dir() / "manifest.json").write_text(json.dumps({"version": "1.0.0"}))
    monkeypatch.setattr(browser, "_download", lambda *a, **k: pytest.fail("downloaded without an upgrade request"))
    assert browser.refresh_extension() == {"version": "1.0.0", "updated": False}


def test_ensure_chrome_uses_only_the_installed_system_chrome(home, monkeypatch):
    monkeypatch.setattr(browser, "_system_chrome", lambda: "/system/chrome")
    assert browser.ensure_chrome() == "/system/chrome"
    assert browser._load_state()["backend"] == "chrome"


def test_missing_chrome_asks_for_setup_and_downloads_nothing(home, monkeypatch):
    from agent_reach.runtime.result import ReachError
    monkeypatch.setattr(browser, "_system_chrome", lambda: None)
    monkeypatch.setattr(browser, "_download", lambda *a, **k: pytest.fail("downloaded a browser"))
    with pytest.raises(ReachError) as err:
        browser.ensure_chrome()
    assert err.value.code == "NEED_SETUP"


def test_clear_site_expires_only_that_sites_cookies(home, monkeypatch):
    jar = [
        {"name": "a", "value": "1", "domain": ".reddit.com", "path": "/"},
        {"name": "b", "value": "2", "domain": "www.reddit.com", "path": "/"},
        {"name": "c", "value": "3", "domain": "notreddit.com", "path": "/"},
    ]

    def fake_call(method, params=None, **kwargs):
        if method == "Storage.getCookies":
            return {"cookies": list(jar)}
        if method == "Storage.clearDataForOrigin":
            assert "reddit.com" in params["origin"]
            return {}
        if method == "Storage.setCookies":
            for c in params["cookies"]:
                assert c["expires"] == 1
                jar[:] = [j for j in jar if (j["name"], j["domain"]) != (c["name"], c["domain"])]
            return {}
        raise AssertionError(method)

    browser.profile_dir().mkdir(parents=True)
    monkeypatch.setattr(browser, "ensure_running", lambda context=None: None)
    monkeypatch.setattr(cdp, "call", fake_call)
    monkeypatch.setattr(cdp, "_json", lambda *a: [])
    assert browser.clear_site(["reddit.com"]) == 2
    assert [c["name"] for c in jar] == ["c"]


def test_clear_site_never_opens_a_browser_that_was_never_used(home, monkeypatch):
    monkeypatch.setattr(browser, "ensure_running", lambda context=None: pytest.fail("opened the browser"))
    assert browser.clear_site(["reddit.com"]) == 0


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


def test_chrome_queries_are_headless_and_human_login_is_visible():
    assert '--headless=new' in browser.chrome_argv('/chrome','about:blank',pipe=False)
    assert '--headless=new' not in browser.chrome_argv('/chrome','about:blank',pipe=False,headless=False)


def test_restore_failure_preserves_the_original_snapshot(home, monkeypatch):
    from agent_reach.runtime.result import ReachError
    browser.browser_root().mkdir(parents=True)
    path=browser.browser_root()/'session.json'
    original=json.dumps({'cookies':[{'name':'sid','value':'saved','domain':'example.test'}]})
    path.write_text(original)
    monkeypatch.setattr(cdp,'call',lambda method,*a,**k:{'cookies':[]}if method=='Storage.getCookies'else None)
    with pytest.raises(ReachError,match='retained'):
        browser.restore_session()
    assert path.read_text()==original
