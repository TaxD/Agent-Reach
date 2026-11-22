"""Offline checks for the login-free channels: parsing, input validation, SSRF and XML guards."""

import gzip
import socket

import pytest

from agent_reach.channels import _public, get_all_channels
from agent_reach.channels.bluesky import BlueskyChannel
from agent_reach.channels.devto import DevToChannel
from agent_reach.channels.feed_sites import MediumChannel
from agent_reach.channels.github_trending import GitHubTrendingChannel, parse_trending
from agent_reach.channels.hackernews import HackerNewsChannel
from agent_reach.channels.lobsters import LobstersChannel
from agent_reach.channels.mdn import MDNChannel
from agent_reach.channels.npm import _package_name
from agent_reach.channels.rss import RSSChannel
from agent_reach.channels.stackoverflow import StackOverflowChannel
from agent_reach.runtime.result import BAD_INPUT, UPSTREAM_BROKEN, ReachError

ATOM = b"""<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns='http://www.w3.org/2005/Atom'>
<entry><title><![CDATA[Hello <b>world</b>]]></title>
<link rel='alternate' href='https://example.org/a?x=1&amp;y=2'/><id>https://example.org/a</id>
<published>2026-11-22T13:33:58+09:00</published><author><name>neo</name></author>
<content type='html'><![CDATA[<p>one</p><script>alert(1)</script><p>two &amp; three</p><!DOCTYPE inside text>]]></content></entry>
</feed>"""

RSS = b"""<?xml version="1.0"?><rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>
<item><title>T1</title><link>https://example.org/1</link><dc:creator>kim</dc:creator>
<pubDate>Fri, 20 Nov 2026 19:35:05 +0000</pubDate><description>summary</description></item>
<item><title>T2</title><link>javascript:alert(1)</link><description>x</description></item>
</channel></rss>"""


def test_every_channel_explains_when_to_use_it():
    channels = get_all_channels()
    assert len({c.name for c in channels}) == len(channels)
    for ch in channels:
        assert len(ch.purpose) > 40, ch.name
        assert ch.description and ch.level in {"default", "optional", "experimental"}, ch.name
        for action in ch.actions:
            assert action.backends and all(hasattr(ch, method) for _, method in action.backends), (ch.name, action.name)


def test_atom_feed_is_parsed_to_plain_text_items():
    (item,) = _public.parse_feed(ATOM, 5)
    assert item["title"] == "Hello world"
    assert item["url"] == "https://example.org/a?x=1&y=2"
    assert item["author"] == "neo" and item["published_at"].startswith("2026-11-22")
    assert "alert" not in item["text"] and "one" in item["text"] and "two & three" in item["text"]


def test_rss_feed_drops_non_http_links_and_honours_limit():
    first, second = _public.parse_feed(RSS, 5)
    assert (first["title"], first["author"], first["url"]) == ("T1", "kim", "https://example.org/1")
    assert first["published_at"].startswith("2026-11-20")
    assert second["url"] is None
    assert len(_public.parse_feed(RSS, 1)) == 1


@pytest.mark.parametrize("payload", [
    b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><rss><channel><item><title>&b;</title></item></channel></rss>',
    b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel><item><title>&x;</title></item></channel></rss>',
    b"<!doctype html><html><body>not a feed</body></html>",
    "<rss/>".encode("utf-16"),
])
def test_feeds_with_dtd_entities_or_odd_encodings_are_refused(payload):
    with pytest.raises(ReachError) as err:
        _public.parse_feed(payload, 5)
    assert err.value.code == UPSTREAM_BROKEN


def test_item_text_is_capped():
    assert len(_public.clip("x" * 10_000)) < _public.MAX_ITEM_CHARS + 30


def test_gzip_bomb_is_refused():
    bomb = gzip.compress(b"0" * (2 * 1024 * 1024))
    with pytest.raises(ReachError):
        _public._gunzip(bomb, 1024 * 1024)
    assert _public._gunzip(gzip.compress(b"ok"), 1024) == b"ok"


def test_fixed_host_helper_refuses_plain_http():
    with pytest.raises(ReachError) as err:
        _public.get_bytes("http://hn.algolia.com/api/v1/search", 1)
    assert err.value.code == BAD_INPUT


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:9222/json", "http://localhost/feed", "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/feed", "http://[::1]/feed", "http://192.168.0.1/rss", "file:///etc/passwd",
    "ftp://example.org/feed", "https://user:pw@example.org/feed", "https://example.org:8443/feed",
    "http://2130706433/", "http://0x7f.1/", "gopher://example.org/", "https://intranet/feed",
])
def test_guarded_fetch_refuses_non_public_targets_before_any_connection(url, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("must not resolve"))
    with pytest.raises(ReachError) as err:
        _public.fetch_public(url, 1)
    assert err.value.code == BAD_INPUT


