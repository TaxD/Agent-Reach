# -*- coding: utf-8 -*-
"""Sites reached through their public feed plus scoped web search and page reading.

None of these has a login-free JSON API, so listings come from the site's own
feed, and search/read go through the Web channel restricted to the site's hosts.
"""

import re
from urllib.parse import quote

from agent_reach.runtime.result import BAD_INPUT, ReachError
from agent_reach.utils.url import host_matches

from ._public import WebReadMixin, feed_items, site_search
from .base import Action, Param

_LIMIT = Param("limit", "최대 결과 수", required=False, type=int, default=10)
_QUERY = (Param("query", "검색어"), Param("limit", "최대 결과 수", required=False, type=int, default=5))


def _search_action(site: str) -> Action:
    return Action("search", f"{site} 안에서 검색 (웹 검색 경유)", _QUERY, (("exa", "_search"),))


def _read_action(what: str) -> Action:
    return Action("read", f"{what} 본문 읽기", (Param("target", "페이지 링크"),), WebReadMixin.READ_BACKENDS)


class _FeedSite(WebReadMixin):
    hosts: tuple = ()
    level = "default"

    def can_handle(self, url: str) -> bool:
        return host_matches(url, *self.hosts)

    def _search(self, *, query: str, limit: int, timeout: float) -> list:
        return site_search(self.hosts, query, limit, timeout)


class ProductHuntChannel(_FeedSite):
    name = "producthunt"
    description = "Product Hunt 신제품"
    purpose = ("새로 출시된 소프트웨어 제품·AI 도구·개발자 도구와 그 한 줄 소개를 파악할 때 사용합니다. "
               "경쟁 제품 조사, 최근 출시 동향 확인, 특정 분야의 신규 도구 탐색에 적합합니다.")
    hosts = ("producthunt.com",)
    actions = (
        Action("latest", "최근 등록된 제품", (_LIMIT,), (("feed", "_latest"),), live_test={"limit": 2}),
        _search_action("Product Hunt"),
        _read_action("제품 페이지"),
    )

    def _latest(self, *, limit: int, timeout: float) -> list:
        return feed_items("https://www.producthunt.com/feed", limit, timeout)


class MediumChannel(_FeedSite):
    name = "medium"
    description = "Medium 글"
    purpose = ("엔지니어링 블로그, 아키텍처 사례, 제품·디자인·데이터 분야의 장문 에세이를 찾을 때 사용합니다. "
               "태그나 작성자·출판물 단위로 최근 글을 훑어볼 수 있으며, 유료 글은 본문 일부만 읽힐 수 있습니다.")
    hosts = ("medium.com",)
    actions = (
        Action("tag", "태그의 최근 글", (Param("name", "태그 (예: programming, machine-learning)"), _LIMIT),
               (("feed", "_tag"),), live_test={"name": "programming", "limit": 2}),
        Action("user", "작성자 또는 출판물의 최근 글", (Param("name", "@사용자 또는 출판물 슬러그 (예: @netflixtechblog)"), _LIMIT),
               (("feed", "_user"),)),
        _search_action("Medium"),
        _read_action("글"),
    )

    def _feed(self, path: str, limit: int, timeout: float) -> list:
        return feed_items("https://medium.com/feed/" + path, limit, timeout)

    def _tag(self, *, name: str, limit: int, timeout: float) -> list:
        if not re.fullmatch(r"[A-Za-z0-9-]{1,60}", name):
            raise ReachError(BAD_INPUT, "tag may contain only letters, digits and hyphens")
        return self._feed("tag/" + quote(name.lower(), safe=""), limit, timeout)

    def _user(self, *, name: str, limit: int, timeout: float) -> list:
        if not re.fullmatch(r"@?[A-Za-z0-9._-]{1,60}", name) or ".." in name:
            raise ReachError(BAD_INPUT, "name must be @username or a publication slug")
        return self._feed(quote(name, safe="@"), limit, timeout)


class DribbbleChannel(_FeedSite):
    name = "dribbble"
    description = "Dribbble 디자인 작업물"
    purpose = ("UI·앱·웹·브랜딩 디자인의 참고 사례와 시각적 경향을 찾을 때 사용합니다. 공개 API가 없어서 웹 검색으로 "
               "작업물 링크와 설명을 찾는 방식이며, 이미지 자체는 반환하지 않으므로 링크를 사용자에게 제시합니다.")
    hosts = ("dribbble.com",)
    level = "experimental"
    actions = (_search_action("Dribbble"), _read_action("작업물 페이지"))


class GeekNewsChannel(_FeedSite):
    name = "geeknews"
    description = "GeekNews 한국어 기술 뉴스"
    purpose = ("한국어로 요약된 개발·기술·스타트업 뉴스와 한국 개발자 커뮤니티의 관심사를 파악할 때 사용합니다. "
               "해외 기술 소식의 한국어 요약이 필요하거나, 한국어 사용자에게 최신 기술 동향을 전달할 때 먼저 확인합니다.")
    hosts = ("news.hada.io",)
    actions = (
        Action("latest", "최신 뉴스 (한국어 요약 포함)", (_LIMIT,), (("feed", "_latest"),), live_test={"limit": 2}),
        _search_action("GeekNews"),
        _read_action("토픽 페이지와 댓글"),
    )

    def _latest(self, *, limit: int, timeout: float) -> list:
        return feed_items("https://news.hada.io/rss/news", limit, timeout)


class YozmChannel(_FeedSite):
    name = "yozm"
    description = "요즘IT 한국어 IT 매거진"
    purpose = ("한국 IT 업계 실무자가 쓴 개발·기획·디자인·프로덕트·커리어 분야의 한국어 심층 글을 찾을 때 사용합니다. "
               "한국 시장과 조직의 맥락이 담긴 실무 사례나 한국어 해설이 필요할 때 적합합니다.")
    hosts = ("yozm.wishket.com",)
    actions = (
        Action("latest", "최신 글 (요약 포함)", (_LIMIT,), (("feed", "_latest"),), live_test={"limit": 2}),
        _search_action("요즘IT"),
        _read_action("글"),
    )

    def _latest(self, *, limit: int, timeout: float) -> list:
        return feed_items("https://yozm.wishket.com/magazine/feed/", limit, timeout, prefer_summary=True)
