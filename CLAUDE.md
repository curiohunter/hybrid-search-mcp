# Hybrid Search MCP

BM25 + Vector 하이브리드 검색 MCP 서버. 한국어↔영어 크로스 언어 코드 검색.

## 검색 우선순위 (필수)

코드를 수정하거나 기능을 이해할 때, **단편적으로 파일 하나만 보지 말 것.**
반드시 아래 순서로 관련 맥락을 먼저 확보하라:

1. **wiki 먼저 확인**: `.hybrid-search/wiki/index.md` → 해당 주제 wiki 페이지 Read
   - wiki에 `[[링크]]`가 있으면 연결된 페이지도 반드시 읽을 것
2. **hybrid_search 사용**: Grep/Glob 전에 `hybrid_search` MCP 도구로 검색
   - 한국어 질문도 지원 (크로스 언어 검색)
   - 벡터 검색이 의미적으로 관련된 코드를 찾아줌
3. **Grep/Glob은 보조**: 정확한 심볼명을 알 때만 사용

### 왜?

Claude Code는 수정 대상 파일만 보고 관련 DB 스키마, 서비스 로직, 설계 문서를 놓치는 경우가 많다.
hybrid_search + wiki 그래프는 진입점에서 연결된 맥락을 자동 확장해준다.
**파일 하나 수정하더라도, 관련된 것을 전부 본 후에 수정하라.**

## 코드베이스 Wiki

이 프로젝트의 코드베이스 wiki는 `.hybrid-search/wiki/index.md`에 있습니다.

### Stale 자동 갱신

`.hybrid-search/wiki/STALE.md` 파일이 존재하면, 소스 코드가 변경된 wiki 페이지가 있다는 뜻이다.
**wiki를 읽기 전에 STALE.md를 먼저 확인하고, stale 페이지가 있으면 해당 페이지를 먼저 갱신하라.**

갱신 절차:
1. STALE.md에서 stale 페이지 목록과 변경된 파일 확인
2. 변경된 소스 파일을 Read로 읽기
3. wiki 페이지 내용을 현재 코드에 맞게 Edit으로 수정
4. 모든 stale 페이지 갱신 후 STALE.md 삭제

## 실행 환경

```bash
source .venv/bin/activate
python -m pytest tests/ -x -q          # 테스트
python -m hybrid_search.cli status     # 인덱스 상태
python -m hybrid_search.cli reindex --cwd .  # 리인덱싱
```

## 임베딩

- OpenAI `text-embedding-3-small` (API 키: `.env.local`)
- 로컬 모델 사용 안 함 — CPU/메모리 부하 제로
- 비용: 인덱싱 ~$0.04, 검색 사실상 무료
