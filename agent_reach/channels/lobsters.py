# -*- coding: utf-8 -*-
"""Lobsters — stories and comments through the site's public JSON endpoints."""

import re
from urllib.parse import quote

from agent_reach.runtime.result import BAD_INPUT, NOT_FOUND, ReachError, make_item
from agent_reach.utils.url import host_matches

from ._public import get_json, strip_html
from .base import Action, Channel, Param

_BASE = "https://lobste.rs"
_LIMIT = Param("limit", "최대 결과 수", required=False, type=int, default=10)


def _story(s: dict, text: str = "") -> dict:
    return make_item(
        title=s.get("title"), url=s.get("url") or s.get("comments_url"), author=s.get("submitter_user"),
        published_at=s.get("created_at"), text=text or s.get("description_plain"),
        raw={"id": s.get("short_id"), "score": s.get("score"), "comment_count": s.get("comment_count"),
             "tags": s.get("tags"), "comments_url": s.get("comments_url")})


class LobstersChannel(Channel):
    name = "lobsters"
    description = "Lobsters 기술 링크와 댓글"
    purpose = ("시스템 프로그래밍, 프로그래밍 언어, 보안, 운영체제처럼 깊이 있는 기술 글과 그에 대한 전문가 토론을 "
               "찾을 때 사용합니다. Hacker News보다 규모는 작지만 기술 주제에 집중되어 있고 태그별로 볼 수 있습니다.")
    level = "default"
    actions = (
        Action("hot", "현재 인기 글", (_LIMIT,), (("lobsters-json", "_hot"),), live_test={"limit": 2}),
        Action("newest", "최신 글", (_LIMIT,), (("lobsters-json", "_newest"),)),
        Action("tag", "태그별 글", (Param("name", "태그 이름 (예: rust, security, ai)"), _LIMIT),
               (("lobsters-json", "_tag"),), live_test={"name": "rust", "limit": 2}),
        Action("read", "글 하나와 댓글 읽기", (Param("target", "글의 short id 또는 lobste.rs/s 링크"),),
               (("lobsters-json", "_read"),)),
    )

    def can_handle(self, url: str) -> bool:
        return host_matches(url, "lobste.rs")

    def _list(self, path: str, limit: int, timeout: float) -> list:
        data = get_json(_BASE + path, timeout)
        if not isinstance(data, list):
            raise ReachError(NOT_FOUND, "Lobsters returned no list")
        return [_story(s) for s in data[:limit]]

    def _hot(self, *, limit: int, timeout: float) -> list:
        return self._list("/hottest.json", limit, timeout)

    def _newest(self, *, limit: int, timeout: float) -> list:
        return self._list("/newest.json", limit, timeout)

    def _tag(self, *, name: str, limit: int, timeout: float) -> list:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_+.-]{0,39}", name):
            raise ReachError(BAD_INPUT, "tag name may contain only letters, digits and _ + . -")
        return self._list(f"/t/{quote(name.lower(), safe='')}.json", limit, timeout)

    def _read(self, *, target: str, timeout: float) -> list:
        t = str(target).strip()
        m = re.fullmatch(r"[a-z0-9]{4,12}", t) or (self.can_handle(t) and re.search(r"/s/([a-z0-9]{4,12})(?:/|$|\?|#|\.)", t))
        if not m:
            raise ReachError(BAD_INPUT, "target must be a Lobsters short id or a lobste.rs/s/ link")
        data = get_json(f"{_BASE}/s/{m.group(m.lastindex or 0)}.json", timeout)
        if not isinstance(data, dict) or not data.get("short_id"):
            raise ReachError(NOT_FOUND, "Lobsters story not found")
        lines = [data.get("description_plain") or ""]
        for c in (data.get("comments") or [])[:100]:
            if not c.get("is_deleted"):
                body = c.get("comment_plain") or strip_html(c.get("comment"))
                depth = c.get("depth")
                indent = "  " * min(depth, 10) if isinstance(depth, int) and depth > 0 else ""
                lines.append(f"{indent}@{c.get('commenting_user') or ''}: {body}")
        return [_story(data, "\n\n".join(x for x in lines if x))]