@pytest.mark.parametrize("answer", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "fd00::1", "::ffff:127.0.0.1", "0.0.0.0",
                                    "224.0.0.1", "ff02::1", "fec0::1", "64:ff9b::7f00:1", "2002:7f00:1::", "100.64.0.1", "240.0.0.1"])
def test_guarded_connect_refuses_names_that_resolve_to_private_addresses(answer, monkeypatch):
    family = socket.AF_INET6 if ":" in answer else socket.AF_INET
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(family, socket.SOCK_STREAM, 6, "", (answer, 80))])
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("must not open a socket"))
    with pytest.raises(ReachError) as err:
        _public._connect_public(("rebind.example.org", 80))
    assert err.value.code == BAD_INPUT


def test_guarded_connect_refuses_mixed_public_and_private_answers(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))])
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("must not open a socket"))
    with pytest.raises(ReachError):
        _public._connect_public(("mixed.example.org", 80))


class _Closable:
    closed = False

    def close(self):
        self.closed = True


def test_redirects_are_revalidated():
    handler = _public._PublicRedirectHandler()
    for target in ("http://127.0.0.1/", "file:///etc/passwd", "https://example.org:22/"):
        with pytest.raises(ReachError):
            handler.redirect_request(None, _Closable(), 302, "Found", {}, target)


def test_public_unicast_accepts_ordinary_addresses_only():
    assert _public.is_public_unicast("93.184.216.34") and _public.is_public_unicast("2606:4700:4700::1111")
    assert not _public.is_public_unicast("not-an-ip")


def test_a_bare_no_proxy_list_does_not_disable_address_pinning(monkeypatch):
    import urllib.request

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"no": "localhost,127.0.0.1"})
    assert _public._proxy_configured() is False
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": "http://127.0.0.1:7890"})
    assert _public._proxy_configured() is True


@pytest.mark.parametrize("payload", [
    b'<?xml version="1.0"?><!-- <a --><!DOCTYPE r [<!ENTITY x "y">]><rss><channel><item><title>&x;</title></item></channel></rss>',
    b'<?xml version="1.0"?><?pi <a ?><!DOCTYPE r [<!ENTITY x "y">]><rss><channel><item><title>&x;</title></item></channel></rss>',
    b'<?xml version="1.0" encoding="cp037"?><rss/>',
    b"\xef\xbb\xbf<!DOCTYPE r><rss/>",
])
def test_dtd_cannot_hide_behind_comments_instructions_or_encodings(payload):
    with pytest.raises(ReachError) as err:
        _public.parse_feed(payload, 5)
    assert err.value.code == UPSTREAM_BROKEN


def test_redirect_body_is_never_buffered():
    class Body:
        closed = False

        def close(self):
            self.closed = True

    import urllib.request

    body = Body()
    req = urllib.request.Request("https://example.org/feed")
    _public._PublicRedirectHandler().redirect_request(req, body, 302, "Found", {}, "https://example.org/next")
    assert body.closed


def test_credentialed_requests_refuse_cross_origin_redirects():
    import urllib.request

    from agent_reach.runtime.run import _SameOriginRedirects

    class Body:
        def close(self):
            pass

    handler = _SameOriginRedirects("https://api.github.com/repos/a/b")
    req = urllib.request.Request("https://api.github.com/repos/a/b", headers={"Authorization": "Bearer t"})
    assert handler.redirect_request(req, Body(), 301, "Moved", {}, "https://api.github.com/repositories/1") is not None
    for target in ("https://evil.example/x", "http://api.github.com/x", "https://api.github.com:8443/x"):
        with pytest.raises(ReachError):
            handler.redirect_request(req, Body(), 302, "Found", {}, target)


def test_github_never_returns_private_repositories(monkeypatch):
    import json

    from agent_reach.channels import github
    from agent_reach.runtime.result import NOT_FOUND

    def get(path, timeout, accept=""):
        if path.startswith("/search"):
            assert "is%3Apublic" in path
            return json.dumps({"items": [{"full_name": "me/secret", "private": True},
                                         {"full_name": "me/open", "private": False}]}).encode()
        return json.dumps({"full_name": "me/secret", "private": True}).encode()

    assert [i["title"] for i in github.GitHubChannel._search(get, "x", 5, 3)] == ["me/open"]
    for target in ("me/secret", "https://github.com/me/secret/issues/1"):
        with pytest.raises(ReachError) as err:
            github.GitHubChannel._read(get, target, 3)
        assert err.value.code == NOT_FOUND


