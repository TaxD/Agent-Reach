# -*- coding: utf-8 -*-
"""Shared helpers for login-free channels: fixed-host JSON APIs, feeds, guarded fetches.

Everything returned from these helpers is untrusted third-party content. Channels
pass it on as data; nothing here executes, renders or follows it.
"""

from __future__ import annotations

import html
import http.client
import ipaddress
import json
import re
import socket
import ssl
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from urllib.parse import urlsplit

from agent_reach.runtime.result import (
    BAD_INPUT,
    NOT_FOUND,
    UPSTREAM_BROKEN,
    ReachError,
    make_item,
)
from agent_reach.runtime.run import http_request
from agent_reach.utils.url import host_matches, normalize_public_http_url

from .base import Channel

MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_FEED_BYTES = 4 * 1024 * 1024
#: Per-item text cap in feed listings, so one feed cannot flood the agent's context.
MAX_ITEM_CHARS = 3000
_USER_AGENT = "agent-reach (+https://github.com/TaxD/Agent-Reach)"


# ── fixed-host requests ──

def _gunzip(body: bytes, max_bytes: int) -> bytes:
    """Bounded gzip decode (Stack Exchange always compresses); refuses decompression bombs."""
    if body[:2] != b"\x1f\x8b":
        return body
    decoder = zlib.decompressobj(31)
    out = decoder.decompress(body, max_bytes + 1)
    if len(out) > max_bytes or decoder.unconsumed_tail:
        raise ReachError(UPSTREAM_BROKEN, f"response exceeds {max_bytes} bytes after decompression")
    return out


def get_bytes(url: str, timeout: float, *, max_bytes: int = MAX_JSON_BYTES, accept: str = "*/*") -> bytes:
    """GET a URL on a host the channel hard-codes. Never pass caller-chosen hosts here."""
    if not url.startswith("https://"):
        raise ReachError(BAD_INPUT, "only HTTPS endpoints are allowed")
    if not urllib.request.getproxies():
        # Direct connection: also pin every hop to public addresses, so an open
        # redirect on a trusted site cannot point this machine at its own network.
        return fetch_public(url, timeout, max_bytes=max_bytes, accept=accept)
    # ponytail: behind a proxy the proxy resolves names, so hops cannot be pinned here.
    body = http_request(url, timeout=timeout, headers={"Accept": accept, "User-Agent": _USER_AGENT},
                        max_bytes=max_bytes)
    return _gunzip(body, max_bytes)


def get_json(url: str, timeout: float) -> Any:
    try:
        return json.loads(get_bytes(url, timeout, accept="application/json").decode("utf-8", errors="replace"))
    except ValueError:
        raise ReachError(UPSTREAM_BROKEN, "the site did not return valid JSON") from None


# ── text ──

_BLOCK_END_RE = re.compile(r"(?i)<br\s*/?>|</(?:p|li|div|h[1-6]|tr|pre|blockquote)>")
_SCRIPT_RE = re.compile(r"(?is)<(script|style)\b[^>]*>.*?</\1\s*>")
_TAG_RE = re.compile(r"<[^>]*>")


def strip_html(text: object) -> str:
    """Plain text from an HTML fragment. Output is still untrusted content, just without markup."""
    s = _SCRIPT_RE.sub(" ", str(text or ""))
    s = _BLOCK_END_RE.sub("\n", s)
    s = html.unescape(_TAG_RE.sub("", s)).replace("]]>", "")
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def clip(text: Optional[str], limit: int = MAX_ITEM_CHARS) -> Optional[str]:
    if text and len(text) > limit:
        return text[:limit].rstrip() + " …(truncated)"
    return text


# ── feeds (RSS 2.0 / Atom / RDF) ──

