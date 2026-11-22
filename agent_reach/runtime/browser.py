"""The Agent Reach dedicated browser.

One Reach-owned Chrome profile under ~/.agent-reach/browser/profile serves the
login channels (reddit, twitter). Design rules:

- Only an installed Google Chrome/Chromium is used; no browser is downloaded.
- The window is always visible. The user can see what is done with their login.
- Chrome is driven over --remote-debugging-pipe. There is no TCP debugging port.
- The watcher process owns the pipe and offers a few commands on a private unix
  socket (see cdp.py). No cookie value leaves the browser profile: nothing is
  copied from other browsers and no cookie snapshot is written to disk.
- The OpenCLI extension and daemon come from a pinned, checksummed release of
  the hardened fork, whose daemon requires a secret token (see bridge.py).
- The browser closes after IDLE_LIMIT seconds without use.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Iterator, List, Optional

from agent_reach.utils.paths import (
    PrivatePathError,
    atomic_write_private_text,
    ensure_no_symlink_path,
    home_dir,
    make_private_dir,
    read_small_text_no_follow,
)

from .result import NEED_SETUP, UPSTREAM_BROKEN, ReachError

#: Close the browser after this many seconds without Agent Reach use or user input.
IDLE_LIMIT = 600.0
#: Longest wait for the browser, the extension and the daemon to connect.
START_WAIT = 90.0
_LOCK_WAIT = START_WAIT + 30.0

#: The extension can read every cookie of the dedicated profile, so it is pinned to one
#: release asset of the hardened fork and verified twice: the archive by SHA-256 before
#: unpacking, and the unpacked files by a tree digest before every browser start.
#: Bump these together with the OpenCLI lock files in runtime/opencli_lock/.
OPENCLI_EXTENSION_URL = (
    "https://github.com/TaxD/OpenCLI/releases/download/v2.0.3-reach.2/opencli-extension-1.0.27.2.zip"
)
OPENCLI_EXTENSION_SHA256 = "41bf22fce131c7bda53c44750bc5c0c385c919a135a292589822148e4f17a475"
OPENCLI_EXTENSION_TREE_SHA256 = "304d62113d398501d359e86b16724ef46f9e7838ffd664657a48497374bf8dd9"
_MAX_EXTENSION_BYTES = 16 * 1024 * 1024
#: Written into the extension folder at install; holds this machine's daemon token.
TOKEN_FILE_IN_EXTENSION = "daemon-token.json"
_USER_AGENT = "agent-reach"


def browser_root() -> Path:
    return home_dir() / ".agent-reach" / "browser"


def profile_dir() -> Path:
    return browser_root() / "profile"


def extension_dir() -> Path:
    return browser_root() / "extension"


def watcher_log_path() -> Path:
    return browser_root() / "watcher.log"


def daemon_token_path() -> Path:
    return browser_root() / "daemon-token"


def _state_path() -> Path:
    return browser_root() / "state.json"


def _last_used_path() -> Path:
    return browser_root() / "last_used"


def watcher_lock_path() -> Path:
    return browser_root() / "watcher.lock"


def _load_state() -> dict:
    try:
        text = read_small_text_no_follow(_state_path(), max_bytes=64 * 1024)
        return json.loads(text) if text else {}
    except (OSError, PrivatePathError, ValueError):
        return {}


def update_state(**changes) -> None:
    """Merge `changes` into the browser state (None deletes a key); re-reads first, other processes write too."""
    from . import state as ledger

    state = _load_state()
    for key, value in changes.items():
        if value is None:
            state.pop(key, None)
        else:
            state[key] = value
    make_private_dir(browser_root())
    ledger.remember_path(_state_path(), "browser lifecycle state")
    atomic_write_private_text(_state_path(), json.dumps(state, indent=2))


# ── last use ──


def touch() -> None:
    """Record an Agent Reach use of the browser (resets the idle timer)."""
    make_private_dir(browser_root())
    path = _last_used_path()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(fd)
    os.utime(path)


def last_used() -> Optional[float]:
    try:
        return _last_used_path().stat().st_mtime
    except OSError:
        return None


# ── cross-process locks ──


def _open_lock(path: Path) -> int:
    from . import state as ledger

    make_private_dir(path.parent)
    ledger.remember_path(path, "browser lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    return os.open(path, flags, 0o600)


def try_lock(fd: int) -> bool:
    """Take an exclusive lock on `fd` without blocking."""
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, ImportError):
        return False
    return True


@contextlib.contextmanager
def start_lock(wait: float = _LOCK_WAIT) -> Iterator[None]:
    """Serializes launching the browser across processes."""
    fd = _open_lock(browser_root() / "start.lock")
    try:
        deadline = time.monotonic() + wait
        while not try_lock(fd):
            if time.monotonic() >= deadline:
                raise ReachError(UPSTREAM_BROKEN, "another Agent Reach process is still starting the browser")
            time.sleep(0.2)
        yield
    finally:
        os.close(fd)  # closing releases the lock


def watcher_running() -> bool:
    fd = _open_lock(watcher_lock_path())
    try:
        return not try_lock(fd)
    finally:
        os.close(fd)


def wait_for(check: Callable[[], bool], seconds: float, interval: float = 1.0) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if check():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


# ── Chrome ──


def _system_chrome() -> Optional[str]:
    if platform.system() == "Darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        candidates = [c for n in ("google-chrome", "google-chrome-stable", "chromium") if (c := shutil.which(n)) is not None]
    return next((c for c in candidates if os.path.exists(c)), None)


def chrome_binary() -> Optional[str]:
    """Offline lookup; never starts a browser or downloads anything."""
    return _system_chrome()


def ensure_chrome() -> str:
    """Use the Chrome/Chromium already installed on this machine; never download a browser."""
    if sys.platform == "win32":
        raise ReachError(NEED_SETUP, "Login channels are not supported on Windows in this build",
                         hint="The dedicated browser needs a private pipe and unix socket. Use the login-free channels.")
    selected = _system_chrome()
    if not selected:
        raise ReachError(NEED_SETUP, "Google Chrome is not installed",
                         hint="Ask the user to install Google Chrome from https://www.google.com/chrome/, "
                              "then run agent-reach browser setup. Reach never downloads a browser itself.")
    return selected


def chrome_argv(exe: str) -> List[str]:
    """A visible Chrome on the dedicated profile, controllable only through the launch pipe."""
    argv = [exe, f"--user-data-dir={profile_dir()}", "--remote-debugging-pipe", "--enable-unsafe-extension-debugging",
            "--no-first-run", "--no-default-browser-check"]
    if os.environ.get("AGENT_REACH_TEST_MOCK_KEYCHAIN") == "1":
        # Tests only (temp HOME has no login keychain). Never for real users: it changes the cookie key.
        argv += ["--use-mock-keychain", "--password-store=basic"]
    return argv + ["about:blank"]


# ── daemon token ──


def ensure_token() -> Path:
    """The secret shared by the OpenCLI CLI, its daemon and the extension on this machine.

    Created once with 256 bits of randomness, readable by this user only. It is
    never printed and never placed in command lines or environment values; only
    the path of the file is passed around.
    """
    path = daemon_token_path()
    from . import state as ledger

    make_private_dir(browser_root())
    try:
        current = read_small_text_no_follow(path, max_bytes=4096)
    except (OSError, PrivatePathError):
        current = None
    if not current or len(current.strip()) < 32:
        ledger.remember_path(path, "daemon token")
        atomic_write_private_text(path, secrets.token_hex(32) + "\n")
    return path


def _read_token() -> str:
    return (read_small_text_no_follow(ensure_token(), max_bytes=4096) or "").strip()


# ── OpenCLI extension ──


def extension_version() -> Optional[str]:
    try:
        return json.loads((extension_dir() / "manifest.json").read_text("utf-8")).get("version")
    except (OSError, ValueError):
        return None


def extension_tree_digest(root: Optional[Path] = None) -> Optional[str]:
    """SHA-256 over every shipped file of the extension (sorted path, NUL, file digest).

    The per-machine token file is not part of the release and is skipped.
    """
    root = root or extension_dir()
    if root.is_symlink() or not root.is_dir():
        return None
    digest = hashlib.sha256()
    try:
        for path in sorted(p for p in root.rglob("*") if not p.is_dir()):
            if path.is_symlink():
                return None
            rel = path.relative_to(root).as_posix()
            if rel == TOKEN_FILE_IN_EXTENSION:
                continue
            digest.update(rel.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    except OSError:
        return None
    return digest.hexdigest()


def extension_intact() -> bool:
    return extension_tree_digest() == OPENCLI_EXTENSION_TREE_SHA256


def _unzip(archive: Path, dest: Path) -> None:
    """Extract regular files only; refuse traversal, absolute paths and symlinks."""
    root = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        if sum(info.file_size for info in zf.infolist()) > 4 * _MAX_EXTENSION_BYTES:
            raise ValueError("archive expands beyond the size limit")
        for info in zf.infolist():
            target = (dest / info.filename).resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"unsafe path in archive: {info.filename}")
            if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                raise ValueError(f"symlink in archive: {info.filename}")
            zf.extract(info, dest)


def _download(url: str, dest: Path, timeout: float = 60.0) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read(_MAX_EXTENSION_BYTES + 1)
    dest.write_bytes(body)


def _write_extension_token() -> None:
    """Give the extension this machine's daemon token (it cannot read files outside its folder)."""
    target = extension_dir() / TOKEN_FILE_IN_EXTENSION
    ensure_no_symlink_path(target, "extension token")
    atomic_write_private_text(target, json.dumps({"token": _read_token()}))


