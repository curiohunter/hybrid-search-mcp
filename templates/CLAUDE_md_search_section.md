<!-- BEGIN hybrid-search-mcp routing (v0.3.0) -->
## 검색 전략 — 반드시 이 순서로

이 프로젝트는 `hybrid-search-mcp` Memory Layer가 설치돼 있다. **아래 규칙을 예외 없이 지킬 것.**

### 1차 도구 선택 — 질문 유형별 강제 규칙

| 질문 유형 | 신호 단어 | **반드시 먼저 호출** | 보충 |
|---|---|---|---|
| **기능 탐색** | "어떤 기능", "어떻게 구성", "관련 기능", "설명해줘", "정리해줘", "흐름", "아키텍처" | `mcp__hybrid-search__hybrid_search` | Grep, Read |
| **설계/맥락** | "왜 이렇게", "배경", "이유", "결정", "히스토리", "지난번" | `mcp__hybrid-search__hybrid_search` | Wiki |
| **스키마/DB** | "테이블", "마이그레이션", "DDL", "스키마" | `mcp__hybrid-search__hybrid_search` (file_pattern="*.sql") | Grep |
| **구조/관계** | "전체 그림", "누가 호출", "의존성" | Wiki (`.hybrid-search/wiki/index.md`) | `mcp__hybrid-search__hybrid_search` |
| **정밀 조회** | 정확한 심볼명 / 파일명 / 에러 문자열 | Grep | Read |

### 금지 — 피해야 할 패턴

- **탐색형 질문에 Grep 먼저 호출 금지**. 반드시 `hybrid_search` 먼저.
- 1차에서 답이 부족해도 **도구를 바꾸지 말고 같은 레인에서 보충**한다 (hybrid→wiki 같은 MCP 레인 내, grep→read 같은 텍스트 레인 내).
- Wiki 참조 시 `index.md`부터 시작. `[[링크]]`가 있으면 그걸 따라갈 것.

### Memory Layer가 항상 동작하는 것 — 수동 개입 불필요

매 질문마다 자동으로 벌어지는 일 (당신이 신경쓰지 않아도 됨):

1. **UserPromptSubmit hook**: 질문을 받는 순간 관련 과거 Q&A가 컨텍스트로 주입됨
2. **SessionStart hook**: 세션 시작 시 프로젝트의 최근 Q&A 요약이 주입됨
3. **Stop hook**: 답변 완료 시 질문과 사용 도구가 `.hybrid-search/qa/`에 자동 저장
4. 매 `git commit` 후 변경된 파일만 재인덱싱 + 좀비 wiki 자동 삭제

위 4가지는 시스템이 보장한다. **당신은 그냥 규칙대로 도구만 고르면 된다.**
<!-- END hybrid-search-mcp routing -->
