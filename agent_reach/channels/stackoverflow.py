# -*- coding: utf-8 -*-
"""Stack Overflow — questions and answers through the public Stack Exchange API (no key)."""

import html
import json
import re
import urllib.error
from urllib.parse import urlencode

from agent_reach.runtime.result import (
    BAD_INPUT,
    NOT_FOUND,
    RATE_LIMITED,
    ReachError,
    make_item,
    unix_to_iso,
)
from agent_reach.utils.url import host_matches

from ._public import _gunzip, get_json, strip_html
from .base import Action, Channel, Param

_API = "https://api.stackexchange.com/2.3"


def _get(path: str, timeout: float, **params) -> dict:
    query = urlencode({"site": "stackoverflow", "filter": "withbody", **params})
    try:
        data = get_json(f"{_API}{path}?{query}", timeout)
    except urllib.error.HTTPError as exc:
        try:
            name = json.loads(_gunzip(exc.read(64 * 1024), 1024 * 1024)).get("error_name", "")
        except Exception:  # noqa: BLE001 - the error body is optional detail
            name = ""
        if exc.code == 429 or name == "throttle_violation":
            raise ReachError(RATE_LIMITED, "Stack Exchange API quota reached (300 requests/day without a key)") from None
        raise
    return data if isinstance(data, dict) else {}


def _question(q: dict, text: str = "") -> dict:
    return make_item(
        title=html.unescape(q.get("title") or ""), url=q.get("link"),
        author=html.unescape((q.get("owner") or {}).get("display_name") or ""),
        published_at=unix_to_iso(q.get("creation_date")), text=text or strip_html(q.get("body")),
        raw={k: q.get(k) for k in ("question_id", "score", "answer_count", "is_answered",
                                   "accepted_answer_id", "view_count", "tags")})


class StackOverflowChannel(Channel):
    name = "stackoverflow"
    description = "Stack Overflow 질문과 답변"
    purpose = ("구체적인 오류 메시지, API 사용법, 구현 방법처럼 정답이 있는 프로그래밍 질문의 검증된 답변을 "
               "찾을 때 사용합니다. 채택 여부와 득표 수가 함께 제공되므로 답변의 신뢰도를 판단할 수 있습니다.")
    level = "default"
    actions = (
        Action("search", "질문 검색 (관련도순, 본문 포함)",
               (Param("query", "검색어 또는 오류 메시지"), Param("limit", "최대 결과 수", required=False, type=int, default=5)),
               (("stackexchange", "_search"),), live_test={"query": "python asyncio gather", "limit": 2}),
        Action("read", "질문 본문과 득표순 상위 답변 읽기",
               (Param("target", "질문 id 또는 stackoverflow.com/questions 링크"),),
               (("stackexchange", "_read"),), live_test={"target": "11227809"}),
    )

    def can_handle(self, url: str) -> bool:
        return host_matches(url, "stackoverflow.com")

    def _search(self, *, query: str, limit: int, timeout: float) -> list:
        data = _get("/search/advanced", timeout, q=query, order="desc", sort="relevance", pagesize=min(limit, 30))
        return [_question(q) for q in data.get("items", [])[:limit]]

    def _read(self, *, target: str, timeout: float) -> list:
        t = str(target).strip()
        m = re.fullmatch(r"\d{1,12}", t) or (self.can_handle(t) and re.search(r"/(?:questions|q)/(\d{1,12})(?:/|$|\?|#)", t))
        if not m:
            raise ReachError(BAD_INPUT, "target must be a Stack Overflow question id or question link")
        qid = m.group(m.lastindex or 0)
        found = _get(f"/questions/{qid}", timeout / 2).get("items") or []
        if not found:
            raise ReachError(NOT_FOUND, f"Stack Overflow question not found: {qid}")
        question = found[0]
        answers = _get(f"/questions/{qid}/answers", timeout / 2, order="desc", sort="votes", pagesize=5).get("items") or []
        parts = [strip_html(question.get("body"))]
        for a in answers:
            mark = " (accepted)" if a.get("is_accepted") else ""
            parts.append(f"--- answer, score {a.get('score')}{mark}, by {html.unescape((a.get('owner') or {}).get('display_name') or '')} ---\n"
                         + strip_html(a.get("body")))
        return [_question(question, "\n\n".join(parts))]
