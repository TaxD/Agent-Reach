# Agent Reach 설치와 업데이트

작업을 수행하는 AI를 위한 안내입니다. 모든 명령은 직접 실행하십시오. `agent-reach` 명령은 JSON 객체 하나를 출력하므로 `ok`, `result`, `error.hint`를 읽고 판단합니다.

> PyPI의 `agent-reach` 패키지는 이 프로젝트가 아닙니다. PyPI에서 설치하지 말고 아래의 GitHub 주소만 사용하십시오.

## 0. 시작하기 전에

- 터미널 명령을 실행할 수 있어야 합니다.
- 이미 설치되어 있는지 확인합니다: `agent-reach version`이 JSON을 출력하면 [업데이트](#업데이트)로 이동합니다.
- `uv --version`이 실패하면 사용자에게 uv 설치를 요청하십시오(<https://docs.astral.sh/uv/getting-started/installation/>). Homebrew가 있으면 `brew install uv`가 가장 간단합니다. 검증되지 않은 설치 스크립트를 사용자 동의 없이 파이프로 실행하지 마십시오.

## 1. 설치와 설정

```bash
uv tool install git+https://github.com/TaxD/Agent-Reach@<검토한 커밋 해시>
```

```bash
agent-reach setup
```

설치 후 `agent-reach`를 찾지 못하면 `uv tool update-shell`을 실행하고 새 셸을 여십시오.

`setup`은 모든 채널을 켜고 에이전트 스킬을 설치합니다. 채널은 추가 도구가 필요 없고, `youtube`만 전사용 ffmpeg를 준비합니다(시스템에 있으면 그것을 사용). 이전 버전이 남긴 브라우저 프로필과 저장된 쿠키가 있으면 이때 삭제합니다.

## 2. 사용자에게 보고

`result.installation_report.markdown`을 그대로 사용자에게 보여 주십시오. 프로그램이 생성한 보고서이므로 고쳐 쓰거나 순서를 바꾸거나 덧붙이지 않습니다.

## 업데이트

프로그램은 스스로 새 버전을 내려받아 설치하지 않습니다. `agent-reach update --check`로 새 버전이 있는지 확인하고, 사용자가 검토한 커밋을 직접 설치합니다.

```bash
uv tool install --force git+https://github.com/TaxD/Agent-Reach@<검토한 커밋 해시>
```

로컬 복제본에서 설치한 경우에는 복제본을 갱신한 뒤 다시 설치합니다.

```bash
uv tool install --force /path/to/Agent-Reach
```

```bash
agent-reach update --sync-only
```

## 이후

- 키는 사용자가 직접 입력합니다. 예: YouTube 전사용 Groq 키 → `agent-reach configure groq-key`
- 그 밖의 사용법은 설치된 스킬 문서에 있습니다.

문제 해결과 이전 버전으로 되돌리기: [troubleshooting.md](troubleshooting.md)
