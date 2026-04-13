# Hybrid Search MCP — Wiki Index

> 프로젝트: hybrid-search-mcp | 페이지: 10개 | 생성: 2026-04-14

## 핵심 페이지

| 페이지 | 설명 |
|--------|------|
| [architecture](architecture.md) | 전체 아키텍처, 기술 스택, 모듈 구조, 데이터 흐름 |
| [indexing-pipeline](indexing-pipeline.md) | 2-pass 인덱싱: scanner → chunker → embedder → store |
| [ast-chunker](ast-chunker.md) | tree-sitter AST 파싱, 14개 언어, 청킹 전략 |
| [embedder](embedder.md) | OpenAI API 임베딩, 진화 과정, tiktoken truncation |
| [search-engine](search-engine.md) | BM25 + Vector + RRF fusion, 쿼리 분류기 |
| [call-graph](call-graph.md) | call graph resolution, DAG, 모듈 트리, WikiPlan |
| [storage](storage.md) | SQLite WAL + USearch + Tantivy 3중 스토어 |
| [wiki-system](wiki-system.md) | wiki 생성/동기화/staleness 추적 |
| [mcp-server](mcp-server.md) | MCP 도구 3개 + CLI 12개 + 서버 구조 |
| [config-project](config-project.md) | config.toml, ProjectRegistry, 데이터 디렉토리 |

## 빠른 참조

- **검색이 안 될 때**: [search-engine](search-engine.md) → 쿼리 분류기 확인
- **인덱싱 에러**: [indexing-pipeline](indexing-pipeline.md) → consistency check / auto rebuild
- **새 언어 추가**: [ast-chunker](ast-chunker.md) → tree-sitter grammar 추가 방법
- **비용 걱정**: [embedder](embedder.md) → 인덱싱 ~$0.04, 검색 무료
- **설정 변경**: [config-project](config-project.md) → ~/.hybrid-search/config.toml
