# 문제 해결

다음 명령부터 실행합니다.

```bash
agent-reach doctor --text            # 모든 채널의 상태 (네트워크 사용 안 함)
```

```bash
agent-reach check <채널> --text      # 채널 하나를 실측
```

## 오류 코드

실패한 명령은 `ok: false`와 함께 `code`, `message`, `hint`가 담긴 `error`를 반환합니다. `hint`가 대개 다음에 할 일입니다.

| 코드 | 의미 | 대응 |
|---|---|---|
| `NEED_SETUP` | 도구나 키가 없거나 로그인 채널이 켜져 있지 않음 | `agent-reach doctor --text`로 확인. 로그인 채널은 사용자가 원할 때 `agent-reach add <채널>` |
| `NEED_LOGIN` | 로그인이 없거나 만료됨 | 사용자가 `agent-reach login <채널>`로 직접 로그인. 연결 오류나 플랫폼 인증 오류를 고치려고 자격 증명을 삭제하지 말 것 |
| `UPSTREAM_BROKEN` | 상위 도구나 플랫폼의 실패 | 반복 재시도하지 말 것. 나중에 다시 시도하고 `agent-reach check <채널>`로 확인 |
| `TIMEOUT` | 시간 안에 응답이 없음 | 나중에 다시 시도하거나 `--timeout 초`를 늘림. 제한된 네트워크에서는 프록시 설정(아래) |
| `RATE_LIMITED` | 플랫폼이 요청을 제한함 | `retry_after_seconds`만큼 대기. 계정이나 백엔드를 바꿔서 계속 요청하지 말 것 |
| `PLATFORM_BLOCKED` | 플랫폼이 사람 인증을 요구함 | 중단하고 사용자가 공식 페이지에서 직접 처리 |
| `BUSY` | 다른 Reach 명령이 같은 브라우저를 사용 중 | 잠시 후 다시 시도 |
| `NOT_FOUND` | 내용이 없음 | 링크, id, 사용자 이름을 확인 |
| `BAD_INPUT` | 인자가 잘못되었거나 채널·동작이 없음 | `agent-reach channels`로 사용법 확인 |

## doctor 상태

| 상태 | 의미 | 대응 |
|---|---|---|
| `not_added` | 선택 채널이 켜져 있지 않음 | `agent-reach add <채널>` |
| `needs_setup` | 무언가 빠져 있음(`problems` 참고) | `agent-reach add <채널>` 또는 안내된 설정 단계 |
| `needs_login` | 마지막 실측이 로그인 때문에만 실패 | `agent-reach login <채널>` (사용자가 로그인) |
| `unverified` | 설정은 되었지만 최근 24시간 안에 실측이 없음 | `agent-reach check <채널>` |
| `search_ready` | 최근 24시간 안에 검색이 결과를 반환함 | 읽기 등 다른 동작은 별도 실측이 필요 |
| `ready` | 최근 24시간 안에 기록된 실측을 통과함 | — |
| `cooldown` | 요청 제한이 아직 유효함 | `retry_after_seconds`만큼 대기 |
| `broken` | 마지막 실측이 다른 이유로 실패 | `agent-reach check <채널> --text`로 원인 확인 |

`doctor` 출력에서 `regressed: true`는 이전 실측에서 통과한 동작이 최근 실측에서 실패했다는 뜻입니다.

## 전용 브라우저 (reddit, twitter)

- 로그인 채널은 `agent-reach add <채널>`로 켜야 동작합니다. 켜지 않은 상태의 사이트 동작은 `NEED_SETUP`을 반환합니다.
- Google Chrome과 Node.js 20.18.1 이상이 설치되어 있어야 합니다. 없으면 `NEED_SETUP`이 반환되므로 사용자가 직접 설치합니다. macOS와 Linux만 지원합니다.
- 전용 브라우저의 창은 항상 보이며, 10분간 사용하지 않으면 닫힙니다. `agent-reach browser status`의 `debugging_port`는 항상 `null`입니다(네트워크 포트를 열지 않습니다).
- `login <채널>`은 전용 창에 로그인 페이지를 열고, 사용자가 그 창에서 로그인합니다. 다른 브라우저의 로그인을 가져오는 기능은 없습니다.
- `Another OpenCLI daemon is already using port 19825`: 사용자가 따로 실행한 OpenCLI나 다른 프로그램이 포트를 쓰고 있습니다. Reach는 그 프로그램에 아무것도 보내지 않으므로, 사용자가 직접 종료한 뒤 다시 시도합니다.
- 확장 프로그램 설치가 `checksum` 오류로 실패하면 고정된 릴리스 파일이 바뀌었거나 다운로드가 변조된 것입니다. 반복 재시도하지 말고 이슈로 알려 주십시오.
- 브라우저가 열리지 않으면 `~/.agent-reach/browser/watcher.log`의 마지막 줄을 확인합니다.

## 프록시

일부 플랫폼은 특정 네트워크나 지역을 차단합니다. 프록시를 한 번 저장합니다(주소에 비밀번호가 포함될 수 있으므로 숨김 입력으로 받습니다).

```bash
agent-reach configure proxy
```

셸에 설정된 `HTTPS_PROXY`가 우선합니다. `rss fetch`는 접속 주소를 검증해야 하므로 프록시를 거치지 않고 직접 접속합니다. 프록시를 사용하는 동안에는 접속 주소 고정 방어가 적용되지 않습니다([SECURITY.md](../SECURITY.md) 참고).

## AI가 스킬을 인식하지 못할 때

`agent-reach skill`은 스킬의 실제 위치(`~/.agents/skills/agent-reach/SKILL.md`)와 다른 AI용으로 만든 링크를 보여 줍니다. 자동으로 연결되지 않은 AI는 `agent-reach skill link <그 AI의 skills 폴더>`로 연결하고, 연결을 원하지 않으면 `agent-reach skill unlink <폴더>`를 실행합니다.

## 이전 버전으로 되돌리기

```bash
uv tool install --force git+https://github.com/TaxD/Agent-Reach@<이전 커밋 해시>
```

```bash
agent-reach update --sync-only
```

## 그래도 해결되지 않을 때

- 채널을 제거했다가 다시 추가합니다: `agent-reach remove <채널>` 후 `agent-reach add <채널>` (제거하면 해당 사이트의 로그인과 쿠키도 지워집니다).
- <https://github.com/TaxD/Agent-Reach/issues>에 `agent-reach doctor --text`와 `agent-reach check <채널> --text`의 출력을 첨부해 이슈를 등록합니다. 개인 정보는 먼저 지우십시오.
