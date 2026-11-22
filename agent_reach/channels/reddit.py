# -*- coding: utf-8 -*-
"""Reddit browser actions supplement public web search and reading.

Public posts and some comments can be available through an agent's existing
web tools without Reddit login. A blocked anonymous JSON endpoint does not
establish a site-wide login requirement. These actions use managed browser
sessions; request authentication only when the selected path requires it.
"""

from agent_reach.backends.opencli import opencli_call, opencli_items
from agent_reach.runtime.result import BAD_INPUT, UPSTREAM_BROKEN, ReachError

from .base import Action, Channel, Param

_REDDIT_LOGIN_HINT = (
    "Ask the user to log in to reddit.com in the Agent Reach dedicated browser. Never log in on their behalf."
)


def _reddit(command: str, values, timeout: float, limit=None) -> list:
    items = opencli_items(opencli_call("reddit", command, values, limit=limit, timeout=timeout, login_hint=_REDDIT_LOGIN_HINT))
    if not items:
        raise ReachError(UPSTREAM_BROKEN, "OpenCLI finished but returned no items")
    return items


class RedditChannel(Channel):
    name = "reddit"
    description = "Reddit 글과 댓글"
    purpose = ("특정 주제에 대한 일반 사용자와 실무자의 솔직한 경험담·추천·비교·불만을 찾을 때 사용합니다. "
               "서브레딧 단위로 커뮤니티 분위기를 볼 수 있으며, 공개 웹 경로를 먼저 시도하고 필요할 때에만 로그인을 요청합니다.")

    level = "optional"
    login_url = "https://www.reddit.com/login/"
    tools = ("opencli",)
    prereqs = ("node",)

    def setup_problems(self):
        from agent_reach.runtime.browser_binding import setup_problems

        return setup_problems(self.name)

    actions = (
        Action(
            "search",
            "공개 글을 먼저 검색. --backend opencli는 사이트 내부 검색 사용",
            (
                Param("query", "검색어"),
                Param("limit", "최대 결과 수", required=False, type=int, default=5),
            ),
            (("opencli", "_opencli_search"),),
            live_test={"query": "python", "limit": 2},
            public_web=("reddit.com/r/",),
        ),
        Action(
            "read",
            "공개 페이지 본문을 먼저 읽기. --backend opencli는 글과 상위 댓글을 요청",
            (Param("target", "글 링크 또는 id"),),
            (("opencli", "_opencli_read"),),
            public_web=("reddit.com/r/", "redd.it/"),
        ),
        Action(
            "subreddit",
            "서브레딧의 최신 글",
            (
                Param("name", "r/ 를 뺀 서브레딧 이름 (예: python)"),
                Param("limit", "최대 글 수", required=False, type=int, default=5),
            ),
            (("opencli", "_opencli_subreddit"),),
            live_test={"name": "python", "limit": 2},
        ),
    )

    def can_handle(self, url: str) -> bool:
        from agent_reach.utils.url import host_matches

        return host_matches(url, "reddit.com", "redd.it")

    # ── unified entry backends ──

    def _opencli_search(self, *, query: str, limit: int, timeout: float) -> list:
        return _reddit("search", [query], timeout, limit)[:limit]


    def _opencli_read(self, *, target: str, timeout: float) -> list:
        t = str(target).strip()
        if not (self.can_handle(t) or t.isalnum()):
            raise ReachError(BAD_INPUT, "target must be a reddit.com post link or post id")
        items = _reddit("read", [t], timeout, 20)
        post, comments = items[0], items[1:]
        if comments:
            lines = [post["text"] or ""] + [f"\n@{c['author'] or ''}: {c['text'] or ''}" for c in comments]
            post["text"] = "\n".join(lines).strip()
            post["raw"] = {"post": post["raw"], "comments": [c["raw"] for c in comments]}
        return [post]

    def _opencli_subreddit(self, *, name: str, limit: int, timeout: float) -> list:
        return _reddit("subreddit", [name.removeprefix("r/")], timeout, limit)[:limit]
