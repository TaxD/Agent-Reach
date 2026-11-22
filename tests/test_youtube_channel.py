# -*- coding: utf-8 -*-
"""Dedicated tests for the ``youtube`` channel.

YouTube's readiness is layered: yt-dlp must probe alive (missing / broken /
unrunnable are distinct), a JS runtime is mandatory (deno works out of the
box, Node needs an explicit ``--js-runtimes`` config), and transcription
readiness (whisper provider + ffmpeg/ffprobe) is surfaced for ``doctor`` without
gating the backend. These tests stub ``probe_command`` / ``shutil.which``
so every branch runs offline. Follow-up to #331 — extends dedicated
channel coverage after github (#361), web (#363) and reddit (#364).
"""

from pathlib import Path

import pytest

from agent_reach.channels import youtube
from agent_reach.channels.youtube import YouTubeChannel
from agent_reach.runtime.result import ReachError


def _which(*present):
    """shutil.which side effect: returns a path for the named tools present."""
    return lambda name: f"/usr/bin/{name}" if name in present else None


# --- can_handle ---

def test_can_handle_matches_youtube_hosts():
    ch = YouTubeChannel()
    for url in [
        "https://www.youtube.com/watch?v=abc",
        "https://youtu.be/abc",
        "https://m.youtube.com/watch?v=abc",
        "https://YOUTUBE.COM/shorts/x",
    ]:
        assert ch.can_handle(url) is True, url
    for url in ["https://example.com", "https://vimeo.com/123", ""]:
        assert ch.can_handle(url) is False, url


# --- _has_js_runtime_config ---









# --- check(): yt-dlp probe states ---







# --- check(): JS runtime gating (backend stays yt-dlp once the probe is ok) ---











def test_constraints_pin_ytdlp_to_current_stable_release():
    constraints = Path(__file__).parents[1] / "constraints.txt"
    pin = next(
        line
        for line in constraints.read_text(encoding="utf-8").splitlines()
        if line.startswith("yt-dlp==")
    )

    assert pin == "yt-dlp==2026.8.19"


def test_project_installs_official_ytdlp_default_dependencies():
    root = Path(__file__).parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    constraints = (root / "constraints.txt").read_text(encoding="utf-8")

    assert '"yt-dlp[default]==2026.8.19"' in pyproject
    assert "yt-dlp-ejs==0.8.0" in constraints




# --- check(): transcription readiness surfaced (deno present so JS path is ok) ---









# --- TLS trust store ---

def test_ytdlp_uses_system_trust_store_when_ssl_cert_file_is_set(monkeypatch):
    from agent_reach.channels import youtube

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    assert "no-certifi" not in youtube._ytdlp_argv("--version")
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/corp-bundle.pem")
    argv = youtube._ytdlp_argv("--version")
    assert argv[argv.index("--compat-options") + 1] == "no-certifi"


# --- links are reduced to a video id; the caller's URL never reaches yt-dlp ---

VID = "dQw4w9WgXcQ"
CANONICAL = f"https://www.youtube.com/watch?v={VID}"


