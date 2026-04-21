# Memory Layer 완성 + 검색 10배 로드맵

**Status:** ACTIVE — 2026-04-21 기획
**목표:** hybrid-search-mcp를 "쓸수록 똑똑해지는 기억+검색 장치"로 완성.
**증거 기준:** valuein_homepage(1306 files, 8332 chunks)에서 Grep/DeepWiki 류 대비 retrieval 품질·속도 우위 측정.

---

## 왜 "MVP 완료 ≠ 완성"인가

15회차 세션에서 완료된 것은 **Sprint 1 (write-only qa_log)**. 현실:

- `src/hybrid_search/memory/qa_log.py` 259줄, 실제 동작 경로는 `record()` 한 개
- 유일한 caller: `tools/hybrid_search.py:119`
- `qa list / qa show / qa grep` CLI 없음
- `.hybrid-search/qa/`는 `.gitignore`로 인덱서에서 제외 → Claude가 옆 세션 qa를 소환할 경로 없음
- 디스크 append-only log 수준

**HANDOFF 15회차 line 52, 60**이 그대로 적시: "C MVP만으로는 write-only → 완전 가치 실현 X", "Memory Layer가 실사용에서 write+read 둘 다 되어야 포지셔닝이 말이 됨".

추가로 **우리 자체 발견한 결함**:

- Wiki 98개 페이지 중 절반이 `test_wiki-1.md` ~ `test_wiki-11.md`, `test_cli_hook_install-1.md` ~ `-12.md` 식으로 파편화
- `coverage.json` total_pages=75 vs 실제 98개 → orphan drift 23개
- STALE 4개, needs_synthesis 4개 누적

즉, **"기억"도 read-side 없고 "검색"도 wiki 파편화로 신뢰 낮음**. 두 축 모두 복구/완성 필요.

---

## Graphify 정밀 분석과의 관계

`_study/graphify-analysis/` (5,035줄, 8 문서)에서 이미 gap 분석이 끝나 있음. 이 플랜은 그 결과의 우선순위화 + 우리가 실제로 아직 안 한 것으로 재구성.

Graphify 분석에서 **이미 우리가 shipped한 것들은 제외**:
- Q1 PreToolUse hook ✅
- Q2 status 명령 ✅
- Q7 CLAUDE.md 자동 주입 ✅
- Q8 core.hooksPath ✅
- Q9 hook identity filter ✅
- M1/M1.1/M1.2 Confidence 3단계 (SCHEMA v5 CONFIDENCE_LEVELS) ✅
- M2/M3/M4 multi-hook + needs_synthesis ✅
- M5 MCP graph tools (god-nodes/shortest-path/subgraph) ✅
- L6 Benchmark (n=60) ✅
- 15회차 A/B/C/D ✅

**남은 핵심**: L1 (Memory 완성) + M9 (two-pass callgraph) + M10 (rationale) + L2 (Leiden wiki) + L4/L5 (watch/two-tier).

---

## 완성의 정의 — "10배"의 조작적 정의

`hybrid-search-mcp` (현재) 대비:

| 축 | 현재 | 완성 후 |
|----|-----|--------|
| **기억 write** | HYBRID_SEARCH_QA_LOG=1 opt-in, async daemon | 유지 (이미 shipped) |
| **기억 read (사람)** | 없음 | `qa list/show/grep` CLI |
| **기억 self-ref (AI)** | 없음 | qa 로그가 hybrid_search 결과에 섞임 — "저번에 뭐 물어봤지" 자연 해결 |
| **Wiki 품질** | 98 files 파편화, coverage drift, STALE 4 | 파일 단위 통합 or Leiden 그룹, coverage 정확, STALE 0 자동 유지 |
| **Cross-file 호출 커버리지** | ~60% (추정) | ≥85% (two-pass resolve) |
| **Rationale 인덱스** | docstring은 chunk 단위로만 | `# NOTE:`, design rationale 별도 필드 / 검색 가능 |
| **대형 프로젝트 검증** | benchmark n=60 self-contained | valuein_homepage 별도 gold 10~20q, Grep 대비 NDCG/time/tokens |

