"""Unified entry for Agent Reach.

Agent commands:      <channel> <action>, read <url>, channels
Management commands: setup, add, remove, skill, doctor, check, update,
                     uninstall, configure, version

Everything prints one JSON object to stdout so agents never parse prose; logs
go to stderr. Exit code 0 = ok, 1 = error. Exceptions for humans: `help`, and
`--text` on setup / doctor / check. No command logs in to anything.

When a newer release is known (cached by an explicit update --check), every JSON object carries meta.update = {current, latest, notes}.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Optional, Sequence, cast

from agent_reach.channels import get_all_channels
from agent_reach.channels.base import Action, Channel

from .dispatch import DEFAULT_TIMEOUT, find_action, run_action
from .result import (
    BAD_INPUT,
    DEFAULT_HINTS,
    NEED_SETUP,
    SCHEMA_VERSION,
    ReachError,
    envelope,
)

_MANAGEMENT = (
    "setup", "add", "remove", "update", "doctor", "uninstall", "configure", "version",
)

HELP = """agent-reach — AI 에이전트를 위한 인터넷 접근 도구 (모든 출력은 JSON)

에이전트용:
  agent-reach channels                    채널, 용도(purpose), 동작, 인자 목록
  agent-reach <channel> <action> [args]   예: agent-reach hackernews search rust
  agent-reach read <url>                  링크에 맞는 채널을 골라서 읽기
  모든 채널은 로그인 없이 공개 데이터만 읽습니다. --backend로 백엔드를 지정할 수 있습니다.

사용자용:
  agent-reach setup [--text]              기본 채널과 스킬 설치. --text는 기능 표를 출력
  agent-reach add <channel>               제거했던 채널을 다시 켜기
  agent-reach remove <channel>            채널 끄기 (그 채널만 사용하던 도구도 제거)
  agent-reach skill [link|unlink <dir>]   에이전트 스킬 위치 확인, 다른 AI에 연결하거나 연결 해제
  agent-reach doctor [--text]             채널 상태와 설치된 도구 버전 (네트워크 사용 안 함)
  agent-reach check [channel] [--text]    채널 실측. --search는 검색만 실측
  agent-reach update [--check]            새 버전 확인(설치는 하지 않음), 도구와 스킬 정리
  agent-reach configure <key> [value]     groq-key | openai-key | proxy | github-token
  agent-reach uninstall [--dry-run]       Agent Reach가 설치한 모든 것 제거
