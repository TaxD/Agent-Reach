"""Public-first queries reuse WebChannel; no browser or site extraction rules."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from agent_reach.channels.base import Action, Channel
from agent_reach.utils.url import host_matches, normalize_public_http_url

from .result import UPSTREAM_BROKEN

BACKEND = "public-web"
MAX_SECONDS = 20.0


def _in_scope(url: str, scopes: tuple[str, ...]) -> bool:
    try:
        url = normalize_public_http_url(url)
        path = urlsplit(url).path
    except ValueError:
        return False
    for scope in scopes:
        host, _, prefix = scope.partition("/")
        if host_matches(url, host) and (not prefix or path.startswith("/" + prefix)):
            return True
    return False


def _readable(item: dict) -> bool:
    """Reject empty shells and clear login/challenge pages, not inline login links.

    ponytail: this is a conservative text check, not proof of a complete article.
    The response always says page_text with completeness unverified.
    """
    text = str(item.get("text") or "").strip()
    title = str(item.get("title") or "").strip()
    if not text or (len(text) < 80 and text.casefold() == title.casefold()):
        return False
    wall = r"log\s*in|sign\s*in|join\s+now|로그인|just a moment|prove your humanity|access denied|security verification"
    if re.fullmatch(rf"(?:{wall})(?:\s*[-|·:].*)?", title, re.I):
        return False
    login_only = r"(?:(?:please\s+)?(?:log\s*in|sign\s*in)(?:\s+to\s+continue)?[.!?\s]*)+"
    return re.fullmatch(login_only, text, re.I) is None


def attempt(channel: Channel, action: Action, params: dict, *, timeout: float) -> dict | None:
    """Return a scoped Web result, or None when this input has no public route."""
    if not action.public_web:
        return None
    from agent_reach.channels.web import WebChannel

    from .dispatch import run_action

    in_scope = lambda url: _in_scope(url, action.public_web)  # noqa: E731
    if action.name in {"search", "jobs"}:
        query = " ".join(str(params[k]) for k in ("query", "location", "city") if params.get(k))
        scope = " OR ".join("site:" + url for url in action.public_web)
        web_action = "search"
        web_params = {"query": f"({query}) ({scope})", "limit": params.get("limit", 5)}
    elif action.name == "read" and _in_scope(str(params.get("target", "")), action.public_web):
        web_action = "read"
        web_params = {"target": params["target"]}
    elif action.name == "subreddit" and re.fullmatch(r"(?:r/)?\w{2,21}", str(params.get("name", "")), re.A):
        # Reddit refuses its listing endpoints on some networks even when logged in.
        name = str(params["name"]).removeprefix("r/")
        prefix = f"/r/{name}/".casefold()
        web_action = "search"
        web_params = {"query": f"r/{name} (site:reddit.com/r/{name})", "limit": params.get("limit", 5)}
        in_scope = lambda url: _in_scope(url, action.public_web) and urlsplit(url).path.casefold().startswith(prefix)  # noqa: E731
    else:
        return None

    out = run_action(WebChannel(), web_action, web_params, timeout=timeout)
    out["channel"], out["action"] = channel.name, action.name
    if out["ok"]:
        items = [item for item in out["items"] if in_scope(str(item.get("url") or ""))]
        if web_action == "read":
            items = [item for item in items if _readable(item)]
        if not items:
            out.update(ok=False, backend=None, items=[], error={"code": UPSTREAM_BROKEN,
                       "message": "Public web returned no usable content in the requested scope",
                       "hint": "Try the site tool; this does not establish a login requirement."})
            out["meta"]["attempts"].append({"backend": "content-check", "result": "INSUFFICIENT_CONTENT"})
        else:
            scope = "search_results" if web_action == "search" else "page_text"
            for item in items:
                item["raw"] = {"source": item.get("raw"), "content_scope": scope,
                               "access": "public", "completeness_verified": False}
            out.update(backend=BACKEND, items=items)
            out["meta"]["access"] = {"mode": "public", "scope": scope, "completeness_verified": False,
                "note": "공개 웹 결과입니다. 사이트 내부 정렬, 최신 상태, 전문, 전체 댓글은 보장되지 않습니다. 사이트 내부 데이터가 필요하면 --backend를 지정하십시오."}
    # Provider 429/errors belong to Web, never to the destination platform.
    out["meta"]["attempts"] = [{**row, "backend": BACKEND + "/" + row["backend"]}
                                for row in out["meta"]["attempts"]]
    return out