def test_ytdlp_ignores_ambient_config_plugins_and_browser_cookies():
    from agent_reach.channels.youtube import ytdlp_base_argv

    argv = ytdlp_base_argv()
    assert argv[1] == "-I"
    assert {"--ignore-config", "--no-plugin-dirs", "--no-cookies-from-browser", "--no-cookies"} <= set(argv)


def test_modified_extension_files_are_not_trusted(tmp_path, monkeypatch):
    from agent_reach.runtime import browser

    monkeypatch.setattr(browser, "extension_dir", lambda: tmp_path / "extension")
    (tmp_path / "extension").mkdir()
    (tmp_path / "extension" / "manifest.json").write_text('{"version": "1.0.27"}')
    assert browser.extension_tree_digest() and not browser.extension_intact()


def test_rss_failures_never_pause_the_channel_for_other_feeds(monkeypatch):
    def boom(url, timeout):
        raise TimeoutError()
    monkeypatch.setattr("agent_reach.channels.rss.fetch_public", boom)
    with pytest.raises(ReachError) as err:
        RSSChannel()._fetch(url="https://example.org/feed", limit=5, timeout=1)
    assert err.value.code == BAD_INPUT


def test_trending_page_is_parsed():
    page = '''<article class="Box-row"> <h2 class="h3 lh-condensed"> <a data-x="1" href="/octo/demo-repo" class="Link"> octo / demo-repo </a></h2>
      <p class="col-9 color-fg-muted my-1 pr-4"> A &amp; B <b>tool</b> </p>
      <span itemprop="programmingLanguage">Python</span>
      <a href="/octo/demo-repo/stargazers" class="Link"> <svg class="octicon"><path d="M8"></path></svg> 12,345</a>
      <span class="d-inline-block float-sm-right"> 678 stars today </span></article>
      <article class="Box-row"><h2><a href="/login?return_to=x">x</a></h2></article>'''
    (item,) = parse_trending(page)
    assert item["title"] == "octo/demo-repo" and item["url"] == "https://github.com/octo/demo-repo"
    assert item["text"] == "A & B tool"
    assert item["raw"] == {"language": "Python", "stars": 12345, "stars_in_period": 678, "period": "today"}


@pytest.mark.parametrize("call", [
    lambda: GitHubTrendingChannel()._list(language="../../settings", since="daily", limit=3, timeout=1),
    lambda: GitHubTrendingChannel()._list(language=None, since="yearly", limit=3, timeout=1),
    lambda: MDNChannel()._read(target="/en-US/docs/../../api/v1/whoami", timeout=1),
    lambda: MDNChannel()._read(target="https://evil.example/en-US/docs/Web", timeout=1),
    lambda: MDNChannel()._search(query="x", limit=1, locale="en-US&size=999", timeout=1),
    lambda: HackerNewsChannel()._read(target="https://evil.example/item?id=1", timeout=1),
    lambda: HackerNewsChannel()._read(target="1/../../x", timeout=1),
    lambda: StackOverflowChannel()._read(target="https://evil.example/questions/1/x", timeout=1),
    lambda: LobstersChannel()._tag(name="rust/../../x", limit=1, timeout=1),
    lambda: LobstersChannel()._read(target="../etc", timeout=1),
    lambda: DevToChannel()._read(target="https://evil.example/a/b", timeout=1),
    lambda: DevToChannel()._top(limit=1, tag="py&top=1", days=7, timeout=1),
    lambda: BlueskyChannel()._profile(handle="x&actor=y", timeout=1),
    lambda: BlueskyChannel()._read(target="https://evil.example/profile/a.b/post/1", timeout=1),
    lambda: MediumChannel()._tag(name="../feed", limit=1, timeout=1),
    lambda: MediumChannel()._user(name="@a/../../b", limit=1, timeout=1),
    lambda: MediumChannel()._jina_read(target="https://medium.com.evil.example/x", timeout=1),
    lambda: MediumChannel()._exa_read(target="https://user@evil.example/medium.com", timeout=1),
    lambda: _package_name("../../-/v1/search"),
    lambda: _package_name("https://evil.example/package/react"),
    lambda: _package_name("React Native"),
    lambda: DevToChannel()._read(target="https://dev.to/../api", timeout=1),
    lambda: GitHubTrendingChannel()._list(language="..", since="daily", limit=1, timeout=1),
    lambda: MediumChannel()._user(name=".", limit=1, timeout=1),
    lambda: LobstersChannel()._tag(name="..", limit=1, timeout=1),
])
def test_hostile_arguments_are_rejected_before_any_request(call, monkeypatch):
    monkeypatch.setattr(_public, "fetch_public", lambda *a, **k: pytest.fail("must not send a request"))
    with pytest.raises(ReachError) as err:
        call()
    assert err.value.code == BAD_INPUT


