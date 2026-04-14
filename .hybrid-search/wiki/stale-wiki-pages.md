# Stale Wiki Pages
> 이 파일은 자동 생성됩니다. 아래 페이지들의 소스 코드가 변경되었습니다. | synthesized: 2026-04-14

## Overview

The STALE.md file is an auto-generated staleness tracker that flags wiki pages whose underlying source files have changed since the page was last compiled. It exists to ensure wiki documentation stays in sync with the codebase by providing a clear list of pages that need regeneration, along with the specific files that triggered the staleness.

## Key Design Decisions

- **File-level change tracking**: Staleness is detected by comparing the `file_hash` snapshot stored at wiki compile time against the current `file_hash` in the files table, rather than diffing content -- this makes detection O(1) per dependency
- **Explicit deletion protocol**: The file should be deleted only after all stale pages have been regenerated, acting as a persistent reminder that prevents partial updates from being forgotten
- **Per-page granularity with changed file details**: Each stale entry lists the specific test files that changed (e.g., `tests/test_config.py`, `tests/test_scanner.py`), enabling targeted regeneration without re-reading the entire codebase

## Data Flow

```
Source file modified (e.g., tests/test_config.py)
  |
  v
reindex / post-commit hook
  |
  v
file_hash updated in store.db
  |
  v
WikiStore.check_staleness() detects hash mismatch
  |
  v
STALE.md generated with page_id + changed files list
  |
  v
Developer/AI reads STALE.md before consulting wiki
  |
  v
Regenerate stale pages -> delete STALE.md
```

## Caveats

- Currently only the **Tests** page (page_id: `fe79f02fd2767927`) is flagged as stale, with 5 changed test files: `test_config.py`, `test_scanner.py`, `test_query_classifier.py`, `test_wiki.py`, `test_embedder.py`
- STALE.md is a point-in-time snapshot -- if additional source files change after generation but before regeneration, the file may undercount staleness until the next `check_staleness()` run
- The file contains no source code and relies entirely on the wiki staleness tracking infrastructure in `storage/wiki.py` and the `check_staleness` CLI command

## Related Modules

- [[tests]] -- the currently stale wiki page that needs regeneration
- [[architecture]] -- wiki staleness tracking is part of the reactive wiki layer in the architecture

<details>
<summary>Structure (auto-generated)</summary>

# Stale Wiki Pages

> 이 파일은 자동 생성됩니다. 아래 페이지들의 소스 코드가 변경되었습니다.
> 각 페이지를 읽고, 변경된 소스 파일을 확인한 후, wiki 내용을 갱신하세요.
> 모든 페이지를 갱신하면 이 파일을 삭제하세요.

- **Tests** (page_id: `fe79f02fd2767927`)
  - 변경된 파일: tests/test_config.py, tests/test_scanner.py, tests/test_query_classifier.py, tests/test_wiki.py, tests/test_embedder.py

</details>