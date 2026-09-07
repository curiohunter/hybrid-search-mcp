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
- MCP 서버가 많은 환경에선 이 도구가 **deferred**(스키마 미로드)일 수 있다 — 직접 호출이 실패하면 `ToolSearch`(query `select:mcp__hybrid-search__hybrid_search`)로 로드한 뒤 다시 호출할 것. Grep으로 이탈 금지.
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

## 공개물에 코퍼스를 인용하지 말 것 (2026-09-07)

이 레포는 **public**이고 PyPI에 배포된다. 그런데 이 도구는 남의 코퍼스를
측정하는 물건이라, 산출물이 **측정 대상을 그대로 인용**한다. 실제로
벤치 결과 JSON이 도그푸딩 대상의 실제 기관명 7종(124회), 분반명, 내부 문서
트리 경로를 GitHub와 PyPI sdist 양쪽에 실어 날랐다.

**규칙**

1. **벤치 골드셋과 결과물은 커밋하지 않는다.** 러너(`benchmarks/*.py`)는
   공유하고, 무엇을 쟀는지는 재는 쪽 프로젝트에 둔다. `.gitignore`에
   패턴이 있으니 새 산출물도 그 이름 규칙을 따를 것.
2. **테스트 픽스처는 합성 데이터로.** 경로 해석·중첩 루트 같은 테스트에
   실제 학교·반·거래처 이름이 필요했던 적은 없다. 오픈소스가 감추는 것은
   테스트가 아니라 픽스처 안의 진짜 데이터다.
3. **배포 전 sdist를 열어볼 것.** 휠에는 `src/`만 들어가지만 sdist는
   기본이 작업 트리 전체다. `pyproject.toml`의
   `[tool.hatch.build.targets.sdist] exclude`로 막아 뒀고,
   릴리스 전에 `tar tzf dist/*.tar.gz | grep benchmarks` 로 확인한다.
4. **공개 전 스캔.** 커밋·푸시 전에 실명 패턴을 훑는다 — 기관명
   (`[가-힣]{2,4}(여고|여중|고등학교|중학교)`), 분반명, 도그푸딩 대상의 내부
   문서 디렉토리명, 사람 이름, 원문 사용자 발화. **이 문서에 실제 이름을
   예시로 적지 말 것** — 규칙 문서도 배포물에 포함된다(2026-09-07에 한 번
   그랬다).
5. **의도적으로 공개한 것은 건드리지 않는다.** `docs/why.md`의 `결제선생`은
   "한국어 도메인 용어" 예시로 쓴 서사고, `밸류인`은 공개 브랜드명이다.
   지우면 왜 이 도구를 만들었는지가 훼손된다.

**히스토리는 되돌리기 어렵다.** 2026-09-07에 main·브랜치·태그를 재작성했지만,
**닫힌 PR의 `refs/pull/*`는 push로 지워지지 않아** 옛 커밋이 남아 있고,
PyPI 0.6.0~0.8.0 sdist도 그대로 두기로 했다.
즉 **처음부터 안 넣는 것 말고는 완전한 방법이 없다.**
