"""reddit/twitter site actions: read-only OpenCLI calls, the add gate, and the removed login options."""

import json
from unittest.mock import Mock

import pytest

from agent_reach.backends import opencli
from agent_reach.channels.reddit import RedditChannel
from agent_reach.channels.twitter import TwitterChannel
from agent_reach.channels.web import WebChannel
from agent_reach.runtime import browser, browser_binding, cli, manage, run, state
from agent_reach.runtime.dispatch import run_action
from agent_reach.runtime.result import ReachError


@pytest.fixture
def no_browser(monkeypatch):
    """Any attempt to prepare the dedicated browser fails the test."""
    for module, name in ((browser_binding, "environment"), (browser, "ensure_running"), (browser, "login_window"),
                         (browser, "clear_site"), (manage, "add")):
        monkeypatch.setattr(module, name, Mock(side_effect=AssertionError(f"{name} must not run")))


# ── opencli_call ──


@pytest.mark.parametrize("site, command", [
    ("reddit", "comment"), ("reddit", "upvote"), ("reddit", "save"), ("reddit", "subscribe"),
    ("twitter", "post"), ("twitter", "reply"), ("twitter", "like"), ("twitter", "follow"),
    ("twitter", "subreddit"), ("reddit", "--help"), ("hackernews", "search"), ("reddit", ""),
])
def test_only_read_only_commands_are_reachable(no_browser, site, command):
    with pytest.raises(ReachError) as err:
        opencli.opencli_call(site, command, ["x"], timeout=1)
    assert err.value.code == "BAD_INPUT"


@pytest.mark.parametrize("values", [[""], ["-"], ["--limit=999"], ["-f", "yaml"], ["ok", "--profile", "x"], ["--"]])
def test_option_like_or_empty_values_are_refused_before_the_browser(no_browser, values):
    with pytest.raises(ReachError) as err:
        opencli.opencli_call("reddit", "search", values, timeout=1)
    assert err.value.code == "BAD_INPUT"


def test_values_follow_the_end_of_options_marker(monkeypatch):
    monkeypatch.setattr(browser_binding, "environment", lambda site: {"OPENCLI_PROFILE": "dedicated"})
    command = Mock(return_value='[{"title": "t"}]')
    monkeypatch.setattr(run, "run_cmd", command)
    assert opencli.opencli_call("reddit", "search", ["rust -lang", "x"], limit=5, timeout=3) == [{"title": "t"}]
    argv = command.call_args.args[0]
    assert argv == ["opencli", "reddit", "search", "--limit", "5", "-f", "json", "--", "rust -lang", "x"]
    assert command.call_args.kwargs["env"] == {"OPENCLI_PROFILE": "dedicated"}
    opencli.opencli_call("twitter", "profile", ["jack"], timeout=3)
    assert command.call_args.args[0] == ["opencli", "twitter", "profile", "-f", "json", "--", "jack"]


def test_channel_backends_use_only_allowed_commands(monkeypatch):
    seen = []

    def fake_call(site, command, values=(), *, limit=None, timeout, login_hint=""):
        assert command in opencli.READ_ONLY[site]
        seen.append((site, command, list(values), limit))
        return [{"title": "t", "url": "https://x.com/a/status/1", "text": "body"}]

    from agent_reach.channels import reddit, twitter

    monkeypatch.setattr(reddit, "opencli_call", fake_call)
    monkeypatch.setattr(twitter, "opencli_call", fake_call)
    RedditChannel()._opencli_search(query="q", limit=3, timeout=1)
    RedditChannel()._opencli_subreddit(name="r/python", limit=3, timeout=1)
    RedditChannel()._opencli_read(target="abc123", timeout=1)
    TwitterChannel()._opencli_profile(username="@jack", timeout=1)
    TwitterChannel()._opencli_tweets(username="jack", limit=2, timeout=1)
    TwitterChannel()._opencli_thread(target="https://x.com/a/status/123?s=1", timeout=1)
    assert seen == [("reddit", "search", ["q"], 3), ("reddit", "subreddit", ["python"], 3),
                    ("reddit", "read", ["abc123"], 20), ("twitter", "profile", ["jack"], None),
                    ("twitter", "tweets", ["jack"], 2), ("twitter", "thread", ["123"], None)]


def test_upstream_help_is_never_forwarded_as_a_hint():
    err = opencli._opencli_error("ok: false\nerror:\n  code: AUTH_REQUIRED\n  message: Login required\n"
                                 "  help: run `opencli login --import-cookies ~/x`\n", "")
    assert err.code == "NEED_LOGIN" and not err.hint and "import-cookies" not in err.message