**10배 net = DX × precision × recall × navigation.** Week 1 DX, Month 1 recall 누적, Month 2 graph navigation.

---

## Phase 1 — Memory Layer 완성 (Sprint 2/3/4) — 1~2주

### Sprint 2 — qa 조회 CLI (반나절~1일)

**완료 조건:**
- `hybrid-search-mcp qa list [--cwd .] [--limit N] [--since DATE] [--project <name>]` — 최근 N개 로그 목록 (timestamp, query preview, query_type, result_count)
- `hybrid-search-mcp qa show <id>` — id = `DD-HHMMSS-<hash>` 또는 hash prefix 매칭. 전문 출력
- `hybrid-search-mcp qa grep <term> [--cwd .]` — frontmatter + body ripgrep. 파일:매치줄 포맷
- `hybrid-search-mcp qa stats [--cwd .]` — 총 개수, 월별 분포, query_type 분포

**구현:**
- 신규 `src/hybrid_search/memory/reader.py` (~150줄):
  - `iter_qa_files(project_root: Path) -> Iterator[Path]` — YYYY/MM 계층 순회
  - `parse_qa_frontmatter(path: Path) -> QAIndex` — 파싱, `QAIndex` dataclass
  - `QAIndex`: query, query_type, bm25_weight, timestamp, result_count, path
- 신규 `cmd_qa_*` 4개를 `cli.py`에 subparser로 추가
- 테스트: `tests/test_qa_reader.py` — 샘플 로그 10개 생성 → list/show/grep/stats 왕복 검증 (~12개 케이스)

**diff 규모 추정:** +400줄 (reader.py 150 + cli.py 100 + tests 150)

### Sprint 3 — qa 자기 인덱싱 (1일)

**완료 조건:**
- `.hybrid-search/qa/` 내용이 hybrid_search 검색 결과에 포함됨 (opt-in, default off)
- `node_type="qa_log"` 또는 신규 필드로 구분 가능
- "저번 세션에서 authority_alpha 어떻게 결정했지?" 같은 질의에 과거 qa 로그가 top-3에 등장

**설계 결정 (먼저 정해야 함):**
1. **A안: 일반 scanner에 포함** — `.hybrid-search/qa/` 디렉토리를 exclude_patterns에서 예외 처리, doc_chunker로 처리. 장점: 재사용. 단점: BM25/vector 가중치가 일반 코드랑 섞임
2. **B안: 전용 테이블 + 병합 검색** — `qa_chunks` 테이블 별도, hybrid_search 응답에 `qa_hits` 섹션 추가. 장점: 메모리 노이즈 격리, ranking 분리 가능. 단점: 구현 두 배

추천: **A안으로 시작 → 문제 생기면 B안으로 분리**. 실제 사용 데이터 쌓이기 전엔 over-engineering.

**구현 (A안):**
- `scanner.py`: `.gitignore`의 `.hybrid-search/qa/` 엔트리를 scanner 레벨에서만 덮어쓸 수 있도록 `HYBRID_SEARCH_INDEX_QA=1` opt-in 플래그 추가
- `doc_chunker.py`: markdown frontmatter 파싱해서 YAML 키를 검색 메타로 보존 (이미 md 지원이라 작업 적음)
- `ast_chunker` / `orchestrator`에서 `node_type="qa_log"` 히트는 snippet 형태 살짝 다르게
- 테스트: 로그 5개 쓰고 → reindex → 검색 → top-N에 qa 히트 존재 검증

**diff 규모:** +250줄 (scanner.py 50 + orchestrator 50 + tests 150)

### Sprint 4 — Rotation + cross-project aggregator (반나절)

**완료 조건:**
- `hybrid-search-mcp qa prune --cwd . --older-than 90d` — 오래된 로그 삭제
- `hybrid-search-mcp qa list --all` — 등록된 모든 프로젝트 qa 로그 집계
- Per-project size cap (config.toml에서 `qa_max_files` 기본 1000)

