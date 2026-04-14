---
name: setup-hybrid-search
description: "Hybrid Search MCP 첫 설치. 빌드 + API 키 확인 + MCP 서버 등록 + 글로벌 hook 설정을 자동 처리합니다."
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# Setup Hybrid Search MCP

hybrid-search-mcp를 처음 설치하는 스킬. 모든 단계를 자동 처리합니다.

## 전제 조건

- Python 3.11+
- Claude Code CLI
- OpenAI API 키

## 실행 절차

### 1. hybrid-search-mcp 설치 확인

```bash
# pip 설치 여부 확인
which hybrid-search-mcp 2>/dev/null && echo "INSTALLED" || echo "NOT_INSTALLED"
```

**INSTALLED**: 다음 단계로.
**NOT_INSTALLED**: 설치:
```bash
pip install hybrid-search-mcp
```

개발 모드로 설치하려면 (소스 수정 시):
```bash
git clone https://github.com/curiohunter/hybrid-search-mcp.git
cd hybrid-search-mcp
pip install -e .
```

### 2. OpenAI API 키 확인

```bash
echo "${OPENAI_API_KEY:+SET}" || echo "NOT_SET"
```

**SET**: 다음 단계로.
**NOT_SET**: 사용자에게 키를 물어봐서 설정:
```bash
# 방법 1: 환경변수 (세션용)
export OPENAI_API_KEY=sk-...

# 방법 2: 파일 (영구)
echo "OPENAI_API_KEY=sk-..." >> ~/.env.local
```

### 3. 자동 설정 실행

```bash
hybrid-search-mcp setup
```

이 명령이 자동으로:
- `~/.claude.json`에 MCP 서버 등록
- `~/.claude/settings.json`에 글로벌 hook 3개 등록
  - Auto-index (Read 시 미인덱싱 프로젝트 자동 인덱싱)
  - Stale check (Edit/Write 전 STALE.md 경고)
  - Gap check (Edit/Write 후 wiki-gaps.txt 알림)

### 4. 설치 확인

```bash
# MCP 서버 등록 확인
grep "hybrid-search" ~/.claude.json && echo "MCP: OK" || echo "MCP: FAIL"

# Hook 등록 확인
grep "hybrid-search" ~/.claude/settings.json && echo "Hooks: OK" || echo "Hooks: FAIL"
```

### 5. 완료 메시지

설치가 완료되면 사용자에게 알려줍니다:

```
설치 완료. Claude Code를 재시작하면 적용됩니다.

이후 아무 프로젝트에서:
- hybrid-search-mcp index .     → 인덱싱
- hybrid-search-mcp search "질문" → 검색
- Claude Code에서는 MCP 도구로 자동 사용
```

## CLI 단독 사용 (Claude Code 없이)

Claude Code 없이도 CLI로 바로 사용 가능합니다:

```bash
# 프로젝트 인덱싱
hybrid-search-mcp index /path/to/project

# 검색
hybrid-search-mcp search "authentication flow"
hybrid-search-mcp search "인증 로직" --json

# 상태 확인
hybrid-search-mcp status
```

## 주의사항

- `setup` 명령은 멱등성이 있음 (여러 번 실행해도 안전)
- 기존 `~/.claude.json`과 `~/.claude/settings.json`의 다른 설정은 보존됨
- Claude Code 재시작이 필요함 (MCP 서버 등록 반영)
