"""Reddit refuses listing endpoints on some networks; `subreddit` then answers from public search."""

from agent_reach.channels.reddit import RedditChannel
from agent_reach.channels.web import WebChannel
from agent_reach.runtime import public_web


def _action():
    return next(a for a in RedditChannel.actions if a.name == "subreddit")


def test_subreddit_public_route_keeps_only_that_subreddit(monkeypatch):
    seen = {}

    def fake(self, *, query, limit, timeout):
        seen["query"] = query
        return [{"title": "a", "url": "https://www.reddit.com/r/Python/comments/1/a/", "text": "t"},
                {"title": "b", "url": "https://www.reddit.com/r/pythonic/comments/2/b/", "text": "t"},
                {"title": "c", "url": "https://example.com/r/python/x", "text": "t"}]

    monkeypatch.setattr(WebChannel, "_exa_search", fake)
    out = public_web.attempt(RedditChannel(), _action(), {"name": "r/python", "limit": 5}, timeout=10)
    assert out["ok"] and out["backend"] == "public-web" and out["action"] == "subreddit"
    assert [i["title"] for i in out["items"]] == ["a"]
    assert "site:reddit.com/r/python" in seen["query"]
    assert out["meta"]["access"]["completeness_verified"] is False


def test_subreddit_public_route_refuses_odd_names():
    for name in ("a b", "x) OR (site:evil.test", "", "p/../q"):
        assert public_web.attempt(RedditChannel(), _action(), {"name": name, "limit": 5}, timeout=10) is None
