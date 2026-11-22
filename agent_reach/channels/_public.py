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
    RATE_LIMITED,
    UPSTREAM_BROKEN,
    ReachError,
    make_item,
)
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
    return fetch_public(url, timeout, max_bytes=max_bytes, accept=accept, allow_proxy=True)


def get_json(url: str, timeout: float) -> Any:
    try:
        return json.loads(get_bytes(url, timeout, accept="application/json").decode("utf-8", errors="replace"))
    except ValueError:
        raise ReachError(UPSTREAM_BROKEN, "the site did not return valid JSON") from None


# ── text ──

_BLOCK_TAGS = frozenset(("br", "/p", "/li", "/div", "/h1", "/h2", "/h3", "/h4", "/h5", "/h6", "/tr", "/pre", "/blockquote"))
_MAX_HTML_CHARS = 400_000


def strip_html(text: object) -> str:
    """Plain text from an HTML fragment, in one linear pass (no regex backtracking).

    Output is still untrusted content, just without markup.
    """
    s = str(text or "")[:_MAX_HTML_CHARS]
    low = s.lower()
    out: list = []
    i = 0
    while True:
        lt = s.find("<", i)
        if lt < 0:
            out.append(s[i:])
            break
        out.append(s[i:lt])
        gt = s.find(">", lt + 1)
        if gt < 0:
            out.append(s[lt:])  # no closing ">" anywhere after: plain text, and we are done
            break
        name = s[lt + 1:gt].strip().split(" ", 1)[0].rstrip("/").lower()
        i = gt + 1
        if name in ("script", "style"):
            end = low.find("</" + name, i)
            close = s.find(">", end) if end >= 0 else -1
            if close < 0:
                break  # unterminated script/style: drop the rest
            i = close + 1
            out.append(" ")
        elif name in _BLOCK_TAGS:
            out.append("\n")
    s = html.unescape("".join(out)).replace("]]>", "")
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return re.sub(r"\n[ \n]*\n", "\n\n", s).strip()


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


_ASCII_COMPATIBLE = (b"utf-8", b"utf8", b"us-ascii", b"ascii", b"iso-8859-1", b"latin-1", b"windows-1252")
_PROLOG_RE = re.compile(rb"\s*(?:<\?.*?\?>|<!--.*?-->)?\s*", re.S)


def parse_feed(body: bytes, limit: int, *, prefer_summary: bool = False) -> list:
    """Parse a feed into unified items.

    DTDs and entity declarations are refused outright: feeds do not need them and
    they are the vehicle for entity-expansion and external-entity attacks.
    """
    head = body[:4096]
    if b"\x00" in head or body[:2] in (b"\xff\xfe", b"\xfe\xff"):
        raise ReachError(UPSTREAM_BROKEN, "feed is not UTF-8/ASCII-compatible XML")
    start = body.removeprefix(b"\xef\xbb\xbf").lstrip()
    if start[:5].lower() == b"<?xml":
        end = start.find(b"?>", 0, 4096)
        if end < 0:
            raise ReachError(UPSTREAM_BROKEN, "feed has a malformed XML declaration; refused")
        declared = re.search(rb"encoding\s*=\s*[\"']([^\"']*)[\"']", start[:end])
    else:
        declared = None
    if declared and declared.group(1).strip().lower() not in _ASCII_COMPATIBLE:
        # The checks below read bytes; an EBCDIC-style encoding would hide markup from them.
        raise ReachError(UPSTREAM_BROKEN, "feed uses an unsupported text encoding; refused")
    # Walk the prolog properly (XML declaration, comments, processing instructions).
    # The first other construct must be the root element; "<!" there is a DTD.
    pos = 3 if body[:3] == b"\xef\xbb\xbf" else 0
    while True:
        skipped = _PROLOG_RE.match(body, pos)
        if not skipped or skipped.end() == pos:
            break
        pos = skipped.end()
    if body[pos:pos + 2] == b"<!" or not re.match(rb"<[A-Za-z_]", body[pos:pos + 2]):
        raise ReachError(UPSTREAM_BROKEN, "feed declares a DTD or is not XML; refused")
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
            title=clip(strip_html(text_of("title")), 300) or None,
            url=link if link and len(link) <= 2000 and link.startswith(("http://", "https://")) else None,
            author=clip(text_of("author", "creator"), 100),
            published_at=_iso_date(text_of("published", "pubdate", "updated", "date")),
            text=clip(strip_html(text_of(*order))),
            raw={"id": clip(guid, 300)},
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


#: IPv6 ranges that embed or translate an IPv4 address (NAT64, 6to4, Teredo) or are
#: deprecated site-local space; `is_global` alone lets some of them through.
_V6_REFUSED = tuple(ipaddress.ip_network(n) for n in (
    "64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32", "fec0::/10"))


