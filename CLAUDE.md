# Hybrid Search MCP

BM25 + Vector 하이브리드 검색 MCP 서버.

## 실행 환경

```bash
source .venv/bin/activate
python -m pytest tests/ -x -q
```

<!-- hybrid-search -->
## 검색 전략 — 의도 기반 라우팅

고정 순서가 아니라 **질문 유형에 따라 1차 도구를 선택**하고, 부족하면 fallback으로 보충한다.

| 질문 유형 | 신호 | 1차 | fallback |
|-----------|------|-----|----------|
| 구조/관계 | "누가 호출", 의존, 모듈 구조, 전체 그림 | Wiki | hybrid_search |
| 기능 탐색 | 자연어, 한국어, 넓은 기능 질문 | hybrid_search | Wiki |
| 정밀 조회 | 정확한 심볼명, 파일명, 에러 문자열 | Grep | Read |
| 설계/맥락 | "왜 이렇게", QA 히스토리, 계획 문서 | hybrid_search | Wiki |
| 스키마/DB | 마이그레이션, DDL, 테이블 구조 변화 | hybrid_search (node_types/file_pattern 활용) | Grep |

**운영 규칙**:
- 1차에서 답이 부족하면 도구를 **바꾸지 말고 보충**한다 (hybrid→wiki, wiki→hybrid, grep→read)
- Wiki는 `.hybrid-search/wiki/index.md`에서 시작. `[[링크]]`가 있으면 따라갈 것
- hybrid_search는 한국어 자연어 질의 + 코드/문서/계획 문서 크로스 도메인 검색이 강점