@pytest.fixture
def ytdlp(monkeypatch):
    calls = []

    def fake_run_cmd(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return '{"title": "t", "webpage_url": "%s"}' % CANONICAL

    monkeypatch.setattr(youtube, "run_cmd", fake_run_cmd)
    return calls


@pytest.mark.parametrize("target", [
    "https://www.youtube.com/redirect?q=http://127.0.0.1/",
    f"https://www.youtube.com/redirect?q=https://evil.example/&v={VID}",
    f"https://consent.youtube.com/m?continue=https://www.youtube.com/watch?v={VID}",
    f"https://evil.youtube.com.example/watch?v={VID}",
    f"https://www.youtube.com:8443/watch?v={VID}",
    f"https://user@www.youtube.com/watch?v={VID}",
    f"https://user:pw@youtu.be/{VID}",
    f"https://accounts.google.com/ServiceLogin?continue=https://www.youtube.com/watch?v={VID}",
    f"https://www.youtube.com/attribution_link?u=/watch?v={VID}",
    f"https://www.youtube.com/playlist?list=PL123&v={VID}",
    f"https://www.youtube.com/watch/{VID}",
    "https://www.youtube.com/watch?v=short",
    f"https://www.youtube.com/shorts/{VID}/extra",
    f"file:///www.youtube.com/watch?v={VID}",
    f"ftp://www.youtube.com/watch?v={VID}",
    f"javascript://www.youtube.com/watch?v={VID}",
    f"https://youtu.be/{VID}/../x",
    "http://[::1/",
    "",
    f"-{VID}",
    "--exec=touch /tmp/x",
])
def test_wrapper_and_foreign_links_are_bad_input_before_yt_dlp(ytdlp, target):
    for call in (lambda: youtube.YouTubeChannel()._ytdlp_read(target=target, timeout=5),
                 lambda: youtube.YouTubeChannel()._ytdlp_subtitle(target=target, lang="en", timeout=5)):
        with pytest.raises(ReachError) as caught:
            call()
        assert caught.value.code == "BAD_INPUT"
    assert ytdlp == []


@pytest.mark.parametrize("target", [
    VID,
    f"https://www.youtube.com/watch?v={VID}&list=PL123&index=4",
    f"https://www.youtube.com/watch?feature=share&v={VID}",
    f"http://youtube.com/watch?v={VID}",
    f"https://m.youtube.com/watch?v={VID}",
    f"https://music.youtube.com/watch?v={VID}",
    f"www.youtube.com/watch?v={VID}",
    f"https://youtu.be/{VID}?si=tracking",
    f"https://www.youtube.com/shorts/{VID}",
    f"https://www.youtube.com/embed/{VID}",
    f"https://www.youtube.com/live/{VID}/",
    f"https://www.youtube.com/v/{VID}",
    f"https://WWW.YOUTUBE.COM:443/watch?v={VID}",
])
def test_plain_video_links_become_a_fresh_canonical_url(ytdlp, target):
    youtube.YouTubeChannel()._ytdlp_read(target=target, timeout=5)
    (argv, kwargs), = ytdlp
    assert argv[-1] == CANONICAL
    if target != VID:
        assert target not in argv, "the caller's own URL is never passed on"
    assert not any("list=" in a or "si=" in a for a in argv)


def test_ytdlp_runs_only_youtube_extractors_without_playlists_or_node_settings(ytdlp):
    base = youtube.ytdlp_base_argv()
    i = base.index("--use-extractors")
    assert base[i + 1] == "youtube,youtube:search"
    assert "--no-playlist" in base
    youtube.YouTubeChannel()._ytdlp_read(target=VID, timeout=5)
    (argv, kwargs), = ytdlp
    assert argv[:len(base)] == base
    assert set(kwargs["drop_env_prefix"]) >= {"NODE_", "npm_"}


def test_subtitles_are_read_only_up_to_the_limit(monkeypatch):
    import os

    def fake_run_cmd(argv, cwd=None, **kwargs):
        with open(os.path.join(cwd, f"{VID}.en.vtt"), "w", encoding="utf-8") as fh:
            fh.write("WEBVTT\n\n" + "00:00:01.000 --> 00:00:02.000\n" + "word " * 100_000 + "\n")
        return "Title\n"

    monkeypatch.setattr(youtube, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(youtube, "_MAX_SUBTITLE_CHARS", 1000)
    (item,) = youtube.YouTubeChannel()._ytdlp_subtitle(target=VID, lang="en", timeout=5)
    assert 0 < len(item["text"]) <= 1000


@pytest.mark.parametrize("lang", ["all", "en.*", ".*", "", " , ", "en,ko,ja,fr,de", "-live_chat", "en,all",
                                  "e", "english", "en-", "en_US", "en/../x", "en;rm", "ko-KR-x-y"])
def test_subtitle_languages_must_be_a_few_literal_tags(ytdlp, lang):
    with pytest.raises(ReachError) as caught:
        youtube.YouTubeChannel()._ytdlp_subtitle(target=VID, lang=lang, timeout=5)
    assert caught.value.code == "BAD_INPUT"
    assert ytdlp == []


@pytest.mark.parametrize("lang, passed", [("ko", "ko"), ("ko,en", "ko,en"), (" en , ko ", "en,ko"),
                                          ("zh-Hans", "zh-Hans"), ("pt-BR,es-419,en,ko", "pt-BR,es-419,en,ko")])
def test_literal_language_tags_reach_yt_dlp(monkeypatch, lang, passed):
    seen = []

    def fake_run_cmd(argv, **kwargs):
        seen.append(argv)
        raise ReachError("NOT_FOUND", "stop here")

    monkeypatch.setattr(youtube, "run_cmd", fake_run_cmd)
    with pytest.raises(ReachError):
        youtube.YouTubeChannel()._ytdlp_subtitle(target=VID, lang=lang, timeout=5)
    argv = seen[0]
    assert argv[argv.index("--sub-langs") + 1] == passed


@pytest.mark.parametrize("target", [VID + "\n", f"https://youtu.be/{VID}%0A", f"https://www.youtube.com/watch?v={VID}%0A"])
def test_an_id_with_a_trailing_newline_is_not_an_id(target):
    if target.endswith("\n"):  # strip() would remove a bare trailing newline, so check the matcher itself
        assert youtube._VIDEO_ID_RE.fullmatch(target) is None
        return
    with pytest.raises(ReachError):
        youtube._video_id(target)
