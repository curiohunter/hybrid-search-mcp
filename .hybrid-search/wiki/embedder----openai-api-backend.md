# Embedder -- OpenAI API Backend
> 마지막 업데이트: 2026-04-14 | 상태: fresh | synthesized: 2026-04-14

## Overview

The Embedder module converts text into 1536-dimensional vectors via the OpenAI text-embedding-3-small API, providing the semantic search backbone with zero local compute overhead. It evolved through four backend iterations (sentence-transformers, ONNX INT8, Ollama GPU, OpenAI API) to eliminate local ML dependencies, using only urllib, numpy, and tiktoken.

## Key Design Decisions

- **stdlib urllib over requests/httpx**: Uses Python built-in urllib.request to minimize dependency footprint (`src/hybrid_search/index/embedder.py:L68`)
- **tiktoken truncation at 8000 tokens**: OpenAI limit is 8192; 192-token safety margin. Tokenizer lazily loaded as class variable (`src/hybrid_search/index/embedder.py:L107`)
- **L2 re-normalization**: Defensive re-normalization after API response despite OpenAI returning normalized vectors (`src/hybrid_search/index/embedder.py:L117`)
- **Custom .env.local parser**: Walks up to 10 directory levels for API key without requiring python-dotenv (`src/hybrid_search/index/embedder.py:L132`)

## Data Flow

```
texts: list[str]
  -> _embed_all() batch by 100
    -> _truncate() tiktoken cap at 8000 tokens
      -> _openai_embed_request() POST api.openai.com/v1/embeddings
        -> np.array (N, 1536) -> L2 normalize -> float32
```

## Caveats

- API key lookup starts from Path.cwd(), not project root
- No retry logic: transient API failures immediately propagate as exceptions
- HTTPError/URLError both re-raised as ConnectionError, losing error type distinction

## Related Modules

- [[indexing-pipeline]] -- calls embed_texts() for chunk batch embedding
- [[search]] -- SearchOrchestrator calls embed_query() at query time
- [[configuration-&-project-management]] -- EmbeddingConfig controls model, batch size, backend

<details>
<summary>Structure (auto-generated)</summary>

## 개요

`src/hybrid_search/index/embedder.py`는 텍스트를 벡터로 변환하는 임베딩 모듈이다.
OpenAI `text-embedding-3-small` (1536차원)을 HTTP API로 호출하며,
로컬 모델 로딩, GPU, CPU 오버헤드가 전혀 없다.
외부 의존성은 `numpy`, `tiktoken`, 그리고 Python 내장 `urllib`뿐이다.

## 진화 과정

| 커밋 | 변경 | 이유 |
|------|------|------|
| `acd9741` | 최초 구현 | Phase 1+2 기본 구조 |
| `ec88375` | sentence-transformers + e5-small | FK CASCADE 버그 수정과 함께 백엔드 전환 |
| `1db871e` | PyTorch 스레드 제한 + 크로스 배칭 | 인덱싱 성능 최적화 |
| `27f48a8` | ONNX INT8 양자화 | Xenova 프리빌트 다운로드로 경량화 |
| `25aa0f1` | ONNX inter_op 스레드 제한 | CPU 사용량 추가 제한 |
| `c4fe84d` | CPU 백엔드 제거, Ollama GPU 전용 | 로컬 CPU 모델의 한계 |
| `e5cd9e5` | **OpenAI API 전환** | 로컬 리소스 제로 달성 |
| `b0ba78d` | tiktoken 기반 truncation | 8192 토큰 제한 대응 |

경로: `sentence-transformers` -> `ONNX INT8` -> `Ollama GPU` -> `OpenAI API`.

## 왜 OpenAI로 전환했나

18GB 맥북 Air에서 로컬 임베딩 모델은 다음 문제가 있었다:

- **sentence-transformers**: PyTorch 로딩만 수백 MB 메모리, CPU 100% 고정
- **ONNX INT8**: 양자화로 개선했지만 여전히 큰 바이너리와 스레드 폭주
- **Ollama**: GPU 오프로딩은 좋았지만 Ollama 서버 상시 가동 필요

OpenAI API는 이 모든 문제를 제거했다. HTTP 호출 하나로 끝나며,
`urllib` 하나면 충분하다. 외부 ML 라이브러리 의존성이 사라졌다.

## Embedder 클래스

```
Embedder(config: EmbeddingConfig, models_dir=None)
```

- `embed_texts(texts: list[str]) -> np.ndarray` -- (N, 1536) float32 배열 반환
- `embed_query(query: str) -> np.ndarray` -- (1536,) 벡터 반환 (내부적으로 embed_texts 호출)
- `embedding_dim` 프로퍼티 -- 기본값 1536

`EmbeddingConfig` 주요 필드 (`config.py:42`):
- `openai_model`: 기본 `"text-embedding-3-small"`
- `batch_size`: 기본 100 (OpenAI 최대 2048)
- `backend`: `"openai"` (레거시 필드 `ollama_model`, `model` 등 하위 호환용 보존)

## API 호출 흐름

`_openai_embed_request(texts)` 메서드:

1. `_get_api_key()`로 API 키 확보
2. 각 텍스트를 `_truncate()`로 8000 토큰 이내로 자름
3. `urllib.request`로 `https://api.openai.com/v1/embeddings` POST
4. 응답에서 `data[*].embedding` 추출
5. HTTPError/URLError를 `ConnectionError`로 래핑

`_embed_all(texts)` 메서드:
- `batch_size` (기본 100)씩 나눠서 `_openai_embed_request` 호출
- 결과를 `np.vstack`으로 합치고 L2 정규화 (이미 정규화되어 있지만 검증)

## tiktoken 토큰 제한 처리

OpenAI 임베딩 모델의 최대 입력은 8192 토큰이다.
`_truncate()` 메서드가 8000 토큰(안전 마진 192)에서 잘라낸다:

```python
Embedder._enc = tiktoken.encoding_for_model("text-embedding-3-small")
tokens = Embedder._enc.encode(text)
if len(tokens) <= 8000:
    return text
return Embedder._enc.decode(tokens[:8000])
```

`_enc`는 클래스 변수로 lazy-load되어 한 번만 초기화된다.

## .env.local API 키 로딩

키 탐색 순서:
1. `os.environ["OPENAI_API_KEY"]`
2. `.env.local` 파일 (cwd에서 상위 10단계까지 탐색)

`_load_dotenv_key()` 모듈 함수가 `.env.local` 파일을 줄 단위로 파싱한다.
별도의 `python-dotenv` 의존성 없이 직접 구현했다.
키가 없으면 `ValueError`를 발생시킨다.

## 비용

`text-embedding-3-small` 가격: **$0.02 / 1M 토큰**

| 작업 | 토큰 규모 | 예상 비용 |
|------|----------|----------|
| 중규모 프로젝트 전체 인덱싱 (~2000 청크) | ~2M 토큰 | ~$0.04 |
| 검색 쿼리 1건 | ~20 토큰 | ~$0.0000004 |
| 하루 100회 검색 | ~2000 토큰 | ~$0.00004 |

인덱싱은 한 번이고, 검색은 사실상 무료다.
배치 사이즈 100으로 API 호출 횟수도 최소화한다.

</details>