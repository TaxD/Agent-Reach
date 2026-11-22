# -*- coding: utf-8 -*-
"""YouTube — video info, search, subtitles (yt-dlp) and Whisper transcription."""

import html
import json
import os
import re
import shutil
import sys

from agent_reach.runtime.result import (
    BAD_INPUT,
    NOT_FOUND,
    PLATFORM_BLOCKED,
    RATE_LIMITED,
    ReachError,
    make_item,
)
from agent_reach.runtime.run import CommandFailed, run_cmd
from agent_reach.utils.paths import private_temporary_directory

from .base import Action, Channel, Param

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_VTT_TIME_RE = re.compile(
    r"^(?:\d{2,}:)?\d{2}:\d{2}\.\d{3}[ \t]+-->[ \t]+"
    r"(?:\d{2,}:)?\d{2}:\d{2}\.\d{3}(?:[ \t]|$)"
)
_TAG_RE = re.compile(r"<[^>]+>")
_RAW_KEYS = ("id", "duration", "view_count", "like_count", "channel_id", "tags", "categories")


def ytdlp_tls_args() -> list:
    """yt-dlp trusts its bundled certifi and ignores SSL_CERT_FILE; behind a TLS-inspecting
    proxy (corporate, school) every request then fails CERTIFICATE_VERIFY_FAILED."""
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return ["--compat-options", "no-certifi"]
    return []


def ytdlp_base_argv() -> list:
    """The bundled yt-dlp, isolated from everything ambient.

    -I keeps a module in the working directory from shadowing yt_dlp. The yt-dlp
    flags stop it from reading the user's own yt-dlp config and plugins, which
    could otherwise make an ordinary read pull cookies out of a personal browser.
    """
    return [sys.executable, "-I", "-X", "utf8", "-m", "yt_dlp", "--ignore-config", "--no-plugin-dirs",
            "--no-cookies-from-browser", "--no-cookies", *ytdlp_tls_args()]


def _ytdlp_argv(*args: str) -> list:
    """Run the yt-dlp installed with Agent Reach (one of its dependencies)."""
    runtime = []
    if shutil.which("deno") is None and shutil.which("node"):
        runtime = ["--js-runtimes", "node"]
    return [*ytdlp_base_argv(), "--no-warnings", *runtime, *args]


def _video_url(target: str) -> str:
    from agent_reach.utils.url import host_matches

    t = str(target).strip()
    if _VIDEO_ID_RE.match(t):
        return f"https://www.youtube.com/watch?v={t}"
    candidate = t if "://" in t else f"https://{t}"
    if host_matches(candidate, "youtube.com", "youtu.be"):
        return candidate
    raise ReachError(BAD_INPUT, "target must be a YouTube link or 11-character video id")


def _run_ytdlp(args: list, timeout: float, cwd=None) -> str:
    try:
        return run_cmd(_ytdlp_argv(*args), timeout=timeout, cwd=cwd, label="yt-dlp")
    except CommandFailed as exc:
        err = exc.stderr.lower()
        if any(s in err for s in ("video unavailable", "private video", "has been removed", "does not exist")):
            raise ReachError(NOT_FOUND, exc.message) from None
        if "sign in to confirm" in err or "not a bot" in err:
            raise ReachError(
                PLATFORM_BLOCKED,
                exc.message,
                hint="YouTube returned a verification challenge. Stop this request; this does not establish that the public video requires login.",
            ) from None
        if "429" in err or "too many requests" in err:
            raise ReachError(RATE_LIMITED, exc.message) from None
        raise


