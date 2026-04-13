# Hybrid Search MCP

BM25 + Vector 하이브리드 검색 MCP 서버. 한국어↔영어 크로스 언어 코드 검색.

## 코드베이스 Wiki

이 프로젝트의 코드베이스 wiki는 `.hybrid-search/wiki/index.md`에 있습니다.
코드 관련 질문 시 먼저 해당 wiki 파일을 Read로 확인하세요.
wiki가 stale이면 관련 코드를 다시 읽고 wiki를 업데이트하세요.

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
