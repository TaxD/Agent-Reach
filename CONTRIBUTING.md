# 기여 안내

## 개발 환경

```bash
uv venv .venv && uv pip install -p .venv -c constraints.txt -e '.[dev]'
```

```bash
.venv/bin/python -m pytest -q && .venv/bin/ruff check agent_reach tests && .venv/bin/mypy agent_reach
```

## 채널 추가하기

1. `agent_reach/channels/`에 `Channel`을 상속한 클래스를 만듭니다. `name`, `description`(짧은 한국어 이름표), `purpose`(에이전트가 이 채널을 언제 골라야 하는지에 대한 한국어 설명), `level`, `actions`, `can_handle`을 정의합니다.
2. 로그인이 필요 없는 공개 API는 `_public.py`의 `get_json`·`feed_items`를 사용하고, 요청 호스트는 코드에 고정합니다. 호출자가 넘긴 값은 형식을 검증한 뒤 `urlencode`/`quote`로만 URL에 넣습니다.
3. 호출자가 고른 주소로 접속해야 한다면 반드시 `fetch_public`을 사용합니다(SSRF 방어).
4. `agent_reach/channels/__init__.py`의 `ALL_CHANNELS`에 등록하고, `tests/`에 네트워크 없이 도는 테스트를 추가합니다.
5. 반환하는 텍스트에는 상한을 둡니다. 가져온 콘텐츠는 신뢰할 수 없는 데이터입니다.

## 원칙

- 읽기 전용입니다. 쓰기 동작(게시, 댓글, 좋아요)은 받지 않습니다.
- 새 의존성보다 표준 라이브러리를 우선합니다.
- 내려받아 실행하는 것은 버전과 해시를 고정합니다.
