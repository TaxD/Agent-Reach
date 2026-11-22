# -*- coding: utf-8 -*-
"""Unified entry: backend fallback, error selection, JSON contract, routing, parsers."""

import json
import time
import urllib.error

import pytest

from agent_reach.channels.base import Action, Channel, Param
from agent_reach.channels.github import _parse_target
from agent_reach.channels.web import _parse_exa_text
from agent_reach.channels.youtube import vtt_to_text
from agent_reach.runtime import cli as unified
from agent_reach.runtime.dispatch import run_action
from agent_reach.runtime.result import ReachError, classify


@pytest.mark.parametrize("args, query, verbose", [
    (["fake", "search", "--", "-v"], "-v", False),
    (["fake", "search", "--", "--verbose"], "--verbose", False),
    (["fake", "search", "--", "explain", "--verbose", "option"], "explain --verbose option", False),
    (["--verbose", "fake", "search", "--", "-v"], "-v", True),
    (["fake", "search", "query", "-v"], "query", True),
])
def test_console_entry_respects_end_of_options(monkeypatch, capsys, args, query, verbose):
    from agent_reach import cli as entry

    channel = FakeChannel()
    logging_modes = []
    monkeypatch.setattr(entry.sys, "argv", ["agent-reach", *args])
    monkeypatch.setattr(entry, "_configure_logging", logging_modes.append)
    monkeypatch.setattr(unified, "unified_channels", lambda: [channel])

    with pytest.raises(SystemExit) as exited:
        entry.main()

    assert exited.value.code == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert channel.calls[0][1]["query"] == query
    assert logging_modes == [verbose]

ENVELOPE_KEYS = {"schema_version", "ok", "channel", "action", "backend", "items", "meta", "error"}


class FakeChannel(Channel):
    name = "fake"
    description = "fake"
    level = "default"
    actions = (
        Action(
            "search",
            "search",
            (Param("query", "q"), Param("limit", "n", required=False, type=int, default=3)),
            (("first", "_first"), ("second", "_second")),
            live_test={"query": "x"},
        ),
        Action("read", "read", (Param("target", "t"),), (("first", "_first_read"),)),
    )

    def __init__(self, first=None, second=None):
        self.first = first or (lambda **kw: [{"title": "first"}])
        self.second = second or (lambda **kw: [{"title": "second"}])
        self.calls = []

    def can_handle(self, url):
        return "fake.test" in url

    def _first(self, **kw):
        self.calls.append(("first", kw))
        return self.first(**kw)

    def _second(self, **kw):
        self.calls.append(("second", kw))
        return self.second(**kw)

    def _first_read(self, **kw):
        return [{"title": kw["target"]}]


def _raise(code):
    def fn(**kw):
        raise ReachError(code, code.lower())
    return fn


# ── dispatcher ──


def test_falls_back_to_next_backend_on_retryable_error():
    ch = FakeChannel(first=_raise("TIMEOUT"))
    out = run_action(ch, "search", {"query": "q"})
    assert out["ok"] is True
    assert out["backend"] == "second"
    assert [a["result"] for a in out["meta"]["attempts"]] == ["TIMEOUT", "ok"]


def test_not_found_stops_without_trying_other_backends():
    ch = FakeChannel(first=_raise("NOT_FOUND"))
    out = run_action(ch, "search", {"query": "q"})
    assert out["ok"] is False
    assert out["error"]["code"] == "NOT_FOUND"
    assert [name for name, _ in ch.calls] == ["first"]


def test_real_failure_is_reported_over_not_installed():
    ch = FakeChannel(first=_raise("NEED_SETUP"), second=_raise("UPSTREAM_BROKEN"))
    out = run_action(ch, "search", {"query": "q"})
    assert out["error"]["code"] == "UPSTREAM_BROKEN"


def test_all_backends_missing_reports_need_setup():
    ch = FakeChannel(first=_raise("NEED_SETUP"), second=_raise("NEED_SETUP"))
    assert run_action(ch, "search", {"query": "q"})["error"]["code"] == "NEED_SETUP"


def test_params_are_validated_and_defaulted_before_any_backend_runs():
    ch = FakeChannel()
    assert run_action(ch, "search", {})["error"]["code"] == "BAD_INPUT"
    assert run_action(ch, "search", {"query": "q", "limit": "many"})["error"]["code"] == "BAD_INPUT"
    assert ch.calls == []
    run_action(ch, "search", {"query": "q"})
    assert ch.calls[0][1]["limit"] == 3


