"""Tools Agent Reach uses, all at exact pinned versions.

- ffmpeg (YouTube transcription): the system copy when there is one, otherwise
  the pinned `imageio-ffmpeg` wheel in ~/.agent-reach/tools/ffmpeg/ (own venv).
- opencli (login channels): the hardened fork, installed privately into
  ~/.agent-reach/tools/opencli/ with `npm ci --ignore-scripts` from the lock
  files shipped in runtime/opencli_lock/. The lock pins the fork's release
  tarball and every transitive package by integrity hash. A copy the user
  installed themselves is never used.
- node: only the user's own Node.js is used; Reach does not download one.

Nothing is put on the user's PATH. state.json records what was installed, so
remove/uninstall delete only that.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from agent_reach.utils.process import windows_hidden_kwargs

from . import state
from .result import NEED_SETUP, TIMEOUT, UPSTREAM_BROKEN, ReachError, _clean

OURS = "agent-reach"


@dataclass(frozen=True)
class Tool:
    name: str
    kind: str                       # "pip" | "npm-locked" | "system"
    package: str                    # exact pip requirement / npm package name
    commands: Tuple[str, ...]       # executables it provides
    needs: Tuple[str, ...] = ()


TOOLS: Dict[str, Tool] = {t.name: t for t in (
    Tool("ffmpeg", "pip", "imageio-ffmpeg==0.8.1", ("ffmpeg",)),
    Tool("node", "system", "", ("node", "npm")),
    Tool("opencli", "npm-locked", "@jackwener/opencli", ("opencli",), needs=("node",)),
)}
#: Oldest Node.js the pinned OpenCLI supports.
NODE_MINIMUM = (20, 18, 1)

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_WIN = sys.platform == "win32"


def tools_root() -> Path:
    return state.root() / "tools"


def tool_dir(name: str) -> Path:
    return tools_root() / name


def _private_bin(tool: Tool, command: str) -> Path:
    if tool.kind == "npm-locked":
        return tool_dir(tool.name) / "node_modules" / ".bin" / command
    return tool_dir(tool.name) / "bin" / (command + (".exe" if _WIN else ""))


def opencli_root() -> Path:
    """The installed OpenCLI package folder (the daemon and its package.json live here)."""
    return tool_dir("opencli") / "node_modules" / "@jackwener" / "opencli"


def bin_dirs() -> List[Path]:
    """Private bin folders that exist."""
    return [d for d in (tool_dir("ffmpeg") / "bin",) if d.is_dir()]


def extend_path() -> None:
    """Append our private bin folders to PATH (system copies still win) so children find ffmpeg."""
    parts = os.environ.get("PATH", "").split(os.pathsep)
    extra = [str(d) for d in bin_dirs() if str(d) not in parts]
    if extra:
        os.environ["PATH"] = os.pathsep.join([p for p in parts if p] + extra)


def resolve(command: str) -> Optional[str]:
    """Absolute path for `command`: our private copy first, then the user's PATH.

    OpenCLI is resolved to our private pinned copy only, never to one on PATH.
    """
    tool = next((t for t in TOOLS.values() if command in t.commands), None)
    if tool and tool.kind != "system":
        private = _private_bin(tool, command)
        if private.exists():
            return str(private)
        if tool.kind == "npm-locked":
            return None
    return shutil.which(command)


def ffmpeg_path() -> Optional[str]:
    return resolve("ffmpeg")


def present(name: str) -> bool:
    """Is the tool usable right now (ours or the system's)? Offline."""
    if name == "chrome":
        from .browser import chrome_binary

        return chrome_binary() is not None
    return all(resolve(c) for c in TOOLS[name].commands)


def install_env() -> dict:
    """Environment for the pip download: a private cache and the configured proxy.

    Explicit shell proxies win; only without one is the OS proxy consulted.
    Returns a copy and never changes the user's proxy settings.
    """
    extend_path()
    env = dict(os.environ)
    cache = state.root() / "cache" / "downloads"
    if cache.exists() and not state.owned_path(cache):
        raise ReachError(NEED_SETUP, "Unrecorded download cache left untouched; choose a clean Reach directory")
    from agent_reach.utils.paths import make_private_dir

    make_private_dir(cache)
    state.remember_path(cache, "download caches")
    env.update(PIP_CACHE_DIR=str(cache / "pip"), npm_config_cache=str(cache / "npm"), XDG_CACHE_HOME=str(cache))
    https = env.get("HTTPS_PROXY") or env.get("https_proxy")
    http = env.get("HTTP_PROXY") or env.get("http_proxy")
    all_proxy = env.get("ALL_PROXY") or env.get("all_proxy")
    if not (https or http or all_proxy):
        found = urllib.request.getproxies()
        https, http = found.get("https"), found.get("http")
        if found.get("no") and not (env.get("NO_PROXY") or env.get("no_proxy")):
            env["NO_PROXY"] = found["no"]
    proxy = https or http or all_proxy
    if proxy:
        env.setdefault("HTTPS_PROXY", https or proxy)
        env.setdefault("HTTP_PROXY", http or proxy)
    return env


def _run(argv, timeout: float = 600, *, stage: str = "tool_install", cwd: Optional[str] = None,
         node_safe: bool = False) -> str:
    hint = f"Stage: {stage}. Check the network, then rerun setup; installed packages and download caches are reused."
    run_env = install_env()
    if node_safe:
        # npm must not inherit inspector flags, preloads or a user npmrc override; keep only our cache setting.
        cache = run_env.get("npm_config_cache")
        run_env = {k: v for k, v in run_env.items() if not k.startswith(("NODE_", "npm_", "NPM_", "OPENCLI_"))}
        if cache:
            run_env["npm_config_cache"] = cache
        run_env["npm_config_userconfig"] = os.devnull
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                              env=run_env, cwd=cwd or os.path.expanduser("~"),  # never the agent's project folder
                              **windows_hidden_kwargs())
    except FileNotFoundError:
        raise ReachError(NEED_SETUP, f"{Path(argv[0]).name} is not installed") from None
    except subprocess.TimeoutExpired:
        raise ReachError(TIMEOUT, f"{stage} timed out after {timeout:.0f}s", hint=hint) from None
    if proc.returncode != 0:
        output = proc.stderr or proc.stdout
        last = next((ln for ln in reversed(output.splitlines()) if ln.strip()), "")
        raise ReachError(UPSTREAM_BROKEN, f"{stage} failed: {_clean(last) or 'no error output'}", hint=hint)
    return proc.stdout


