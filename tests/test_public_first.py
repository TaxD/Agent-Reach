"""Public access precedes installation/auth, and keeps its evidence scope."""

import json
from unittest.mock import Mock

import pytest

from agent_reach.channels.reddit import RedditChannel
from agent_reach.channels.web import WebChannel
from agent_reach.runtime import cli, manage, state, tools
from agent_reach.runtime.dispatch import run_action
from agent_reach.runtime.result import ReachError, make_item

URL = "https://www.reddit.com/r/dataengineering/comments/abc123/data_engineer/"
TEXT = "Design data pipelines with Python and SQL. " * 20


@pytest.fixture
def public_search(monkeypatch):
    search = Mock(return_value=[make_item(title="Data Engineer", url=URL, text="Search excerpt")])
    monkeypatch.setattr(WebChannel, "_exa_search", search)
    return search


def test_public_success_never_installs_binds_or_opens_a_browser(monkeypatch, public_search):
    from agent_reach.runtime import browser, dispatch
    for module, name in ((manage, "add"), (tools, "ensure"), (browser, "ensure_running"), (dispatch, "query_slot")):
        monkeypatch.setattr(module, name, Mock(side_effect=AssertionError("public query touched browser setup")))
    monkeypatch.setattr(RedditChannel, "_opencli_search", Mock(side_effect=AssertionError("used site tool")))
    out = run_action(RedditChannel(), "search", {"query": "Data Engineer", "limit": 2})
    assert out["ok"] and out["backend"] == "public-web"
    query = public_search.call_args.kwargs["query"]
    assert "site:reddit.com/r/" in query and "Data Engineer" in query
    assert out["meta"]["access"]["scope"] == "search_results"
    assert out["meta"]["access"]["completeness_verified"] is False
    assert out["items"][0]["raw"]["content_scope"] == "search_results"


def _add_reddit():
    state.mutate(lambda data: data["channels"].__setitem__("reddit", {"added_at": state.now()}))


def test_explicit_site_backend_bypasses_web_search(monkeypatch, public_search):
    _add_reddit()
    site = Mock(return_value=[make_item(title="Site result", url=URL)])
    monkeypatch.setattr(RedditChannel, "_opencli_search", site)
    out = run_action(RedditChannel(), "search", {"query": "Data"}, only_backend="opencli")
    assert out["backend"] == "opencli" and site.call_count == 1
    public_search.assert_not_called()


def test_explicit_site_backend_of_a_channel_not_added_needs_setup(monkeypatch, public_search):
    site = Mock(side_effect=AssertionError("site tool ran for a channel that is not added"))
    monkeypatch.setattr(RedditChannel, "_opencli_search", site)
    out = run_action(RedditChannel(), "search", {"query": "Data"}, only_backend="opencli")
    assert out["error"]["code"] == "NEED_SETUP" and "agent-reach add reddit" in out["error"]["hint"]
    public_search.assert_not_called()


def test_added_channel_goes_straight_to_its_site_tool(monkeypatch, public_search):
    data = state.load()
    data["channels"]["reddit"] = {"added_at": state.now()}
    state.save(data)
    site = Mock(return_value=[make_item(title="Site result", url=URL)])
    monkeypatch.setattr(RedditChannel, "_opencli_search", site)
    out = run_action(RedditChannel(), "search", {"query": "Data"})
    assert out["ok"] and out["backend"] != "public-web" and site.call_count == 1
    public_search.assert_not_called()


@pytest.mark.parametrize("code", ["UPSTREAM_BROKEN", "TIMEOUT", "RATE_LIMITED"])
def test_web_provider_failure_does_not_become_platform_login_or_cooldown(monkeypatch, code):
    def fail(self, **kwargs):
        raise ReachError(code, "Web provider failure")
    monkeypatch.setattr(WebChannel, "_exa_search", fail)
    monkeypatch.setattr(RedditChannel, "_opencli_search", Mock(side_effect=AssertionError("site tool not added")))
    out = run_action(RedditChannel(), "search", {"query": "Data"})
    assert out["error"]["code"] == "NEED_SETUP", "the public failure is not reported as a platform verdict"
    assert not state.load().get("cooldowns", {}).get("reddit")


@pytest.mark.parametrize("rows", [[], [make_item(url="https://reddit.com.evil.test/r/python/1")],
                                 [make_item(url="https://www.reddit.com/user/person")],
                                 [make_item(url="https://x.com@evil.test/status/1")]])
def test_no_hits_or_wrong_scope_only_trigger_site_attempt_not_login(monkeypatch, public_search, rows):
    public_search.return_value = rows
    _add_reddit()
    monkeypatch.setattr(RedditChannel, "_opencli_search", Mock(side_effect=ReachError("TIMEOUT", "Site timed out")))
    out = run_action(RedditChannel(), "search", {"query": "Data"})
    assert out["error"]["code"] == "TIMEOUT"
    public_search.assert_called_once()  # an added channel falls back to public once after a site failure


