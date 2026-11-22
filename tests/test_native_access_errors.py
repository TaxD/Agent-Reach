"""Local credential setup, denied requests and platform challenges stay distinct."""
import json
import urllib.error

import pytest

from agent_reach.channels import github, youtube
from agent_reach.runtime.dispatch import run_action
from agent_reach.runtime.result import ReachError
from agent_reach.runtime.run import CommandFailed


def test_missing_gh_auth_falls_back_to_public_api_without_login(monkeypatch):
    def no_gh(*a, **k):
        raise CommandFailed("UPSTREAM_BROKEN", "gh exited", stderr="please run gh auth login")
    monkeypatch.setattr(github, "run_cmd", no_gh)
    monkeypatch.setattr(github, "http_request", lambda *a, **k: json.dumps(
        {"items": [{"full_name": "owner/repo", "html_url": "https://github.com/owner/repo", "private": False}]}
    ).encode())
    out = run_action(github.GitHubChannel(), "search", {"query": "repo", "limit": 1})
    assert out["ok"] and out["backend"] == "github-api" and len(out["items"]) == 1


@pytest.mark.parametrize("headers,code", [({}, "UPSTREAM_BROKEN"),
    ({"X-RateLimit-Remaining": "0"}, "RATE_LIMITED"), ({"Retry-After": "60"}, "RATE_LIMITED")])
def test_github_403_requires_rate_limit_evidence(monkeypatch, headers, code):
    def denied(*a, **k):
        raise urllib.error.HTTPError("https://api.github.com/test", 403, "Forbidden", headers, None)
    monkeypatch.setattr(github, "http_request", denied)
    with pytest.raises(ReachError) as exc:
        github._api_get("/test", 5)
    assert exc.value.code == code


def test_youtube_bot_challenge_does_not_instruct_a_login(monkeypatch):
    def blocked(*a, **k):
        raise CommandFailed("UPSTREAM_BROKEN", "YouTube rejected request", stderr="Sign in to confirm you're not a bot")
    monkeypatch.setattr(youtube, "run_cmd", blocked)
    with pytest.raises(ReachError) as exc:
        youtube._run_ytdlp([], 5)
    assert exc.value.code == "PLATFORM_BLOCKED"


@pytest.mark.parametrize('backend', ['github-api', 'gh'])
def test_github_repository_without_readme_keeps_metadata(monkeypatch, backend):
    repository = {
        'full_name': 'example/code-only',
        'html_url': 'https://github.com/example/code-only',
        'description': 'A repository without a README',
        'private': False,
    }

    def get(url, **kwargs):
        if url.endswith('/readme'):
            # Content is always requested anonymously over HTTP, whichever backend found the repository.
            assert 'Authorization' not in kwargs.get('headers', {})
            raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
        return json.dumps(repository).encode()

    monkeypatch.setattr(github, 'http_request', get)
    monkeypatch.setattr(github, 'run_cmd', lambda argv, **kw: get(argv[-1], **kw).decode())
    out = run_action(github.GitHubChannel(), 'read', {'target': 'example/code-only'}, only_backend=backend)
    assert out['ok'] is True
    assert out['backend'] == backend
    assert len(out['items']) == 1
    assert out['items'][0]['title'] == repository['full_name']
    assert out['items'][0]['text'] == repository['description']


@pytest.mark.parametrize('status,headers,code', [
    (404, {}, 'NOT_FOUND'),
    (403, {}, 'UPSTREAM_BROKEN'),
    (403, {'X-RateLimit-Remaining': '0'}, 'RATE_LIMITED'),
    (429, {}, 'RATE_LIMITED'),
])
def test_github_missing_repo_and_readme_failures_stay_errors(monkeypatch, status, headers, code):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        # A missing repository must not be mistaken for an optional missing README.
        if status == 404 or url.endswith('/readme'):
            raise urllib.error.HTTPError(url, status, 'Denied', headers, None)
        return json.dumps({'full_name': 'example/code-only', 'private': False}).encode()

    monkeypatch.setattr(github, 'http_request', get)
    out = run_action(github.GitHubChannel(), 'read', {'target': 'example/code-only'}, only_backend='github-api')
    assert out['ok'] is False
    assert out['error']['code'] == code
    assert out['items'] == []
    assert len(calls) == (1 if status == 404 else 2)
