"""P1-2 v2 tests — needs_revalidation as a current-HEAD projection.

Round-2 lifecycle contract: the flag set is a pure function of
(current HEAD, qa corpus), recomputed from scratch each pass. The suite
drives real git repositories through the scenarios the round-2 review
demanded: force rebuild, branch checkout leakage, revert, rename,
whitespace-only edits, and idempotent recomputation.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hybrid_search.memory.revalidation import anchor_paths, project_revalidations
from hybrid_search.search.orchestrator import (
    HybridResult,
    _apply_revalidation_flag,
    _memory_verification,
)
from hybrid_search.storage.db import StoreDB


# --- git fixture ---------------------------------------------------------------

T0 = "2026-07-01T00:00:00+00:00"   # initial commit
T_QA = "2026-07-05T00:00:00+00:00"  # qa written here
T1 = "2026-07-10T00:00:00+00:00"   # later change
T_QA_LATE = "2026-07-12T00:00:00+00:00"  # qa written AFTER the change


class Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@t")
        self.git("config", "user.name", "t")

    def git(self, *argv: str, date: str | None = None) -> str:
        env = dict(os.environ)
        if date:
            env["GIT_AUTHOR_DATE"] = date
            env["GIT_COMMITTER_DATE"] = date
        proc = subprocess.run(
            ["git", *argv], cwd=self.root, capture_output=True, text=True,
            env=env, check=True,
        )
        return proc.stdout.strip()

    def write(self, rel: str, content: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def commit(self, msg: str, date: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg, date=date)
        return self.git("rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path: Path) -> Repo:
    r = Repo(tmp_path / "repo")
    r.write("src/auth.py", "def verify_token():\n    return 'v1'\n")
    r.commit("init", T0)
    return r


def _qa(ts: str, paths: list[str], chunk_id: str = "qa-1") -> tuple[str, str]:
    body = f'---\nquery: "auth 어디서 처리해?"\ntimestamp: {ts}\n---\n\n'
    body += "## Answer excerpt\n\nanswer\n\n## Top results\n\n"
    for i, p in enumerate(paths, 1):
        body += f"### {i}. `{p}:1-10` — name\n- chunk_id: `c{i}`\n\n"
    return (chunk_id, body)


def _flags(repo: Repo, entries) -> list[tuple[str, str, str]]:
    rows, head = project_revalidations(repo.root, entries)
    assert head is not None
    return rows


# --- core projection semantics ---------------------------------------------------

class TestProjection:
    def test_change_after_qa_flags(self, repo: Repo) -> None:
        entries = [_qa(T_QA, ["src/auth.py"])]
        repo.write("src/auth.py", "def verify_token():\n    return 'v2'\n")
        cause = repo.commit("change auth", T1)
        rows = _flags(repo, entries)
        assert len(rows) == 1
        chunk_id, cause_commit, path = rows[0]
        assert (chunk_id, path) == ("qa-1", "src/auth.py")
        assert cause.startswith(cause_commit)

    def test_qa_written_after_change_not_flagged(self, repo: Repo) -> None:
        repo.write("src/auth.py", "def verify_token():\n    return 'v2'\n")
        repo.commit("change auth", T1)
        assert _flags(repo, [_qa(T_QA_LATE, ["src/auth.py"])]) == []

    def test_unrelated_change_not_flagged(self, repo: Repo) -> None:
        repo.write("docs/readme.md", "hello\n")
        repo.commit("docs", T1)
        assert _flags(repo, [_qa(T_QA, ["src/auth.py"])]) == []

    def test_recompute_is_idempotent_from_scratch(self, repo: Repo) -> None:
        """Force-rebuild scenario: the table is gone, recompute restores
        exactly the flags that still hold — no state carried over."""
        entries = [_qa(T_QA, ["src/auth.py"])]
        repo.write("src/auth.py", "v2\n")
        repo.commit("change", T1)
        first = _flags(repo, entries)
        second = _flags(repo, entries)  # fresh pass, no prior table
        assert first == second and len(first) == 1

    def test_checkout_does_not_leak_across_branches(self, repo: Repo) -> None:
        """A→B→A: flags computed on B must vanish when HEAD is back on A."""
        entries = [_qa(T_QA, ["src/auth.py"])]
        repo.git("checkout", "-q", "-b", "feature-b")
        repo.write("src/auth.py", "def verify_token():\n    return 'b'\n")
        repo.commit("b change", T1)
        assert len(_flags(repo, entries)) == 1  # on B: flagged
        repo.git("checkout", "-q", "main")
        assert _flags(repo, entries) == []      # back on A: clean

    def test_revert_clears_the_flag(self, repo: Repo) -> None:
        entries = [_qa(T_QA, ["src/auth.py"])]
        repo.write("src/auth.py", "v2\n")
        sha = repo.commit("change", T1)
        assert len(_flags(repo, entries)) == 1
        repo.git("revert", "--no-edit", "-n", sha)
        repo.commit("revert", "2026-07-11T00:00:00+00:00")
        assert _flags(repo, entries) == []

    def test_whitespace_only_change_not_flagged(self, repo: Repo) -> None:
        original = (repo.root / "src/auth.py").read_text()
        repo.write(
            "src/auth.py",
            original.replace("    return 'v1'", "        return 'v1'") + "\n\n",
        )
        repo.commit("reformat", T1)
        assert _flags(repo, [_qa(T_QA, ["src/auth.py"])]) == []

    def test_rename_is_delete_plus_add(self, repo: Repo) -> None:
        repo.git("mv", "src/auth.py", "src/auth_v2.py")
        repo.commit("rename", T1)
        rows = _flags(repo, [_qa(T_QA, ["src/auth.py"])])
        assert len(rows) == 1 and rows[0][2] == "src/auth.py"

    def test_anchor_absent_at_qa_time_is_skipped(self, repo: Repo) -> None:
        """Conservative: a path that didn't exist at qa time can't be a
        reliable anchor."""
        repo.write("src/new_module.py", "x = 1\n")
        repo.commit("add later", T1)
        assert _flags(repo, [_qa(T_QA, ["src/new_module.py"])]) == []

    def test_undated_or_unanchored_qa_skipped(self, repo: Repo) -> None:
        no_ts = ("qa-x", '---\nquery: "q"\n---\n\n## Top results\n\n'
                          "### 1. `src/auth.py:1-2` — n\n")
        no_anchor = ("qa-y", f'---\nquery: "q"\ntimestamp: {T_QA}\n---\nbody')
        repo.write("src/auth.py", "v2\n")
        repo.commit("change", T1)
        assert _flags(repo, [no_ts, no_anchor]) == []

    def test_non_git_directory_returns_no_head(self, tmp_path: Path) -> None:
        rows, head = project_revalidations(
            tmp_path / "plain", [_qa(T_QA, ["a.py"])],
        )
        assert (rows, head) == ([], None)


# --- anchor extraction ----------------------------------------------------------

class TestAnchorPaths:
    def test_top_three_distinct(self) -> None:
        _, content = _qa(T_QA, ["a.py", "b.py", "a.py", "c.py", "d.py"])
        assert anchor_paths(content) == ["a.py", "b.py", "c.py"]

    def test_no_results_section(self) -> None:
        assert anchor_paths('---\nquery: "q"\n---\nbody') == []


# --- storage: projection replace ---------------------------------------------------

class TestRevalidationStore:
    def test_replace_overwrites_previous_projection(self, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        try:
            with db.transaction() as conn:
                db.replace_qa_revalidation(
                    conn, "p1", [("qa-old", "abc1234", "a.py")],
                    projection_head="abc1234deadbeef",
                )
            with db.transaction() as conn:
                db.replace_qa_revalidation(
                    conn, "p1", [("qa-new", "def5678", "b.py")],
                    projection_head="def5678deadbeef",
                )
            assert db.get_qa_revalidations(["qa-old", "qa-new"]) == {
                "qa-new": ("def5678", "b.py"),
            }
            assert db.get_meta("qa_reval_projection_head") == "def5678deadbeef"
        finally:
            db.close()

    def test_replace_with_empty_projection_clears(self, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        try:
            with db.transaction() as conn:
                db.replace_qa_revalidation(
                    conn, "p1", [("qa-1", "abc1234", "a.py")],
                )
            with db.transaction() as conn:
                db.replace_qa_revalidation(conn, "p1", [])
            assert db.get_qa_revalidations(["qa-1"]) == {}
        finally:
            db.close()


# --- read path (unchanged contract) --------------------------------------------------

def _result(chunk_id: str = "qa-1") -> HybridResult:
    return HybridResult(
        chunk_id=chunk_id, rrf_score=0.02, bm25_rank=1, vector_rank=1,
        file_path=f"qa/{chunk_id}.md", project="p", name=chunk_id,
        qualified_name=chunk_id, node_type="qa_log", start_line=1, end_line=5,
        content='---\nquery: "q"\n---\nbody', snippet="s",
        trust_meta="[qa - 10d ago]",
    )


class TestReadPath:
    def test_flag_stamps_trust_meta_and_cause(self) -> None:
        flagged = _apply_revalidation_flag(_result(), ("abc1234", "src/auth.py"))
        assert flagged.revalidation_cause == "abc1234"
        assert "needs_revalidation — src/auth.py changed in abc1234" in flagged.trust_meta
        assert flagged.snippet.startswith("[needs_revalidation")

    def test_no_flag_is_identity(self) -> None:
        r = _result()
        assert _apply_revalidation_flag(r, None) is r

    def test_flag_overrides_written_verification(self) -> None:
        r = _result()
        r = _apply_revalidation_flag(r, ("abc1234", "src/auth.py"))
        assert _memory_verification(r) == "needs_revalidation"