def _iso_date(yyyymmdd) -> str:
    s = str(yyyymmdd or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else ""


def vtt_to_text(vtt: str) -> str:
    """Plain text from WebVTT: drop headers, timings, tags; collapse auto-caption repeats."""
    out: list[str] = []
    in_cue = False
    block_start = True
    metadata = False
    for line in vtt.splitlines():
        s = line.strip()
        if not s:
            in_cue, block_start, metadata = False, True, False
            continue
        if block_start:
            block_start = False
            metadata = s in {"NOTE", "STYLE", "REGION"} or s.startswith(("NOTE ", "NOTE\t"))
        if metadata:
            continue
        if _VTT_TIME_RE.match(s):
            in_cue = True
            continue
        if not in_cue:
            # Headers and optional cue identifiers precede the timing line.
            continue
        s = html.unescape(_TAG_RE.sub("", s)).strip()
        if s and (not out or out[-1] != s):
            out.append(s)
    return "\n".join(out)


class YouTubeChannel(Channel):
    name = "youtube"
    description = "YouTube 영상 정보와 자막"
    purpose = ("강연·튜토리얼·발표·인터뷰 영상을 찾고, 영상을 재생하지 않고도 자막 전문으로 내용을 파악할 때 사용합니다. "
               "영상 요약이 필요하면 subtitle을 먼저 시도하고, 자막이 없을 때에만 사용자 동의하에 transcribe를 사용합니다.")

    level = "default"
    #: transcribe re-encodes and chunks audio
    prereqs = ("ffmpeg",)
    actions = (
        Action(
            "read",
            "영상 제목·채널·날짜·설명",
            (Param("target", "영상 링크 또는 id"),),
            (("yt-dlp", "_ytdlp_read"),),
            live_test={"target": "https://www.youtube.com/watch?v=jNQXAC9IVRw"},
        ),
        Action(
            "search",
            "영상 검색",
            (
                Param("query", "검색어"),
                Param("limit", "최대 결과 수", required=False, type=int, default=5),
            ),
            (("yt-dlp", "_ytdlp_search"),),
            live_test={"query": "python tutorial", "limit": 2},
        ),
        Action(
            "subtitle",
            "영상의 자막 전문 (수동 자막 또는 자동 생성 자막)",
            (
                Param("target", "영상 링크 또는 id"),
                Param("lang", "선호 언어, 쉼표로 구분", required=False, default="ko,en,en-orig"),
            ),
            (("yt-dlp", "_ytdlp_subtitle"),),
            live_test={"target": "https://www.youtube.com/watch?v=jNQXAC9IVRw"},
        ),
        Action(
            "transcribe",
            "영상 음성을 텍스트로 전사 (음성이 Groq/OpenAI Whisper API로 전송되며 사용량이 차감됨)",
            (Param("target", "영상 링크 또는 id"),),
            (("whisper", "_whisper_transcribe"),),
            timeout=1800,
        ),
    )

    def can_handle(self, url: str) -> bool:
        from agent_reach.utils.url import host_matches

        return host_matches(url, "youtube.com", "youtu.be")

    # ── unified entry backends ──

    def _ytdlp_read(self, *, target: str, timeout: float) -> list:
        out = _run_ytdlp(["--dump-json", "--skip-download", _video_url(target)], timeout)
        info = json.loads(out.splitlines()[-1])
        return [
            make_item(
                title=info.get("title"),
                url=info.get("webpage_url"),
                author=info.get("uploader") or info.get("channel"),
                published_at=_iso_date(info.get("upload_date")),
                text=info.get("description"),
                raw={k: info.get(k) for k in _RAW_KEYS},
            )
        ]

    def _ytdlp_search(self, *, query: str, limit: int, timeout: float) -> list:
        out = _run_ytdlp(["--flat-playlist", "--dump-json", f"ytsearch{limit}:{query}"], timeout)
        items = []
        for line in out.splitlines():
            if not line.strip():
                continue
            info = json.loads(line)
            vid = info.get("id")
            items.append(
                make_item(
                    title=info.get("title"),
                    url=info.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else None),
                    author=info.get("channel") or info.get("uploader"),
                    text=info.get("description"),
                    raw={k: info.get(k) for k in _RAW_KEYS},
                )
            )
        return items

    def _ytdlp_subtitle(self, *, target: str, lang: str, timeout: float) -> list:
        url = _video_url(target)
        wanted = [x.strip() for x in lang.split(",") if x.strip()]
        with private_temporary_directory(prefix="agent-reach-subs-") as tmp:
            out = _run_ytdlp(
                [
                    "--skip-download", "--no-simulate",
                    "--write-subs", "--write-auto-subs",
                    "--sub-langs", ",".join(wanted), "--sub-format", "vtt",
                    "--print", "%(title)s",
                    "-o", "%(id)s.%(ext)s",
                    url,
                ],
                timeout,
                cwd=tmp,
            )
            files = [f for f in os.listdir(tmp) if f.endswith(".vtt")]
            if not files:
                raise ReachError(NOT_FOUND, f"no subtitles in {', '.join(wanted)} for this video")

            def rank(name: str) -> int:
                code = name.rsplit(".", 2)[-2] if name.count(".") >= 2 else ""
                return wanted.index(code) if code in wanted else len(wanted)

            best = min(files, key=rank)
            with open(os.path.join(tmp, best), encoding="utf-8", errors="replace") as fh:
                text = vtt_to_text(fh.read())
        title = next((ln for ln in out.splitlines() if ln.strip()), None)
        code = best.rsplit(".", 2)[-2] if best.count(".") >= 2 else None
        return [make_item(title=title, url=url, text=text, raw={"lang": code})]

    def _whisper_transcribe(self, *, target: str, timeout: float) -> list:
        from agent_reach.transcribe import NoProviderConfigured, TranscribeError
        from agent_reach.transcribe import transcribe as _transcribe

        url = _video_url(target)
        try:
            text = _transcribe(url)
        except NoProviderConfigured:
            raise ReachError(
                "NEED_SETUP", "no Whisper API key configured",
                hint="Ask the user for a free key from https://console.groq.com, then run `agent-reach configure groq-key`.",
            ) from None
        except TranscribeError as exc:
            raise ReachError("UPSTREAM_BROKEN", str(exc)[:300]) from None
        return [make_item(url=url, text=text)]
