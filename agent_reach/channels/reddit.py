# -*- coding: utf-8 -*-
"""Reddit — public content only, through scoped web search and page reading. No login, no browser."""

from agent_reach.utils.url import host_matches

from ._public import WebReadMixin, site_search
from .base import Action, Param


class RedditChannel(WebReadMixin):
    name = "reddit"
    description = "Reddit 공개 글"
    purpose = ("특정 주제에 대한 일반 사용자와 실무자의 솔직한 경험담·추천·비교·불만을 찾을 때 사용합니다. "
               "로그인 없이 공개 웹 검색과 페이지 읽기로만 접근하므로, Reddit이 요청을 차단하면 본문이나 댓글 일부를 읽지 못할 수 있습니다.")
    level = "default"
    hosts = ("reddit.com", "redd.it")
    actions = (
        Action("search", "Reddit 글 검색 (웹 검색 경유)",
               (Param("query", "검색어. 서브레딧으로 좁히려면 검색어에 r/이름을 포함"),
                Param("limit", "최대 결과 수", required=False, type=int, default=5)),
               (("exa", "_search"),), live_test={"query": "python packaging", "limit": 2}),
        # Reddit blocks most page readers; the search index copy is tried first.
        Action("read", "글 본문과 보이는 댓글 읽기", (Param("target", "reddit.com 글 링크"),),
               (("exa", "_exa_read"), ("jina", "_jina_read"))),
    )

    def can_handle(self, url: str) -> bool:
        return host_matches(url, *self.hosts)

    def _search(self, *, query: str, limit: int, timeout: float) -> list:
        return site_search(self.hosts, query, limit, timeout)
