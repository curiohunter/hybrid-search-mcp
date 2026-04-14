---
name: save-wiki
description: "대화 중 분석한 내용을 wiki 페이지로 저장합니다. .hybrid-search/wiki/에 저장하고 index.md를 갱신합니다."
allowed-tools: Read, Write, Edit, Glob, Bash
---

# Save Wiki

대화 중 분석한 내용을 `.hybrid-search/wiki/` 디렉토리에 wiki 페이지로 저장합니다.

## 언제 사용하는가

- 사용자가 "이거 저장해", "wiki에 추가해", "나중에 참고" 등 요청할 때
- 복잡한 코드 분석을 마친 후 결과를 영구 보존하고 싶을 때
- 기존 wiki 페이지가 stale이라 업데이트가 필요할 때

## 실행 절차

### 1. 저장할 내용 결정

방금 대화에서 분석/설명한 내용을 기반으로:
- 제목 결정 (예: "인증 시스템", "학원비 결제 흐름")
- 파일명 결정 (예: `auth-system.md`, `tuition-billing.md`)
- 디스크에서 Glob으로 `.hybrid-search/wiki/*.md` 확인하여 중복 체크

### 2. Wiki 파일 작성

`{project_root}/.hybrid-search/wiki/{filename}.md` 에 작성합니다.

형식:
```markdown
# {제목}

> 마지막 업데이트: {YYYY-MM-DD} | 상태: fresh

## 개요
{1-2줄 요약}

## 핵심 파일
- `path/to/file.ts` -- 역할

## 상세
{분석 내용}

## 주의사항
{특이사항}
```

### 3. index.md 갱신

`.hybrid-search/wiki/index.md`의 페이지 목록에 새 항목을 추가합니다.
기존 항목을 업데이트하는 경우 날짜만 갱신합니다.

### 4. DB 동기화

```bash
hybrid-search-mcp sync-wiki --cwd {project_root}
```

## 작성 원칙

- 대화에서 분석한 내용을 정리하여 작성 (코드 복붙 아님)
- 파일 경로는 현재 존재하는 파일만
- 200줄 이하
- 다음 대화에서 이 wiki만 읽으면 검색 없이 이해할 수 있는 수준
