"""Unified result envelope and error codes for the agent-facing entry point.

Every unified command prints exactly one JSON object with this shape:

    {schema_version, ok, channel, action, backend, items, meta, error}

`schema_version` only changes on incompatible shape changes. Agents branch
on `ok` and `error.code`; they should never need to parse upstream output.
"""

from __future__ import annotations

import datetime as _dt
import socket
import subprocess
import urllib.error
from typing import Any, Optional

from agent_reach.utils.text import scrub_url_credentials

SCHEMA_VERSION = 1

NEED_SETUP = "NEED_SETUP"
NEED_LOGIN = "NEED_LOGIN"
UPSTREAM_BROKEN = "UPSTREAM_BROKEN"
TIMEOUT = "TIMEOUT"
RATE_LIMITED = "RATE_LIMITED"
PLATFORM_BLOCKED = "PLATFORM_BLOCKED"
NOT_FOUND = "NOT_FOUND"
BAD_INPUT = "BAD_INPUT"

#: Codes where trying another backend cannot help.
FINAL_CODES = frozenset({NOT_FOUND, BAD_INPUT, PLATFORM_BLOCKED, RATE_LIMITED, NEED_LOGIN, "BUSY"})

DEFAULT_HINTS = {
    "BUSY": "The shared browser stayed full while this command waited in line, or a login window is open. Retry later; do not switch profiles.",
    PLATFORM_BLOCKED: "Stop automation. Keep this profile; the user must resolve the platform verification in the official page. Do not switch backends or retry.",
    NEED_SETUP: "This backend is not installed. Ask the user to run `agent-reach doctor` for setup steps.",
    NEED_LOGIN: "The site wants a login. Only the user can log in, in the visible Agent Reach browser window (`agent-reach login <channel>`); never ask for a password and never log in for them.",
    UPSTREAM_BROKEN: "This channel is temporarily not working; `message` is what was reported. Tell the user in plain words which platform has a problem, without naming tools; suggest `agent-reach update` only if the message points to an outdated tool; do not retry in a loop.",
    TIMEOUT: "The operation timed out. Inspect channel status and the failed stage; do not reopen login windows or repeat requests in a loop.",
    RATE_LIMITED: "The platform reported a rate limit. Reach pauses this channel; respect retry_after_seconds and do not switch backends.",
    NOT_FOUND: "The requested content does not exist. Check the link or id.",
    BAD_INPUT: "Invalid arguments. Run `agent-reach channels` to see valid usage.",
}

_MAX_MESSAGE_CHARS = 500
#: Upper bound for one item's text (a long README or transcript still fits).
MAX_TEXT_CHARS = 200_000
MAX_RAW_CHARS = 20_000
#: Budget for all items of one answer together.
MAX_OUTPUT_CHARS = 400_000