def _node_version() -> tuple:
    node = shutil.which("node")
    if not node:
        return ()
    try:
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("NODE_", "npm_", "NPM_", "OPENCLI_"))}
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=20, env=clean,
                             cwd=os.path.expanduser("~")).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ()
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in m.groups()) if m else ()


def _lock_files():
    from importlib.resources import files

    return files("agent_reach").joinpath("runtime").joinpath("opencli_lock")


def locked_opencli_version() -> str:
    lock = json.loads(_lock_files().joinpath("package-lock.json").read_text("utf-8"))
    return str(lock["packages"]["node_modules/@jackwener/opencli"]["version"])


def _install_opencli(tool: Tool) -> None:
    """`npm ci` from the shipped lock: every package is verified against its recorded integrity hash.

    --ignore-scripts: no install hook of any package runs (OpenCLI's own hook would fetch
    site adapters from the network; the pinned release already contains them).
    """
    folder = tool_dir(tool.name)
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "package-lock.json"):
        (folder / name).write_text(_lock_files().joinpath(name).read_text("utf-8"), encoding="utf-8")
    npm = shutil.which("npm")
    if not npm:
        raise ReachError(NEED_SETUP, "npm is not installed", hint="Ask the user to install Node.js 20.18.1 or newer.")
    _run([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--omit=dev"], stage="opencli:npm_ci", cwd=str(folder),
         node_safe=True)
    if not _private_bin(tool, "opencli").exists():
        raise ReachError(UPSTREAM_BROKEN, "npm finished but the OpenCLI command is missing")


def _install(tool: Tool) -> None:
    """Install one tool at its pinned version into its own private folder."""
    folder = tool_dir(tool.name)
    if folder.exists() and not state.owned_path(folder) and state.load()["tools"].get(tool.name, {}).get("owner") != OURS:
        raise ReachError(NEED_SETUP, f"Unrecorded files occupy the {tool.name} install directory; left untouched")
    state.remember_path(folder, "installed dependency")
    if tool.kind == "npm-locked":
        _install_opencli(tool)
        return
    venv = folder / "venv"
    venv.parent.mkdir(parents=True, exist_ok=True)
    py = venv / ("Scripts/python.exe" if _WIN else "bin/python")
    # -I: a module in the working directory or user site must never shadow venv/pip.
    if not py.is_file():
        _run([sys.executable, "-I", "-m", "venv", str(venv)], stage=f"{tool.name}:venv")
    _run([str(py), "-I", "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--only-binary", ":all:",
          tool.package], stage=f"{tool.name}:pip_packages")
    exe = _run([str(py), "-I", "-c", "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"]).strip()
    link = _private_bin(tool, "ffmpeg")
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists() or link.is_symlink():
        link.unlink()
    try:
        link.symlink_to(exe)
    except (OSError, NotImplementedError):
        shutil.copy2(exe, link)
    extend_path()


def record(name: str, version: Optional[str], *, owner: str = OURS, scope: str = "private") -> dict:
    entry = {"owner": owner, "scope": scope, "version": version}
    state.mutate(lambda data: data["tools"].__setitem__(name, entry))
    return {"tool": name, **entry}


def ensure(name: str, *, upgrade: bool = False) -> dict:
    """Make sure the tool is usable; returns {"tool", "owner", "scope", "version"}.

    owner "system" means the user's own copy is used and nothing is recorded.
    """
    tool = TOOLS[name]
    for dep in tool.needs:
        ensure(dep)
    if tool.kind == "system":
        found = _node_version()
        if found < NODE_MINIMUM:
            raise ReachError(NEED_SETUP, "Node.js 20.18.1 or newer is required for the login channels",
                             hint="Ask the user to install Node.js (https://nodejs.org or `brew install node`). "
                                  "Reach does not download Node.js itself.")
        return {"tool": name, "owner": "system", "scope": "system", "version": ".".join(map(str, found))}
    entry = state.load()["tools"].get(name) or {}
    private_ok = all(_private_bin(tool, c).exists() for c in tool.commands)
    if tool.kind == "npm-locked":
        wanted = locked_opencli_version()
        if private_ok and entry.get("owner") == OURS and entry.get("version") == wanted and not upgrade:
            return {"tool": name, **entry}
        _install(tool)
        return record(name, wanted)
    if not private_ok and all(shutil.which(c) for c in tool.commands):
        return {"tool": name, "owner": "system", "scope": "system", "version": None}
    if private_ok and entry.get("owner") == OURS and not upgrade:
        return {"tool": name, **entry}
    _install(tool)
    return record(name, tool.package.partition("==")[2])


def remove_private(name: str) -> bool:
    """Delete one of our installs and its record. System copies are never touched."""
    entry = state.load()["tools"].get(name)
    if not entry or entry.get("owner") != OURS:
        return False
    if _SAFE_NAME.fullmatch(name) and tool_dir(name).exists():
        from agent_reach.utils.paths import ensure_no_symlink_path

        ensure_no_symlink_path(tool_dir(name), "private tool")
        shutil.rmtree(tool_dir(name))
    state.mutate(lambda data: data["tools"].pop(name, None))
    return True


def closure(names: Iterable[str]) -> Set[str]:
    """The tools plus everything they need."""
    out: Set[str] = set()
    todo = [n for n in names if n in TOOLS]
    while todo:
        n = todo.pop()
        if n not in out:
            out.add(n)
            todo.extend(TOOLS[n].needs)
    return out


def prune(keep: Set[str]) -> List[str]:
    """Remove our tools not in `keep`, including records left by older versions (node, opencli...)."""
    removed = []
    for name, entry in list(state.load()["tools"].items()):
        if name in keep:
            continue
        if entry.get("owner") != OURS or not _SAFE_NAME.match(name):
            def forget(data: dict, n: str = name) -> None:
                data["tools"].pop(n, None)
            state.mutate(forget)
            continue
        if remove_private(name):
            removed.append(name)
    return removed


def recorded() -> List[dict]:
    """Every tool in state.json with its installed version (for doctor)."""
    return [{"tool": n, **e} for n, e in sorted(state.load()["tools"].items())]