def test_scoped_and_plain_npm_names_are_accepted():
    assert _package_name("@types/node") == "@types/node"
    assert _package_name("https://www.npmjs.com/package/@types/node") == "@types/node"
    assert _package_name("react") == "react"


def test_site_search_keeps_only_results_on_the_site(monkeypatch):
    from agent_reach.channels.web import WebChannel

    rows = [{"url": "https://dev.to/a/b", "title": "ok"}, {"url": "https://dev.to.evil.example/x", "title": "bad"},
            {"url": "https://evil.example/?u=dev.to", "title": "bad"}]
    monkeypatch.setattr(WebChannel, "_exa_search", lambda self, **kw: rows)
    assert [r["title"] for r in _public.site_search(("dev.to",), "q", 5, 1)] == ["ok"]


def test_opencli_refuses_option_like_arguments_before_touching_the_browser(monkeypatch):
    from agent_reach.backends import opencli
    from agent_reach.runtime import browser_binding

    monkeypatch.setattr(browser_binding, "environment", lambda site: pytest.fail("must not prepare the browser"))
    for command, values in (("search", ["--profile=other"]), ("read", ["-f"]), ("profile", ["--help"]),
                            ("search", [""]), ("search", ["ok", "-x"])):
        with pytest.raises(ReachError) as err:
            opencli.opencli_call("reddit" if command != "profile" else "twitter", command, values, timeout=1)
        assert err.value.code == BAD_INPUT


@pytest.mark.parametrize("target", ["../user", "./x", "x/..", "https://github.com/../user"])
def test_github_target_cannot_walk_the_api_path(target):
    from agent_reach.channels.github import _parse_target

    with pytest.raises(ReachError) as err:
        _parse_target(target)
    assert err.value.code == BAD_INPUT


def test_site_search_rate_limit_is_the_providers_not_the_sites(monkeypatch):
    from agent_reach.channels.web import WebChannel

    def limited(self, **kw):
        raise ReachError("RATE_LIMITED", "exa 429")

    monkeypatch.setattr(WebChannel, "_exa_search", limited)
    with pytest.raises(ReachError) as err:
        _public.site_search(("dev.to",), "q", 5, 1)
    assert err.value.code == UPSTREAM_BROKEN, "a search provider limit must not cool the channel down"

    def broken(self, **kw):
        raise ReachError("TIMEOUT", "slow")

    monkeypatch.setattr(WebChannel, "_exa_search", broken)
    with pytest.raises(ReachError) as err:
        _public.site_search(("dev.to",), "q", 5, 1)
    assert err.value.code == "TIMEOUT"


@pytest.mark.parametrize("text", ["", "   ", "Just a moment...\nChecking your browser",
                                  "Access Denied\nYou don't have permission", "Please verify you are human",
                                  "You've been blocked by network security."])
def test_web_readers_treat_block_pages_as_failures(monkeypatch, text):
    from agent_reach.channels.web import WebChannel

    monkeypatch.setattr(WebChannel, "_jina_read", lambda self, **kw: [{"title": "x", "text": text}])
    with pytest.raises(ReachError) as err:
        MediumChannel()._jina_read(target="https://medium.com/@a/post", timeout=1)
    assert err.value.code == UPSTREAM_BROKEN


def test_web_readers_pass_real_content_through(monkeypatch):
    from agent_reach.channels.web import WebChannel

    rows = [{"title": "Post", "text": "A long article about access control and denied requests."}]
    monkeypatch.setattr(WebChannel, "_exa_read", lambda self, **kw: rows)
    assert MediumChannel()._exa_read(target="https://medium.com/@a/post", timeout=1) == rows
    monkeypatch.setattr(WebChannel, "_exa_read", lambda self, **kw: [])
    with pytest.raises(ReachError):
        MediumChannel()._exa_read(target="https://medium.com/@a/post", timeout=1)


def test_raw_and_whole_answers_are_bounded():
    from agent_reach.runtime import result

    big = result.make_item(title="t", raw={"deps": ["x" * 100] * 1000})
    assert big["raw"] == {"omitted": f"raw data exceeded {result.MAX_RAW_CHARS} characters"}
    assert result.make_item(raw={"a": 1})["raw"] == {"a": 1}
    items = [result.make_item(title=str(i), text="y" * 150_000) for i in range(6)]
    out = result.envelope(channel="c", action="a", backend="b", items=items)
    assert 1 <= len(out["items"]) < 6 and out["meta"]["truncated"]["dropped_items"] == 6 - len(out["items"])
    assert "truncated" not in result.envelope(channel="c", action="a", backend="b", items=items[:1])["meta"]


