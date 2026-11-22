# 변경 내역

## 한국어 개발자판 (원본 1.6.1 기준 포크)

[Panniantong/Agent-Reach](https://github.com/Panniantong/Agent-Reach) 1.6.1에서 갈라져 나왔습니다.

### 제거

- 채널: bilibili, boss, facebook, instagram, linkedin, v2ex, xiaohongshu, xiaoyuzhou, xueqiu
- CloakBrowser(탐지 회피 브라우저) 백엔드, bili-cli, boss-agent-cli, uv·Node.js 자동 설치
- 다른 브라우저의 로그인 가져오기, 쿠키 스냅샷 파일, 디버깅 포트, 헤드리스 모드, 프로그램의 자체 업그레이드
- 1.5 이전 버전에서의 이전(마이그레이션) 로직, 릴리스 승인 스크립트와 릴리스 워크플로
- 중국어 문서·문자열·스폰서 자료

### 추가

- 채널: hackernews, stackoverflow, lobsters, devto, mdn, github-trending, npm, producthunt, medium, dribbble, bluesky, rss, geeknews, yozm
- 모든 채널에 에이전트용 용도 설명(`purpose`)을 추가하고 `agent-reach channels`와 스킬 문서에 노출
- 문서, 스킬, 채널 설명, 도움말, 설치 보고서를 한국어로 작성

### 보안

- 전용 브라우저를 다시 설계: 설치된 Chrome만 사용, 창이 항상 보임, 네트워크 디버깅 포트 없음(파이프와 비공개 유닉스 소켓), 쿠키를 내보내거나 가져오지 않음
- OpenCLI를 보안 강화 포크(TaxD/OpenCLI)로 교체: 데몬이 토큰 서명 요청만 수락하고 자신의 신원을 증명
- OpenCLI는 잠금 파일의 무결성 해시로, 확장 프로그램은 SHA-256과 트리 다이제스트로 검증
- 로그인 채널은 사용자가 켜기 전에는 실행되지 않고, 읽기 전용 명령만 호출
- GitHub 토큰이 있어도 비공개 저장소를 반환하지 않음
- 직접 접속하는 요청에 SSRF·DNS 리바인딩·XML DTD 방어와 크기 제한 적용
- yt-dlp와 내부 파이썬 호출을 격리해서 실행
- 자세한 내용은 [SECURITY.md](SECURITY.md) 참고
