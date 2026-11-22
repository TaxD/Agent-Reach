# 변경 내역

## 한국어 개발자판 (원본 1.6.1 기준 포크)

[Panniantong/Agent-Reach](https://github.com/Panniantong/Agent-Reach) 1.6.1에서 갈라져 나왔습니다.

### 제거

- 채널: bilibili, boss, facebook, instagram, linkedin, v2ex, xiaohongshu, xiaoyuzhou, xueqiu
- 전용 브라우저 구조 전체: 로그인·로그아웃·쿠키 가져오기, 디버깅 포트, OpenCLI와 브라우저 확장 프로그램, Node·npm 설치, CloakBrowser
- reddit의 로그인 기반 동작(subreddit, 사이트 내부 검색), twitter의 search·profile·tweets·thread
- bili-cli, boss-agent-cli, uv 자동 설치, 프로그램의 자체 업그레이드
- 1.5 이전 버전에서의 이전(마이그레이션) 로직, 릴리스 승인 스크립트와 릴리스 워크플로
- 중국어 문서·문자열·스폰서 자료

### 추가

- 채널: hackernews, stackoverflow, lobsters, devto, mdn, github-trending, npm, producthunt, medium, dribbble, bluesky, rss, geeknews, yozm
- 모든 채널에 에이전트용 용도 설명(`purpose`)을 추가하고 `agent-reach channels`와 스킬 문서에 노출
- 문서, 스킬, 채널 설명, 도움말, 설치 보고서를 한국어로 작성

### 보안

- 모든 채널이 로그인 없이 공개 데이터만 읽음(브라우저, 쿠키, 확장 프로그램 없음)
- reddit은 공개 검색·읽기, twitter는 공개 임베드로 트윗 하나 읽기만 제공
- GitHub 토큰이 있어도 비공개 저장소를 반환하지 않음
- 직접 접속하는 요청에 SSRF·DNS 리바인딩·XML DTD 방어와 크기 제한 적용
- yt-dlp와 내부 파이썬 호출을 격리해서 실행
- 자세한 내용은 [SECURITY.md](SECURITY.md) 참고