"""


def unified_channels() -> List[Channel]:
    """Channels exposed on the command line."""
    return get_all_channels()


def _channel(name: str) -> Optional[Channel]:
    return next((ch for ch in unified_channels() if ch.name == name), None)


def _mgmt(action: str, fn, *args, **kwargs) -> int:
    """Run a management function and print {schema_version, ok, action, result, error}."""
    try:
        result, error = fn(*args, **kwargs), None
    except ReachError as err:
        result = None
        error = {"code": err.code, "message": err.message, "hint": err.hint or DEFAULT_HINTS.get(err.code, "")}
    return _emit({"schema_version": SCHEMA_VERSION, "ok": error is None, "action": action,
                  "result": result, "error": error})


class _ArgError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    """argparse that raises instead of printing usage and exiting."""

    def error(self, message: str):
        raise _ArgError(message)


#: Attach meta.update to JSON output (off for `update`, which reports versions itself).
_SHOW_NOTICE = True


def _emit(payload: dict) -> int:
    if _SHOW_NOTICE:
        from .update_notice import notice

        info = notice()
        if info:
            meta = payload.get("meta")
            payload["meta"] = {**(meta if isinstance(meta, dict) else {}), "update": info}
    output = json.dumps(payload, ensure_ascii=False)
    try:
        output.encode(getattr(sys.stdout, "encoding", None) or "utf-8")
    except UnicodeEncodeError:
        output = json.dumps(payload, ensure_ascii=True)
    sys.stdout.write(output + "\n")
    sys.stdout.flush()
    return 0 if payload.get("ok") else 1


def _usage(channel: Channel, action: Action) -> str:
    parts = ["agent-reach", channel.name, action.name]
    for p in action.params:
        parts.append(f"<{p.name}>" if p.required else f"[--{p.name} {p.name.upper()}]")
    return " ".join(parts)


def _bad_input(channel: Optional[str], action: Optional[str], message: str) -> int:
    return _emit(envelope(channel=channel, action=action, error=ReachError(BAD_INPUT, message)))


def _parse_action_args(action: Action, args: Sequence[str]) -> Dict[str, object]:
    parser = _Parser(prog=action.name, add_help=False)
    for p in action.params:
        if p.required:
            # Join extra words so an unquoted multi-word query still works.
            parser.add_argument(p.name, nargs="+" if p.type is str else None)
        else:
            parser.add_argument(f"--{p.name}", dest=p.name, default=None)
    parser.add_argument("--backend", choices=[b[0] for b in action.backends], default=None)
    parser.add_argument("--timeout", type=float, default=action.timeout or DEFAULT_TIMEOUT)
    ns = vars(parser.parse_args(list(args)))
    for p in action.params:
        if p.required and p.type is str and isinstance(ns.get(p.name), list):
            ns[p.name] = " ".join(ns[p.name])
    return ns


def _cmd_action(channel: Channel, args: Sequence[str]) -> int:
    if not args or args[0].startswith("-"):
        valid = ", ".join(a.name for a in channel.actions)
        return _bad_input(channel.name, None, f"missing action for {channel.name}; valid: {valid}")
    action = find_action(channel, args[0])
    if action is None:
        # Let run_action produce the standard unknown-action envelope.
        return _emit(run_action(channel, args[0], {}))
    try:
        ns = _parse_action_args(action, args[1:])
    except _ArgError as exc:
        return _bad_input(channel.name, action.name, f"{exc}; usage: {_usage(channel, action)}")
    timeout = cast(float, ns.pop("timeout"))
    backend = cast(Optional[str], ns.pop("backend"))
    return _emit(run_action(channel, action.name, ns, timeout=timeout, only_backend=backend))


def _cmd_read(args: Sequence[str]) -> int:
    parser = _Parser(prog="read", add_help=False)
    parser.add_argument("url")
    parser.add_argument("--backend", default=None)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    try:
        ns = parser.parse_args(list(args))
    except _ArgError as exc:
        return _bad_input(None, "read", f"{exc}; usage: agent-reach read <url>")
    readers = [ch for ch in unified_channels() if find_action(ch, "read")]
    specific = [ch for ch in readers if ch.name != "web"]
    target = next((ch for ch in specific if ch.can_handle(ns.url)), None)
    action = find_action(target, "read") if target is not None else None
    if action is not None and ns.backend in ("jina", "exa") and not any(label == ns.backend for label, _ in action.backends):
        target = next((ch for ch in readers if ch.name == "web"), None) or target
    target = target or next((ch for ch in readers if ch.name == "web"), None)
    if target is None:
        return _bad_input(None, "read", "no channel can read this URL")
    return _emit(run_action(target, "read", {"target": ns.url}, timeout=ns.timeout, only_backend=ns.backend))


def _cmd_channels(_args: Sequence[str]) -> int:
    channels = []
    for ch in unified_channels():
        channels.append(
            {
                "name": ch.name,
                "description": ch.description,
                "purpose": ch.purpose,
                "level": ch.level,
                "actions": [
                    {
                        "name": a.name,
                        "help": a.help,
                        "usage": _usage(ch, a),
                        "params": [
                            {
                                "name": p.name,
                                "help": p.help,
                                "required": p.required,
                                "type": p.type.__name__,
                                "default": p.default,
                            }
                            for p in a.params
                        ],
                        "backends": [label for label, _ in a.backends],
                    }
                    for a in ch.actions
                ],
            }
        )
    return _emit(
        {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "commands": {
                "read": "agent-reach read <url>  (picks the channel from the URL)",
                "action": "agent-reach <channel> <action> [args] [--timeout SECONDS]",
            },
            "channels": channels,
        }
    )


def _cmd_check(args: Sequence[str]) -> int:
    from . import manage

    parser = _Parser(prog="check", add_help=False)
    parser.add_argument("channels", nargs="*")
    parser.add_argument("--text", action="store_true")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--search", action="store_true", help="Verify search/jobs through the normal route only")
    try:
        ns = parser.parse_args(list(args))
        out = manage.run_checks(ns.channels or None, timeout=ns.timeout, search_only=ns.search)
    except _ArgError as exc:
        return _bad_input(None, "check", str(exc))
    except ReachError as err:
        return _bad_input(None, "check", err.message)
    if ns.text:
        by_action: Dict[tuple, bool] = {}
        for r in out["results"]:
            mark = "PASS" if r["passed"] else "FAIL"
            why = "" if r["passed"] else f"  {r['error']['code']}: {r['error']['message']}"
            sys.stdout.write(f"{mark}  {r['channel']} {r['action']} [{r['backend']}] {r['elapsed_ms']}ms{why}\n")
            key = (r["channel"], r["action"])
            by_action[key] = by_action.get(key, False) or r["passed"]
        sys.stdout.write(f"\n{sum(by_action.values())}/{len(by_action)} actions working\n")
        return 0 if out["ok"] else 1
    return _emit({"schema_version": SCHEMA_VERSION, **out})


def _cmd_doctor(args: Sequence[str]) -> int:
    from . import manage

    parser = _Parser(prog="doctor", add_help=False)
    parser.add_argument("--text", action="store_true")
    try:
        ns = parser.parse_args(list(args))
    except _ArgError as exc:
        return _bad_input(None, "doctor", str(exc))
    if not ns.text:
        return _mgmt("doctor", manage.doctor)
    result = manage.doctor()
    sys.stdout.write(f"Agent Reach {result['version']}\n\n")
    for c in result["channels"]:
        extra = "; ".join(c["problems"]) or c.get("hint", "")
        if c["regressed"]:
            extra = f"REGRESSED (worked at the previous check) {extra}"
        sys.stdout.write(f"{c['status']:<12} {c['channel']:<12} {c['level']:<13} {extra}\n")
    if result["tools"]:
        sys.stdout.write("\nTools\n")
        for t in result["tools"]:
            sys.stdout.write(f"{t['tool']:<16} {str(t.get('version') or '?'):<12} {t.get('owner', '')}\n")
    return 0


def _cmd_skill(args: Sequence[str]) -> int:
    from . import manage

    if not args:
        return _mgmt("skill", manage.skill_status)
    if args[0] not in ("link", "unlink") or len(args) != 2:
        return _bad_input(None, "skill", "usage: agent-reach skill [link|unlink <skills-dir>]")
    if args[0] == "unlink":
        return _mgmt("skill unlink", manage.unlink_skill, args[1])
    return _mgmt("skill link", manage.link_skill, args[1])


def _cmd_management(head: str, args: Sequence[str]) -> int:
    from agent_reach import __version__

    from . import manage

    parser = _Parser(prog=head, add_help=False)
    if head == "setup":
        parser.add_argument("--text", action="store_true")
    elif head in ("add", "remove"):
        parser.add_argument("channel")
    elif head == "update":
        parser.add_argument("--check", action="store_true")
        parser.add_argument("--sync-only", action="store_true")
    elif head == "uninstall":
        parser.add_argument("--dry-run", action="store_true")
    elif head == "configure":
        parser.add_argument("key")
        parser.add_argument("value", nargs="?")
        parser.add_argument("--stdin", action="store_true")
    try:
        ns = parser.parse_args(list(args))
    except _ArgError as exc:
        return _bad_input(None, head, str(exc))
    if head == "setup":
        if ns.text:
            try:
                result = manage.setup()
            except ReachError as err:
                sys.stdout.write(f"설치가 끝나지 않았습니다: {err.code} {err.message}\n{err.hint or ''}\n")
                return 1
            sys.stdout.write(result['installation_report']['markdown'])
            report = result['installation_report']
            if report.get('path'):
                sys.stdout.write(f"\n전체 보고서를 저장했습니다: {report['path']}\n")
            if report.get('write_error'):
                sys.stdout.write(f"\n{report['write_error']}\n")
            return 0 if all(row['ok'] for row in result['default_channels']) else 1
        return _mgmt(head, manage.setup)
    if head == "add":
        return _mgmt(head, manage.add, ns.channel)
    if head == "remove":
        return _mgmt(head, manage.remove, ns.channel)
    if head == "update":
        if ns.sync_only:
            return _mgmt(head, manage.sync)
        return _mgmt(head, manage.update, check_only=ns.check)
    if head == "uninstall":
        return _mgmt(head, manage.uninstall, dry_run=ns.dry_run)
    if head == "configure":
        value = ns.value
        if ns.stdin:
            if sys.stdin.isatty():
                import getpass

                value = getpass.getpass(f"{ns.key}: ").strip()
            else:
                value = sys.stdin.read().strip()
        elif value is None and sys.stdin.isatty():
            import getpass

            value = getpass.getpass(f"{ns.key}: ").strip()
        return _mgmt(head, manage.configure, ns.key, value or "")
    return _mgmt(head, lambda: {"version": __version__, "install_method": manage.install_method()})


def _apply_saved_proxy() -> None:
    """Use the proxy saved by `configure proxy` unless the shell already sets one."""
    # Loopback addresses must never be sent through the outbound proxy.
    bypass = list(dict.fromkeys(x.strip() for key in ("NO_PROXY", "no_proxy")
                               for x in os.environ.get(key, "").split(",") if x.strip()))
    for host in ("localhost", "127.0.0.1", "::1"):
        if host not in bypass:
            bypass.append(host)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(bypass)
    if os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"):
        return
    try:
        from agent_reach.config import Config

        proxy = Config(read_only=True).get("proxy")
    except Exception:  # noqa: BLE001
        return
    if proxy:
        os.environ["HTTPS_PROXY"] = os.environ["HTTP_PROXY"] = str(proxy)


def main(argv: Sequence[str]) -> int:
    from agent_reach.config import ConfigError

    try:
        return _main(argv)
    except ConfigError as exc:
        # Config problems are reported as one JSON error, never as a traceback.
        return _emit({"schema_version": SCHEMA_VERSION, "ok": False, "channel": None, "action": argv[0] if argv else None,
                      "backend": None, "items": [], "meta": {"elapsed_ms": 0, "attempts": []},
                      "error": {"code": NEED_SETUP, "message": str(exc),
                                "hint": "Ask the user to fix or remove ~/.agent-reach/config.yaml. Do not print its contents."}})


def _main(argv: Sequence[str]) -> int:
    global _SHOW_NOTICE

    if not argv or argv[0] in ("help", "-h", "--help"):
        sys.stdout.write(HELP)
        return 0
    from . import tools

    tools.extend_path()
    _SHOW_NOTICE = argv[0] != "update"
    _apply_saved_proxy()
    head, rest = argv[0], argv[1:]
    if head == "channels":
        return _cmd_channels(rest)
    if head == "check":
        return _cmd_check(rest)
    if head == "read":
        return _cmd_read(rest)
    if head == "doctor":
        return _cmd_doctor(rest)
    if head == "skill":
        return _cmd_skill(rest)
    if head in _MANAGEMENT:
        return _cmd_management(head, rest)
    channel = _channel(head)
    if channel is None:
        return _bad_input(None, head, f"unknown command or channel: {head}; run `agent-reach help`")
    return _cmd_action(channel, rest)
