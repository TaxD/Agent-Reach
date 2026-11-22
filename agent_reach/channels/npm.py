# -*- coding: utf-8 -*-
"""npm — package search and metadata through the public registry API."""

import re
from urllib.parse import quote, urlencode, urlsplit

from agent_reach.runtime.result import BAD_INPUT, NOT_FOUND, ReachError, make_item
from agent_reach.utils.url import host_matches

from ._public import get_json
from .base import Action, Channel, Param

_REGISTRY = "https://registry.npmjs.org"
_NAME_RE = re.compile(r"(?:@[a-z0-9~][a-z0-9._~-]*/)?[a-z0-9~][a-z0-9._~-]*")


def _package_name(target: str) -> str:
    t = str(target).strip()
    if "://" in t:
        if not host_matches(t, "npmjs.com"):
            raise ReachError(BAD_INPUT, "target must be a package name or an npmjs.com/package link")
        m = re.match(r"/package/((?:@[^/]+/)?[^/]+)", urlsplit(t).path)
        t = m.group(1) if m else ""
    if len(t) > 214 or not _NAME_RE.fullmatch(t):
        raise ReachError(BAD_INPUT, "invalid npm package name")
    return t


class NpmChannel(Channel):
    name = "npm"
    description = "npm 패키지 검색과 메타데이터"
    purpose = ("JavaScript/TypeScript 라이브러리를 고르거나, 패키지의 최신 버전·의존성·라이선스·주간 다운로드 수·"
               "저장소 주소를 확인할 때 사용합니다. 패키지의 소스나 이슈는 반환된 저장소 주소로 github 채널에서 읽습니다.")
    level = "default"
    actions = (
        Action("search", "패키지 검색",
               (Param("query", "검색어"), Param("limit", "최대 결과 수", required=False, type=int, default=5)),
               (("npm-registry", "_search"),), live_test={"query": "react", "limit": 2}),
        Action("read", "패키지 최신 버전의 메타데이터와 주간 다운로드 수",
               (Param("target", "패키지 이름 (예: react, @types/node) 또는 npmjs.com/package 링크"),),
               (("npm-registry", "_read"),), live_test={"target": "react"}),
    )

    def can_handle(self, url: str) -> bool:
        return host_matches(url, "npmjs.com")

    def _search(self, *, query: str, limit: int, timeout: float) -> list:
        data = get_json(f"{_REGISTRY}/-/v1/search?" + urlencode({"text": query, "size": min(limit, 50)}), timeout)
        items = []
        for obj in (data.get("objects") or [])[:limit]:
            p = obj.get("package") or {}
            items.append(make_item(
                title=p.get("name"), url=(p.get("links") or {}).get("npm"),
                author=(p.get("publisher") or {}).get("username"), published_at=p.get("date"),
                text=p.get("description"),
                raw={"version": p.get("version"), "keywords": p.get("keywords"), "links": p.get("links"),
                     "downloads": obj.get("downloads")}))
        return items

    def _read(self, *, target: str, timeout: float) -> list:
        name = _package_name(target)
        data = get_json(f"{_REGISTRY}/{quote(name, safe='@')}/latest", timeout * 0.6)
        if not isinstance(data, dict) or not data.get("name"):
            raise ReachError(NOT_FOUND, f"npm package not found: {name}")
        try:
            weekly = get_json(f"https://api.npmjs.org/downloads/point/last-week/{quote(name, safe='@/')}", timeout * 0.3).get("downloads")
        except Exception:  # noqa: BLE001 - download counts are optional detail
            weekly = None
        repo = data.get("repository")
        repo_url = repo.get("url") if isinstance(repo, dict) else repo
        lines = [str(data.get("description") or ""), f"version: {data.get('version')}", f"license: {data.get('license')}",
                 f"repository: {repo_url}", f"homepage: {data.get('homepage')}", f"weekly downloads: {weekly}",
                 "dependencies: " + (", ".join(sorted(data.get("dependencies") or {})) or "none")]
        author = data.get("author")
        return [make_item(
            title=data.get("name"), url=f"https://www.npmjs.com/package/{name}",
            author=author.get("name") if isinstance(author, dict) else author, text="\n".join(lines),
            raw={"version": data.get("version"), "license": data.get("license"), "repository": repo_url,
                 "weekly_downloads": weekly, "dependencies": data.get("dependencies"),
                 "peerDependencies": data.get("peerDependencies"), "engines": data.get("engines"),
                 "deprecated": data.get("deprecated")})]
