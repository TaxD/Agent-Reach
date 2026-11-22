# -*- coding: utf-8 -*-
"""Generic RSS/Atom feed reader for any public feed URL."""

from agent_reach.runtime.result import BAD_INPUT, NOT_FOUND, ReachError, classify

from ._public import fetch_public, parse_feed
from .base import Action, Channel, Param


class RSSChannel(Channel):
    name = "rss"
    description = "임의의 공개 RSS/Atom 피드"
    purpose = ("전용 채널이 없는 블로그·뉴스·릴리스 노트·팟캐스트의 최신 글을 피드 주소로 직접 읽을 때 사용합니다. "
               "기업 기술 블로그나 프로젝트 릴리스 피드처럼 주소를 알고 있는 출처의 새 글을 확인하는 용도입니다.")
    level = "default"
    actions = (
        Action("fetch", "피드의 최신 항목 읽기",
               (Param("url", "피드 주소 (공개 http/https)"),
                Param("limit", "최대 항목 수", required=False, type=int, default=10)),
               (("feed", "_fetch"),), live_test={"url": "https://github.blog/feed/", "limit": 2}),
    )

    def can_handle(self, url: str) -> bool:
        return False  # `agent-reach read <url>` is for pages; feeds are requested explicitly

    def _fetch(self, *, url: str, limit: int, timeout: float) -> list:
        # A failure belongs to this one caller-chosen feed. Report it with codes that
        # neither pause the backend nor cool down the channel for every other feed.
        try:
            return parse_feed(fetch_public(url, timeout), min(limit, 100))
        except Exception as exc:  # noqa: BLE001
            err = classify(exc)
            if err.code in (BAD_INPUT, NOT_FOUND):
                raise err from None
            raise ReachError(BAD_INPUT, f"this feed could not be read: {err.message}") from None