def test_unknown_action_lists_valid_actions():
    out = run_action(FakeChannel(), "nope", {})
    assert out["error"]["code"] == "BAD_INPUT"
    assert "search" in out["error"]["message"] and "read" in out["error"]["message"]


def test_hung_backend_is_abandoned_at_the_deadline():
    ch = FakeChannel(first=lambda **kw: time.sleep(5) or [], second=lambda **kw: [{"title": "x"}])
    started = time.monotonic()
    out = run_action(ch, "search", {"query": "q"}, timeout=0.3)
    assert time.monotonic() - started < 3
    assert out["ok"] is False
    assert out["error"]["code"] == "TIMEOUT"


def test_unexpected_exception_becomes_upstream_broken():
    ch = FakeChannel(first=lambda **kw: 1 / 0, second=_raise("NEED_SETUP"))
    out = run_action(ch, "search", {"query": "q"})
    assert out["error"]["code"] == "UPSTREAM_BROKEN"
    assert "ZeroDivisionError" in out["error"]["message"]


@pytest.mark.parametrize(
    "exc, code",
    [
        (urllib.error.HTTPError("u", 404, "nf", {}, None), "NOT_FOUND"),
        (urllib.error.HTTPError("u", 429, "rl", {}, None), "RATE_LIMITED"),
        (urllib.error.HTTPError("u", 500, "x", {}, None), "UPSTREAM_BROKEN"),
        (urllib.error.URLError(TimeoutError()), "TIMEOUT"),
        (FileNotFoundError("gh"), "NEED_SETUP"),
    ],
)
def test_classify_maps_upstream_failures(exc, code):
    assert classify(exc).code == code


def test_error_messages_do_not_leak_url_credentials():
    err = classify(RuntimeError("failed https://user:secret@example.com/x?token=abc123"))
    assert "secret" not in err.message and "abc123" not in err.message


# ── CLI contract ──


@pytest.fixture
def fake_cli(monkeypatch):
    ch = FakeChannel()
    monkeypatch.setattr(unified, "get_all_channels", lambda: [ch])
    return ch


def _run(capsys, argv):
    code = unified.main(argv)
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1, "unified commands print exactly one JSON line"
    return code, json.loads(lines[0])


def test_action_prints_one_envelope_and_exits_zero(capsys, fake_cli):
    code, out = _run(capsys, ["fake", "search", "two", "words", "--limit", "2"])
    assert code == 0
    assert set(out) == ENVELOPE_KEYS
    assert out["schema_version"] == 1
    assert fake_cli.calls[0][1]["query"] == "two words"
    assert fake_cli.calls[0][1]["limit"] == 2


def test_failures_still_print_an_envelope_and_exit_one(capsys, fake_cli):
    code, out = _run(capsys, ["fake", "search"])
    assert code == 1
    assert set(out) == ENVELOPE_KEYS
    assert out["error"]["code"] == "BAD_INPUT"
    assert "usage: agent-reach fake search <query>" in out["error"]["message"]


def test_read_routes_by_url(capsys, fake_cli):
    code, out = _run(capsys, ["read", "https://fake.test/a"])
    assert code == 0
    assert out["channel"] == "fake"
    assert out["items"][0]["title"] == "https://fake.test/a"


def test_unknown_command_is_bad_input(capsys, fake_cli):
    for argv in (["install"], ["check-update"], ["nosuchchannel", "search", "x"]):
        code, out = _run(capsys, argv)
        assert code == 1
        assert out["ok"] is False
        assert out["error"]["code"] == "BAD_INPUT"
        assert "agent-reach help" in out["error"]["message"]


def test_channels_lists_usage_for_agents(capsys, fake_cli):
    code, out = _run(capsys, ["channels"])
    assert code == 0
    action = out["channels"][0]["actions"][0]
    assert action["usage"] == "agent-reach fake search <query> [--limit LIMIT]"
    assert action["backends"] == ["first", "second"]


def test_check_passes_when_any_backend_works(capsys, fake_cli):
    fake_cli.first = _raise("TIMEOUT")
    code, out = _run(capsys, ["check"])
    assert code == 0
    assert [(r["backend"], r["passed"]) for r in out["results"]] == [("first", False), ("second", True)]


def test_check_fails_when_backend_returns_nothing(capsys, fake_cli):
    fake_cli.first = lambda **kw: []
    fake_cli.second = lambda **kw: []
    code, out = _run(capsys, ["check"])
    assert code == 1
    assert out["results"][0]["error"]["code"] == "EMPTY"