**구현:**
- `cli.py`에 `cmd_qa_prune` 추가 (mtime 기반)
- `qa list --all`: `ProjectRegistry.list_all()` 순회 + 각 프로젝트 `qa/` 집계
- `config.toml` `[memory]` 섹션 신설

**diff 규모:** +150줄

### Phase 1 완료 조건 (체크리스트)

- [ ] `qa list/show/grep/stats` 동작
- [ ] 자기참조 검색 가능 (과거 질의가 현재 검색 결과에 등장)
- [ ] rotation CLI 동작
- [ ] 전체 테스트 +40개 내외, 기존 500개 green 유지
- [ ] README의 "Memory Layer (MVP)" 섹션을 정식 섹션으로 승격 (MVP 경고 제거)

---

## Phase 2 — Wiki 파편화 해결 (2~3일)

### 문제 재진단

- `test_wiki.py` 하나 → `test_wiki-1.md` ~ `test_wiki-11.md` (11쪽)
- `test_cli_hook_install.py` → 12쪽
- 원인 추정: wiki generation 로직이 "파일당 심볼 수 임계 → 같은 파일이 여러 module_id로 배정"

### Step 1 — 원인 파악 (반나절)

**조사 대상:**
- `src/hybrid_search/storage/wiki.py` — `WikiStore.compile_page` 근처
- `src/hybrid_search/index/pipeline.py` — module 배정 로직
- 파일 → module_id 매핑이 어디서 `-N` 접미사를 붙이는지 grep

**산출물:** 플랜 문서 하단에 원인·해결안 추가.

### Step 2 — 패치 (1~2일)

두 가지 접근 중 선택 (원인 파악 후 결정):

1. **소극적 — 파일 경계 존중**: 같은 파일 내 심볼은 같은 module로 강제. 임계 초과 시 심볼 수만 많을 뿐 페이지 1개.
2. **적극적 — Leiden 커뮤니티 (L2)**: 그래프 기반 그룹핑. graphify가 쓰는 방식. 더 정교하지만 구현 크다.

Phase 2는 **소극적 패치부터**. L2는 Phase 4로 미룸.

### Step 3 — Drift 청소 (1시간)

- `hybrid-search-mcp rebuild-index` → 재생성
- `coverage.json` ↔ 디스크 wiki 파일 정합성 검증 테스트 추가

### Phase 2 완료 조건

- [ ] `test_wiki.py` 1개 파일 = `test_wiki.md` 1개 페이지
- [ ] `.hybrid-search/wiki/*.md` 카운트 == `coverage.json` total_pages
- [ ] STALE 0, needs_synthesis 0 (재생성 후)
- [ ] Orphan 검출 테스트 신규

---

## Phase 3 — 검색 정확도 상승 (1~2주)

### 3.1 M9 Two-pass callgraph (3~4일)

graphify 패턴: intra-file AST pass → global name-map pass → cross-file resolve.

**대상:** `src/hybrid_search/index/callgraph.py`
**목표:** valuein_homepage에서 `in_degree≥5` 노드 수 +30% (측정 후 조정)
**confidence 활용:** 이미 SCHEMA v5에 있으니 2-pass 결과를 EXTRACTED/INFERRED/AMBIGUOUS로 분류만 추가

### 3.2 M10 Rationale 추출 (1~2일)

**대상:** `src/hybrid_search/index/ast_chunker.py`, `doc_chunker.py`
- Python: leading docstring + `# NOTE:`, `# WHY:`, `# TODO:` 주석
- TS/JS: JSDoc `@remarks`, `// NOTE:` 주석
- 별도 필드 `rationale` 또는 기존 docstring 확장 후 BM25에서 가중
- snippet 생성 시 rationale 우선

**테스트:** 수동 샘플 → rationale 히트 확인

---

## Phase 4 — 대형 프로젝트 실전 검증 (1주)

### valuein_homepage 골드셋 (1~2일)

