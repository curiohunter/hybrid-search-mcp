---
name: setup-hybrid-search
description: "Hybrid Search MCP 첫 설치. venv 생성 + 빌드 + API 키 + MCP 등록 + hook 설정을 원클릭 처리."
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# Setup Hybrid Search MCP

hybrid-search-mcp를 처음 설치하는 스킬. **venv 생성부터 MCP 등록까지 전부 자동.**

사용자에게 묻지 않고 바로 실행한다. 실패하면 에러 메시지로 안내.

## 실행 절차

### 1. hybrid-search-mcp 소스 찾기

```bash
# 가능한 경로들을 순서대로 탐색
for dir in \
  ~/project/claude_project/hybrid-search-mcp \
  ~/projects/hybrid-search-mcp \
  ~/hybrid-search-mcp; do
  [ -f "$dir/pyproject.toml" ] && echo "$dir" && break
done
```

없으면 클론:
```bash
cd ~ && git clone https://github.com/curiohunter/hybrid-search-mcp.git
```

### 2. venv 생성 + 의존성 설치

이 단계가 핵심이다. **반드시 .venv를 만들어야 한다.**

```bash
HSDIR=<찾은 경로>
cd "$HSDIR"

# venv가 없으면 생성
if [ ! -f "$HSDIR/.venv/bin/python" ]; then
  python3.11 -m venv .venv 2>/dev/null || python3.12 -m venv .venv 2>/dev/null || python3 -m venv .venv
fi

# 의존성 설치
"$HSDIR/.venv/bin/pip" install -e . 2>&1
```

설치 확인:
```bash
"$HSDIR/.venv/bin/python" -m hybrid_search.cli --help
```

### 3. OpenAI API 키 확인

프로젝트 루트의 `.env.local` 또는 환경변수에서 확인:
```bash
grep OPENAI_API_KEY "$HSDIR/.env.local" 2>/dev/null || echo "${OPENAI_API_KEY:+SET}"
```

없으면 사용자에게 키를 물어서 `.env.local`에 저장:
```bash
echo "OPENAI_API_KEY=sk-..." > "$HSDIR/.env.local"
```

### 4. setup 실행 (MCP + hook 등록)

```bash
"$HSDIR/.venv/bin/python" -m hybrid_search.cli setup
```

### 5. 설치 확인

```bash
grep "hybrid-search" ~/.claude.json && echo "MCP: OK"
grep "hybrid-search" ~/.claude/settings.json && echo "Hooks: OK"
```

### 6. 완료 메시지

```
설치 완료. Claude Code를 재시작하면 적용됩니다.

이후 아무 프로젝트에서:
- 파일을 Read하면 자동으로 첫 인덱싱 시작
- 커밋하면 자동으로 delta reindex
- /maintain으로 wiki 유지보수
- /rebuild-index로 인덱스 복구
```

## 주의사항

- `setup` 명령은 멱등성이 있음 (여러 번 실행해도 안전)
- 기존 `~/.claude.json`과 `~/.claude/settings.json`의 다른 설정은 보존됨
- Claude Code 재시작이 필요함 (MCP 서버 등록 반영)
- venv 경로는 머신마다 다르지만 setup이 자동으로 현재 경로 기준으로 등록
