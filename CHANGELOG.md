# 변경 내역

## 한국어 개발자판 (원본 1.6.1 기준 포크)

[Panniantong/Agent-Reach](https://github.com/Panniantong/Agent-Reach) 1.6.1에서 갈라져 나왔습니다.

### 제거

- 채널: bilibili, boss, facebook, instagram, linkedin, v2ex, xiaohongshu, xiaoyuzhou, xueqiu
- CloakBrowser(탐지 회피 브라우저) 백엔드, bili-cli, boss-agent-cli, uv 자동 설치
- 1.5 이전 버전에서의 이전(마이그레이션) 로직, 릴리스 승인 스크립트와 릴리스 워크플로
- 중국어 문서·문자열·스폰서 자료

### 추가

- 채널: hackernews, stackoverflow, lobsters, devto, mdn, github-trending, npm, producthunt, medium, dribbble, bluesky, rss, geeknews, yozm
- 모든 채널에 에이전트용 용도 설명(`purpose`)을 추가하고 `agent-reach channels`와 스킬 문서에 노출
- 문서, 스킬, 채널 설명, 도움말, 설치 보고서를 한국어로 작성

### 보안

- 설치되어 있는 Google Chrome만 사용(브라우저 바이너리를 내려받지 않음)
- 다른 브라우저의 쿠키를 자동으로 복사하지 않음(명시적 요청 전용)
- 브라우저 확장 프로그램을 고정 버전으로 내려받아 SHA-256 검증 후 설치
- 임의 주소를 받는 피드 읽기에 SSRF·DNS 리바인딩·XML 엔티티 방어 적용
- 자세한 내용은 [SECURITY.md](SECURITY.md) 참고
