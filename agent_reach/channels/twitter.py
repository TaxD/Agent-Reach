# -*- coding: utf-8 -*-
"""X (Twitter) — public content only, through the public embed endpoints. No login."""

import math
import re
from urllib.parse import urlencode, urlsplit

from agent_reach.runtime.result import BAD_INPUT, NOT_FOUND, ReachError, make_item
from agent_reach.utils.url import host_matches

from ._public import get_json
from .base import Action, Channel, Param

_EMBED = "https://cdn.syndication.twimg.com/tweet-result"
_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _embed_token(tweet_id: str) -> str:
    """The token the public embed widget derives from the id (base 36 of id/1e15*pi, zeros and dot dropped)."""
    value = int(tweet_id) / 1e15 * math.pi
    whole, frac = int(value), value - int(value)
    head = ""
    while whole:
        head = _DIGITS[whole % 36] + head
        whole //= 36
    tail = ""
    for _ in range(12):
        frac *= 36
        tail += _DIGITS[int(frac)]
        frac -= int(frac)
    return (head + tail).replace("0", "") or "a"


class TwitterChannel(Channel):
    name = "twitter"
    description = "X(Twitter) 공개 트윗"
    purpose = ("x.com 또는 twitter.com 트윗 링크(또는 트윗 id)를 받았을 때 그 트윗 하나의 본문·작성자·날짜를 읽는 용도입니다. "
               "로그인 없이 공개 임베드로만 읽으므로 검색·타임라인·답글은 제공하지 않습니다. 주제 검색과 실시간 반응은 bluesky를 사용합니다.")
    level = "default"
    hosts = ("x.com", "twitter.com")
    actions = (
        Action("read", "트윗 하나 읽기 (공개 임베드)", (Param("target", "트윗 링크 또는 id"),),
               (("embed", "_read"),), live_test={"target": "20"}),
    )

    def can_handle(self, url: str) -> bool:
        return host_matches(url, *self.hosts)

    def _read(self, *, target: str, timeout: float) -> list:
        t = str(target).strip()
        m = re.fullmatch(r"\d{1,20}", t) or (
            re.search(r"/status(?:es)?/(\d{1,20})(?:/|$)", urlsplit(t).path) if self.can_handle(t) else None)
        if not m:
            raise ReachError(BAD_INPUT, "target must be a tweet link or numeric tweet id")
        tweet_id = m.group(m.lastindex or 0)
        data = get_json(_EMBED + "?" + urlencode({"id": tweet_id, "token": _embed_token(tweet_id), "lang": "en"}), timeout)
        if not isinstance(data, dict) or not data.get("text"):
            raise ReachError(NOT_FOUND, "tweet not found, deleted, or not public")
        user = data.get("user") or {}
        handle = user.get("screen_name")
        return [make_item(
            title=None, url=f"https://x.com/{handle}/status/{tweet_id}" if handle else f"https://x.com/i/status/{tweet_id}",
            author=handle, published_at=data.get("created_at"), text=data.get("text"),
            raw={"id": tweet_id, "name": user.get("name"), "favorite_count": data.get("favorite_count"),
                 "conversation_count": data.get("conversation_count"), "lang": data.get("lang")})]
