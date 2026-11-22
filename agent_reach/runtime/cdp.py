"""Control of the dedicated browser without any network listener.

Chrome is started with --remote-debugging-pipe by the watcher process, which
is the only holder of that pipe. There is no TCP debugging port. Other Agent
Reach processes talk to the watcher over a unix socket that lives in the
private (0700) browser folder with mode 0600, and that socket accepts a few
high-level commands only. No cookie value and no raw CDP call crosses it.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path
from typing import Callable, Dict, Optional

_TIMEOUT = 15.0
_MAX_LINE = 64 * 1024


def socket_path() -> Path:
    from .browser import browser_root

    return browser_root() / "control.sock"


def request(command: str, timeout: float = _TIMEOUT, **args) -> Optional[dict]:
    """Send one command to the watcher; returns its reply, or None when no watcher answers."""
    if not hasattr(socket, "AF_UNIX"):
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(socket_path()))
            sock.sendall(json.dumps({"command": command, **args}).encode("utf-8") + b"\n")
            buf = b""
            while not buf.endswith(b"\n") and len(buf) <= _MAX_LINE:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
        reply = json.loads(buf.decode("utf-8"))
    except (OSError, ValueError):
        return None
    return reply if isinstance(reply, dict) else None


def domain_matches(cookie_domain: str, domain: str) -> bool:
    """`cookie_domain` is `domain` or one of its subdomains (".a.reddit.com" yes, "notreddit.com" no)."""
    c, d = cookie_domain.lstrip(".").lower(), domain.lstrip(".").lower()
    return c == d or c.endswith("." + d)


class PipeConnection:
    """CDP over `--remote-debugging-pipe`: NUL-delimited JSON, we write `wfd`, read `rfd`.

    A reader thread drains the pipe for as long as the browser lives (an
    undrained pipe would stall Chrome); `closed` is set when Chrome exits.
    Closing our write end makes Chrome exit.
    """

    def __init__(self, wfd: int, rfd: int) -> None:
        self._wfd, self._rfd = wfd, rfd
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._waiting: Dict[int, list] = {}
        self.closed = threading.Event()
        threading.Thread(target=self._read_loop, name="cdp-pipe", daemon=True).start()

    def _read_loop(self) -> None:
        buf = b""
        try:
            while True:
                chunk = os.read(self._rfd, 65536)
                if not chunk:
                    break
                buf += chunk
                while b"\0" in buf:
                    raw, buf = buf.split(b"\0", 1)
                    try:
                        msg = json.loads(raw.decode("utf-8"))
                    except ValueError:
                        continue
                    with self._lock:
                        message_id = msg.get("id") if isinstance(msg, dict) else None
                        slot = self._waiting.get(message_id) if isinstance(message_id, int) else None
                    if slot is not None:
                        slot.append(msg)
                        slot[0].set()
        except OSError:
            pass
        finally:
            self.closed.set()
            with self._lock:
                for slot in self._waiting.values():
                    slot[0].set()

    def call(self, method: str, params: Optional[dict] = None, timeout: float = 30.0,
             session_id: Optional[str] = None) -> dict:
        """Send one command; returns the whole reply ({"result": ...} or {"error": ...})."""
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
            slot: list = [threading.Event()]
            self._waiting[msg_id] = slot
        try:
            if self.closed.is_set():
                return {"error": {"message": "browser closed"}}
            message: dict = {"id": msg_id, "method": method, "params": params or {}}
            if session_id:
                message["sessionId"] = session_id
            data = json.dumps(message).encode("utf-8") + b"\0"
            try:
                with self._write_lock:  # commands from several threads must not interleave
                    while data:
                        data = data[os.write(self._wfd, data):]
            except OSError as exc:
                return {"error": {"message": f"pipe write failed: {exc}"}}
            if not slot[0].wait(timeout) or len(slot) < 2:
                return {"error": {"message": "browser closed" if self.closed.is_set() else f"no reply in {timeout:.0f}s"}}
            return slot[1]
        finally:
            with self._lock:
                self._waiting.pop(msg_id, None)

    def on_target(self, target_id: str, method: str, params: Optional[dict] = None, timeout: float = 30.0) -> dict:
        """Run one command against a page or worker target, attaching only for that call."""
        attached = self.call("Target.attachToTarget", {"targetId": target_id, "flatten": True}, timeout=timeout)
        session = (attached.get("result") or {}).get("sessionId")
        if not session:
            return {"error": {"message": "could not attach to the target"}}
        try:
            return self.call(method, params, timeout=timeout, session_id=session)
        finally:
            self.call("Target.detachFromTarget", {"sessionId": session}, timeout=5)

    def targets(self) -> list:
        infos = (self.call("Target.getTargets").get("result") or {}).get("targetInfos")
        return [t for t in infos if isinstance(t, dict)] if isinstance(infos, list) else []

    def close(self) -> None:
        """Close our write end (Chrome exits); the reader thread closes the read end when it stops."""
        try:
            os.close(self._wfd)
        except OSError:
            pass
        if self.closed.wait(15):
            try:
                os.close(self._rfd)
            except OSError:
                pass


def _peer_uid(client: socket.socket) -> Optional[int]:
    """The user id of the process on the other end of a unix socket, when the OS can tell."""
    import struct
    import sys

    try:
        if sys.platform.startswith("linux"):
            raw = client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            return struct.unpack("3i", raw)[1]
        if sys.platform == "darwin":
            raw = client.getsockopt(0, 0x001, struct.calcsize("2I"))  # SOL_LOCAL, LOCAL_PEERCRED -> xucred
            return struct.unpack("2I", raw)[1]
    except (OSError, struct.error, AttributeError):
        return None
    return None


class ControlServer:
    """The watcher's side of the control socket: one JSON line in, one JSON line out."""

    def __init__(self, handlers: Dict[str, Callable[..., dict]]) -> None:
        from agent_reach.utils.paths import ensure_no_symlink_path, make_private_dir

        path = socket_path()
        make_private_dir(path.parent)
        ensure_no_symlink_path(path, "control socket")
        if path.exists():
            path.unlink()  # a socket left by a watcher that is gone; we hold the watcher lock
        self._handlers = handlers
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # The folder is 0700, so nobody else can reach the socket even before the chmod.
        self._sock.bind(str(path))
        os.chmod(path, 0o600)
        self._sock.listen(8)
        self._path = path
        threading.Thread(target=self._accept_loop, name="reach-control", daemon=True).start()

    def _accept_loop(self) -> None:
        while True:
            try:
                client, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        with client:
            try:
                if _peer_uid(client) != os.getuid():
                    return  # only this user's processes are answered; an unknown peer is refused
                client.settimeout(5)
                buf = b""
                while not buf.endswith(b"\n") and len(buf) <= _MAX_LINE:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                req = json.loads(buf.decode("utf-8"))
                handler = self._handlers.get(req.pop("command", None)) if isinstance(req, dict) else None
                if handler is None:
                    reply = {"ok": False, "error": "unknown command"}
                else:
                    client.settimeout(120)
                    reply = handler(**{k: v for k, v in req.items() if isinstance(k, str)})
            except Exception as exc:  # noqa: BLE001 - one bad request must not stop the watcher
                reply = {"ok": False, "error": type(exc).__name__}
            try:
                client.sendall(json.dumps(reply).encode("utf-8") + b"\n")
            except OSError:
                pass

    def close(self) -> None:
        try:
            self._sock.close()
        finally:
            try:
                self._path.unlink()
            except OSError:
                pass
