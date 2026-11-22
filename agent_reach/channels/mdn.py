# -*- coding: utf-8 -*-
"""MDN Web Docs — search and read reference pages through MDN's public JSON."""

import re
from urllib.parse import quote, urlencode, urlsplit

from agent_reach.runtime.result import BAD_INPUT, NOT_FOUND, ReachError, make_item
from agent_reach.utils.url import host_matches

from ._public import get_json, strip_html
from .base import Action, Channel, Param

_BASE = "https://developer.mozilla.org"
_LOCALE_RE = re.compile(r"[A-Za-z]{2}(?:-[A-Za-z]{2})?")
_DOC_PATH_RE = re.compile(r"/[A-Za-z]{2}(?:-[A-Za-z]{2})?/docs/[A-Za-z0-9_./:()*@+-]{1,300}")


class MDNChannel(Channel):
    name = "mdn"
    description = "MDN 웹 표준 레퍼런스 문서"
    purpose = ("HTML·CSS·JavaScript·Web API·HTTP의 정확한 문법, 동작, 브라우저 호환성을 확인할 때 사용하는 "
               "1차 레퍼런스입니다. 웹 표준에 관한 사실 확인은 블로그나 Q&A보다 이 채널을 먼저 사용합니다.")
    level = "default"
    actions = (
        Action("search", "문서 검색",
               (Param("query", "검색어 (예: fetch, flexbox, Array.prototype.map)"),
                Param("limit", "최대 결과 수", required=False, type=int, default=5),
                Param("locale", "문서 언어 (en-US, ko 등)", required=False, default="en-US")),
               (("mdn-api", "_search"),), live_test={"query": "fetch", "limit": 2}),
        Action("read", "문서 본문 읽기", (Param("target", "MDN 문서 링크 또는 /en-US/docs/... 경로"),),
               (("mdn-json", "_read"),), live_test={"target": "/en-US/docs/Web/API/Fetch_API"}),
    )

    def can_handle(self, url: str) -> bool:
        return host_matches(url, "developer.mozilla.org")

    def _search(self, *, query: str, limit: int, locale: str, timeout: float) -> list:
        if not _LOCALE_RE.fullmatch(locale):
            raise ReachError(BAD_INPUT, "locale must look like en-US or ko")
        data = get_json(f"{_BASE}/api/v1/search?" + urlencode({"q": query, "locale": locale, "size": min(limit, 30)}), timeout)
        return [make_item(title=d.get("title"), url=_BASE + str(d.get("mdn_url") or ""), text=d.get("summary"),
                          raw={"slug": d.get("slug"), "locale": d.get("locale"), "score": d.get("score")})
                for d in (data.get("documents") or [])[:limit]]

    def _read(self, *, target: str, timeout: float) -> list:
        t = str(target).strip()
        if "://" in t:
            if not self.can_handle(t):
                raise ReachError(BAD_INPUT, "target must be a developer.mozilla.org link")
            t = urlsplit(t).path
        path = t.rstrip("/")
        if not _DOC_PATH_RE.fullmatch(path) or ".." in path or "//" in path:
            raise ReachError(BAD_INPUT, "target must be an MDN document path such as /en-US/docs/Web/API/Fetch_API")
        data = get_json(f"{_BASE}{quote(path, safe='/:()*@+')}/index.json", timeout)
        doc = data.get("doc") if isinstance(data, dict) else None
        if not isinstance(doc, dict):
            raise ReachError(NOT_FOUND, "MDN document not found")
        parts = []
        for section in doc.get("body") or []:
            value = section.get("value") or {}
            title = value.get("title")
            content = strip_html(value.get("content"))
            if title or content:
                parts.append((f"## {title}\n" if title else "") + content)
        return [make_item(title=doc.get("title"), url=_BASE + str(doc.get("mdn_url") or path),
                          published_at=doc.get("modified"), text="\n\n".join(parts) or doc.get("summary"),
                          raw={"locale": doc.get("locale"), "summary": doc.get("summary"), "page_type": doc.get("pageType")})]