def _local(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _iso_date(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError, IndexError):
        return value[:64]


def parse_feed(body: bytes, limit: int, *, prefer_summary: bool = False) -> list:
    """Parse a feed into unified items.

    DTDs and entity declarations are refused outright: feeds do not need them and
    they are the vehicle for entity-expansion and external-entity attacks.
    """
    head = body[:4096]
    if b"\x00" in head or body[:2] in (b"\xff\xfe", b"\xfe\xff"):
        raise ReachError(UPSTREAM_BROKEN, "feed is not UTF-8/ASCII-compatible XML")
    # A DTD (and the entities it declares) can only precede the root element.
    root_at = re.search(rb"<[A-Za-z_]", body)
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", body[:root_at.start() if root_at else len(body)], re.IGNORECASE):
        raise ReachError(UPSTREAM_BROKEN, "feed declares a DTD or entities; refused")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise ReachError(UPSTREAM_BROKEN, "not a valid RSS/Atom feed") from None
    items = []
    for entry in root.iter():
        if _local(entry.tag) not in ("item", "entry"):
            continue
        fields: dict = {}
        link = None
        for child in entry:
            name = _local(child.tag)
            if name == "link":
                href = child.get("href")
                if href is None:
                    link = link or (child.text or "").strip()
                elif child.get("rel") in (None, "alternate") or link is None:
                    link = href.strip()
                continue
            fields.setdefault(name, child)

        def text_of(*names: str) -> Optional[str]:
            for key in names:
                node = fields.get(key)
                if node is None:
                    continue
                value = (node.text or "").strip()
                if not value and key == "author":  # Atom: <author><name>…</name></author>
                    value = next(((c.text or "").strip() for c in node if _local(c.tag) == "name"), "")
                if value:
                    return value
            return None

        order = ("summary", "description", "encoded", "content") if prefer_summary else (
            "encoded", "content", "summary", "description")
        guid = text_of("guid", "id")
        if not link and guid and guid.startswith(("http://", "https://")):
            link = guid
        items.append(make_item(
            title=strip_html(text_of("title")) or None,
            url=link if link and link.startswith(("http://", "https://")) else None,
            author=text_of("author", "creator"),
            published_at=_iso_date(text_of("published", "pubdate", "updated", "date")),
            text=clip(strip_html(text_of(*order))),
            raw={"id": guid},
        ))
        if len(items) >= limit:
            break
    if not items:
        raise ReachError(NOT_FOUND, "the feed has no entries")
    return items


def feed_items(url: str, limit: int, timeout: float, *, prefer_summary: bool = False) -> list:
    """Fetch and parse a feed on a host the channel hard-codes."""
    return parse_feed(get_bytes(url, timeout, max_bytes=MAX_FEED_BYTES,
                                accept="application/atom+xml, application/rss+xml, application/xml, text/xml"),
                      limit, prefer_summary=prefer_summary)


# ── guarded fetch for caller-chosen URLs (generic RSS) ──

_PUBLIC_PORTS = (None, 80, 443)


def _public_url(url: str) -> str:
    try:
        clean = normalize_public_http_url(url)
        port = urlsplit(clean).port
    except ValueError:
        raise ReachError(BAD_INPUT, "only public http(s) URLs are allowed") from None
    if port not in _PUBLIC_PORTS:
        raise ReachError(BAD_INPUT, "only the default ports 80 and 443 are allowed")
    return clean


def _connect_public(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, **_kwargs):  # type: ignore[attr-defined]
    """socket.create_connection that connects only to globally routable addresses.

    The address that is checked is the address that is connected to, so a DNS
    answer cannot change between the check and the request (DNS rebinding).
    Every resolved address must be public; a mixed answer is refused.
    """
    host, port = address
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    for *_, sockaddr in infos:
        ip = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
        mapped = getattr(ip, "ipv4_mapped", None)
        if not (mapped or ip).is_global:
            raise ReachError(BAD_INPUT, "the address resolves to a non-public network; refused")
    last: Optional[OSError] = None
    for family, kind, proto, _, sockaddr in infos:
        sock = socket.socket(family, kind, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:  # type: ignore[attr-defined]
                sock.settimeout(timeout)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last = exc
            sock.close()
    raise last or OSError("no address to connect to")


class _PublicHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_public


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_public


class _PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PublicHTTPConnection, req)


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHTTPSConnection, req, context=ssl.create_default_context())


class _PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _public_url(newurl)  # every hop is re-validated; non-http(s) schemes fail here
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_FEED_ACCEPT = "application/atom+xml, application/rss+xml, application/xml, text/xml"


def fetch_public(url: str, timeout: float, *, max_bytes: int = MAX_FEED_BYTES, accept: str = _FEED_ACCEPT) -> bytes:
    """GET a caller-chosen URL without reaching loopback, private or link-local networks.

    ponytail: connects directly and ignores a configured proxy, because address
    pinning is impossible through a proxy that resolves names itself. Add a
    proxy-aware path only if users behind a mandatory proxy need generic feeds.
    """
    clean = _public_url(url)
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.ProxyHandler({}), _PublicHTTPHandler(), _PublicHTTPSHandler(),
                    _PublicRedirectHandler(), urllib.request.HTTPDefaultErrorHandler(),
                    urllib.request.HTTPErrorProcessor()):
        opener.add_handler(handler)
    req = urllib.request.Request(clean, headers={"User-Agent": _USER_AGENT, "Accept": accept})
    with opener.open(req, timeout=max(timeout, 0.1)) as resp:
        body = resp.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ReachError(UPSTREAM_BROKEN, f"response exceeds {max_bytes} bytes")
    return _gunzip(body, max_bytes)


# ── sites without a public API: scoped web search and page reading ──

def site_search(domains: tuple, query: str, limit: int, timeout: float) -> list:
    """Search one site through the Web channel and keep only results on that site."""
    from .web import WebChannel

    scope = " OR ".join("site:" + d for d in domains)
    found = WebChannel()._exa_search(query=f"({query}) ({scope})", limit=min(limit * 2, 20), timeout=timeout)
    items = [i for i in found if host_matches(str(i.get("url") or ""), *domains)][:limit]
    if not items:
        raise ReachError(NOT_FOUND, "no results on this site")
    return items


class WebReadMixin(Channel):
    """`read` for a site with no public API: its own pages only, through the Web channel readers."""

    READ_BACKENDS = (("jina", "_jina_read"), ("exa", "_exa_read"))

    def _own_url(self, target: str) -> str:
        url = str(target).strip()
        url = url if "://" in url else "https://" + url
        if not self.can_handle(url):
            raise ReachError(BAD_INPUT, f"target must be a {self.name} page link")
        return url

    def _jina_read(self, *, target: str, timeout: float) -> list:
        from .web import WebChannel

        return WebChannel()._jina_read(target=self._own_url(target), timeout=timeout)

    def _exa_read(self, *, target: str, timeout: float) -> list:
        from .web import WebChannel

        return WebChannel()._exa_read(target=self._own_url(target), timeout=timeout)
