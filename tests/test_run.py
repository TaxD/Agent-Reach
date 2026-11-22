"""run_cmd: bounded output, timeouts and a cleaned environment, with real short-lived Python children."""

import sys
import time

import pytest

from agent_reach.runtime import run
from agent_reach.runtime.result import ReachError


def _py(code: str) -> list:
    return [sys.executable, "-c", code]


def test_output_and_errors_are_returned(monkeypatch):
    assert run.run_cmd(_py("print('hello')"), timeout=20) == "hello\n"
    with pytest.raises(run.CommandFailed) as caught:
        run.run_cmd(_py("import sys; sys.stderr.write('bad thing'); sys.exit(3)"), timeout=20)
    assert "bad thing" in caught.value.stderr


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_a_child_that_floods_a_pipe_is_stopped(monkeypatch, stream):
    monkeypatch.setattr(run, "_MAX_OUTPUT_BYTES", 64 * 1024)
    started = time.monotonic()
    code = f"import sys, time\nwhile True:\n    sys.{stream}.write('x' * 65536); sys.{stream}.flush()\n"
    with pytest.raises(ReachError) as caught:
        run.run_cmd(_py(code), timeout=30)
    assert caught.value.code == "UPSTREAM_BROKEN" and "byte limit" in caught.value.message
    assert time.monotonic() - started < 20, "stopped at the limit, not at the timeout"


def test_output_over_the_limit_is_never_returned_cut_short(monkeypatch):
    """A child that writes a little too much and exits at once must fail, not return a truncated answer."""
    monkeypatch.setattr(run, "_MAX_OUTPUT_BYTES", 1000)
    for _ in range(20):
        with pytest.raises(ReachError) as caught:
            run.run_cmd(_py("import sys; sys.stdout.write('x' * 5000)"), timeout=20)
        assert caught.value.code == "UPSTREAM_BROKEN"


def test_a_child_that_hangs_is_killed_at_the_timeout():
    started = time.monotonic()
    with pytest.raises(ReachError) as caught:
        run.run_cmd(_py("import time; time.sleep(60)"), timeout=0.5)
    assert caught.value.code == "TIMEOUT"
    assert time.monotonic() - started < 10


def test_dropped_environment_prefixes_never_reach_the_child(monkeypatch):
    monkeypatch.setenv("NODE_OPTIONS", "--require /tmp/evil.js")
    monkeypatch.setenv("npm_config_registry", "https://evil.example/")
    monkeypatch.setenv("KEEP_ME", "1")
    code = "import os; print(sorted(k for k in os.environ if k in ('NODE_OPTIONS','npm_config_registry','KEEP_ME','SET')))"
    out = run.run_cmd(_py(code), timeout=20, drop_env_prefix=("NODE_", "npm_"), env={"SET": "1"})
    assert out.strip() == "['KEEP_ME', 'SET']"


def test_a_grandchild_holding_the_output_open_is_not_a_success(tmp_path):
    """The child exits at once but leaves a descendant that inherited stdout and keeps sleeping."""
    marker = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"open({str(marker)!r}, 'w').write(str(p.pid))\n"
        "print('partial')\n"
    )
    started = time.monotonic()
    with pytest.raises(ReachError) as caught:
        run.run_cmd(_py(code), timeout=20)
    assert caught.value.code == "UPSTREAM_BROKEN" and "holding its output open" in caught.value.message
    assert time.monotonic() - started < 15
    import os
    import signal

    pid = int(marker.read_text())

    def alive() -> bool:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    deadline = time.monotonic() + 3  # an orphan is reaped by init shortly after it dies
    while alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    alive = alive()
    if alive:  # killing the session should have taken it; clean up either way
        os.kill(pid, signal.SIGKILL)
    assert not alive, "the descendant that held the pipe is stopped too"


def test_run_cmd_no_longer_takes_input():
    import inspect

    assert "input_data" not in inspect.signature(run.run_cmd).parameters