**작성 대상:** `benchmarks/valuein_gold.json` — 15~20 쿼리
- 구조 질문 5개 (e.g. "수학 문제 모델은 어떻게 구성됨", "인증 흐름 클래스")
- 기능 탐색 5개 (한국어 자연어)
- 정밀 조회 5개 (exact symbol)
- Rationale 질문 5개 ("왜 이 설계를 택했나")

### 측정 (반나절)

- **baseline 1**: Grep+Read (Claude가 자발 사용) — 턴 수, 토큰, 정답 도달률
- **baseline 2**: DeepWiki/MCP-other 류 (있으면)
- **우리**: hybrid+wiki+qa (Phase 1-3 반영본)
- 지표: NDCG@10, MRR@10, time-to-answer, tokens consumed

### 리포트

- `benchmarks/valuein_report_2026-04-XX.md` — 표 + 분석
- README에 "Real-world benchmark" 섹션 추가

---

## Phase 5 (선택) — L4/L5 Watch & Two-tier

Phase 1-4가 "완성"의 뼈대. 5는 사용자 DX 향상용.

- L4: `watchdog` 기반 실시간 reindex. post-commit 훅 없이도 동작
- L5: Two-tier merge — 코드 AST는 매번 재빌드, LLM synthesis 결과는 보존

Phase 4 후 실사용 피드백 보고 결정.

---

## 실행 순서 (구현 착수부터)

### Week 1 — Memory Layer 완성
- Day 1: Sprint 2 (qa list/show/grep)
- Day 2: Sprint 3 (qa 자기 인덱싱)
- Day 3: Sprint 4 (rotation) + README 섹션 승격

### Week 2 — Wiki 파편화
- Day 4-5: 원인 파악 + 소극적 패치
- Day 6: drift 청소 + 테스트

### Week 3 — 검색 정확도
- Day 7-10: M9 two-pass callgraph
- Day 11-12: M10 rationale

### Week 4 — valuein 검증
- Day 13-14: gold set 작성 + 측정 + 리포트

### Total: 4주, 각 Phase 끝에 사용자 리뷰.

---

## 비-목표 (의도적 배제)

- **14개 플랫폼 skill 변형** (graphify 기능). 우리는 Claude Code 포커스.
- **Multimodal (PDF/이미지/비디오)** — 범위 확장은 품질 저하.
- **Platform 마이그레이션 (argparse L7)** — 현재 argparse 이미 씀, 해당 없음.
- **Leiden 풀스케일 wiki auto-gen (L2)** — Phase 2 소극 패치로 파편화 해소 후 필요 시 재평가.

---

## 변경 파일 요약 (추정)

| Phase | 파일 | 주요 변경 |
|-------|------|----------|
| Sprint 2 | `memory/reader.py` (신규), `cli.py` | list/show/grep/stats |
| Sprint 3 | `scanner.py`, `orchestrator.py`, `doc_chunker.py` | qa 인덱싱 opt-in |
| Sprint 4 | `cli.py`, `config.py` | prune + cross-project |
| Phase 2 | `storage/wiki.py`, `index/pipeline.py` | 파일 경계 존중 |
| Phase 3 | `index/callgraph.py`, `ast_chunker.py` | two-pass + rationale |
| Phase 4 | `benchmarks/valuein_gold.json`, `valuein_report.md` | 검증 |

예상 총 diff: +3,000 / -500, 테스트 +80~100개.

---

## 완료 조건 (전체)

- [ ] Phase 1: Memory Layer write+read+self-ref 완성
- [ ] Phase 2: Wiki 파편화 해소, drift 0
- [ ] Phase 3: cross-file resolve ≥85%, rationale 인덱싱
- [ ] Phase 4: valuein_homepage baseline 대비 10× token 효율, 2× 속도 증거
- [ ] README "Memory Layer for Claude Code" 재포지셔닝 최종본
- [ ] 전체 테스트 그린, 600+ cases

---

## 다음 단계 (지금)

**Sprint 2 착수 — qa list/show/grep/stats CLI + reader.py.**