def test_skill_command_links_a_skills_dir_and_reports_it(capsys, fake_cli):
    from agent_reach.utils.paths import home_dir

    target = home_dir() / "my-ai" / "skills"
    code, out = _run(capsys, ["skill", "link", str(target)])
    assert code == 0 and out["action"] == "skill link"
    assert out["result"]["links"] == [str(target / "agent-reach")]
    code, out = _run(capsys, ["skill"])
    assert code == 0 and out["result"]["installed"] is True
    code, out = _run(capsys, ["skill", "unlink"])
    assert code == 1 and out["error"]["code"] == "BAD_INPUT"


def test_doctor_reports_regressed_per_channel(capsys, fake_cli):
    code, out = _run(capsys, ["doctor"])
    assert code == 0
    assert out["result"]["channels"][0]["regressed"] is False


# ── parsers ──


def test_parse_exa_text_splits_results():
    text = (
        "Title: A\nURL: https://a.test\nPublished: N/A\nAuthor: Ann\nHighlights:\nbody a\n\n"
        "Title: B\nURL: https://b.test\nPublished: 2026-01-02\nAuthor: N/A\nbody b"
    )
    items = _parse_exa_text(text)
    assert [(i["title"], i["url"], i["author"], i["published_at"]) for i in items] == [
        ("A", "https://a.test", "Ann", None),
        ("B", "https://b.test", None, "2026-01-02"),
    ]
    assert items[0]["text"] == "body a"


def test_vtt_to_text_drops_timings_tags_and_rolling_repeats():
    vtt = (
        "WEBVTT\nKind: captions\nLanguage: en\n\n"
        "00:00:01.000 --> 00:00:02.000\n<c>hello</c> world\n\n"
        "00:00:02.000 --> 00:00:03.000\nhello world\n\n"
        "2\n00:00:03.000 --> 00:00:04.000\nnext &amp; last\n"
    )
    assert vtt_to_text(vtt) == "hello world\nnext & last"


@pytest.mark.parametrize("vtt,expected", [
    ("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n2026\n", "2026"),
    ("WEBVTT\n\ncaption-one\n00:00:00.000 --> 00:00:01.000\nhello\n", "hello"),
    ("WEBVTT\n\nNOTE internal note\nnot speech\n\n00:00:00.000 --> 00:00:01.000\nhello\n", "hello"),
    ("WEBVTT\n\nSTYLE\n::cue { color: red; }\n\n00:00:00.000 --> 00:00:01.000\nhello\n", "hello"),
    ("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nNOTE this is spoken\nSTYLE matters\n", "NOTE this is spoken\nSTYLE matters"),
])
def test_vtt_to_text_distinguishes_cue_payload_from_metadata(vtt, expected):
    assert vtt_to_text(vtt) == expected


@pytest.mark.parametrize("timing", [
    "00:00:00.000\t-->\t00:00:01.000",
    "00:00:00.000  -->  00:00:01.000",
    "100:00:00.000 --> 100:00:01.000",
])
def test_vtt_to_text_accepts_standard_timing_separators_and_long_hours(timing):
    assert vtt_to_text(f"WEBVTT\n\n{timing}\nspoken words\n") == "spoken words"


@pytest.mark.parametrize(
    "target, expected",
    [
        ("yt-dlp/yt-dlp", ("yt-dlp", "yt-dlp", None)),
        ("https://github.com/cli/cli.git", ("cli", "cli", None)),
        ("https://github.com/cli/cli/issues/42", ("cli", "cli", 42)),
        ("https://github.com/cli/cli/pull/7?x=1", ("cli", "cli", 7)),
    ],
)
def test_github_target_forms(target, expected):
    assert _parse_target(target) == expected


@pytest.mark.parametrize("bad", ["not a target", "https://gitlab.com/a/b/c/d", "a/b/c"])
def test_github_rejects_other_targets(bad):
    with pytest.raises(ReachError) as info:
        _parse_target(bad)
    assert info.value.code == "BAD_INPUT"


def test_login_rejects_channels_without_managed_login(capsys, fake_cli):
    code, out = _run(capsys, ["login", "fake"])
    assert code == 1 and out["error"]["code"] == "BAD_INPUT"


def test_channels_reports_login_only_for_browser_login_channels(capsys, monkeypatch):
    class LoginChannel(FakeChannel):
        name = "site"
        login_url = "https://site.test/login"

    monkeypatch.setattr(unified, "get_all_channels", lambda: [LoginChannel(), FakeChannel()])
    _, out = _run(capsys, ["channels"])
    logins = {c["name"]: c["login"] for c in out["channels"]}
    assert logins == {"site": {"via": "browser", "command": "agent-reach login site"}, "fake": None}


