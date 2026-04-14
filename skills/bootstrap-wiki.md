---
name: bootstrap-wiki
description: "프로젝트의 코드베이스를 분석하여 wiki 페이지를 자동 생성합니다. hybrid_search 인덱스 + MCP 도구를 활용하여 주요 모듈/기능별 wiki를 만듭니다."
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Agent, mcp__hybrid-search__hybrid_search
---

# Bootstrap Wiki

프로젝트의 코드베이스를 분석하여 `.hybrid-search/wiki/` 디렉토리에 wiki 페이지를 자동 생성합니다.

## 실행 절차

### 1. 인덱싱 완료 확인 (필수)

**인덱싱이 완료될 때까지 절대 Step 2로 넘어가지 말 것.**

```bash
hybrid-search-mcp status
```

상태를 확인하고:
- 인덱스가 없거나 파일 수가 0이면 → 인덱싱 실행 후 **완료될 때까지 대기**
- 인덱스가 있지만 파일 수가 프로젝트 규모에 비해 너무 적으면 → `--force` 재인덱싱

```bash
# 인덱싱 실행 (완료까지 대기 — 백그라운드로 보내지 말 것)
hybrid-search-mcp index {project_root} --force
```

**인덱싱이 "Done: +N added" 메시지를 출력할 때까지 다음 단계로 진행 금지.**

### 2. 페이지 목록 확정 (기계적, 판단 금지)

Step 2a: 디렉토리 스캔으로 페이지 목록 생성
```bash
ls {project_root}/
ls {project_root}/app/ 또는 src/ 또는 lib/
```

Step 2b: 디렉토리별 파일/하위폴더 수를 세어서 페이지 수를 결정한다.

```bash
for dir in app services components hooks lib types database src; do
  [ -d "{project_root}/$dir" ] && echo "$dir: $(find {project_root}/$dir -maxdepth 1 -type d | wc -l) subdirs, $(find {project_root}/$dir -maxdepth 2 -name '*.ts' -o -name '*.tsx' -o -name '*.py' | wc -l) files"
done
```

**페이지 수 결정 규칙:**

1. **architecture.md** — 항상 1개
2. **작은 디렉토리 (하위폴더 5개 미만, 파일 10개 미만)** → 디렉토리당 1페이지
3. **중간 디렉토리 (하위폴더 5~15개)** → 도메인 그룹별 2~5페이지로 분리
4. **큰 디렉토리 (하위폴더 15개+)** → 하위폴더/도메인마다 개별 페이지

**최소 페이지 수 가이드라인:**
- 파일 100개 미만: 10~20페이지
- 파일 100~500개: 20~40페이지
- 파일 500개+: 40~80페이지

Step 2c: **목록을 사용자에게 보여주고 확인받은 후 진행**

### 3. Wiki 디렉토리 생성 + .gitignore

```bash
mkdir -p {project_root}/.hybrid-search/wiki
```

`.gitignore`에 `.hybrid-search/` 추가 (없으면 생성).

### 4. 병렬 Agent로 페이지 동시 생성

확정된 목록을 Agent로 병렬 실행합니다. 한 메시지에 여러 Agent를 동시 호출.

각 Agent에게 전달할 프롬프트 템플릿:
```
프로젝트: {project_name} ({project_root})
Wiki 페이지 생성: {filename}.md
주제: {topic_description}
관련 디렉토리: {directory_path}

다음을 수행하세요:
1. hybrid_search MCP 도구로 "{topic_keywords}" 검색
2. Glob으로 {directory_path}/ 하위 파일 목록 확인
3. 핵심 파일 1-2개를 Read로 읽기
4. {project_root}/.hybrid-search/wiki/{filename}.md 작성

형식:
# {title}
> 마지막 업데이트: {today} | 상태: fresh
## 개요 (1-2줄)
## 핵심 파일 (backtick 경로 + 역할)
## 데이터 흐름 (가능하면 ASCII 다이어그램)
## 주의사항

작성 원칙: 200줄 이하, 코드 덤프 아님, 시그니처만, 경로는 실제 존재하는 파일만.
```

한 번에 최대 5개 Agent 병렬 실행. 5개 초과 시 배치로 나누기.

### 5. 병렬 완료 후: index.md 생성

```markdown
# Wiki Index

> 프로젝트: {name} | 페이지: {count}개 | 생성: {YYYY-MM-DD}

## 페이지 목록
- [architecture](architecture.md) -- 전체 아키텍처 개요
- [{module}]({module}.md) -- {1줄 설명}

## 커버리지 gap
{아직 wiki가 없는 디렉토리/모듈 목록}
```

### 6. DB 동기화 (필수)

```bash
hybrid-search-mcp sync-wiki --cwd {project_root}
```

### 7. CLAUDE.md 연동

프로젝트의 CLAUDE.md에 `<!-- hybrid-search -->` 마커 추가.

### 8. post-commit hook 설치

```bash
hybrid-search-mcp install-hook --cwd {project_root}
```

### 9. 검수 Agent 실행 (필수)

누락된 파일이 있으면 해당 파일만 추가 Agent로 생성합니다.

### 10. 최종 결과 보고

```
Wiki Bootstrap 완료
- 확정 목록: N개
- 생성 완료: N개 (100%)
- DB 동기화: N개
- CLAUDE.md 연동: 완료
- post-commit hook: 설치됨
```

## 주의사항

- Step 2의 페이지 목록을 100% 생성할 때까지 완료 보고 금지
- wiki 생성 전 반드시 hybrid_search 인덱스가 최신인지 확인
- Step 6(sync-wiki)를 건너뛰면 staleness 추적이 작동하지 않음