def test_node_warning_does_not_hide_upstream_error_and_404_is_not_login():
    warning = "(node:123) Warning: experimental proxy\n"
    error = opencli._opencli_error(warning + "ok: false\nerror:\n  code: UNKNOWN\n"
                                   "  message: HTTP 404 - make sure you are logged in to Reddit\n", "login")
    assert error.code == "UPSTREAM_BROKEN"
    assert "(node:" not in error.message
    assert opencli._opencli_error(warning + "ok: false\nerror:\n  code: AUTH_REQUIRED\n  message: Login required\n",
                                  "login").code == "NEED_LOGIN"


# ── dispatch: channels that are not added never touch the browser ──


@pytest.mark.parametrize("channel, action, params", [
    (RedditChannel(), "search", {"query": "rust"}),
    (RedditChannel(), "subreddit", {"name": "python"}),
    (TwitterChannel(), "search", {"query": "rust"}),
    (TwitterChannel(), "profile", {"username": "jack"}),
])
def test_site_actions_of_a_channel_not_added_need_setup(no_browser, monkeypatch, channel, action, params):
    monkeypatch.setattr(WebChannel, "_exa_search", lambda self, **kw: [])
    monkeypatch.setattr(WebChannel, "_jina_read", lambda self, **kw: [])
    out = run_action(channel, action, params)
    assert out["ok"] is False and out["error"]["code"] == "NEED_SETUP"
    assert f"agent-reach add {channel.name}" in out["error"]["hint"]
    assert not any(a["backend"] == "opencli" for a in out["meta"]["attempts"])
    assert not state.load().get("backend_health", {}).get(channel.name), "nothing paused: the site was not tried"


def test_an_added_channel_reaches_the_browser(monkeypatch):
    state.mutate(lambda data: data["channels"].__setitem__("reddit", {"added_at": state.now()}))
    environment = Mock(return_value={})
    monkeypatch.setattr(browser_binding, "environment", environment)
    monkeypatch.setattr(run, "run_cmd", Mock(return_value='[{"title": "t", "url": "https://reddit.com/r/a"}]'))
    out = run_action(RedditChannel(), "subreddit", {"name": "python"})
    assert out["ok"] and out["backend"] == "opencli"
    environment.assert_called_once_with("reddit")


# ── CLI: removed commands and options ──


def _run(capsys, argv):
    code = cli.main(argv)
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


@pytest.mark.parametrize("argv", [
    ["browser", "profiles"], ["browser", "use", "Default"], ["browser", "import", "reddit"],
    ["login", "twitter", "--import-existing"], ["login", "reddit", "--sync"],
    ["configure", "browser-port", "9333"], ["add", "reddit", "--allow-upgrade"],
    ["reddit", "search", "q", "--retry-cloak"], ["update", "--allow-upgrade"],
])
def test_removed_commands_and_options_are_bad_input(no_browser, capsys, argv):
    code, out = _run(capsys, argv)
    assert code == 1 and out["error"]["code"] == "BAD_INPUT"


@pytest.mark.parametrize("verb", ["login", "logout"])
@pytest.mark.parametrize("channel", ["hackernews", "github", "web", "no-such-channel"])
def test_login_and_logout_need_a_login_channel(no_browser, capsys, verb, channel):
    code, out = _run(capsys, [verb, channel])
    assert code == 1 and out["error"]["code"] == "BAD_INPUT"


def test_login_channels_are_exactly_reddit_and_twitter():
    from agent_reach.channels import get_all_channels

    assert sorted(ch.name for ch in get_all_channels() if ch.login_url) == ["reddit", "twitter"]
    for ch in get_all_channels():
        if ch.login_url:
            assert set(manage.site_domains(ch)) <= {"reddit.com", "x.com", "twitter.com"}
            assert all(command in opencli.READ_ONLY[ch.name]
                       for action in ch.actions for label, method in action.backends if label == "opencli"
                       for command in [method.removeprefix("_opencli_")])


def test_a_default_channel_the_user_removed_runs_nothing(monkeypatch):
    from agent_reach.channels.hackernews import HackerNewsChannel

    ch = HackerNewsChannel()
    assert ch.level == "default"
    state.mutate(lambda data: data["removed_defaults"].append(ch.name))
    for _, method in (b for a in ch.actions for b in a.backends):
        monkeypatch.setattr(HackerNewsChannel, method, Mock(side_effect=AssertionError("ran a removed channel")))
    monkeypatch.setattr(WebChannel, "_exa_search", Mock(side_effect=AssertionError("public web for a removed channel")))
    for action in ch.actions:
        params = dict(action.live_test or {p.name: "x" for p in action.params if p.required})
        out = run_action(ch, action.name, params)
        assert out["error"]["code"] == "NEED_SETUP", action.name
        assert "agent-reach add hackernews" in out["error"]["hint"]