# ── Agent Reach dedicated browser ──


@pytest.fixture
def fake_browser(monkeypatch):
    """The watcher and the daemon replaced by a small model: the start logic in browser.py runs for real."""
    from agent_reach.runtime import browser

    state = {"contexts": ["daily"], "running": False, "launches": 0, "launch_delay": 0.0, "closing": False,
             "opened": []}

    def fake_launch():
        state["launches"] += 1
        time.sleep(state["launch_delay"])
        state["running"] = True
        state["contexts"].append("dedicated")

    monkeypatch.setattr(browser, "_watcher",
                        lambda: {"ok": True, "context": "dedicated"} if state["running"] else None)
    monkeypatch.setattr(browser, "watcher_running", lambda: state["running"] or state["closing"])
    monkeypatch.setattr(browser, "connected_contexts", lambda: list(state["contexts"]))
    monkeypatch.setattr(browser, "_launch", fake_launch)
    monkeypatch.setattr(browser, "start", lambda url: state["opened"].append(url))
    monkeypatch.setattr(browser, "START_WAIT", 0.5)
    return state


def test_browser_action_starts_the_dedicated_browser_by_itself(fake_browser):
    from agent_reach.runtime import browser
    from agent_reach.runtime.browser_binding import environment

    env = environment("reddit")
    assert env["OPENCLI_PROFILE"] == "dedicated", "the user's own 'daily' profile is never picked"
    assert browser.bound_context() == "dedicated"
    assert fake_browser["launches"] == 1
    assert environment("reddit")["OPENCLI_PROFILE"] == "dedicated"
    assert fake_browser["launches"] == 1, "already open: no second browser"
    fake_browser["running"] = False
    fake_browser["contexts"].remove("dedicated")
    assert environment("reddit")["OPENCLI_PROFILE"] == "dedicated"
    assert fake_browser["launches"] == 2, "closed: reopened"
    assert browser.last_used() is not None


def test_environment_passes_the_token_file_never_the_token(fake_browser):
    from agent_reach.runtime import browser
    from agent_reach.runtime.browser_binding import environment

    env = environment("twitter")
    token = browser.daemon_token_path().read_text().strip()
    assert env["OPENCLI_DAEMON_TOKEN_FILE"] == str(browser.daemon_token_path())
    assert not any(token in str(v) for v in env.values())
    assert env["OPENCLI_CDP_ENDPOINT"] == "" and env["OPENCLI_CDP_TARGET"] == ""


def test_environment_refuses_a_daemon_of_another_build(fake_browser, monkeypatch):
    from agent_reach.runtime import bridge
    from agent_reach.runtime.browser_binding import environment

    monkeypatch.setattr(bridge, "status", lambda: {"ok": True, "daemonVersion": "9.9.9", "profiles": []})
    with pytest.raises(ReachError) as info:
        environment("reddit")
    assert info.value.code == "NEED_SETUP"


def test_a_connected_foreign_profile_is_never_adopted(fake_browser, monkeypatch):
    from agent_reach.runtime import browser

    def launch_without_extension():
        fake_browser["launches"] += 1
        fake_browser["running"] = True  # our window is up, but only the user's own Chrome is connected

    monkeypatch.setattr(browser, "_launch", launch_without_extension)
    with pytest.raises(ReachError) as info:
        browser.ensure_running()
    assert info.value.code == "UPSTREAM_BROKEN"
    assert browser.bound_context() is None


def test_a_closing_previous_browser_is_not_started_twice(fake_browser):
    from agent_reach.runtime import browser

    fake_browser["closing"] = True
    with pytest.raises(ReachError, match="still closing"):
        browser.ensure_running()
    assert fake_browser["launches"] == 0


def test_browser_cold_start_does_not_count_against_timeout(fake_browser):
    from agent_reach.runtime import browser

    fake_browser["launch_delay"] = 1.5
    seen = {}

    def opencli_backend(**kw):
        browser.ensure_running()
        seen["timeout"] = kw["timeout"]
        time.sleep(0.5)
        return [{"title": "ok"}]

    ch = FakeChannel(first=opencli_backend, second=_raise("UPSTREAM_BROKEN"))
    out = run_action(ch, "search", {"query": "q"}, timeout=1.0)
    assert out["ok"] is True, out
    assert out["backend"] == "first"
    assert fake_browser["launches"] == 1


