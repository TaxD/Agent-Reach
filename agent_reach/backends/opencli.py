# -*- coding: utf-8 -*-
"""OpenCLI backend: run `opencli <site> ...` in the Agent Reach browser.

OpenCLI drives Chrome through a browser extension and a local daemon. Agent
Reach uses the hardened fork (github.com/TaxD/OpenCLI), whose daemon requires a
secret token, at a pinned and checksummed release, in one Reach-owned profile.
"""

import json


def _opencli_error(text: str, login_hint: str):
    """Turn OpenCLI's YAML error block (printed even with -f json) into a ReachError.

    OpenCLI's own `message` and `help` are passed through unchanged. The code only
    decides Reach's control flow (stop, pause the channel, try the next backend).
    """
    # Node warnings can precede the upstream YAML envelope on stdout. Parse the
    # envelope itself so warnings neither hide error codes nor flood the user.
    import re

    import yaml

    from agent_reach.runtime.result import (
        NEED_LOGIN,
        PLATFORM_BLOCKED,
        RATE_LIMITED,
        UPSTREAM_BROKEN,
        ReachError,
    )
    start = re.search(r"(?m)^ok: false\s*$", text)
    clean = text[start.start():] if start else text
    try:
        doc = yaml.safe_load(clean)
    except yaml.YAMLError:
        doc = None
    err = (doc or {}).get("error") if isinstance(doc, dict) else None
    err = err if isinstance(err, dict) else {}
    code = str(err.get("code", "")).upper()
    message = str(err.get("message", "") or text.strip() or "OpenCLI failed")[:500]
    # Upstream help text is third-party output. It is never forwarded as `hint`, which
    # the skill tells agents to act on; only Reach's own hints go there.
    low = message.lower()
    if any(w in low for w in ("browser environment", "captcha", "account_risk")) or code in {"ACCOUNT_RISK", "CAPTCHA_REQUIRED"}:
        return ReachError(PLATFORM_BLOCKED, message)
    if code in {"RATE_LIMITED", "RATE_LIMIT"} or any(w in low for w in ("http 429", "too many requests")):
        return ReachError(RATE_LIMITED, message)
    login_words = ("login", "log in", "logged in", "sign in", "http 401")
    if not re.search(r"\bHTTP 404\b", message, re.I) and (
            "AUTH" in code or "LOGIN" in code or any(w in low for w in login_words)):
        # Upstream would send the user to their own Chrome; Reach logs in in its own browser.
        return ReachError(NEED_LOGIN, message, hint=login_hint or None)
    return ReachError(UPSTREAM_BROKEN, message)


#: The only OpenCLI commands Reach may run: read-only ones. OpenCLI also has commands that
#: post, reply, vote or subscribe; they are never reachable through Reach.
READ_ONLY = {"reddit": {"search", "read", "subreddit"}, "twitter": {"search", "profile", "tweets", "thread"}}


def opencli_call(site: str, command: str, values=(), *, limit=None, timeout: float, login_hint: str = ""):
    """One bounded read-only command in the dedicated browser. No retry.

    `values` are caller-supplied data. They are passed after `--`, so none of
    them can be read as an option, and one that starts with `-` is refused.
    """
    from agent_reach.runtime.browser_binding import environment
    from agent_reach.runtime.result import BAD_INPUT, ReachError
    from agent_reach.runtime.run import CommandFailed, run_cmd

    values = [str(v) for v in values]
    if command not in READ_ONLY.get(site, ()) or any(v.startswith("-") or not v for v in values):
        raise ReachError(BAD_INPUT, "arguments must not be empty or start with '-'")
    env = environment(site)
    options = ["--limit", str(int(limit))] if limit is not None else []
    argv = ["opencli", site, command, *options, "-f", "json", "--", *values]
    try:
        out = run_cmd(argv, timeout=timeout, env=env)
    except CommandFailed as exc:
        raise _opencli_error(exc.stdout or exc.stderr, login_hint) from None
    try:
        data = json.loads(out)
    except ValueError:
        raise _opencli_error(out, login_hint) from None
    if isinstance(data, dict) and data.get("ok") is False:
        raise _opencli_error(out, login_hint)
    return data


def opencli_items(rows) -> list:
    """Map OpenCLI result rows to unified items, keeping each row in `raw`."""
    from agent_reach.runtime.result import make_item, unix_to_iso

    if isinstance(rows, dict):
        # OpenCLI 2.0 wraps rows as {ok, data: [...]}; an empty list means no rows, not one row.
        listed = next((rows[k] for k in ("items", "data") if isinstance(rows.get(k), list)), None)
        rows = listed if listed is not None else (rows.get("items") or rows.get("data") or [rows])
    items = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue

        def pick(*keys):
            for k in keys:
                v = r.get(k)
                if isinstance(v, dict):
                    v = v.get("nickname") or v.get("name") or v.get("username")
                if v not in (None, ""):
                    return str(v)
            return None

        items.append(
            make_item(
                title=pick("title", "name"),
                url=pick("url", "link", "note_url"),
                author=pick("author", "user", "nickname", "username"),
                published_at=(
                    unix_to_iso(r.get("created_utc"))
                    if isinstance(r.get("created_utc"), (int, float))
                    else pick("published_at", "created_at", "time", "date")
                ),
                text=pick("content", "text", "desc", "description", "body"),
                raw=r,
            )
        )
    return items
