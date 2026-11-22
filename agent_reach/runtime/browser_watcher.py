"""Owner of the Agent Reach browser (`python -I -m agent_reach.runtime.browser_watcher --chrome PATH`).

Started detached by runtime.browser. Holds ~/.agent-reach/browser/watcher.lock
so only one runs. It starts the token-protected OpenCLI daemon and a visible
Chrome over the CDP pipe, loads the pinned extension, serves the control
socket (cdp.ControlServer) and closes everything after the idle limit. Only
Agent Reach commands count as use; nothing a web page does can keep it open.

It is the only process that can talk to Chrome. It never returns cookie
values to anyone: the control commands are ping, open (the login page of an
enabled login channel, by name), clear_site (that channel's own site) and close.
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


def _enabled_login_channel(name: object):
    """The channel called `name` if it has a login and the user turned it on (read fresh each time)."""
    from agent_reach.channels import get_all_channels

    from . import state
    from .manage import is_added

    if not isinstance(name, str):
        return None
    data = state.load()
    return next((ch for ch in get_all_channels() if ch.name == name and ch.login_url and is_added(ch, data)), None)


def _open(conn: PipeConnection, channel: object = None, **_ignored) -> dict:
    """Open the login page of one enabled login channel in a new tab and bring it to the front."""
    ch = _enabled_login_channel(channel)
    if ch is None:
        return {"ok": False, "error": "not an enabled login channel"}
    reply = conn.call("Target.createTarget", {"url": ch.login_url})
    target = (reply.get("result") or {}).get("targetId")
    if not target:
        return {"ok": False, "error": "could not open a tab"}
    conn.call("Target.activateTarget", {"targetId": target}, timeout=5)
    browser.touch()
    return {"ok": True}


def _clear_site(conn: PipeConnection, channel: object = None, **_ignored) -> dict:
    """Log one enabled login channel's site out of the profile: close its tabs, expire its cookies, clear its storage."""
    from .manage import site_domains

    ch = _enabled_login_channel(channel)
    if ch is None:
        return {"ok": False, "error": "not an enabled login channel"}
    domains = site_domains(ch)

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
    del cookies  # other sites' cookies are not kept around
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
        cleared = conn.on_target(page["targetId"], "Storage.clearDataForOrigin",
                                 {"origin": origin, "storageTypes": "all"}, timeout=15)
        if "result" not in cleared:
            return {"ok": False, "error": "could not clear site storage"}
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