@pytest.mark.parametrize("rows", [[], [make_item(url="https://www.reddit.com/user/person")]])
def test_no_public_hits_for_a_channel_not_added_needs_setup_not_login(monkeypatch, public_search, rows):
    public_search.return_value = rows
    monkeypatch.setattr(RedditChannel, "_opencli_search", Mock(side_effect=AssertionError("browser touched")))
    out = run_action(RedditChannel(), "search", {"query": "Data"})
    assert out["error"]["code"] == "NEED_SETUP"
    public_search.assert_called_once()


def test_actual_site_login_wall_is_reported_only_after_public_attempt(monkeypatch, public_search):
    _add_reddit()
    def site(*args, **kwargs):
        raise ReachError("NEED_LOGIN", "Site requires login")
    monkeypatch.setattr(RedditChannel, "_opencli_search", site)
    out = run_action(RedditChannel(), "search", {"query": "Data"})
    assert out["error"]["code"] == "NEED_LOGIN"
    assert [a["backend"] for a in out["meta"]["attempts"]][-1] == "opencli"
    public_search.assert_not_called()  # a login wall is final: no public fallback


def test_public_read_accepts_full_text_beside_a_login_link(monkeypatch):
    monkeypatch.setattr(WebChannel, "_jina_read", lambda self, **k: [make_item(title="Data Engineer", url=URL,
        text="Sign in to apply\n" + TEXT)])
    monkeypatch.setattr(RedditChannel, "_opencli_read", Mock(side_effect=AssertionError("unnecessary browser read")))
    out = run_action(RedditChannel(), "read", {"target": URL})
    assert out["ok"] and TEXT in out["items"][0]["text"]
    assert out["meta"]["access"]["scope"] == "page_text"


def test_short_public_post_beside_login_link_is_still_usable(monkeypatch):
    text = "Build SQL pipelines. Python required.\n[Sign in to apply](https://www.reddit.com/login)"
    monkeypatch.setattr(WebChannel, "_jina_read", lambda self, **k: [make_item(title="Data Engineer", url=URL, text=text)])
    monkeypatch.setattr(RedditChannel, "_opencli_read", Mock(side_effect=AssertionError("unnecessary login path")))
    out = run_action(RedditChannel(), "read", {"target": URL})
    assert out["ok"] and out["backend"] == "public-web"


@pytest.mark.parametrize("title,text", [("Reddit", ""), ("Sign in | Reddit", TEXT),
                                        ("Reddit", "Please sign in to continue. " * 8),
                                        ("Just a moment", TEXT)])
def test_shells_and_login_pages_are_not_successful_read_results(monkeypatch, title, text):
    monkeypatch.setattr(WebChannel, "_jina_read", lambda self, **k: [make_item(title=title, url=URL, text=text)])
    monkeypatch.setattr(RedditChannel, "_opencli_read", Mock(side_effect=AssertionError("browser touched")))
    out = run_action(RedditChannel(), "read", {"target": URL})
    assert not out["ok"] and out["error"]["code"] == "NEED_SETUP"
    assert any(a["result"] == "INSUFFICIENT_CONTENT" for a in out["meta"]["attempts"])


def test_confirmed_destination_cooldown_blocks_every_path(public_search):
    state.mutate(lambda saved: saved.setdefault("cooldowns", {}).__setitem__("reddit", {
        "code": "RATE_LIMITED", "at": state.now()}))
    out = run_action(RedditChannel(), "search", {"query": "Data"})
    assert out["error"]["code"] == "RATE_LIMITED"
    public_search.assert_not_called()


def test_public_only_cli_does_not_fall_back_to_browser(monkeypatch, public_search, capsys):
    public_search.return_value = []
    monkeypatch.setattr(RedditChannel, "_opencli_search", Mock(side_effect=AssertionError("public-only fell back")))
    assert cli.main(["reddit", "search", "Data", "--backend", "public-web"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["code"] == "UPSTREAM_BROKEN" and out["backend"] is None


def test_read_url_uses_the_same_public_first_route(monkeypatch, capsys):
    monkeypatch.setattr(WebChannel, "_jina_read", lambda self, **k: [make_item(title="Data Engineer", url=URL, text=TEXT)])
    assert cli.main(["read", URL]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["channel"] == "reddit" and out["backend"] == "public-web"


def test_new_policy_leaves_comments_subtitles_and_user_search_semantics_unchanged():
    from agent_reach.channels import get_all_channels
    for ch in get_all_channels():
        for action in ch.actions:
            if action.name not in {"search", "read"}:
                assert not action.public_web, (ch.name, action.name)


def test_backend_option_can_request_public_only(monkeypatch, public_search, capsys):
    monkeypatch.setattr(manage, "add", Mock(side_effect=AssertionError("unexpected setup")))
    monkeypatch.setattr(RedditChannel, "_opencli_search", Mock(side_effect=AssertionError("public-only fell back")))
    assert cli.main(["reddit", "search", "Data", "--backend", "public-web"]) == 0
    assert json.loads(capsys.readouterr().out)["backend"] == "public-web"


def test_invalid_backend_is_rejected_before_any_access(monkeypatch, public_search):
    monkeypatch.setattr(RedditChannel, "_opencli_search", Mock(side_effect=AssertionError("unexpected site access")))
    out = run_action(RedditChannel(), "search", {"query": "Data"}, only_backend="unknown")
    assert out["error"]["code"] == "BAD_INPUT"
    public_search.assert_not_called()