def _within_budget(items: list) -> tuple:
    """Keep leading items while their total serialized size fits; the first item is always kept."""
    import json

    total = 0
    kept: list = []
    if items and isinstance(items[0], dict):
        # The first item is always returned, so it is cut down to the budget itself
        # (a channel may have enlarged it after it was normalized).
        first = items[0]
        if isinstance(first.get("text"), str) and len(first["text"]) > MAX_TEXT_CHARS:
            first["text"] = first["text"][:MAX_TEXT_CHARS] + " …(truncated)"
        if "raw" in first:
            first["raw"] = _bounded_raw(first["raw"])
        try:
            size = len(json.dumps(first, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            size = 0
        if size > MAX_OUTPUT_CHARS and isinstance(first.get("text"), str):
            # Escaping can double the size; cut the text by the excess (and a margin) and drop raw.
            first["text"] = first["text"][:max(0, len(first["text"]) - (size - MAX_OUTPUT_CHARS) - 1024)] + " …(truncated)"
            if "raw" in first:
                first["raw"] = {"omitted": "answer exceeded the output budget"}
    for item in items:
        try:
            size = len(json.dumps(item, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            size = MAX_OUTPUT_CHARS
        if kept and total + size > MAX_OUTPUT_CHARS:
            break
        kept.append(item)
        total += size
    return kept, len(items) - len(kept)


class ReachError(Exception):
    """A failure with a unified code; upstream messages and hints are passed through unchanged."""

    def __init__(self, code: str, message: str, hint: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


def _clean(text: object) -> str:
    return scrub_url_credentials(str(text)).strip()[:_MAX_MESSAGE_CHARS]


def classify(exc: BaseException) -> ReachError:
    """Map any exception raised by a backend to a ReachError."""
    if isinstance(exc, ReachError):
        return exc
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 404:
            return ReachError(NOT_FOUND, f"HTTP 404: {_clean(exc.reason)}")
        if exc.code == 429:
            return ReachError(RATE_LIMITED, "HTTP 429")
        return ReachError(UPSTREAM_BROKEN, f"HTTP {exc.code}: {_clean(exc.reason)}")
    if isinstance(exc, (subprocess.TimeoutExpired, socket.timeout, TimeoutError)):
        return ReachError(TIMEOUT, "upstream did not answer in time")
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            return ReachError(TIMEOUT, "upstream did not answer in time")
        return ReachError(UPSTREAM_BROKEN, f"network error: {_clean(exc.reason)}")
    if isinstance(exc, FileNotFoundError):
        return ReachError(NEED_SETUP, f"missing executable: {_clean(exc.filename or exc)}")
    return ReachError(UPSTREAM_BROKEN, f"{type(exc).__name__}: {_clean(exc)}")


def make_item(
    *,
    title: Optional[str] = None,
    url: Optional[str] = None,
    author: Optional[str] = None,
    published_at: Optional[str] = None,
    text: Optional[str] = None,
    raw: Any = None,
) -> dict:
    """One normalized item. Missing fields are null; `raw` keeps upstream data."""
    def cap(value, limit):
        # Third-party fields are bounded so one response cannot flood the agent's context.
        if value is None or value == "":
            return None
        value = value if isinstance(value, str) else str(value)
        return value if len(value) <= limit else value[:limit] + " …(truncated)"

    return {
        "title": cap(title, 500),
        "url": cap(url, 2000),
        "author": cap(author, 200),
        "published_at": cap(published_at, 64),
        "text": cap(text, MAX_TEXT_CHARS),
        "raw": _bounded_raw(raw),
    }


def _bounded_raw(raw: Any) -> Any:
    """Upstream detail is kept only while it stays small; it must not flood the agent's context."""
    import json

    try:
        size = len(json.dumps(raw, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return {"omitted": "raw data could not be serialized"}
    return raw if size <= MAX_RAW_CHARS else {"omitted": f"raw data exceeded {MAX_RAW_CHARS} characters"}


def unix_to_iso(value: Any) -> Optional[str]:
    """Convert a unix timestamp to ISO 8601 UTC, or None."""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return _dt.datetime.fromtimestamp(seconds, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def envelope(
    *,
    channel: Optional[str],
    action: Optional[str],
    backend: Optional[str] = None,
    items: Optional[list] = None,
    elapsed_ms: int = 0,
    attempts: Optional[list] = None,
    error: Optional[ReachError] = None,
) -> dict:
    """Build the unified output object."""
    kept, dropped = _within_budget(list(items or [])) if error is None else ([], 0)
    meta: dict = {"elapsed_ms": elapsed_ms, "attempts": [
        {**row, "message": _clean(row["message"])} if isinstance(row, dict) and isinstance(row.get("message"), str) else row
        for row in (attempts or [])]}
    if dropped:
        meta["truncated"] = {"dropped_items": dropped, "reason": f"output exceeded {MAX_OUTPUT_CHARS} characters"}
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": error is None,
        "channel": channel,
        "action": action,
        "backend": backend if error is None else None,
        "items": kept,
        "meta": meta,
        "error": None
        if error is None
        else {
            "code": error.code,
            # Last boundary for third-party text: credentials in URLs are masked and length is capped.
            "message": _clean(error.message),
            "hint": error.hint or DEFAULT_HINTS.get(error.code, ""),
        },
    }
