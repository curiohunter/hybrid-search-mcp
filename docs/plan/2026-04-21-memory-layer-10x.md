# Memory Layer 완성 + 검색 10배 로드맵

**Status:** ACTIVE — 2026-04-21 기획, 2026-04-22 Phase 1-4 shipped + Phase 5 근본 재설계 추가
**목표:** hybrid-search-mcp를 "Claude Code가 쓸수록 똑똑해지는 Memory Layer"로 완성. Graphify 대비 우위는 **agent turn/token 효율**이며 그래프 정교함이 아님.
**증거 기준:** valuein_homepage(1307 files, 8335 chunks)에서 Grep/Graphify 대비 retrieval 품질 + turn/token 효율 측정.

## 진행 상태 (2026-04-22 기준)

| Phase | 상태 | 근거 커밋 |
|-------|------|----------|
| Phase 1 — Memory Layer 완성 | ✅ shipped (16회차) | `59a53ec` 외 4커밋 |
| Phase 2 — Wiki 파편화 해소 | ✅ shipped (17회차) | `295c07d` |
| Phase 3 — M9 pass2 + M10 rationale | ✅ shipped (17회차) | `a4dc5c2` |
| Phase 4 — valuein_homepage 골드셋 + 자동 벤치마크 | ⚠️ 절반 (retrieval-only, agent-in-loop 미측정) | `2dcc198` |
| **Phase 5 — Subsystem-first Retrieval** | 🔴 **next — 근본 해결** | — |
| Phase 6 — L4 watch / L5 two-tier (옛 Phase 5) | 📦 deferred | — |

### Phase 4에서 드러난 근본 결함

`benchmarks/valuein_report_2026-04-22.md` 실측:

- **structure 카테고리 recall@10 = 0.22** (primary top-5는 0.80). 한 쿼리당 피처 doc 1장만 집고 관련 **코드 디렉토리(`components/portal-v3/`, `harness/core/` 등)는 top-10 전체에 등장 0회**.
- 원인 조사 (2026-04-22):
  - 디스크 `.hybrid-search/wiki/*.md` = **2220쪽** (valuein)
  - DB `wiki_pages` 테이블 = 100쪽 (accessed-only cache)
  - DB `chunks` 테이블 중 wiki 청크 = **0개**
  - `scanner.py:351-353` — `.hybrid-search/qa/`만 opt-in 허용, **`wiki/`는 명시적으로 제외**
- 표면 증상은 "wiki가 chunk로 인덱싱 안 됨"이지만, 근본 진단은:

  > **검색 리턴 단위가 "chunk"인데, 사용자가 원하는 단위는 "subsystem"이다.**

  chunk는 함수/문단 수준의 텍스트. 구조/탐색 질문의 답은 **경계가 있는 파일군 + 요약 + 진입점 + 의존 + rationale**의 묶음. 현 스키마엔 그 자료구조가 없음. wiki를 chunk로 집어넣는 패치(A안)는 recall 수치만 올리는 밴드에이드.

---

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

## Phase 5 — Subsystem-first Retrieval (2~3주, 근본 해결)

**전제 전환**: 경쟁 축은 "코드 그래프 정교함"이 아니라 **"agent turn/token 효율"**. Graphify는 사람이 브라우저로 탐색하는 그래프, 우리는 agent가 1턴에 흡수하는 답변 카드.

검색 1차 반환 단위를 `chunk`에서 **`module card`** 로 전환:

```
이전:  query → chunks[10], dedupe by file → Claude가 9번 Read
이후:  query → module_cards[1~3] + chunks[3~7]
          module_card = 요약 + 진입점 N개 + depends + links + rationale
```

### Step 1 — Agent-in-loop 측정 추가 (2일)

**이유:** Phase 5 개선 효과는 turn/token 숫자에서만 증명됨. 현 벤치마크는 retrieval-only라 "10×"를 측정하지 못함. 먼저 기준선을 박고 시작.

**작업:**
- `benchmarks/run_valuein_bench.py`에 proxy 지표 추가:
  - `context_pack_bytes`: 각 track이 top-10으로 전달하는 content 총 바이트
  - `read_count_estimate`: `primary_hit_rank` 합 (Claude가 primary 도달까지 열어봐야 할 파일 수)
  - `chunk_count`: top-10의 고유 파일 수
- Agent-in-loop 파일럿: `benchmarks/agent_loop_pilot.md` — 3~5개 샘플 쿼리에 대해 수동으로 Claude Code 세션 로그 캡처, 턴/토큰 수치 확보
- 리포트 v2: `benchmarks/valuein_report_2026-04-22.md` 업데이트 (재작성 아님, 섹션 추가)

**완료 조건:**
- 자동 벤치마크가 context_pack_bytes / read_count_estimate 출력
- 파일럿 5쿼리 수동 측정치 존재
- "Phase 5 기준선" 표 리포트에 박힘

### Step 2 — Module discovery (3~5일)

heuristic 기반 (Leiden은 Phase 5 성공 후 재평가):