def is_public_unicast(address: str) -> bool:
    """Only ordinary, globally routable unicast addresses count as public."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if (not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified
            or ip.is_loopback or ip.is_link_local or ip.is_private):
        return False
    return not (ip.version == 6 and any(ip in net for net in _V6_REFUSED))


def _connect_public(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, **_kwargs):  # type: ignore[attr-defined]
    """socket.create_connection that connects only to globally routable addresses.

    The address that is checked is the address that is connected to, so a DNS
    answer cannot change between the check and the request (DNS rebinding).
    Every resolved address must be public; a mixed answer is refused.
    """
    host, port = address
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    for *_, sockaddr in infos:
        if not is_public_unicast(str(sockaddr[0])):
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


def _proxied(req) -> bool:
    """urllib decided to send this exact request through a proxy (set by its ProxyHandler)."""
    return bool(getattr(req, "_tunnel_host", None)) or req.has_proxy()


class _PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        # A hop that goes direct is always pinned to public addresses. Only a hop urllib
        # itself routed to the proxy uses an ordinary connection (to the proxy).
        return self.do_open(http.client.HTTPConnection if _proxied(req) else _PublicHTTPConnection, req)


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(http.client.HTTPSConnection if _proxied(req) else _PublicHTTPSConnection, req,
                            context=ssl.create_default_context())


class _PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def __init__(self, allow_proxy: bool = False):
        self._allow_proxy = allow_proxy

    def _next_hop_proxied(self, newurl: str) -> bool:
        """urllib would route the redirect target through the proxy (same test its ProxyHandler applies)."""
        if not self._allow_proxy:
            return False
        parts = urlsplit(newurl)
        return bool(urllib.request.getproxies().get(parts.scheme)) and not urllib.request.proxy_bypass(parts.netloc)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib would read the whole redirect body right after this call. Close it first
        # (read() on a closed response is empty) so a huge 3xx body cannot fill memory.
        fp.close()
        _public_url(newurl)  # every hop is re-validated; non-http(s) schemes fail here
        if _proxied(req) or self._next_hop_proxied(newurl):
            # A hop through the proxy cannot be pinned, and urllib would carry the proxy
            # credential along. So a redirect that starts from, or leads into, the proxy
            # may only stay within its own origin.
            old, new = urlsplit(req.full_url), urlsplit(newurl)
            if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
                raise ReachError(UPSTREAM_BROKEN, "a proxied request was redirected to another host; refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_FEED_ACCEPT = "application/atom+xml, application/rss+xml, application/xml, text/xml"


def fetch_public(url: str, timeout: float, *, max_bytes: int = MAX_FEED_BYTES, accept: str = _FEED_ACCEPT,
                 allow_proxy: bool = False) -> bytes:
    """GET a URL without reaching loopback, private or link-local networks.

    Every hop that connects directly is pinned to the public address that was checked.
    With `allow_proxy` (fixed-host channels) the configured proxy is honoured exactly as
    urllib decides per hop; such a hop cannot be pinned, so it may not leave its origin.
    Caller-chosen URLs (`rss fetch`) never use a proxy.
    """
    clean = _public_url(url)
    opener = urllib.request.OpenerDirector()
    proxy = urllib.request.ProxyHandler() if allow_proxy else urllib.request.ProxyHandler({})
    for handler in (proxy, _PublicHTTPHandler(), _PublicHTTPSHandler(), _PublicRedirectHandler(allow_proxy),
                    urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor()):
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
    try:
        found = WebChannel()._exa_search(query=f"({query}) ({scope})", limit=min(limit * 2, 20), timeout=timeout)
    except ReachError as exc:
        if exc.code != RATE_LIMITED:
            raise
        # The search provider's limit must not cool this whole channel down for an hour.
        raise ReachError(UPSTREAM_BROKEN, "the web search provider is rate limited; try again later") from None
    items = [i for i in found if host_matches(str(i.get("url") or ""), *domains)][:limit]
    if not items:
        raise ReachError(NOT_FOUND, "no results on this site")
    return items


class WebReadMixin(Channel):
    """`read` for a site with no public API: its own pages only, through the Web channel readers."""

    READ_BACKENDS = (("jina", "_jina_read"), ("exa", "_exa_read"))

    @staticmethod
    def _usable(items: list) -> list:
        """A block or challenge page is a failure, so the next reader gets its turn."""
        from .web import is_block_page

        first = items[0] if items else {}
        text = str(first.get("text") or "")
        if not text.strip() or is_block_page(first.get("title"), text):
            raise ReachError(UPSTREAM_BROKEN, "the site returned a block or verification page instead of the content")
        return items

    def _own_url(self, target: str) -> str:
        url = str(target).strip()
        url = url if "://" in url else "https://" + url
        if not self.can_handle(url):
            raise ReachError(BAD_INPUT, f"target must be a {self.name} page link")
        return url

    def _jina_read(self, *, target: str, timeout: float) -> list:
        from .web import WebChannel

        return self._usable(WebChannel()._jina_read(target=self._own_url(target), timeout=timeout))

    def _exa_read(self, *, target: str, timeout: float) -> list:
        from .web import WebChannel

        return self._usable(WebChannel()._exa_read(target=self._own_url(target), timeout=timeout))
