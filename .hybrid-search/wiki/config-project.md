# Configuration & Project Management
> 마지막 업데이트: 2026-04-14 | 상태: fresh

## 개요

설정 시스템은 두 파일로 구성된다.

| 파일 | 역할 |
|------|------|
| `src/hybrid_search/config.py` | `~/.hybrid-search/config.toml` 로딩 + frozen dataclass 모델 |
| `src/hybrid_search/project.py` | 프로젝트 레지스트리 (SQLite) — 등록/조회/통계 |

모든 설정 dataclass는 `frozen=True`로 불변. 파일이 없으면 기본값으로 자동 생성.

---

## config.toml 구조

```toml
[general]
data_dir = "~/.hybrid-search"   # 데이터 루트 디렉토리
log_level = "info"              # 로그 레벨

[embedding]
openai_model = "text-embedding-3-small"  # OpenAI 임베딩 모델
batch_size = 100                         # 배치 크기 (최대 2048)

[search]
default_limit = 10              # 기본 검색 결과 수
rrf_k = 60                      # RRF fusion 상수
query_classifier = true         # 쿼리 분류기 사용 여부
default_bm25_weight = 0.5       # BM25 가중치 (0.0~1.0)

[indexing]
exclude_patterns = [...]        # 제외할 디렉토리/파일 패턴
max_file_size_kb = 512          # 최대 파일 크기 (KB)
supported_extensions = [...]    # 인덱싱할 확장자 목록

[wiki]
max_pages_per_project = 100     # 프로젝트당 최대 위키 페이지
eviction_policy = "lru"         # 퇴거 정책

[[projects]]
name = "my-project"
path = "/path/to/project"
```

---

## EmbeddingConfig

기본 백엔드: **OpenAI** (`text-embedding-3-small`), batch_size=100.

레거시 필드(하위 호환): `ollama_model`, `model`, `model_revision`, `model_sha256`, `model_path`, `backend`, `max_tokens`, `device`, `onnx_threads`, `quantized`.

`MODEL_MAX_TOKENS` 딕셔너리로 로컬 모델 토큰 한도 자동 감지:
- `multilingual-e5-small/base`: 512
- `gte-multilingual-base`, `bge-m3`, `Qwen3-Embedding-0.6B/`: 8192

---

## SearchConfig

| 필드 | 기본값 | 설명 |
|------|--------|------|
| `default_limit` | 10 | 검색 결과 반환 수 |
| `rrf_k` | 60 | Reciprocal Rank Fusion 상수 (높을수록 순위 차이 완화) |
| `query_classifier` | true | 쿼리 유형 자동 분류 (keyword vs semantic) |
| `default_bm25_weight` | 0.5 | BM25 점수 가중치. 1.0이면 키워드 전용, 0.0이면 벡터 전용 |

---

## IndexingConfig

**기본 제외 패턴**: `node_modules`, `.git`, `__pycache__`, `.next`, `dist`, `build`, `.venv`, `*.lock`

사용자 config.toml에서 추가 가능 (예: `.claude`, `.agents`).

**지원 확장자** (26개):
`.ts` `.tsx` `.js` `.jsx` `.py` `.rs` `.go` `.rb` `.java` `.c` `.cpp` `.h` `.hpp` `.swift` `.kt` `.sql` `.css` `.scss` `.md` `.json` `.yaml` `.yml` `.toml`

**max_file_size_kb**: 512 (512KB 초과 파일 스킵)

---

## ProjectRegistry

`project.py`의 `ProjectRegistry` 클래스. SQLite DB 위치: `~/.hybrid-search/global/project_registry.db`

### 스키마

```sql
CREATE TABLE projects (
    id TEXT PRIMARY KEY,          -- SHA-256(경로)[:16]
    name TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL UNIQUE,
    last_indexed_at TEXT,          -- UTC ISO timestamp
    file_count INTEGER DEFAULT 0,
    chunk_count INTEGER DEFAULT 0,
    index_version INTEGER DEFAULT 1
);
```

### 핵심 메서드

| 메서드 | 설명 |
|--------|------|
| `project_hash(path)` | `hashlib.sha256(path)[:16]` — 결정론적 프로젝트 ID |
| `register(name, path)` | UPSERT (ON CONFLICT DO UPDATE) |
| `get(id)` / `get_by_name` / `get_by_path` | 단일 조회 |
| `list_all()` | 전체 프로젝트 목록 (이름순) |
| `update_stats(id, file_count, chunk_count)` | 인덱싱 후 통계 갱신 + `last_indexed_at` 자동 설정 |
| `remove(id)` | 프로젝트 삭제 |

---

## 데이터 디렉토리 구조

```
~/.hybrid-search/
├── config.toml                  # 전역 설정
├── global/
│   └── project_registry.db      # 프로젝트 레지스트리 (SQLite)
├── models/                      # (레거시) 로컬 ONNX 모델 캐시
└── projects/
    └── <project_hash>/          # 프로젝트별 인덱스 데이터
        ├── store.db             # 청크 + 메타데이터
        ├── vectors.npy          # 벡터 인덱스
        └── ...
```

`Config` 클래스의 프로퍼티로 경로 접근:
- `config.data_dir` → `~/.hybrid-search`
- `config.models_dir` → `~/.hybrid-search/models`
- `config.projects_dir` → `~/.hybrid-search/projects`
- `config.global_dir` → `~/.hybrid-search/global`

---

## 설정 로딩 흐름

```
load_config(path=None)
  ├─ path 미지정 → ~/.hybrid-search/config.toml
  ├─ 파일 없음 → _create_default_config() → 기본 TOML 생성 + Config() 반환
  └─ 파일 있음 → tomllib.load() → 각 섹션 파싱 → Config 인스턴스 반환
```

누락된 섹션/필드는 자동으로 기본값 적용 (get with default pattern).