def test_concurrent_actions_start_one_browser(fake_browser):
    import threading

    from agent_reach.runtime import browser

    fake_browser["launch_delay"] = 0.5
    errors = []

    def call():
        try:
            browser.ensure_running()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert errors == []
    assert fake_browser["launches"] == 1


def test_login_opens_site_in_dedicated_browser(capsys, monkeypatch, fake_browser):
    class BrowserChannel(FakeChannel):
        name = "web2"
        login_url = "https://example.test/login"

    monkeypatch.setattr(unified, "get_all_channels", lambda: [BrowserChannel()])
    from agent_reach.runtime import manage
    added = []
    # Hermetic: no check of the real OpenCLI install on this machine.
    monkeypatch.setattr(manage, "add", lambda name, **k: added.append(name))
    code, out = _run(capsys, ["login", "web2"])
    assert code == 0
    assert added == ["web2"]
    assert fake_browser["opened"] == ["https://example.test/login"]


def test_logout_clears_only_that_sites_domains(capsys, monkeypatch):
    from agent_reach.runtime import browser

    class BrowserChannel(FakeChannel):
        name = "web2"
        login_url = "https://www.example.test/login"

    monkeypatch.setattr(unified, "get_all_channels", lambda: [BrowserChannel()])
    cleared = []
    monkeypatch.setattr(browser, "clear_site", lambda domains: cleared.append(list(domains)) or 2)
    code, out = _run(capsys, ["logout", "web2"])
    assert code == 0 and out["items"] == [{"removed": True, "cleared_cookies": 2}]
    assert cleared == [["example.test"]]


@pytest.mark.parametrize("argv", [["ask", "find VLA"], ["mcp"], ["cloak", "setup"], ["browser", "repair-opencli"],
                                  ["ensure", "reddit"],
                                  ["execute", "reddit", "search", "--params", '{"query":"VLA"}'],
                                  ["resume", "op_x"]])
def test_retired_commands_are_rejected_without_starting_any_service(argv, monkeypatch, capsys):
    from agent_reach.runtime import browser, manage
    monkeypatch.setattr(browser, "ensure_running", lambda *a, **k: pytest.fail("retired command started browser"))
    monkeypatch.setattr(manage, "add", lambda *a, **k: pytest.fail("retired command installed tools"))
    code, out = _run(capsys, argv)
    assert code == 1 and out["error"]["code"] == "BAD_INPUT"


@pytest.mark.parametrize(
    "yaml_text, code",
    [
        ("ok: false\nerror:\n  code: COMMAND_EXEC\n  message: 'Error: HTTP 401 - make sure you are logged in to Reddit'\n", "NEED_LOGIN"),
        ("ok: false\nerror:\n  code: AUTH_REQUIRED\n  message: 'Not logged into x.com'\n", "NEED_LOGIN"),
        ("ok: false\nerror:\n  code: COMMAND_EXEC\n  message: 'Pre-navigation to https://x.com failed: Navigation rejected.'\n", "UPSTREAM_BROKEN"),
        ("ok: false\nerror:\n  code: COMMAND_EXEC\n  message: 'TypeError: Failed to fetch'\n", "UPSTREAM_BROKEN"),
    ],
)
def test_opencli_errors_map_to_unified_codes(yaml_text, code):
    from agent_reach.backends.opencli import _opencli_error

    assert _opencli_error(yaml_text, "").code == code


def test_opencli_message_reaches_the_agent_but_upstream_help_is_never_a_hint():
    from agent_reach.backends.opencli import _opencli_error
    from agent_reach.runtime.result import envelope

    text = ("node warning\nok: false\nerror:\n  code: BROWSER_CONNECT\n"
            "  message: Browser profile \"x\" is not connected\n  help: Open the matching Chrome profile.\n")
    err = _opencli_error(text, "reach login steps")
    out = envelope(channel="reddit", action="search", error=err)["error"]
    assert out["code"] == "UPSTREAM_BROKEN" and out["message"] == 'Browser profile "x" is not connected'
    assert "matching Chrome profile" not in out["hint"]  # third-party text is not an instruction
    login = _opencli_error("ok: false\nerror:\n  code: AUTH_REQUIRED\n  message: Not logged into x.com\n"
                           "  help: Log in in your Chrome.\n", "reach login steps")
    assert login.message == "Not logged into x.com" and login.hint == "reach login steps"  # Reach's own browser
