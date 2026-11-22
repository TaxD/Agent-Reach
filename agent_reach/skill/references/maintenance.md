# 유지 관리

채널 오류, 업데이트 확인, 제거 요청이 있을 때 읽습니다.

- 업데이트: `agent-reach update --check`는 새 버전이 있는지만 확인합니다. `agent-reach update`는 도구와 스킬 파일을 현재 설치본에 맞게 정리할 뿐이고, 프로그램 자체를 내려받아 교체하지 않습니다. 새 버전의 설치는 사용자가 직접 합니다.
- 상태: `agent-reach doctor --text`는 로컬 상태만 읽고 네트워크를 사용하지 않습니다. `agent-reach check <채널> --text`는 네트워크로 한 번 실측합니다(`--search`는 검색만 실측). 설치 성공은 실측 통과를 뜻하지 않습니다.
- 실패 단계 구분: 네트워크 시간 초과, 플랫폼의 요청 제한, 플랫폼의 차단은 서로 다른 문제입니다. 있는 그대로 전달합니다.
- `PLATFORM_BLOCKED`, `RATE_LIMITED`는 이번 시도를 멈춥니다. 우회를 시도하지 않습니다.
- 도구: 설치하는 외부 도구는 YouTube 전사용 ffmpeg 하나뿐이며, 시스템에 ffmpeg가 있으면 그것을 사용합니다.
- 키와 프록시: `agent-reach configure groq-key | openai-key | proxy | github-token`. 사용자가 자신의 터미널에서 실행해 숨김 입력으로 넣습니다. 값을 대화로 받거나 출력하지 않습니다.
- 제거: 먼저 `agent-reach uninstall --dry-run`으로 범위를 보여 주고, 확인을 받은 뒤 `agent-reach uninstall`을 실행합니다. Reach가 설치한 것만 삭제하며, 사용자의 다른 파일은 그대로 둡니다.
