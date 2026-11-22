"""The OpenCLI daemon of the hardened fork, run only while the dedicated browser is open.

The fork's daemon accepts only requests signed with a secret token (HMAC-SHA256
over method, path, time, nonce and body) and proves to its clients that it
knows the token too. So other programs on this machine can neither drive the
browser through 127.0.0.1:19825 nor pose as the daemon. The token is never
transmitted, and never put in a command line or an environment value; only the
path of its 0600 file is passed around.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import time
import urllib.request

from agent_reach.utils.paths import PrivatePathError, read_small_text_no_follow

from . import tools
from .result import NEED_SETUP, ReachError

URL = "http://127.0.0.1:19825"
_FOREIGN = ("Another OpenCLI daemon is already using port 19825; left it unchanged",
            "Ask the user to stop their own OpenCLI (`opencli daemon stop`), then retry. Reach will not stop it.")


def env() -> dict:
    """Environment that points the OpenCLI CLI and daemon at this machine's token file."""
    from . import browser

    return {"OPENCLI_DAEMON_TOKEN_FILE": str(browser.ensure_token())}


def _token() -> bytes:
    """Read the token; asking about the daemon never creates one."""
    from . import browser

    try:
        return (read_small_text_no_follow(browser.daemon_token_path(), max_bytes=4096) or "").strip().encode("utf-8")
    except (OSError, PrivatePathError):
        return b""


def _open(path: str, headers: dict):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback: never proxy
    with opener.open(urllib.request.Request(URL + path, headers={"X-OpenCLI": "1", **headers}), timeout=2) as response:
        return json.loads(response.read(256 * 1024))


def signature(method: str, path: str, body: bytes = b"") -> str:
    """The fork's request signature. The token signs the request and is never transmitted."""
    ts, nonce = str(int(time.time() * 1000)), secrets.token_hex(16)
    message = "\n".join([method.upper(), path, ts, nonce, hashlib.sha256(body).hexdigest()])
    sig = hmac.new(_token(), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"OpenCLI-HMAC ts={ts};nonce={nonce};sig={sig}"


def daemon_is_ours() -> bool:
    """Whatever listens on the port proves it knows our token (so it is our daemon, not a squatter)."""
    nonce = secrets.token_hex(16)
    if len(_token()) < 32:
        return False
    try:
        reply = _open("/ping?nonce=" + nonce, {})
    except (OSError, ValueError):
        return False
    expected = hmac.new(_token(), ("opencli-daemon-proof\n" + nonce).encode("utf-8"), hashlib.sha256).hexdigest()
    proof = reply.get("proof") if isinstance(reply, dict) else None
    return isinstance(proof, str) and hmac.compare_digest(proof, expected)


def _something_listens() -> bool:
    try:
        return bool(_open("/ping", {}))
    except (OSError, ValueError):
        return False


def status() -> dict:
    """The daemon's status when it is ours; {} when nothing answers.

    A listener that cannot prove it knows our token is someone else's program on
    the port. That is reported and never worked around, and nothing is sent to it.
    """
    if not daemon_is_ours():
        if _something_listens():
            raise ReachError(NEED_SETUP, _FOREIGN[0], hint=_FOREIGN[1])
        return {}
    try:
        value = _open("/status", {"Authorization": signature("GET", "/status")})
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) and value.get("ok") else {}


def launch_spec() -> tuple[list[str], str]:
    node = tools.resolve("node")
    root = tools.opencli_root()
    daemon, package = root / "dist" / "src" / "daemon.js", root / "package.json"
    if not node or not daemon.is_file() or not package.is_file():
        raise ReachError(NEED_SETUP, "Run agent-reach add CHANNEL to install the pinned OpenCLI")
    return [node, str(daemon)], str(json.loads(package.read_text("utf-8"))["version"])


def require_running() -> dict:
    """Our daemon, at the pinned version; raises when it is missing or is another build."""
    _, version = launch_spec()
    current = status()
    if not current:
        raise ReachError(NEED_SETUP, "The OpenCLI daemon is not running", hint="Retry the action; the browser restarts it.")
    if current.get("daemonVersion") != version:
        raise ReachError(NEED_SETUP, _FOREIGN[0], hint=_FOREIGN[1])
    return current


def start():
    """Start our daemon as a child of the watcher; returns its handle."""
    argv, version = launch_spec()
    if status():
        # It proved it knows our token, and the caller holds the watcher lock: this is our own
        # daemon left behind by a watcher that was killed. Ask it to stop, then start a fresh one.
        _shutdown_own_orphan()
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("OPENCLI_")}
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env={**inherited, **env()}, cwd=os.path.expanduser("~"))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        current = status()
        if current:
            if current.get("pid") == proc.pid and current.get("daemonVersion") == version:
                return proc
            break  # something else answers with our token: refuse rather than adopt it
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    close(proc)
    raise ReachError(NEED_SETUP, "The OpenCLI daemon could not start alongside the dedicated browser",
                     hint="Port 19825 may be in use by another program.")


def _shutdown_own_orphan() -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(URL + "/shutdown", data=b"", method="POST",
                                     headers={"X-OpenCLI": "1", "Authorization": signature("POST", "/shutdown")})
    try:
        opener.open(request, timeout=5).close()
    except OSError:
        pass
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _something_listens():
        time.sleep(0.2)
    if _something_listens():
        raise ReachError(NEED_SETUP, "A previous OpenCLI daemon of Agent Reach did not stop",
                         hint="Retry in a minute. If it persists, ask the user to end the leftover `node ... daemon.js` process.")


def close(proc) -> None:
    """Stop only the child this process started."""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