def refresh_extension() -> dict:
    """Install the pinned OpenCLI extension unless the installed files already match it.

    Returns {"version", "updated"}. A running browser is closed before files change.
    """
    if extension_intact():
        _write_extension_token()
        return {"version": extension_version(), "updated": False}
    from . import state as ledger

    make_private_dir(browser_root())
    ledger.remember_path(extension_dir(), "OpenCLI extension")
    work = Path(tempfile.mkdtemp(prefix=".extension-", dir=browser_root()))
    try:
        archive = work / "extension.zip"
        _download(OPENCLI_EXTENSION_URL, archive)
        if archive.stat().st_size > _MAX_EXTENSION_BYTES:
            raise ReachError(UPSTREAM_BROKEN, "the OpenCLI extension download is unexpectedly large; not installed")
        if hashlib.sha256(archive.read_bytes()).hexdigest() != OPENCLI_EXTENSION_SHA256:
            raise ReachError(UPSTREAM_BROKEN, "the OpenCLI extension download failed its checksum; not installed",
                             hint="The pinned release asset changed or the download was tampered with. Do not retry in a loop.")
        unpacked = work / "unpacked"
        unpacked.mkdir()
        _unzip(archive, unpacked)
        if not (unpacked / "manifest.json").is_file() or extension_tree_digest(unpacked) != OPENCLI_EXTENSION_TREE_SHA256:
            raise ReachError(UPSTREAM_BROKEN, "the unpacked OpenCLI extension does not match the pinned files; not installed")
        if is_running():
            stop()  # never swap files under a running extension
        shutil.rmtree(extension_dir(), ignore_errors=True)
        os.replace(unpacked, extension_dir())
    except ReachError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ReachError(UPSTREAM_BROKEN, f"could not install the OpenCLI extension: {type(exc).__name__}") from None
    finally:
        shutil.rmtree(work, ignore_errors=True)
    _write_extension_token()
    return {"version": extension_version(), "updated": True}


