"""Secrets embedded in URLs must not reach user-facing diagnostics."""



from agent_reach.utils.text import scrub_url_credentials


def test_scrubs_userinfo_and_sensitive_query_values():
    raw = (
        "proxy http://user:pass@proxy.example:8080 failed; "
        "upstream https://api.example.test/path?access_token=secret"
        "&page=2&api_key=another-secret"
    )

    scrubbed = scrub_url_credentials(raw)

    assert "user:pass" not in scrubbed
    assert "secret" not in scrubbed
    assert "another-secret" not in scrubbed
    assert "http://***@proxy.example:8080" in scrubbed
    assert "access_token=***" in scrubbed
    assert "page=2" in scrubbed
    assert "api_key=***" in scrubbed


def test_scrubs_multiple_schemes_and_fragment_tokens():
    raw = (
        "socks5://token@host:1080 "
        "https://example.test/#auth_token=fragment-secret"
    )

    scrubbed = scrub_url_credentials(ValueError(raw))

    assert scrubbed == (
        "socks5://***@host:1080 "
        "https://example.test/#auth_token=***"
    )


def test_scrubs_bare_user_password_host_diagnostics():
    raw = "proxy handshake for user:pass@proxy.test failed"
    assert scrub_url_credentials(raw) == "proxy handshake for ***@proxy.test failed"


def test_leaves_non_secret_urls_and_plain_text_unchanged():
    raw = "See https://example.test/search?q=python&page=2 after timeout"
    assert scrub_url_credentials(raw) == raw






def test_error_messages_and_attempts_are_scrubbed_at_the_output_boundary():
    from agent_reach.runtime.result import ReachError, envelope

    secret = "https://alice:hunter2@proxy.example:8080/path?token=abc123"
    out = envelope(channel="web", action="read", error=ReachError("UPSTREAM_BROKEN", f"failed via {secret}"),
                   attempts=[{"backend": "jina", "result": "UPSTREAM_BROKEN", "message": f"x {secret}"},
                             {"backend": "exa", "result": "ok"}])
    text = str(out)
    assert "hunter2" not in text and "abc123" not in text
    assert out["meta"]["attempts"][1] == {"backend": "exa", "result": "ok"}
    long = envelope(channel="web", action="read", error=ReachError("UPSTREAM_BROKEN", "y" * 10_000))
    assert len(long["error"]["message"]) <= 500


def test_an_oversized_first_item_is_cut_to_the_budget():
    import json

    from agent_reach.runtime import result

    first = {"title": "big", "url": None, "author": None, "published_at": None,
             "text": "z" * (result.MAX_TEXT_CHARS * 3), "raw": {"blob": "r" * (result.MAX_RAW_CHARS * 3)}}
    out = result.envelope(channel="web", action="read", items=[first, {"title": "second", "text": "s"}])
    (kept, *rest) = out["items"]
    assert len(kept["text"]) <= result.MAX_TEXT_CHARS + 20 and kept["text"].endswith("(truncated)")
    assert "omitted" in kept["raw"]
    assert len(json.dumps(out, ensure_ascii=False)) <= result.MAX_OUTPUT_CHARS + 10_000