**신호 융합:**
1. 디렉토리 prefix (`components/portal-v3/*` → 같은 모듈 후보)
2. Import/require 그래프 (이미 AST 파서 있음, edge 추출만 추가)
3. Callgraph 공유 (M9 결과 재사용)
4. Feature/plan doc mentions — `docs/features/*.md`와 `docs/plans/*.md` 본문에서 파일 경로 regex 추출 → doc이 모듈의 "표지"
5. Co-change (git log --name-only 최근 90일 co-occurrence)

**출력 스키마:**
```sql
CREATE TABLE modules (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  name TEXT NOT NULL,
  summary TEXT,
  entry_points TEXT,  -- JSON list of chunk_id
  depends_on TEXT,    -- JSON list of module_id
  related_docs TEXT,  -- JSON list of file paths
  rationale TEXT,
  signals TEXT,       -- JSON: which signals defined this module
  updated_at TEXT
);
CREATE TABLE file_modules (
  file_id TEXT NOT NULL,
  module_id TEXT NOT NULL,
  weight REAL,        -- 이 파일이 모듈에 속하는 정도
  PRIMARY KEY (file_id, module_id)
);
```

**구현:**
- 신규 `src/hybrid_search/index/modules.py` — discovery 알고리즘
- `pipeline.py` 후단에 module build 단계 추가
- 테스트: 합성 그래프 + 실 프로젝트 일부로 모듈 수/멤버 검증

**완료 조건:**
- valuein_homepage reindex 후 modules 테이블 ≥50개 레코드
- 각 구조 gold 쿼리 expected_files가 동일 module에 묶임 (수동 확인)

### Step 3 — Module synthesis (3일)

각 module당 LLM 1회로 card 생성:

- Input: 멤버 파일 docstring + 관련 doc 본문 + rationale 태그
- Output: 2~3문장 summary + entry points (top-5 chunks) + depends-on + related docs + rationale 1줄
- 저장: 기존 `wiki_pages`에 `query_key = "module:<module_id>"`, tags에 `"module_card"` 추가. 스키마 변경 없음

**업데이트 전략:** 모듈 멤버/신호 delta만 재합성. 전체 재생성 X.

**완료 조건:**
- `modules` 각 row에 synthesized card 존재
- 재인덱스 후 변경 없는 모듈은 synthesis 재실행 안 함 (hash 검증)

### Step 4 — Module-first retrieval (2일)

**SearchOrchestrator 변경:**
- 신규 `_module_search(query, limit)` — BM25 FTS + vector on module card content
- `hybrid_search()`에 query_type 분기:
  - `structure` / `exploration` → module top-3 + chunk top-7
  - `precision` → chunk top-10 (현재 유지)
  - `rationale` → chunk top-10 (rationale 인덱싱됨) + 관련 module 1개
- HybridResult에 `module_id` 필드 추가 (optional)
- Response shape: `results` 리스트 안에 module/chunk 섞되 `node_type`으로 구분

**완료 조건:**
- S1~S5 5쿼리 모두 module 카드가 top-3에 등장
- precision/rationale 카테고리 성능 비회귀 (recall@10 1.00 유지)

### Step 5 — 재측정 + 리포트 v2 (2일)

같은 gold set에 재실행. 비교 축:
- retrieval: primary_top5 / recall@10 / MRR (기존)
- context pack: bytes delivered / read_count_estimate (Step 1 추가)
- agent-in-loop 파일럿 재측정 5쿼리: 실제 턴/토큰

**완료 조건:**
- structure recall@10: 0.22 → ≥0.55
- 전체 read_count_estimate: ≥30% 감소
- 리포트 v2 `benchmarks/valuein_report_v2.md`
- README "Real-world benchmark" 섹션 갱신

---

## Phase 6 (deferred, 옛 Phase 5) — L4 Watch / L5 Two-tier

Phase 5 결과 보고 재평가.

- L4: `watchdog` 기반 실시간 reindex
- L5: Two-tier merge — 코드 AST는 매번 재빌드, LLM synthesis 결과 보존

---

## 실행 순서 (구현 착수부터)

### Week 1-3: Phase 1-4 (✅ 2026-04-22까지 완료)

### Week 4-6 — Phase 5 Subsystem-first
- Week 4: Step 1 (agent-in-loop 측정) + Step 2 착수 (module discovery)
- Week 5: Step 2 완료 + Step 3 (synthesis)
- Week 6: Step 4 (module-first retrieval) + Step 5 (재측정 + 리포트 v2)

### 이후 Phase 6는 실사용 피드백 보고 결정.

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

- [x] Phase 1: Memory Layer write+read+self-ref 완성
- [x] Phase 2: Wiki 파편화 해소, drift 0
- [x] Phase 3: rationale 인덱싱 + M9 pass2 (cross-file ≥85%는 valuein_homepage에서 재측정 필요)
- [~] Phase 4: 자동 벤치마크 shipped, 10× token 효율 증거는 Phase 5 이후
- [ ] Phase 5: structure recall@10 ≥0.55, read_count_estimate ≥30% 감소, agent-in-loop 파일럿 5쿼리
- [ ] README "Memory Layer for Claude Code" 최종본 (Phase 5 후)
- [ ] 전체 테스트 그린, 600+ cases (현재 580)

---

## 다음 단계 (지금)

**Phase 5 Step 1 착수 — agent-in-loop 측정 proxy를 `run_valuein_bench.py`에 추가 + 5쿼리 수동 파일럿.**
