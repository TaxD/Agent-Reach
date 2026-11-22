# -*- coding: utf-8 -*-
"""Bluesky — public posts, profiles and threads through the public AppView API (no login)."""

import re
import urllib.error
from urllib.parse import urlencode, urlsplit

from agent_reach.runtime.result import BAD_INPUT, NOT_FOUND, ReachError, make_item
from agent_reach.utils.url import host_matches

from ._public import get_json
from .base import Action, Channel, Param

_PUBLIC = "https://public.api.bsky.app/xrpc/"
#: searchPosts is refused on the public.api host but served here without a session.
_SEARCH = "https://api.bsky.app/xrpc/"
_ACTOR_RE = re.compile(r"(?:[A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,63}|did:(?:plc|web):[A-Za-z0-9._:%-]{1,200}")
_RKEY_RE = re.compile(r"[A-Za-z0-9._~-]{1,64}")
_LIMIT = Param("limit", "최대 결과 수", required=False, type=int, default=5)


def _xrpc(base: str, method: str, timeout: float, **params) -> dict:
    try:
        data = get_json(base + method + "?" + urlencode(params), timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 400:
            raise ReachError(NOT_FOUND, "Bluesky could not find that account or post") from None
        raise
    return data if isinstance(data, dict) else {}


def _actor(value: str) -> str:
    actor = str(value).strip().lstrip("@")
    if not _ACTOR_RE.fullmatch(actor):
        raise ReachError(BAD_INPUT, "handle must look like name.bsky.social (or a did:plc: identifier)")
    return actor


def _post(post: dict, text: str = "") -> dict:
    author = post.get("author") or {}
    record = post.get("record") or {}
    handle = author.get("handle") or author.get("did") or ""
    rkey = str(post.get("uri") or "").rsplit("/", 1)[-1]
    return make_item(
        title=None, url=f"https://bsky.app/profile/{handle}/post/{rkey}" if handle and rkey else None,
        author=handle, published_at=record.get("createdAt"), text=text or record.get("text"),
        raw={"uri": post.get("uri"), "display_name": author.get("displayName"),
             **{k: post.get(k) for k in ("replyCount", "repostCount", "likeCount", "quoteCount")}})


def _replies(node: dict, depth: int, out: list) -> None:
    if depth > 30:
        return
    for reply in node.get("replies") or []:
        post = reply.get("post") if isinstance(reply, dict) else None
        if not post or len(out) >= 80:
            continue
        out.append(f"{'  ' * min(depth, 10)}@{(post.get('author') or {}).get('handle', '')}: {(post.get('record') or {}).get('text', '')}")
        _replies(reply, depth + 1, out)


class BlueskyChannel(Channel):
    name = "bluesky"
    description = "Bluesky 공개 게시물과 프로필"
    purpose = ("개발자·연구자·오픈소스 메인테이너가 올리는 실시간 반응과 공지를 로그인 없이 확인할 때 사용합니다. "
               "X(Twitter)와 비슷한 용도이지만 로그인이 필요 없으므로, 실시간 여론을 볼 때 이 채널을 먼저 시도합니다.")
    level = "default"
    actions = (
        Action("search", "게시물 검색", (Param("query", "검색어"), _LIMIT),
               (("bsky-appview", "_search"),), live_test={"query": "rust", "limit": 2}),
        Action("profile", "사용자 프로필", (Param("handle", "핸들 (예: bsky.app)"),),
               (("bsky-appview", "_profile"),), live_test={"handle": "bsky.app"}),
        Action("posts", "사용자의 최근 게시물", (Param("handle", "핸들 (예: bsky.app)"), _LIMIT),
               (("bsky-appview", "_posts"),)),
        Action("read", "게시물 하나와 답글 스레드", (Param("target", "bsky.app/profile/<핸들>/post/<id> 링크 또는 at:// URI"),),
               (("bsky-appview", "_read"),)),
    )

    def can_handle(self, url: str) -> bool:
        return host_matches(url, "bsky.app")

    def _search(self, *, query: str, limit: int, timeout: float) -> list:
        data = _xrpc(_SEARCH, "app.bsky.feed.searchPosts", timeout, q=query, limit=min(limit, 50))
        return [_post(p) for p in (data.get("posts") or [])[:limit]]

    def _profile(self, *, handle: str, timeout: float) -> list:
        p = _xrpc(_PUBLIC, "app.bsky.actor.getProfile", timeout, actor=_actor(handle))
        if not p.get("did"):
            raise ReachError(NOT_FOUND, "Bluesky profile not found")
        return [make_item(title=p.get("displayName") or p.get("handle"), url=f"https://bsky.app/profile/{p.get('handle')}",
                          author=p.get("handle"), published_at=p.get("createdAt"), text=p.get("description"),
                          raw={k: p.get(k) for k in ("did", "followersCount", "followsCount", "postsCount")})]

    def _posts(self, *, handle: str, limit: int, timeout: float) -> list:
        data = _xrpc(_PUBLIC, "app.bsky.feed.getAuthorFeed", timeout, actor=_actor(handle),
                     limit=min(limit, 50), filter="posts_no_replies")
        return [_post(row["post"]) for row in (data.get("feed") or [])[:limit] if isinstance(row.get("post"), dict)]

    def _read(self, *, target: str, timeout: float) -> list:
        t = str(target).strip()
        if t.startswith("at://"):
            m = re.fullmatch(r"at://([^/]+)/app\.bsky\.feed\.post/([^/]+)", t)
        else:
            m = re.fullmatch(r"/profile/([^/]+)/post/([^/]+)/?", urlsplit(t).path) if self.can_handle(t) else None
        if not m or not _RKEY_RE.fullmatch(m.group(2)):
            raise ReachError(BAD_INPUT, "target must be a bsky.app post link or an at:// post URI")
        uri = f"at://{_actor(m.group(1))}/app.bsky.feed.post/{m.group(2)}"
        thread = _xrpc(_PUBLIC, "app.bsky.feed.getPostThread", timeout, uri=uri, depth=4, parentHeight=0).get("thread") or {}
        post = thread.get("post")
        if not isinstance(post, dict):
            raise ReachError(NOT_FOUND, "Bluesky post not found")
        lines: list = []
        _replies(thread, 0, lines)
        body = (post.get("record") or {}).get("text") or ""
        return [_post(post, "\n\n".join(x for x in (body, "\n".join(lines)) if x))]
