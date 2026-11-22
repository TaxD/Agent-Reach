"""Bounded calls to upstream tools: subprocesses and HTTP."""

from __future__ import annotations

import http.client
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Mapping, Optional, Sequence

from agent_reach import __version__
from agent_reach.utils.process import utf8_subprocess_env, windows_hidden_kwargs

from .result import NEED_SETUP, TIMEOUT, ReachError, _clean

_DEFAULT_MAX_BYTES = 8 * 1024 * 1024
_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
# Some fronts (Exa's Cloudflare) block the default Python-urllib agent (error 1010).
_USER_AGENT = f"agent-reach/{__version__}"


class CommandFailed(ReachError):
    """Upstream exited non-zero. Channels may re-map it using `stderr`."""

    def __init__(self, code: str, message: str, stderr: str = "", stdout: str = ""):
        super().__init__(code, message)
        self.stderr = stderr
        self.stdout = stdout


def run_cmd(
    argv: Sequence[str],
    *,
    timeout: float,
    env: Optional[Mapping[str, str]] = None,
    cwd: Optional[str] = None,
    label: Optional[str] = None,
    input_data: Optional[str] = None,
    drop_env_prefix: tuple = (),
) -> str:
    """Run an upstream command and return stdout.

    Runs from the user's home by default so tools never pick up config from
    whatever project the agent happens to be in. On timeout the whole process
    group is killed.
    """
    from .tools import resolve

    name = label or Path(argv[0]).name
    exe = resolve(argv[0]) if not os.path.isabs(argv[0]) else (argv[0] if os.path.exists(argv[0]) else None)
    if not exe:
        raise ReachError(NEED_SETUP, f"{name} is not installed")
    run_env = utf8_subprocess_env()
    if drop_env_prefix:  # inherited settings of that tool must not steer it; only `env` does
        run_env = {k: v for k, v in run_env.items() if not k.startswith(drop_env_prefix)}
    if env:
        run_env.update(env)
    # The local API worker is already in a supervised process group. Keep its
    # children there so the API's hard deadline can stop the entire execution.
    supervised = os.environ.get("REACH_API_SUPERVISED") == "1"
    child_argv = [exe, *argv[1:]]
    proc = subprocess.Popen(
        child_argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
        env=run_env,
        cwd=cwd or os.path.expanduser("~"),
        start_new_session=(sys.platform != "win32" and not supervised),
        **windows_hidden_kwargs(),
    )
    def kill() -> None:
        if sys.platform == "win32":
            taskkill = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe")
            try:
                subprocess.run([taskkill, "/PID", str(proc.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
        elif not supervised:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    # Both pipes are drained by threads with a byte budget each, so a chatty or hostile
    # child cannot exhaust memory; going over the budget stops the child.
    buffers = {"out": bytearray(), "err": bytearray()}
    over = threading.Event()

    def pump(stream, key: str) -> None:
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                if len(buffers[key]) + len(chunk) > _MAX_OUTPUT_BYTES:
                    over.set()
                    return
                buffers[key] += chunk
        except (OSError, ValueError):
            return

    readers = [threading.Thread(target=pump, args=(proc.stdout, "out"), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, "err"), daemon=True)]
    for reader in readers:
        reader.start()
    if input_data is not None and proc.stdin is not None:
        try:
            proc.stdin.write(input_data.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass
    deadline = time.monotonic() + max(timeout, 0.1)
    while proc.poll() is None and not over.is_set():
        if time.monotonic() >= deadline:
            kill()
            raise ReachError(TIMEOUT, f"{name} did not finish within {timeout:.0f}s")
        time.sleep(0.02)
    for reader in readers:
        reader.join(timeout=5)
    if over.is_set():  # checked after the readers finished, so a late overflow is never returned as success
        kill()
        raise ReachError("UPSTREAM_BROKEN", f"{name} produced more output than the {_MAX_OUTPUT_BYTES} byte limit")
    out, err = bytes(buffers["out"]), bytes(buffers["err"])
    stdout = out.decode("utf-8", errors="replace")
    stderr = err.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        last = next((ln for ln in reversed(stderr.splitlines()) if ln.strip()), "")
        raise CommandFailed(
            "UPSTREAM_BROKEN",
            f"{name} exited {proc.returncode}: {_clean(last) or 'no error output'}",
            stderr=stderr,
            stdout=stdout,
        )
    return stdout


class _SameOriginRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, url: str):
        from urllib.parse import urlsplit

        self._origin = self._key(urlsplit(url))

    @staticmethod
    def _key(parts):
        return parts.scheme.lower(), (parts.hostname or "").lower(), parts.port

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        from urllib.parse import urlsplit

        fp.close()  # do not buffer the redirect body
        if self._key(urlsplit(newurl)) != self._origin:
            raise ReachError("UPSTREAM_BROKEN", "refused a redirect to another origin on a credentialed request")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_request(
    url: str,
    *,
    timeout: float,
    data: Optional[bytes] = None,
    headers: Optional[Mapping[str, str]] = None,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    same_origin_redirects: bool = True,
) -> bytes:
    """GET (or POST when `data` is given) with a size cap. Errors propagate to classify().

    `same_origin_redirects` (default): a hop that leaves the original scheme://host:port is
    refused. urllib would otherwise repeat every header and, for some redirects, the body
    at whatever address the answering server names.
    """
    merged = {"User-Agent": _USER_AGENT}
    merged.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=merged)
    opened = (urllib.request.build_opener(_SameOriginRedirects(url)).open(req, timeout=max(timeout, 0.1))
              if same_origin_redirects else urllib.request.urlopen(req, timeout=max(timeout, 0.1)))
    with opened as resp:
        body = resp.read(max_bytes + 1)
        declared = getattr(resp, "headers", {}).get("Content-Length", "")
        if isinstance(declared, str) and declared.strip().isdigit() and len(body) <= max_bytes:
            expected = int(declared)
            if len(body) < expected:
                # A bounded read can silently return a short body on early EOF.
                raise http.client.IncompleteRead(body, expected - len(body))
    if len(body) > max_bytes:
        raise ReachError("UPSTREAM_BROKEN", f"response exceeds {max_bytes} bytes")
    return body
