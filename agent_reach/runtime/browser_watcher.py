"""Owner of the Agent Reach browser (`python -I -m agent_reach.runtime.browser_watcher --chrome PATH`).

Started detached by runtime.browser. Holds ~/.agent-reach/browser/watcher.lock
so only one runs. It starts the token-protected OpenCLI daemon and a visible
Chrome over the CDP pipe, loads the pinned extension, serves the control
socket (cdp.ControlServer) and closes everything after the idle limit.

It is the only process that can talk to Chrome. It never returns cookie
values to anyone: the control commands are ping, open (a channel's login page),
clear_site (a channel's own domains) and close.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from typing import List, Optional
from urllib.parse import urlsplit

from . import bridge, browser
from .cdp import ControlServer, PipeConnection, domain_matches

#: Key the OpenCLI extension uses to persist its profile id inside each browser profile.
_CONTEXT_ID_KEY = "opencli_context_id_v1"

# Generic activity signal only: no site selectors, content or credentials.
_ACTIVITY = """(() => {
  if (!window.__reachActivityInstalled) {
    window.__reachActivityInstalled = true;
    window.__reachLastInput = 0;
    for (const name of ['pointerdown', 'keydown', 'wheel', 'touchstart'])
      addEventListener(name, event => {
        if (event.isTrusted) window.__reachLastInput = Date.now();
      }, {capture: true, passive: true});
  }
  return window.__reachLastInput;
})()"""


def _launch_with_pipe(chrome: str):
    """Start Chrome with the CDP pipe on its fds 3 (commands in) and 4 (replies out)."""
    import fcntl

    to_chrome_r, to_chrome_w = os.pipe()
    from_chrome_r, from_chrome_w = os.pipe()
    # Child ends above 4 so placing them on 3/4 never overwrites one with the other.
    child_in = fcntl.fcntl(to_chrome_r, fcntl.F_DUPFD_CLOEXEC, 10)
    child_out = fcntl.fcntl(from_chrome_w, fcntl.F_DUPFD_CLOEXEC, 10)
    for fd in (to_chrome_r, from_chrome_w):
        os.close(fd)

    def place_pipe() -> None:  # in the child, after fork: dup2 leaves 3/4 inheritable
        os.dup2(child_in, 3)
        os.dup2(child_out, 4)

    try:
        proc = subprocess.Popen(
            browser.chrome_argv(chrome),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=False,  # Python fds are non-inheritable by default; only 3/4 get through
            preexec_fn=place_pipe,  # noqa: PLW1509 - single-threaded here
            start_new_session=True,
        )
    finally:
        os.close(child_in)
        os.close(child_out)
    return proc, PipeConnection(to_chrome_w, from_chrome_r)


def _load_extension(conn: PipeConnection) -> None:
    if not browser.extension_intact():
        # Never load extension files that differ from the pinned release into the profile that holds logins.
        print("OpenCLI extension missing or modified; browser opened without it", file=sys.stderr, flush=True)
        return
    reply = conn.call("Extensions.loadUnpacked", {"path": str(browser.extension_dir())}, timeout=60)
    print("extension loaded" if "result" in reply else "Extensions.loadUnpacked failed", file=sys.stderr, flush=True)


def _pages(conn: PipeConnection) -> List[dict]:
    return [t for t in conn.targets() if t.get("type") == "page"]


def _context_id(conn: PipeConnection) -> Optional[str]:
    """The OpenCLI profile id the extension saved in this profile's chrome.storage.local."""
    expr = f"chrome.storage.local.get({_CONTEXT_ID_KEY!r}).then(r => r[{_CONTEXT_ID_KEY!r}] || null)"
    for target in conn.targets():
        if target.get("type") != "service_worker" or not str(target.get("url", "")).startswith("chrome-extension://"):
            continue
        reply = conn.on_target(target["targetId"], "Runtime.evaluate",
                               {"expression": expr, "awaitPromise": True, "returnByValue": True}, timeout=10)
        value = ((reply.get("result") or {}).get("result") or {}).get("value")
        if isinstance(value, str) and 0 < len(value.strip()) <= 200:
            return value.strip()
    return None


def _observe_activity(conn: PipeConnection) -> None:
    """Trusted human input in any page counts as use, so a login in progress is not closed."""
    for target in _pages(conn):
        reply = conn.on_target(target["targetId"], "Runtime.evaluate",
                               {"expression": _ACTIVITY, "returnByValue": True}, timeout=5)
        value = ((reply.get("result") or {}).get("result") or {}).get("value")
        if isinstance(value, (int, float)) and (browser.last_used() or 0) < value / 1000 <= time.time() + 1:
            browser.touch()


def _login_pages() -> dict:
    """login URL -> cookie domains, for the channels that have a login."""
    from agent_reach.channels import get_all_channels

    from .manage import site_domains

    return {ch.login_url: site_domains(ch) for ch in get_all_channels() if ch.login_url}


