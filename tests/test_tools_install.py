"""Download diagnostics and repeatable setup, without network access."""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_reach import __version__
from agent_reach.runtime import tools
from agent_reach.runtime.result import TIMEOUT, ReachError


def _no_proxy(monkeypatch):
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(tools, "extend_path", lambda: None)


def test_package_version_matches_runtime_version():
    # The package metadata and --version must describe the same release.
    import re

    project = (Path(__file__).parents[1] / "pyproject.toml").read_text()
    assert re.search(r'^version = "([^"]+)"', project, re.MULTILINE).group(1) == __version__


def test_download_env_copies_system_proxy_for_both_install_stages(monkeypatch):
    _no_proxy(monkeypatch)
    monkeypatch.setattr(tools.urllib.request, "getproxies", lambda: {"https": "http://127.0.0.1:8899", "no": "localhost"})
    env = tools.install_env()
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == "http://127.0.0.1:8899"
    assert env["NO_PROXY"] == "localhost"
    assert "HTTPS_PROXY" not in os.environ


@pytest.mark.parametrize("key", ["https_proxy", "HTTP_PROXY", "ALL_PROXY"])
def test_explicit_proxy_is_kept_without_consulting_system(monkeypatch, key):
    _no_proxy(monkeypatch)
    monkeypatch.setenv(key, "http://127.0.0.1:7788")
    monkeypatch.setattr(tools.urllib.request, "getproxies", lambda: pytest.fail("explicit proxy must win"))
    env = tools.install_env()
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == "http://127.0.0.1:7788"


def test_pip_timeout_names_stage_and_redacts_proxy(monkeypatch):
    monkeypatch.setattr(tools, "install_env", lambda: {"HTTPS_PROXY": "http://user:secret@127.0.0.1:7788"})

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(tools.subprocess, "run", timeout)
    with pytest.raises(ReachError) as err:
        tools._run(["python", "-m", "pip"], timeout=1, stage="sample:pip_packages")
    assert err.value.code == TIMEOUT
    assert "sample:pip_packages" in err.value.message
    assert "sample:pip_packages" in err.value.hint
    assert "secret" not in str(err.value) + err.value.hint


def test_install_failure_scrubs_upstream_url_credentials(monkeypatch):
    monkeypatch.setattr(tools, "install_env", lambda: {})
    monkeypatch.setattr(tools.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout="", stderr="Could not connect to https://user:secret@example.com/download?token=abc"))
    with pytest.raises(ReachError) as err:
        tools._run(["pip", "install", "sample"], stage="sample:pip_packages")
    assert "secret" not in str(err.value)
    assert "token=abc" not in str(err.value)
    assert "rerun setup" in err.value.hint


def test_rerunning_pip_setup_preserves_partial_environment(monkeypatch):
    from agent_reach.runtime import state

    tool = tools.TOOLS["ffmpeg"]
    folder = tools.tool_dir(tool.name)
    state.remember_path(folder, "installed dependency")  # recorded by the interrupted first run
    venv = folder / "venv"
    py = venv / ("Scripts/python.exe" if tools._WIN else "bin/python")
    py.parent.mkdir(parents=True)
    py.write_text("already installed interpreter")
    marker = venv / "partially-installed-package"
    marker.write_text("keep")
    exe = folder / "real-ffmpeg"
    exe.write_text("binary")
    calls = []
    monkeypatch.setattr(tools, "_run", lambda argv, **kwargs: calls.append((argv, kwargs)) or str(exe))
    tools._install(tool)
    assert marker.read_text() == "keep"
    assert len(calls) == 2  # no new venv: pip install, then locate the binary
    assert calls[0][0][:5] == [str(py), "-I", "-m", "pip", "install"]
    assert calls[0][1]["stage"] == "ffmpeg:pip_packages"
    assert "imageio-ffmpeg==0.8.1" in calls[0][0]
    assert tools._private_bin(tool, "ffmpeg").exists()


def test_unrecorded_install_directory_is_left_untouched(monkeypatch):
    folder = tools.tool_dir("ffmpeg")
    folder.mkdir(parents=True)
    (folder / "user-file").write_text("mine")
    monkeypatch.setattr(tools, "_run", lambda *a, **k: pytest.fail("must not install over unrecorded files"))
    with pytest.raises(ReachError):
        tools._install(tools.TOOLS["ffmpeg"])
    assert (folder / "user-file").read_text() == "mine"


def test_only_ffmpeg_is_installable():
    assert set(tools.TOOLS) == {"ffmpeg"}
    assert not hasattr(tools, "_install_uv")
