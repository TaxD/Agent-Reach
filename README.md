# Agent Reach (한국어 개발자판)

AI 에이전트가 여러 플랫폼의 공개 콘텐츠를 **읽기 전용**으로 검색하고 읽도록 해 주는 CLI입니다. 명령 하나(`agent-reach`)로 호출하며, 모든 출력은 JSON 한 줄입니다.

이 저장소는 [Panniantong/Agent-Reach](https://github.com/Panniantong/Agent-Reach)(MIT)의 포크입니다. 원본에서 개발자에게 필요한 플랫폼만 남기고, 개발·기술 플랫폼을 추가했으며, 로그인 채널의 브라우저 구조를 보안 위주로 다시 만들었습니다. 원본과 달라진 점은 [CHANGELOG.md](CHANGELOG.md)에 정리되어 있습니다.

## 지원 플랫폼과 용도

각 플랫폼의 **용도**는 에이전트가 "어떤 질문에 어떤 채널을 써야 하는지"를 판단하는 기준입니다. 같은 내용이 `agent-reach channels`의 `purpose` 필드와 설치되는 스킬 문서에도 들어 있습니다.

| 채널 | 용도 | 주요 동작 | 로그인 |
|---|---|---|---|
| `github` | 오픈소스 저장소 검색, README·이슈·PR과 댓글 읽기 | search, read | 불필요 |
| `github-trending` | 지금 주목받는 저장소를 언어·기간별로 파악 | list | 불필요 |
| `stackoverflow` | 오류 메시지·API 사용법 등 정답이 있는 질문의 검증된 답변 | search, read | 불필요 |
| `mdn` | HTML·CSS·JS·Web API·HTTP의 정확한 문법과 동작 (1차 레퍼런스) | search, read | 불필요 |
| `npm` | JS/TS 패키지 선택, 최신 버전·의존성·라이선스·다운로드 수 확인 | search, read | 불필요 |
| `hackernews` | 영어권 기술 뉴스와 실무자 토론, 출시 반응 | search, top, read | 불필요 |
| `lobsters` | 시스템·언어·보안 등 깊이 있는 기술 글과 전문가 토론 | hot, newest, tag, read | 불필요 |
| `devto` | 튜토리얼, 입문·실습 가이드, 개발 경험담 | top, latest, search, read | 불필요 |
| `geeknews` | 한국어로 요약된 기술·스타트업 뉴스 | latest, search, read | 불필요 |
| `yozm` | 한국 IT 실무자의 개발·기획·디자인 심층 글 (요즘IT) | latest, search, read | 불필요 |
| `medium` | 엔지니어링 블로그와 장문 에세이 | tag, user, search, read | 불필요 |
| `producthunt` | 새로 출시된 제품·AI 도구·개발자 도구 탐색 | latest, search, read | 불필요 |
| `dribbble` | UI·앱·웹 디자인 참고 사례 | search, read | 불필요 |
| `youtube` | 강연·튜토리얼 영상 검색, 자막 전문으로 내용 파악 | search, read, subtitle, transcribe | 불필요 |
| `bluesky` | 개발자·연구자의 실시간 반응과 공지 | search, profile, posts, read | 불필요 |
| `reddit` | 일반 사용자와 실무자의 경험담·추천·비교 | search, read, subreddit | 선택 (켜야 사용) |
| `twitter` | 인물·회사·프로젝트의 공식 발표와 실시간 반응 (실험적) | search, profile, tweets, thread | 필요 (켜야 사용) |
| `rss` | 주소를 아는 블로그·릴리스 노트의 RSS/Atom 피드 | fetch | 불필요 |
| `web` | 전용 채널이 없는 사이트의 검색과 페이지 읽기 | search, read | 불필요 |

## 설치

[uv](https://docs.astral.sh/uv/)가 필요합니다.

검토한 커밋을 지정해서 설치하는 것을 권장합니다.

```bash
uv tool install git+https://github.com/TaxD/Agent-Reach@<커밋 해시>
```

```bash
agent-reach setup --text
```

`setup`은 기본 채널을 켜고, 에이전트용 스킬을 `~/.agents/skills/agent-reach/`에 설치한 뒤, Claude Code(`~/.claude/skills/`)를 비롯해 감지된 AI 도구에 연결합니다. 자세한 절차는 [docs/install.md](docs/install.md)를 참고하십시오.

> PyPI의 `agent-reach` 패키지는 이 프로젝트가 아닙니다. PyPI에서 설치하지 마십시오.

## 사용법

```bash
agent-reach channels                                  # 채널별 용도·동작·인자 (JSON)
agent-reach hackernews search "rust async" --limit 5
agent-reach stackoverflow search "TypeError: Cannot read properties of undefined"
agent-reach mdn read /ko/docs/Web/API/Fetch_API
agent-reach npm read @types/node
agent-reach github-trending list --language python --since weekly
agent-reach geeknews latest --limit 5
agent-reach youtube subtitle https://www.youtube.com/watch?v=jNQXAC9IVRw
agent-reach rss fetch https://github.blog/feed/ --limit 5
agent-reach read https://news.ycombinator.com/item?id=8863   # 링크에 맞는 채널 자동 선택
```

성공하면 `ok: true`와 함께 `items`(각 항목은 `title / url / author / published_at / text / raw`)가 반환되고, 실패하면 `ok: false`와 `error.code / message / hint`가 반환됩니다. 오류 코드의 의미는 [docs/troubleshooting.md](docs/troubleshooting.md)에 있습니다.

## 관리 명령

```bash
agent-reach doctor --text          # 채널 상태 (네트워크 사용 안 함)
agent-reach check <채널> --text    # 채널 실측
agent-reach add twitter            # 로그인 채널 켜기 (Node.js와 Chrome은 미리 설치되어 있어야 함)
agent-reach login twitter          # 자동화가 붙지 않은 일반 Chrome 창(전용 프로필)에서 사용자가 직접 로그인
agent-reach browser stop           # 로그인을 마친 뒤 로그인 창 닫기 (이후 조회 가능)
agent-reach logout twitter
agent-reach configure github-token # 값은 숨김 입력
agent-reach uninstall --dry-run    # 삭제될 항목 미리 보기
```

## 보안 모델

- **읽기 전용**: 글쓰기·댓글·좋아요·DM 동작이 없습니다. 로그인 채널에서도 읽기 전용 명령만 실행할 수 있습니다.
- **가져온 콘텐츠는 신뢰할 수 없는 데이터**: 스킬 문서는 에이전트에게 가져온 글 안의 지시를 따르지 말라고 명시합니다. 외부 도구의 안내문은 `hint`로 전달하지 않습니다.
- **공개 데이터만**: GitHub 토큰이 있어도 비공개 저장소의 내용은 반환하지 않습니다.
- **로그인 채널은 선택 사항**: `reddit`의 사이트 동작과 `twitter`는 사용자가 `add`로 켜기 전에는 브라우저를 띄우지 않습니다.
- **전용 브라우저**: 설치된 Chrome만 헤드리스가 아닌 창으로 실행하고, 네트워크 디버깅 포트를 열지 않습니다. 쿠키를 파일로 내보내거나 다른 브라우저에서 가져오지 않습니다.
- **OpenCLI 보안 강화 포크**: 로컬 데몬은 비밀 토큰으로 서명된 요청만 받아들이고 응답에도 서명합니다. 토큰은 전송되지 않습니다.
- **공급망**: OpenCLI는 잠금 파일의 무결성 해시로, 확장 프로그램은 SHA-256과 파일 트리 다이제스트로 검증하며, 설치된 버전이 고정된 버전과 다르면 실행하지 않습니다. 설치 스크립트를 실행하지 않고, Node.js나 브라우저를 내려받지 않으며, 프로그램이 스스로를 업그레이드하지 않습니다.
- **SSRF 방지**: 신규 채널과 `rss`의 요청은 실제로 접속하는 IP가 공인 유니캐스트 주소인지 접속 시점에 확인하고, 리디렉션마다 다시 검증합니다(프록시를 거치는 요청은 예외이며 SECURITY.md에 설명되어 있습니다).
- **외부 전송**: `web`과 웹 검색 경유 동작은 검색어·주소를 Exa/Jina로 보내고, `youtube transcribe`는 음성을 Groq/OpenAI로 보냅니다.

알려진 한계와 취약점 신고 방법은 [SECURITY.md](SECURITY.md)를 참고하십시오.

## 라이선스

MIT. 원저작자의 저작권 표시는 [LICENSE](LICENSE)에 유지되어 있습니다.