# ── process ──


def _watcher() -> Optional[dict]:
    """The watcher's answer to a ping, or None when no browser of ours is up."""
    from . import cdp

    reply = cdp.request("ping", timeout=3)
    return reply if reply and reply.get("ok") else None


def is_running() -> bool:
    return _watcher() is not None


def _launch() -> None:
    """Start the watcher, which starts Chrome. Caller holds start_lock()."""
    from . import state as ledger

    exe = ensure_chrome()
    ensure_token()
    refresh_extension()
    make_private_dir(profile_dir())
    ledger.remember_path(profile_dir(), "dedicated browser profile")
    for name in ("watcher.log", "last_used", "watcher.lock", "start.lock", "control.sock"):
        ledger.remember_path(browser_root() / name, "browser lifecycle")
    update_state(started_at=time.time())
    log_path = watcher_log_path()
    ensure_no_symlink_path(log_path, "watcher log")
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as log:
        # -I: nothing in the working directory or the user's Python settings can shadow the package.
        proc = subprocess.Popen(
            [sys.executable, "-I", "-m", "agent_reach.runtime.browser_watcher", "--chrome", exe],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=str(browser_root()), start_new_session=True)
    if not wait_for(lambda: is_running() or proc.poll() is not None, START_WAIT, interval=0.2) or not is_running():
        tail = ""
        with contextlib.suppress(OSError):
            tail = watcher_log_path().read_text("utf-8", "replace").strip().splitlines()[-1][:200]
        raise ReachError(UPSTREAM_BROKEN, "the Agent Reach browser did not open" + (f": {tail}" if tail else ""))