@pytest.mark.parametrize("payload", [
    b'<?xml version="1.0"' + b" " * 600 + b'encoding="cp037"?><rss/>',
    b'<?xml version="1.0" encoding="utf-16"?><rss/>',
    b'<?xml version="1.0" ' + b" " * 5000 + b'?><rss/>',
])
def test_the_whole_xml_declaration_decides_the_encoding(payload):
    with pytest.raises(ReachError) as err:
        _public.parse_feed(payload, 5)
    assert err.value.code == UPSTREAM_BROKEN


def test_the_daemon_port_is_never_followed_through_a_redirect():
    from agent_reach.runtime import bridge

    class Body:
        closed = False

        def close(self):
            self.closed = True

    body = Body()
    with pytest.raises(OSError):
        bridge._NoRedirects().redirect_request(None, body, 302, "Found", {}, "http://evil.example/")
    assert body.closed


_PROXY_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")


def _proxies(monkeypatch, **values):
    import urllib.request

    for key in _PROXY_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(urllib.request, "getproxies", urllib.request.getproxies_environment)


def test_proxy_use_is_decided_per_request(monkeypatch):
    _proxies(monkeypatch)
    assert _public._uses_proxy("https://example.org/feed") is False
    _proxies(monkeypatch, https_proxy="http://127.0.0.1:7890")
    assert _public._uses_proxy("https://example.org/feed") is True
    assert _public._uses_proxy("http://example.org/feed") is False, "no proxy for this scheme"
    _proxies(monkeypatch, https_proxy="http://127.0.0.1:7890", no_proxy="example.org")
    assert _public._uses_proxy("https://example.org/feed") is False
    assert _public._uses_proxy("https://other.example/feed") is True


def test_a_proxied_request_is_never_redirected_to_another_host():
    import urllib.request

    class Body:
        closed = False

        def close(self):
            self.closed = True

    req = urllib.request.Request("https://example.org/feed")
    proxied = _public._PublicRedirectHandler(same_host_only=True)
    assert proxied.redirect_request(req, Body(), 302, "Found", {}, "https://example.org/next") is not None
    for target in ("https://evil.example/feed", "http://example.org/feed", "https://example.org:8443/feed",
                   "https://sub.example.org/feed"):
        with pytest.raises(ReachError):
            proxied.redirect_request(req, Body(), 302, "Found", {}, target)
    direct = _public._PublicRedirectHandler()
    assert direct.redirect_request(req, Body(), 302, "Found", {}, "https://evil.example/feed") is not None


def test_get_bytes_through_a_proxy_refuses_a_cross_host_redirect(monkeypatch):
    import io
    import urllib.request

    _proxies(monkeypatch, https_proxy="http://127.0.0.1:7890")
    seen = []

    class Redirecting(urllib.request.BaseHandler):
        handler_order = 100

        def https_open(self, req):
            import http.client

            seen.append((req.full_url, req.host))
            headers = http.client.HTTPMessage()
            headers["Location"] = "https://evil.example/x"
            resp = urllib.request.addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
            resp.msg = "Found"
            return resp

    real_init = urllib.request.OpenerDirector.__init__

    def init(self):
        real_init(self)
        self.add_handler(Redirecting())

    monkeypatch.setattr(urllib.request.OpenerDirector, "__init__", init)
    with pytest.raises(ReachError, match="another host"):
        _public.get_bytes("https://example.org/feed", 1)
    assert seen == [("https://example.org/feed", "example.org")]
    assert all("evil.example" not in url for url, _ in seen)


@pytest.mark.parametrize("env", [{"ALL_PROXY": "http://127.0.0.1:7890"}, {"all_proxy": "socks5://127.0.0.1:1080"}])
def test_an_all_proxy_setting_does_not_turn_off_address_pinning(monkeypatch, env):
    """urllib's ProxyHandler never uses an "all" proxy for http/https, so such a request goes out directly;
    it must still be pinned to a public address."""
    _proxies(monkeypatch, **env)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    connected = []

    def connect(address, *a, **k):
        connected.append(address)
        raise OSError("blocked by the test")

    monkeypatch.setattr(socket, "create_connection", connect)
    with pytest.raises((ReachError, OSError)):
        _public.get_bytes("https://rebind.example.org/api", 1)
    assert connected == [], "connected to a name that resolves to loopback"
