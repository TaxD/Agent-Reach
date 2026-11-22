"""The one upstream tool Agent Reach may install: ffmpeg (for YouTube transcription).

A system ffmpeg is used when there is one. Otherwise a private copy comes from
the pinned `imageio-ffmpeg` wheel into ~/.agent-reach/tools/ffmpeg/ (its own
venv); nothing is put on the user's PATH and no admin password is needed.
state.json records what was installed, so remove/uninstall delete only that.
"""

from __future__ import annotations

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
    package: str                    # exact pip requirement
    commands: Tuple[str, ...]       # executables it provides


TOOLS: Dict[str, Tool] = {"ffmpeg": Tool("ffmpeg", "imageio-ffmpeg==0.8.1", ("ffmpeg",))}

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_WIN = sys.platform == "win32"


def tools_root() -> Path:
    return state.root() / "tools"


def tool_dir(name: str) -> Path:
    return tools_root() / name


def _private_bin(tool: Tool, command: str) -> Path:
    return tool_dir(tool.name) / "bin" / (command + (".exe" if _WIN else ""))


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
    """Absolute path for `command`: our private copy first, then the user's PATH."""
    tool = next((t for t in TOOLS.values() if command in t.commands), None)
    if tool:
        private = _private_bin(tool, command)
        if private.exists():
            return str(private)
    return shutil.which(command)


def ffmpeg_path() -> Optional[str]:
    return resolve("ffmpeg")


def present(name: str) -> bool:
    """Is the tool usable right now (ours or the system's)? Offline."""
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
    env.update(PIP_CACHE_DIR=str(cache / "pip"), XDG_CACHE_HOME=str(cache))
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


def _run(argv, timeout: float = 600, *, stage: str = "tool_install") -> str:
    hint = f"Stage: {stage}. Check the network, then rerun setup; installed packages and download caches are reused."
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                              env=install_env(), cwd=os.path.expanduser("~"),  # never the agent's project folder
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


def _install(tool: Tool) -> None:
    """Install the pinned wheel into the tool's own venv and expose its ffmpeg binary."""
    folder = tool_dir(tool.name)
    if folder.exists() and not state.owned_path(folder) and state.load()["tools"].get(tool.name, {}).get("owner") != OURS:
        raise ReachError(NEED_SETUP, f"Unrecorded files occupy the {tool.name} install directory; left untouched")
    state.remember_path(folder, "installed dependency")
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
    data = state.load()
    entry = {"owner": owner, "scope": scope, "version": version}
    data["tools"][name] = entry
    state.save(data)
    return {"tool": name, **entry}


def ensure(name: str, *, upgrade: bool = False) -> dict:
    """Make sure the tool is usable; returns {"tool", "owner", "scope", "version"}.

    owner "system" means the user's own copy is used and nothing is recorded.
    """
    tool = TOOLS[name]
    entry = state.load()["tools"].get(name) or {}
    private_ok = all(_private_bin(tool, c).exists() for c in tool.commands)
    if not private_ok and all(shutil.which(c) for c in tool.commands):
        return {"tool": name, "owner": "system", "scope": "system", "version": None}
    if private_ok and entry.get("owner") == OURS and not upgrade:
        return {"tool": name, **entry}
    _install(tool)
    return record(name, tool.package.partition("==")[2])


def remove_private(name: str) -> bool:
    """Delete one of our installs and its record. System copies are never touched."""
    data = state.load()
    entry = data["tools"].get(name)
    if not entry or entry.get("owner") != OURS:
        return False
    removed = data["tools"].pop(name, None) is not None
    if _SAFE_NAME.fullmatch(name) and tool_dir(name).exists():
        from agent_reach.utils.paths import ensure_no_symlink_path

        ensure_no_symlink_path(tool_dir(name), "private tool")
        shutil.rmtree(tool_dir(name))
        removed = True
    state.save(data)
    return removed


def closure(names: Iterable[str]) -> Set[str]:
    return {n for n in names if n in TOOLS}


def prune(keep: Set[str]) -> List[str]:
    """Remove our tools not in `keep`, including records left by older versions (node, opencli...)."""
    removed = []
    for name, entry in list(state.load()["tools"].items()):
        if name in keep:
            continue
        if entry.get("owner") != OURS or not _SAFE_NAME.match(name):
            data = state.load()
            data["tools"].pop(name, None)
            state.save(data)
            continue
        if remove_private(name):
            removed.append(name)
    return removed


def recorded() -> List[dict]:
    """Every tool in state.json with its installed version (for doctor)."""
    return [{"tool": n, **e} for n, e in sorted(state.load()["tools"].items())]
