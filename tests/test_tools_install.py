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
    assert "sample:pip_packages" in err.value.message and "sample:pip_packages" in err.value.hint
    assert "secret" not in str(err.value) + err.value.hint


def test_install_failure_scrubs_upstream_url_credentials(monkeypatch):
    monkeypatch.setattr(tools, "install_env", lambda: {})
    monkeypatch.setattr(tools.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout="", stderr="Could not connect to https://user:secret@example.com/download?token=abc"))
    with pytest.raises(ReachError) as err:
        tools._run(["pip", "install", "sample"], stage="sample:pip_packages")
    assert err.value.code == "UPSTREAM_BROKEN"
    assert "secret" not in str(err.value)
    assert "token=abc" not in str(err.value)
    assert "sample:pip_packages" in err.value.hint


def test_rerunning_pip_setup_preserves_partial_environment(monkeypatch, tmp_path):
    tool = tools.Tool("sample", "pip", "sample-package==1.2.3", ("ffmpeg",))
    venv = tools.tool_dir(tool.name) / "venv"
    py = venv / ("Scripts/python.exe" if tools._WIN else "bin/python")
    py.parent.mkdir(parents=True)
    py.write_text("already installed interpreter")
    tools.state.remember_path(tools.tool_dir(tool.name), "installed dependency")  # an earlier, interrupted run
    marker = venv / "partially-installed-package"
    marker.write_text("keep")
    exe = tmp_path / "ffmpeg"
    exe.write_text("binary")
    calls = []
    monkeypatch.setattr(tools, "_run", lambda argv, **kwargs: calls.append((argv, kwargs)) or str(exe))
    tools._install(tool)
    assert marker.read_text() == "keep"
    assert len(calls) == 2, "the existing venv is reused, not recreated"
    assert calls[0][0][:5] == [str(py), "-I", "-m", "pip", "install"]
    assert calls[0][1]["stage"] == "sample:pip_packages"
    assert "--only-binary" in calls[0][0] and "sample-package==1.2.3" in calls[0][0]


def test_no_remote_installer_script_or_anti_detect_browser_is_declared():
    assert not {"uv", "cloakbrowser", "bili-cli", "boss-agent-cli"} & set(tools.TOOLS)
    assert not hasattr(tools, "_install_uv")


def _fake_lock(monkeypatch, tmp_path, version="2.0.3"):
    import json

    lock = tmp_path / "opencli_lock"
    lock.mkdir()
    (lock / "package.json").write_text(json.dumps({"dependencies": {"@jackwener/opencli": version}}))
    (lock / "package-lock.json").write_text(json.dumps(
        {"packages": {"node_modules/@jackwener/opencli": {"version": version}}}))
    monkeypatch.setattr(tools, "_lock_files", lambda: lock)
    return lock


def test_opencli_installs_from_the_shipped_lock_without_install_hooks(monkeypatch, tmp_path):
    lock = _fake_lock(monkeypatch, tmp_path)
    assert tools.locked_opencli_version() == "2.0.3"
    monkeypatch.setattr(tools.shutil, "which", lambda c, *a, **k: "/usr/bin/" + c)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        bin_ = tools._private_bin(tools.TOOLS["opencli"], "opencli")
        bin_.parent.mkdir(parents=True, exist_ok=True)
        bin_.write_text("#!/bin/sh\n")
        return ""

    monkeypatch.setattr(tools, "_run", fake_run)
    tools._install(tools.TOOLS["opencli"])
    argv, kwargs = calls[0]
    assert argv[:2] == ["/usr/bin/npm", "ci"] and "--ignore-scripts" in argv
    assert "install" not in argv, "never resolves versions; only the lock is installed"
    assert kwargs["cwd"] == str(tools.tool_dir("opencli"))
    for name in ("package.json", "package-lock.json"):
        assert (tools.tool_dir("opencli") / name).read_text() == (lock / name).read_text()


def test_opencli_install_without_npm_downloads_nothing(monkeypatch, tmp_path):
    _fake_lock(monkeypatch, tmp_path)
    monkeypatch.setattr(tools.shutil, "which", lambda c, *a, **k: None)
    monkeypatch.setattr(tools, "_run", lambda *a, **k: pytest.fail("ran an installer"))
    with pytest.raises(ReachError) as err:
        tools._install(tools.TOOLS["opencli"])
    assert err.value.code == "NEED_SETUP"


def test_opencli_on_path_is_never_used(monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda c, *a, **k: "/usr/local/bin/" + c)
    assert tools.resolve("opencli") is None
    assert not tools.present("opencli")
    bin_ = tools._private_bin(tools.TOOLS["opencli"], "opencli")
    bin_.parent.mkdir(parents=True)
    bin_.write_text("#!/bin/sh\n")
    assert tools.resolve("opencli") == str(bin_)


def test_a_user_opencli_is_not_adopted_by_ensure(monkeypatch, tmp_path):
    _fake_lock(monkeypatch, tmp_path)
    monkeypatch.setattr(tools, "_node_version", lambda: (22, 0, 0))
    monkeypatch.setattr(tools.shutil, "which", lambda c, *a, **k: "/usr/local/bin/" + c)
    installed = []
    monkeypatch.setattr(tools, "_install", lambda tool: installed.append(tool.name))
    assert tools.ensure("opencli") == {"tool": "opencli", "owner": "agent-reach", "scope": "private",
                                       "version": "2.0.3"}
    assert installed == ["opencli"]


@pytest.mark.parametrize("found", [(), (18, 20, 4), (20, 18, 0)])
def test_missing_or_old_node_needs_setup_and_is_never_downloaded(monkeypatch, found):
    monkeypatch.setattr(tools, "_node_version", lambda: found)
    monkeypatch.setattr(tools, "_install", lambda tool: pytest.fail("installed something"))
    monkeypatch.setattr(tools, "_run", lambda *a, **k: pytest.fail("ran an installer"))
    for name in ("node", "opencli"):
        with pytest.raises(ReachError) as err:
            tools.ensure(name)
        assert err.value.code == "NEED_SETUP"
    assert "node" not in tools.state.load()["tools"]


def test_node_at_the_minimum_is_the_users_own(monkeypatch):
    monkeypatch.setattr(tools, "_node_version", lambda: tools.NODE_MINIMUM)
    assert tools.ensure("node") == {"tool": "node", "owner": "system", "scope": "system", "version": "20.18.1"}
    assert tools.TOOLS["node"].kind == "system"


def test_node_version_probe_runs_with_a_cleaned_environment(monkeypatch, tmp_path):
    from types import SimpleNamespace

    monkeypatch.setenv("NODE_OPTIONS", "--require /tmp/evil.js")
    monkeypatch.setenv("npm_config_prefix", "/tmp/evil")
    monkeypatch.setenv("OPENCLI_DAEMON_PORT", "1")
    monkeypatch.setenv("KEEP_ME", "1")
    monkeypatch.setattr(tools.shutil, "which", lambda c, *a, **k: "/usr/bin/node" if c == "node" else None)
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return SimpleNamespace(stdout="v22.11.0\n")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    assert tools._node_version() == (22, 11, 0)
    assert seen["argv"] == ["/usr/bin/node", "--version"]
    assert not any(k.startswith(("NODE_", "npm_", "NPM_", "OPENCLI_")) for k in seen["env"])
    assert seen["env"]["KEEP_ME"] == "1"
    assert seen["cwd"] != str(tmp_path), "never runs in the agent's project folder"
