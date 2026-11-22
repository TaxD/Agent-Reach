# -*- coding: utf-8 -*-
"""GitHub Trending — parsed from the public trending page (there is no official API)."""

import html
import re
from urllib.parse import quote

from agent_reach.runtime.result import BAD_INPUT, UPSTREAM_BROKEN, ReachError, make_item

from ._public import get_bytes, strip_html
from .base import Action, Channel, Param

_REPO_RE = re.compile(r'<h2[^>]*>\s*<a[^>]*\bhref="/([\w.-]+)/([\w.-]+)"')
_DESC_RE = re.compile(r'<p class="col-9[^"]*"[^>]*>(.*?)</p>', re.S)
_LANG_RE = re.compile(r'itemprop="programmingLanguage"[^>]*>([^<]*)<')
_STARS_RE = re.compile(r'href="/[\w.-]+/[\w.-]+/stargazers"[^>]*>(?:\s*<svg.*?</svg>)?\s*([\d,]+)', re.S)
_PERIOD_RE = re.compile(r"([\d,]+)\s+stars?\s+(today|this week|this month)")


def _number(text) -> int | None:
    digits = str(text or "").replace(",", "")
    return int(digits) if digits.isdigit() else None


def parse_trending(page: str) -> list:
    items = []
    for block in page.split('<article class="Box-row">')[1:]:
        repo = _REPO_RE.search(block)
        if not repo:
            continue
        name = f"{repo.group(1)}/{repo.group(2)}"
        desc, lang, stars, period = (r.search(block) for r in (_DESC_RE, _LANG_RE, _STARS_RE, _PERIOD_RE))
        items.append(make_item(
            title=name, url=f"https://github.com/{name}", author=repo.group(1),
            text=strip_html(desc.group(1)) if desc else None,
            raw={"language": html.unescape(lang.group(1)).strip() if lang else None,
                 "stars": _number(stars.group(1)) if stars else None,
                 "stars_in_period": _number(period.group(1)) if period else None,
                 "period": period.group(2) if period else None}))
    return items


class GitHubTrendingChannel(Channel):
    name = "github-trending"
    description = "GitHub 트렌딩 저장소"
    purpose = ("지금 개발자들 사이에서 빠르게 주목받는 오픈소스 저장소를 언어별·기간별로 파악할 때 사용합니다. "
               "특정 저장소의 내용을 읽거나 저장소를 키워드로 검색하는 작업은 github 채널을 사용합니다.")
    level = "default"
    actions = (
        Action("list", "트렌딩 저장소 목록",
               (Param("language", "언어로 좁히기 (예: python, rust, typescript)", required=False),
                Param("since", "집계 기간: daily | weekly | monthly", required=False, default="daily"),
                Param("limit", "최대 결과 수", required=False, type=int, default=10)),
               (("github-html", "_list"),), live_test={"limit": 2}),
    )

    def can_handle(self, url: str) -> bool:
        return False  # github.com links belong to the github channel

    def _list(self, *, language, since: str, limit: int, timeout: float) -> list:
        if since not in ("daily", "weekly", "monthly"):
            raise ReachError(BAD_INPUT, "since must be daily, weekly or monthly")
        if language and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+#. -]{0,39}", str(language)):
            raise ReachError(BAD_INPUT, "language may contain only letters, digits, space and + # . -")
        slug = "/" + quote(str(language).strip().lower().replace(" ", "-"), safe="") if language else ""
        page = get_bytes(f"https://github.com/trending{slug}?since={since}", timeout, accept="text/html").decode("utf-8", "replace")
        items = parse_trending(page)
        if not items:
            raise ReachError(UPSTREAM_BROKEN, "no repositories could be read from the GitHub trending page (layout may have changed)")
        return items[:limit]