def _open(conn: PipeConnection, url: object = None, **_ignored) -> dict:
    """Open a registered channel's login page in a new tab and bring it to the front."""
    if not isinstance(url, str) or url not in _login_pages():
        return {"ok": False, "error": "not a registered login page"}
    reply = conn.call("Target.createTarget", {"url": url})
    target = (reply.get("result") or {}).get("targetId")
    if not target:
        return {"ok": False, "error": "could not open a tab"}
    conn.call("Target.activateTarget", {"targetId": target}, timeout=5)
    browser.touch()
    return {"ok": True}


def _clear_site(conn: PipeConnection, domains: object = None, **_ignored) -> dict:
    """Log one registered site out of the profile: close its tabs, expire its cookies, clear its storage."""
    allowed = {d for group in _login_pages().values() for d in group}
    if not isinstance(domains, list) or not domains or not all(isinstance(d, str) and d in allowed for d in domains):
        return {"ok": False, "error": "not a registered site"}

    def host(url: str) -> str:
        return urlsplit(url).hostname or ""

    def ours(value: str) -> bool:
        return any(domain_matches(value, d) for d in domains)

    pages = _pages(conn)
    closing = [t for t in pages if ours(host(str(t.get("url", ""))))]
    if closing and len(closing) == len(pages):
        conn.call("Target.createTarget", {"url": "about:blank"})  # keep the window alive
    for target in closing:
        conn.call("Target.closeTarget", {"targetId": target["targetId"]})
    cookies = (conn.call("Storage.getCookies").get("result") or {}).get("cookies")
    if not isinstance(cookies, list):
        return {"ok": False, "error": "could not inspect site data"}
    doomed = [c for c in cookies if isinstance(c, dict) and ours(str(c.get("domain", "")))]
    keys = ("name", "domain", "path", "secure", "httpOnly", "sameSite", "partitionKey", "sourceScheme", "sourcePort")
    expired = [{**{k: c[k] for k in keys if k in c}, "value": "", "expires": 1} for c in doomed]
    if expired and "result" not in conn.call("Storage.setCookies", {"cookies": expired}):
        return {"ok": False, "error": "could not clear cookies"}
    hosts = {d.lstrip(".") for d in domains} | {"www." + d.lstrip(".") for d in domains}
    hosts |= {str(c.get("domain", "")).lstrip(".") for c in doomed}
    page = next(iter(_pages(conn)), None)
    if page is None:
        return {"ok": False, "error": "no page to clear storage from"}
    for origin in sorted({scheme + h for h in hosts if h for scheme in ("https://", "http://")}):
        conn.on_target(page["targetId"], "Storage.clearDataForOrigin", {"origin": origin, "storageTypes": "all"}, timeout=15)
    after = (conn.call("Storage.getCookies").get("result") or {}).get("cookies")
    if not isinstance(after, list) or any(isinstance(c, dict) and ours(str(c.get("domain", ""))) for c in after):
        return {"ok": False, "error": "cleanup could not be verified"}
    return {"ok": True, "cleared": len(doomed)}


def watch(chrome: str, idle: Optional[float] = None, poll: float = 3.0) -> str:
    """Run until the browser is gone. Returns why it stopped."""
    idle = browser.IDLE_LIMIT if idle is None else idle
    fd = browser._open_lock(browser.watcher_lock_path())
    proc = conn = bridge_proc = server = None
    try:
        # A watcher of a browser that just closed may still be on its way out: give it a moment.
        if not browser.wait_for(lambda: browser.try_lock(fd), 10.0, interval=0.1):
            return "another watcher is running"
        bridge_proc = bridge.start()
        proc, conn = _launch_with_pipe(chrome)
        _load_extension(conn)
        context: dict = {"id": None}
        stopping = threading.Event()

        def close(**_ignored) -> dict:
            stopping.set()
            return {"ok": True}

        connection = conn
        server = ControlServer({
            "ping": lambda **_: {"ok": True, "context": context["id"]},
            "open": lambda **kw: _open(connection, **kw),
            "clear_site": lambda **kw: _clear_site(connection, **kw),
            "close": close,
        })
        began = time.time()
        while True:
            if conn.closed.is_set() or proc.poll() is not None:
                return "browser closed"
            if stopping.is_set():
                return "closed on request"
            if bridge_proc is not None and bridge_proc.poll() is not None:
                return "owned daemon exited; browser closed with it"
            if context["id"] is None:
                context["id"] = _context_id(conn)
            _observe_activity(conn)
            if max(began, browser.last_used() or 0.0) + idle <= time.time():
                return "closed after idle"
            if stopping.wait(poll):
                return "closed on request"
    finally:
        if server is not None:
            server.close()
        if conn is not None:
            conn.call("Browser.close", timeout=5)
            conn.close()  # Chrome exits when its pipe closes
        if proc is not None:  # only ever the child this process started
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)
        bridge.close(bridge_proc)
        os.close(fd)


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent_reach.runtime.browser_watcher")
    parser.add_argument("--chrome", required=True, help="Chrome binary to start")
    parser.add_argument("--idle", type=float, default=None, help="seconds without use before closing")
    ns = parser.parse_args()
    reason = watch(ns.chrome, ns.idle)
    print(reason, file=sys.stderr, flush=True)
    sys.exit(0 if reason in ("browser closed", "closed after idle", "closed on request") else 1)


if __name__ == "__main__":
    main()
