# 유지 관리

업데이트 실패, 브라우저가 열리지 않는 문제, 채널 오류, 제거 요청이 있을 때 읽습니다.

- 업데이트: `agent-reach update`는 Agent Reach, 추가된 채널의 도구, 스킬을 갱신합니다. `agent-reach update --check`는 새 버전이 있는지만 확인합니다. 사용자가 동의한 경우에만 실행합니다.
- 상태: `agent-reach doctor --text`는 로컬 상태만 읽고 네트워크를 사용하지 않습니다. `agent-reach check <채널> --text`는 네트워크로 한 번 실측합니다(`--search`는 검색만 실측). 설치 성공은 실측 통과를 뜻하지 않습니다.
- 브라우저: `agent-reach browser status`로 상태를 확인하고, `browser setup`으로 확장 프로그램과 연결을 준비하고, `browser stop`으로 닫습니다. 전용 브라우저는 이 컴퓨터에 설치된 Google Chrome을 사용합니다. Chrome이 없으면 `NEED_SETUP`이 나오며, 사용자가 Chrome을 직접 설치해야 합니다. Reach는 브라우저를 내려받지 않습니다.
- 실패 단계 구분: 의존성 다운로드, 브라우저 연결, 로그인, 페이지 로딩, 플랫폼 요청 제한은 서로 다른 문제입니다. 네트워크 시간 초과를 로그인 만료라고 설명하지 않습니다.
- 상위 도구 오류: 오류와 버전을 기록하고, 사용자가 동의하면 `agent-reach update`를 한 번 실행합니다. 상위 도구의 설치 파일을 직접 수정하지 않으며 반복 재시도하지 않습니다.
- `PLATFORM_BLOCKED`, `RATE_LIMITED`, `NEED_LOGIN`, `BUSY`는 이번 시도를 멈추고, 사용자가 처리한 뒤에 계속합니다.
- 키와 프록시: `agent-reach configure groq-key | openai-key | proxy | github-token`. 값은 사용자가 직접 입력합니다. 값을 대화에 출력하지 않습니다.
- 포트 충돌(`Port <n> belongs to an unmanaged browser`): 다른 프로그램이 전용 브라우저 포트(기본 9222)를 사용하고 있습니다. 사용자의 그 프로그램을 종료하지 말고, 사용자가 동의하면 `agent-reach configure browser-port <빈 포트>`를 실행합니다.
- 제거: 먼저 `agent-reach uninstall --dry-run`으로 범위를 보여 주고, 확인을 받은 뒤 `agent-reach uninstall`을 실행합니다. Reach가 설치한 것만 삭제하며, 사용자의 다른 파일과 브라우저는 그대로 둡니다.
