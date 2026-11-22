# 유지 관리

업데이트 실패, 브라우저가 열리지 않는 문제, 채널 오류, 제거 요청이 있을 때 읽습니다.

- 업데이트: `agent-reach update --check`는 새 버전이 있는지만 확인합니다. `agent-reach update`는 도구와 스킬 파일을 현재 설치본에 맞게 정리할 뿐이고, 프로그램 자체를 내려받아 교체하지 않습니다. 새 버전의 설치는 사용자가 직접 합니다.
- 상태: `agent-reach doctor --text`는 로컬 상태만 읽고 네트워크를 사용하지 않습니다. `agent-reach check <채널> --text`는 네트워크로 한 번 실측합니다(`--search`는 검색만 실측). 설치 성공은 실측 통과를 뜻하지 않습니다.
- 브라우저(로그인 채널 전용): `agent-reach browser status`로 상태를 확인하고, `browser setup`으로 열고, `browser stop`으로 닫습니다. 설치된 Google Chrome과 Node.js 20.18.1 이상이 필요하며, 없으면 `NEED_SETUP`이 나오므로 사용자가 직접 설치합니다. Reach는 브라우저나 Node.js를 내려받지 않습니다.
- 실패 단계 구분: 의존성 다운로드, 브라우저 연결, 로그인, 페이지 로딩, 플랫폼 요청 제한은 서로 다른 문제입니다. 네트워크 시간 초과를 로그인 만료라고 설명하지 않습니다.
- 상위 도구 오류: 오류를 그대로 전달하고 반복 재시도하지 않습니다. 설치된 도구의 파일을 직접 수정하지 않습니다(확장 프로그램 파일이 바뀌면 다음 실행 때 고정 릴리스로 다시 설치됩니다).
- `PLATFORM_BLOCKED`, `RATE_LIMITED`, `NEED_LOGIN`, `BUSY`는 이번 시도를 멈추고, 사용자가 처리한 뒤에 계속합니다.
- 키와 프록시: `agent-reach configure groq-key | openai-key | proxy | github-token`. 사용자가 자신의 터미널에서 실행해 숨김 입력으로 넣습니다. 값을 대화로 받거나 출력하지 않습니다.
- `Another OpenCLI daemon is already using port 19825`: 사용자가 따로 실행한 OpenCLI이거나 다른 프로그램이 그 포트를 쓰고 있습니다. Reach는 그 프로그램을 종료하지 않고, 그 프로그램에 아무것도 보내지 않습니다. 사용자에게 알리고 사용자가 직접 종료하게 합니다.
- 제거: 먼저 `agent-reach uninstall --dry-run`으로 범위를 보여 주고, 확인을 받은 뒤 `agent-reach uninstall`을 실행합니다. Reach가 설치한 것만 삭제하며, 사용자의 다른 파일과 브라우저는 그대로 둡니다.
