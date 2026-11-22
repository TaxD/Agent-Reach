# -*- coding: utf-8 -*-
"""Twitter/X — OpenCLI in the Agent Reach browser (experimental)."""

from agent_reach.backends.opencli import opencli_call, opencli_items
from agent_reach.runtime.result import UPSTREAM_BROKEN, ReachError
from agent_reach.utils.url import host_matches

from .base import Action, Channel, Param

_TWITTER_LOGIN_HINT = (
    "Ask the user to log in to x.com in the Agent Reach dedicated browser. Never log in on their behalf."
)


def _x(command: str, values, timeout: float, limit=None) -> list:
    items = opencli_items(opencli_call("twitter", command, values, limit=limit, timeout=timeout, login_hint=_TWITTER_LOGIN_HINT))
    if not items:
        raise ReachError(UPSTREAM_BROKEN, "OpenCLI finished but returned no items")
    return items


class TwitterChannel(Channel):
    name = "twitter"
    description = "X(Twitter) 트윗"
    purpose = ("특정 인물·회사·프로젝트의 공식 발표와 실시간 반응을 확인할 때 사용합니다. 사용자가 전용 브라우저에서 직접 "
               "로그인해야 동작하는 실험적 채널이므로, 로그인 없이 가능한 bluesky·웹 검색으로 충분한지 먼저 판단합니다.")

    # twitter-cli is not used here: even with explicit credentials it re-reads
    # browser cookies whenever verification fails.
    level = "experimental"
    login_url = "https://x.com/i/flow/login"
    tools = ("opencli",)
    prereqs = ("node",)

    def setup_problems(self):
        from agent_reach.runtime.browser_binding import setup_problems

        return setup_problems(self.name)

    actions = (
        Action(
            "search",
            "트윗 검색",
            (
                Param("query", "검색어"),
                Param("limit", "최대 결과 수", required=False, type=int, default=5),
            ),
            (("opencli", "_opencli_search"),),
            live_test={"query": "openai", "limit": 2},
            public_web=("x.com/", "twitter.com/"),
        ),
        Action(
            "profile",
            "사용자 프로필",
            (Param("username", "@ 를 뺀 핸들"),),
            (("opencli", "_opencli_profile"),),
            live_test={"username": "OpenAI"},
        ),
        Action(
            "tweets",
            "사용자의 최근 트윗",
            (
                Param("username", "@ 를 뺀 핸들"),
                Param("limit", "최대 트윗 수", required=False, type=int, default=5),
            ),
            (("opencli", "_opencli_tweets"),),
        ),
        Action(
            "thread",
            "트윗 하나와 답글",
            (Param("target", "트윗 id 또는 링크"),),
            (("opencli", "_opencli_thread"),),
        ),
    )

    def can_handle(self, url: str) -> bool:
        return host_matches(url, "x.com", "twitter.com")

    # ── unified entry backends ──

    def _opencli_search(self, *, query: str, limit: int, timeout: float) -> list:
        return _x("search", [query], timeout, limit)[:limit]


    def _opencli_profile(self, *, username: str, timeout: float) -> list:
        return _x("profile", [username.lstrip("@")], timeout)[:1]

    def _opencli_tweets(self, *, username: str, limit: int, timeout: float) -> list:
        return _x("tweets", [username.lstrip("@")], timeout, limit)[:limit]

    def _opencli_thread(self, *, target: str, timeout: float) -> list:
        tweet_id = str(target).rstrip("/").split("/")[-1].split("?")[0]
        return _x("thread", [tweet_id], timeout)
