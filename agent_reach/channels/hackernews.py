# -*- coding: utf-8 -*-
"""Hacker News — stories and comment threads through the public Algolia HN API."""

import re
from urllib.parse import urlencode

from agent_reach.runtime.result import BAD_INPUT, NOT_FOUND, ReachError, make_item
from agent_reach.utils.url import host_matches

from ._public import get_json, strip_html
from .base import Action, Channel, Param

_API = "https://hn.algolia.com/api/v1"
_ITEM = "https://news.ycombinator.com/item?id="
_MAX_COMMENTS = 80


def _story(hit: dict) -> dict:
    hn_url = _ITEM + str(hit.get("objectID") or hit.get("story_id") or "")
    return make_item(
        title=hit.get("title"), url=hit.get("url") or hn_url, author=hit.get("author"),
        published_at=hit.get("created_at"), text=strip_html(hit.get("story_text")),
        raw={"id": hit.get("objectID"), "points": hit.get("points"),
             "num_comments": hit.get("num_comments"), "hn_url": hn_url})


def _flatten(node: dict, depth: int, out: list) -> None:
    if depth > 30:
        return
    for child in node.get("children") or []:
        if len(out) >= _MAX_COMMENTS or not isinstance(child, dict):
            return
        if child.get("text"):
            out.append(f"{'  ' * min(depth, 10)}@{child.get('author') or ''}: {strip_html(child['text'])}")
        _flatten(child, depth + 1, out)


class HackerNewsChannel(Channel):
    name = "hackernews"
    description = "Hacker News 글과 댓글 토론"
    purpose = ("영어권 개발자·스타트업 커뮤니티의 기술 뉴스와, 특정 기술·제품·회사에 대한 실무자들의 "
               "평가와 토론을 확인할 때 사용합니다. 출시 반응이나 기술 선택에 대한 논쟁을 찾기에 적합합니다.")
    level = "default"
    actions = (
        Action("search", "키워드로 글 검색 (관련도순)",
               (Param("query", "검색어"), Param("limit", "최대 결과 수", required=False, type=int, default=5)),
               (("algolia", "_search"),), live_test={"query": "rust", "limit": 2}),
        Action("top", "현재 첫 페이지에 올라온 글",
               (Param("limit", "최대 결과 수", required=False, type=int, default=10),),
               (("algolia", "_top"),), live_test={"limit": 2}),
        Action("read", "글 하나와 댓글 스레드 읽기",
               (Param("target", "글 id 또는 news.ycombinator.com/item 링크"),),
               (("algolia", "_read"),), live_test={"target": "1"}),
    )

    def can_handle(self, url: str) -> bool:
        return host_matches(url, "news.ycombinator.com")

    def _search(self, *, query: str, limit: int, timeout: float) -> list:
        data = get_json(f"{_API}/search?" + urlencode({"query": query, "tags": "story", "hitsPerPage": min(limit, 50)}), timeout)
        return [_story(h) for h in data.get("hits", [])[:limit]]

    def _top(self, *, limit: int, timeout: float) -> list:
        data = get_json(f"{_API}/search?" + urlencode({"tags": "front_page", "hitsPerPage": min(limit, 50)}), timeout)
        return [_story(h) for h in data.get("hits", [])[:limit]]

    def _read(self, *, target: str, timeout: float) -> list:
        t = str(target).strip()
        m = re.fullmatch(r"\d{1,12}", t) or (self.can_handle(t) and re.search(r"[?&]id=(\d{1,12})(?:&|#|$)", t))
        if not m:
            raise ReachError(BAD_INPUT, "target must be a Hacker News item id or item link")
        item_id = m.group(m.lastindex or 0)
        data = get_json(f"{_API}/items/{item_id}", timeout)
        if not isinstance(data, dict) or not data.get("id"):
            raise ReachError(NOT_FOUND, f"Hacker News item not found: {item_id}")
        lines: list = []
        _flatten(data, 0, lines)
        body = strip_html(data.get("text"))
        return [make_item(
            title=data.get("title"), url=data.get("url") or _ITEM + item_id, author=data.get("author"),
            published_at=data.get("created_at"), text="\n\n".join(x for x in (body, "\n".join(lines)) if x),
            raw={"id": data.get("id"), "points": data.get("points"), "hn_url": _ITEM + item_id,
                 "comments_included": len(lines), "comments_capped_at": _MAX_COMMENTS})]