def ensure_running() -> str:
    """Make the browser ready (window open, extension connected to the daemon); returns the OpenCLI profile id."""
    from .clock import off_the_clock

    touch()

    def ready() -> Optional[str]:
        info = _watcher()
        context = info.get("context") if info else None
        return context if context and context in connected_contexts() else None

    context = ready()
    if context:
        update_state(opencli_context=context)
        return context
    with off_the_clock():
        with start_lock():
            if not is_running():
                if watcher_running():
                    raise ReachError(UPSTREAM_BROKEN, "the previous browser session is still closing; retry in a few seconds")
                _launch()
            found: List[str] = []

            def connected() -> bool:
                if not is_running():
                    raise ReachError(UPSTREAM_BROKEN, "The dedicated browser closed during startup",
                                     hint=f"Details: {watcher_log_path()}")
                context = ready()
                if context:
                    found.append(context)
                return bool(context)

            if not wait_for(connected, START_WAIT, interval=0.5):
                raise ReachError(UPSTREAM_BROKEN, "the OpenCLI extension did not connect in the Agent Reach browser",
                                 hint=f"Details: {watcher_log_path()}")
    update_state(opencli_context=found[-1])
    touch()
    return found[-1]


def start(url: str) -> None:
    """Open `url` (a channel's login page) in the dedicated browser window for the user."""
    from . import cdp

    ensure_running()
    reply = cdp.request("open", url=url)
    if not reply or not reply.get("ok"):
        raise ReachError(UPSTREAM_BROKEN, "could not open the login page in the Agent Reach browser")


def stop(timeout: float = 20.0) -> bool:
    """Ask the watcher to close the browser. True when nothing of ours is running afterwards."""
    from . import cdp

    if not watcher_running():
        return True
    cdp.request("close", timeout=5)
    return wait_for(lambda: not watcher_running(), timeout, interval=0.2)


def clear_site(domains: List[str]) -> int:
    """Delete the dedicated browser's cookies and storage for `domains`; returns how many cookies went.

    Opens the browser if it is closed; does nothing when it was never used.
    """
    from . import cdp
    from .browser_binding import exclusive_browser

    if not domains or not (is_running() or profile_dir().is_dir()):
        return 0
    with exclusive_browser():
        ensure_running()
        reply = cdp.request("clear_site", timeout=60, domains=list(domains))
    if not reply or not reply.get("ok"):
        raise ReachError(UPSTREAM_BROKEN, "Site login cleanup could not be verified; channel retained")
    return int(reply.get("cleared") or 0)


def delete_profile() -> bool:
    """Remove recorded browser artifacts only, preserving unknown files."""
    from . import state as ledger

    root = browser_root()
    if not root.exists():
        return False
    if not stop():
        raise ReachError(UPSTREAM_BROKEN, "The dedicated browser could not close; its files were retained")
    paths = [root.parent / n for n in ledger.load().get("owned_paths", {})
             if n.startswith("browser/") and ".." not in Path(n).parts]
    for path in sorted(paths, key=lambda p: len(p.parts), reverse=True):
        ensure_no_symlink_path(path.parent, "browser artifact parent")
        if path.is_file() or path.is_symlink():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)
    with contextlib.suppress(OSError):
        root.rmdir()
    return not root.exists()


# ── OpenCLI binding ──


def connected_contexts() -> List[str]:
    """OpenCLI Browser Bridge profiles connected to the daemon right now."""
    from . import bridge

    return [p["contextId"] for p in bridge.status().get("profiles", [])
            if isinstance(p, dict) and p.get("extensionConnected") and isinstance(p.get("contextId"), str)]


def bound_context() -> Optional[str]:
    """The OpenCLI profile recorded as the dedicated browser, or None."""
    return _load_state().get("opencli_context") or None


def setup() -> dict:
    """Make the dedicated browser ready; returns status(). Needs node and the pinned OpenCLI installed."""
    ensure_running()
    return status()


def status() -> dict:
    info = _watcher()
    context = info.get("context") if info else None
    connected = None
    if context:
        try:
            connected = context in connected_contexts()
        except Exception:  # noqa: BLE001 - status must never raise
            connected = False
    return {
        "profile_dir": str(profile_dir()),
        "chrome": chrome_binary(),
        "extension_version": extension_version(),
        "extension_intact": extension_intact(),
        "running": info is not None,
        "window": "visible",
        "debugging_port": None,
        "opencli_context": context or bound_context(),
        "opencli_connected": connected,
        "idle_close_after_s": int(IDLE_LIMIT),
    }


def setup_problems() -> List[str]:
    """What blocks browser-login channels right now (offline checks only)."""
    if sys.platform == "win32":
        return ["login channels are not supported on Windows in this build"]
    return [] if chrome_binary() else ["Google Chrome is not installed"]
