# Hybrid Search MCP

BM25 + Vector 하이브리드 검색 MCP 서버.

## 실행 환경

```bash
source .venv/bin/activate
python -m pytest tests/ -x -q
```

<!-- BEGIN hybrid-search-mcp routing v1 -->
## 검색 전략 — 반드시 이 순서로

이 프로젝트는 `hybrid-search-mcp` Memory Layer가 설치돼 있다. **아래 규칙을 예외 없이 지킬 것.**

| 질문 유형 | 신호 | **반드시 먼저 호출** | 보충 |
|---|---|---|---|
| **기능 탐색** | "어떤 기능", "관련 기능", "어떻게 구성", "흐름", "설명해줘", "정리해줘", "아키텍처" | `mcp__hybrid-search__hybrid_search` | Grep, Read |
| **설계/맥락** | "왜 이렇게", "배경", "이유", "결정", "히스토리", "지난번" | `mcp__hybrid-search__hybrid_search` | Wiki |
| **스키마/DB** | "테이블", "마이그레이션", "DDL", "스키마 변화" | `mcp__hybrid-search__hybrid_search` (file_pattern `*.sql`) | Grep |
| **구조/관계** | "전체 그림", "누가 호출", "의존" | Wiki (`.hybrid-search/wiki/index.md`) | `mcp__hybrid-search__hybrid_search` |
| **정밀 조회** | 정확한 심볼명 / 파일명 / 에러 문자열 | Grep | Read |

**운영 규칙**:
- **탐색형 질문에 Grep 먼저 호출 금지** — 반드시 `mcp__hybrid-search__hybrid_search` 먼저.
- **쿼리는 사용자의 자연어 문장을 그대로** 쓸 것 — 키워드 뭉치로 재작성 금지.
  (예: "우리 환불 기능에 대해 알려줘" ⭕ / "환불 퇴원 refund 워크플로우 정산" ❌)
  자연어 문장이 벡터 매칭 품질이 더 좋고, 분류기가 가중치를 자동 조정한다.
- 1차에서 답이 부족해도 도구를 **바꾸지 말고 같은 레인에서 보충** (hybrid→wiki MCP 레인, grep→read 텍스트 레인).
- Wiki는 `.hybrid-search/wiki/index.md`에서 시작, `[[링크]]` 있으면 따라갈 것.

**자동 동작 (수동 개입 불필요)**:
- 질문 시작 시 관련 과거 Q&A 자동 컨텍스트 주입 (UserPromptSubmit)
- 세션 시작 시 최근 Q&A 요약 주입 (SessionStart)
- 답변 종료 시 `.hybrid-search/qa/`에 자동 저장 (Stop)
- `git commit` 후 변경 파일만 재인덱싱 + 좀비 wiki 자동 삭제

**자기 정당화 (Self-justify)**:
- 모든 검색 호출 직전, **한 문장으로 어떤 도구를 골랐고 왜인지** 말할 것.
- 예: "탐색형 질문이라 `mcp__hybrid-search__hybrid_search` 먼저 호출합니다."

**Confidence 계약 (weak → fallback)**:
- `hybrid_search` 응답의 `confidence: weak`이면 답하기 전에 `fallback_hint`에 적힌 대체 도구로 한 번 더 시도할 것.
- `strong`/`mixed`면 그대로 진행.
<!-- END hybrid-search-mcp routing v1 -->
