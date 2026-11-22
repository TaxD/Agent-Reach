# -*- coding: utf-8 -*-
"""dev.to — articles through the public Forem API; search through the Web channel."""

import re
from urllib.parse import quote, urlencode, urlsplit

from agent_reach.runtime.result import BAD_INPUT, NOT_FOUND, ReachError, make_item
from agent_reach.utils.url import host_matches

from ._public import get_json, site_search
from .base import Action, Channel, Param

_API = "https://dev.to/api"
_TAG = Param("tag", "태그로 좁히기 (예: python, webdev)", required=False)
_LIMIT = Param("limit", "최대 결과 수", required=False, type=int, default=10)


def _article(a: dict, text: str = "") -> dict:
    user = a.get("user") or {}
    return make_item(
        title=a.get("title"), url=a.get("url"), author=user.get("username") or user.get("name"),
        published_at=a.get("published_at"), text=text or a.get("description"),
        raw={k: a.get(k) for k in ("id", "tag_list", "tags", "positive_reactions_count",
                                   "comments_count", "reading_time_minutes")})


def _tag_query(tag) -> dict:
    if not tag:
        return {}
    if not re.fullmatch(r"[A-Za-z0-9]{1,30}", str(tag)):
        raise ReachError(BAD_INPUT, "tag may contain only letters and digits")
    return {"tag": str(tag).lower()}


class DevToChannel(Channel):
    name = "devto"
    description = "dev.to 개발 블로그 글"
    purpose = ("튜토리얼, 입문·실습 가이드, 개발자의 경험담처럼 따라 하기 쉬운 개발 글을 찾을 때 사용합니다. "
               "웹·프런트엔드·입문자 대상 주제가 풍부하며, 태그와 기간별 인기 글로 최근 관심사를 볼 수 있습니다.")
    level = "default"
    actions = (
        Action("top", "최근 N일간 인기 글",
               (_LIMIT, _TAG, Param("days", "집계 기간(일)", required=False, type=int, default=7)),
               (("forem-api", "_top"),), live_test={"limit": 2}),
        Action("latest", "최신 글", (_LIMIT, _TAG), (("forem-api", "_latest"),)),
        Action("search", "키워드로 글 검색 (웹 검색 경유)",
               (Param("query", "검색어"), Param("limit", "최대 결과 수", required=False, type=int, default=5)),
               (("exa", "_search"),)),
        Action("read", "글 전문(마크다운) 읽기", (Param("target", "글 id 또는 dev.to/<사용자>/<슬러그> 링크"),),
               (("forem-api", "_read"),)),
    )

    def can_handle(self, url: str) -> bool:
        return host_matches(url, "dev.to")

    def _list(self, path: str, params: dict, limit: int, timeout: float) -> list:
        data = get_json(f"{_API}{path}?" + urlencode({**params, "per_page": min(limit, 50)}), timeout)
        return [_article(a) for a in (data if isinstance(data, list) else [])[:limit]]

    def _top(self, *, limit: int, tag, days: int, timeout: float) -> list:
        if not 1 <= days <= 365:
            raise ReachError(BAD_INPUT, "days must be between 1 and 365")
        return self._list("/articles", {**_tag_query(tag), "top": days}, limit, timeout)

    def _latest(self, *, limit: int, tag, timeout: float) -> list:
        return self._list("/articles/latest", _tag_query(tag), limit, timeout)

    def _search(self, *, query: str, limit: int, timeout: float) -> list:
        return site_search(("dev.to",), query, limit, timeout)

    def _read(self, *, target: str, timeout: float) -> list:
        t = str(target).strip()
        if re.fullmatch(r"\d{1,12}", t):
            path = t
        else:
            url = t if "://" in t else "https://" + t
            parts = [p for p in urlsplit(url).path.split("/") if p]
            if not self.can_handle(url) or len(parts) != 2 or not all(re.fullmatch(r"\w[\w.-]{0,199}", p) for p in parts):
                raise ReachError(BAD_INPUT, "target must be an article id or a dev.to/<user>/<slug> link")
            path = "/".join(quote(p, safe="") for p in parts)
        data = get_json(f"{_API}/articles/{path}", timeout)
        if not isinstance(data, dict) or not data.get("id"):
            raise ReachError(NOT_FOUND, "dev.to article not found")
        return [_article(data, data.get("body_markdown") or "")]
