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

import json

from hybrid_search.index.scanner import compute_file_hash
from hybrid_search.memory.revalidation import (
    GitError,
    ProjectionResult,
    anchor_paths,
    head_unchanged,
    is_source_anchor,
    project_revalidations,
    replace_projection_guarded,
)
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


def _qa(
    ts: str,
    paths: list[str],
    chunk_id: str = "qa-1",
    anchor_hashes: dict[str, str] | None = None,
) -> tuple[str, str]:
    body = f'---\nquery: "auth 어디서 처리해?"\ntimestamp: {ts}\n'
    if anchor_hashes:
        body += "anchor_hash_algo: index\n"
        body += "anchor_hashes: '" + json.dumps(anchor_hashes) + "'\n"
    body += "---\n\n## Answer excerpt\n\nanswer\n\n## Top results\n\n"
    for i, p in enumerate(paths, 1):
        body += f"### {i}. `{p}:1-10` — name\n- chunk_id: `c{i}`\n\n"
    return (chunk_id, body)


def _flags(repo: Repo, entries) -> list[tuple[str, str, str]]:
    result = project_revalidations(repo.root, entries)
    assert result.head is not None and result.complete
    return result.rows


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
        plain = tmp_path / "plain"
        plain.mkdir()
        result = project_revalidations(plain, [_qa(T_QA, ["a.py"])])
        assert result.head is None and result.complete is False


# --- anchor evidence = what the SEARCH RESULTS carried (round-2, 3rd pass) ---------

def _index_hash(repo: Repo, rel: str) -> str:
    return compute_file_hash(repo.root / rel)


class TestAnchorEvidence:
    def test_qa_grounded_on_current_head_is_clean(self, repo: Repo) -> None:
        h = _index_hash(repo, "src/auth.py")
        entries = [_qa(T_QA, ["src/auth.py"], anchor_hashes={"src/auth.py": h})]
        assert _flags(repo, entries) == []

    def test_qa_grounded_on_v1_flags_after_v2_commit(self, repo: Repo) -> None:
        """The round-2 3rd-pass scenario, resolved correctly by result
        provenance: search served v1 (indexed) even though the worktree
        already had v2 — the memory is grounded in v1, so once v2 lands
        at HEAD it MUST be flagged. (v3's worktree hashing recorded v2
        and would have wrongly判 clean.)"""
        v1_hash = _index_hash(repo, "src/auth.py")   # what the index served
        repo.write("src/auth.py", "def verify_token():\n    return 'v2'\n")
        # worktree is v2 now, but the result carried v1:
        entries = [_qa(T_QA, ["src/auth.py"], anchor_hashes={"src/auth.py": v1_hash})]
        repo.commit("commit v2", T1)
        rows = _flags(repo, entries)
        assert len(rows) == 1 and rows[0][2] == "src/auth.py"

    def test_inflight_result_evidence_stays_clean_after_same_commit(
        self, repo: Repo,
    ) -> None:
        """Search served the DIRTY v2 via the in-flight overlay (result
        hash = live content) → committing that same content keeps the
        memory clean."""
        repo.write("src/auth.py", "def verify_token():\n    return 'v2'\n")
        live_hash = _index_hash(repo, "src/auth.py")  # overlay index hash
        entries = [_qa(T_QA, ["src/auth.py"], anchor_hashes={"src/auth.py": live_hash})]
        repo.commit("commit what the overlay served", T1)
        assert _flags(repo, entries) == []

    def test_evidence_is_branch_agnostic(self, repo: Repo) -> None:
        repo.git("checkout", "-q", "-b", "feature-b")
        repo.write("src/auth.py", "def verify_token():\n    return 'b'\n")
        repo.commit("b change", T1)
        h = _index_hash(repo, "src/auth.py")
        entries = [_qa(T_QA_LATE, ["src/auth.py"], anchor_hashes={"src/auth.py": h})]
        assert _flags(repo, entries) == []           # on B: matches ✓
        repo.git("checkout", "-q", "main")
        assert len(_flags(repo, entries)) == 1       # on main: differs → flag

    def test_unknown_algo_falls_back_to_legacy(self, repo: Repo) -> None:
        """Evidence with an unrecognised hash algo must be ignored, not
        misinterpreted — the record degrades to the timestamp path."""
        content = (
            f'---\nquery: "q"\ntimestamp: {T_QA_LATE}\n'
            "anchor_hash_algo: blob\n"
            "anchor_hashes: '" + json.dumps({"src/auth.py": "deadbeef"}) + "'\n"
            "---\n\n## Top results\n\n### 1. `src/auth.py:1-2` — n\n"
        )
        # Legacy path with T_QA_LATE (after any change) → no flag; the
        # bogus "deadbeef" hash must NOT be compared.
        repo.write("src/auth.py", "v2\n")
        repo.commit("change", T1)
        assert _flags(repo, [("qa-x", content)]) == []


class TestAnchorIdentityBoundaries:
    """Round-2 final P0: anchors need node-type and project boundaries —
    memory/virtual results must never anchor, and another project's
    paths must never be checked against this repo's HEAD."""

    @pytest.mark.parametrize("node_type,path,ok", [
        ("function", "src/auth.py", True),
        ("section", "docs/readme.md", True),
        ("in_flight_file", "src/wip.py", True),
        ("qa_log", ".hybrid-search/qa/2026/07/x.md", False),
        ("conv_turn", ".conversations/claude/s.jsonl", False),
        ("commit", ".git-history/commits.md", False),
        ("memory_card", ".hybrid-search/memory/cards/x.md", False),
        ("module", "src/hybrid_search/search/orchestrator.py", False),
        # Virtual path wins even with a source-looking node type.
        ("section", ".hybrid-search/wiki/index.md", False),
        ("function", "", False),
    ])
    def test_is_source_anchor(self, node_type, path, ok) -> None:
        assert is_source_anchor(node_type, path) is ok

    def test_virtual_only_results_never_flag(self, repo: Repo) -> None:
        """Required test 1: a recall qa whose top results were past
        qa/conversations/commits must not flag itself just because those
        virtual paths are absent from HEAD."""
        content = (
            f'---\nquery: "가장 최근 대화가 뭐였지?"\ntimestamp: {T_QA}\n'
            "---\n\n## Answer excerpt\n\nrecall answer\n\n## Top results\n\n"
            "### 1. `.hybrid-search/qa/2026/07/09-x.md:1-10` — old qa\n"
            "### 2. `.conversations/claude/abc.jsonl:1-5` — turn\n"
            "### 3. `.git-history/commits-2026-07.md:1-3` — commit\n"
        )
        repo.write("src/auth.py", "v2\n")
        repo.commit("unrelated change", T1)
        # Legacy path (no evidence): virtual paths never existed at base
        # → conservative skip, complete pass, zero flags.
        result = project_revalidations(repo.root, [("qa-recall", content)])
        assert result.complete is True and result.rows == []

    def test_evidence_bearing_virtual_anchors_never_flag(self, repo: Repo) -> None:
        """Round-2 final follow-up: records written BEFORE the
        writer-side boundary already carry virtual anchors in their
        evidence — the projection itself must filter them, or every such
        record reads as renamed/deleted (virtual paths are never in
        HEAD). This is the evidence path, not the legacy-timestamp path."""
        entries = [_qa(
            T_QA, [".hybrid-search/qa/2026/07/old.md"],
            anchor_hashes={
                ".hybrid-search/qa/2026/07/old.md": {
                    "h": "previous-hash", "p": "hybrid-search-mcp",
                },
                ".conversations/claude/abc.jsonl": "conv-hash",
                ".git-history/commits-2026-07.md": {"h": "x"},
            },
        )]
        result = project_revalidations(
            repo.root, entries, project="hybrid-search-mcp",
        )
        assert result.complete is True and result.rows == []

    def test_evidence_mixed_virtual_and_source_checks_source_only(
        self, repo: Repo,
    ) -> None:
        """Virtual anchors must be skipped WITHOUT shadowing the real
        one — even when three of them occupy the leading top-N slots
        (filter runs before the slice)."""
        entries = [_qa(
            T_QA, ["src/auth.py"],
            anchor_hashes={
                ".hybrid-search/qa/2026/07/a.md": {"h": "x1"},
                ".conversations/claude/b.jsonl": {"h": "x2"},
                ".git-history/c.md": {"h": "x3"},
                "src/auth.py": {"h": "stale-hash"},
            },
        )]
        result = project_revalidations(repo.root, entries)
        assert len(result.rows) == 1 and result.rows[0][2] == "src/auth.py"

    def test_cross_project_anchor_not_checked_against_local_head(
        self, repo: Repo,
    ) -> None:
        """Required test 2: project B's anchor must not be compared with
        (or flagged against) project A's HEAD."""
        entries = [_qa(
            T_QA, ["src/auth.py"],
            anchor_hashes={"src/auth.py": {"h": "bogus-b-hash", "p": "project-b"}},
        )]
        result = project_revalidations(
            repo.root, entries, project="project-a",
        )
        assert result.complete is True and result.rows == []

    def test_same_project_anchor_still_checked(self, repo: Repo) -> None:
        entries = [_qa(
            T_QA, ["src/auth.py"],
            anchor_hashes={"src/auth.py": {"h": "stale-hash", "p": "project-a"}},
        )]
        result = project_revalidations(
            repo.root, entries, project="project-a",
        )
        assert len(result.rows) == 1


class TestWriterIntegration:
    """Round-2 3rd-pass required tests: the REAL writer → frontmatter →
    parser → projection, and the hot path making zero subprocess calls."""

    class _FakeResult:
        def __init__(self, path: str, ihash: str | None, node_type: str = "function"):
            self.chunk_id = "c1"
            self.file_path = path
            self.project = "p"
            self.name = "n"
            self.qualified_name = "q"
            self.node_type = node_type
            self.start_line = 1
            self.end_line = 2
            self.snippet = "s"
            self.indexed_file_hash = ihash

    class _FakeResponse:
        def __init__(self, results):
            self.results = results
            self.query_type = "KOREAN_NL"
            self.effective_bm25_weight = 0.15
            self.query_time_ms = 1.0
            self.total_chunks_searched = 10

    def _record(self, repo: Repo, ihash: str | None, monkeypatch) -> Path:
        from hybrid_search.memory import qa_log

        monkeypatch.setenv(qa_log.ENV_TOGGLE, "1")
        (repo.root / ".hybrid-search").mkdir(exist_ok=True)
        written = qa_log.record(
            query="auth 어디서 처리해?",
            response=self._FakeResponse(
                [self._FakeResult("src/auth.py", ihash)]
            ),
            cwd=str(repo.root),
            async_write=False,
        )
        assert written is not None
        return written

    def test_writer_stores_result_hash_not_worktree(
        self, repo: Repo, monkeypatch,
    ) -> None:
        """Stale index v1 + dirty worktree v2 → the qa must store v1
        (what the search returned), NOT the v2 on disk."""
        v1_hash = _index_hash(repo, "src/auth.py")
        repo.write("src/auth.py", "def verify_token():\n    return 'v2'\n")
        written = self._record(repo, v1_hash, monkeypatch)
        text = written.read_text(encoding="utf-8")
        assert "anchor_hash_algo: index" in text
        assert v1_hash in text
        v2_hash = _index_hash(repo, "src/auth.py")
        assert v2_hash not in text

    def test_writer_to_projection_end_to_end(
        self, repo: Repo, monkeypatch,
    ) -> None:
        """Writer output feeds the projection without hand-assembled
        frontmatter: v1-grounded qa flags after v2 lands at HEAD."""
        v1_hash = _index_hash(repo, "src/auth.py")
        written = self._record(repo, v1_hash, monkeypatch)
        repo.write("src/auth.py", "def verify_token():\n    return 'v2'\n")
        repo.commit("v2", T1)
        entries = [("qa-real", written.read_text(encoding="utf-8"))]
        rows = _flags(repo, entries)
        assert rows and rows[0][0] == "qa-real" and rows[0][2] == "src/auth.py"

    def test_mixed_results_store_only_source_anchors(
        self, repo: Repo, monkeypatch,
    ) -> None:
        """Required test 3: memory-lane hits ahead of code hits must not
        consume anchor slots or leave virtual paths in the evidence."""
        from hybrid_search.memory import qa_log

        monkeypatch.setenv(qa_log.ENV_TOGGLE, "1")
        (repo.root / ".hybrid-search").mkdir(exist_ok=True)
        code_hash = _index_hash(repo, "src/auth.py")
        written = qa_log.record(
            query="auth 어디서 처리해?",
            response=self._FakeResponse([
                self._FakeResult(
                    ".hybrid-search/qa/2026/07/old.md", "qa-file-hash",
                    node_type="qa_log",
                ),
                self._FakeResult(
                    ".conversations/claude/s.jsonl", "conv-hash",
                    node_type="conv_turn",
                ),
                self._FakeResult("src/auth.py", code_hash),
            ]),
            cwd=str(repo.root),
            async_write=False,
        )
        assert written is not None
        text = written.read_text(encoding="utf-8")
        assert code_hash in text
        assert "qa-file-hash" not in text and "conv-hash" not in text
        assert ".hybrid-search/qa" not in text.split("## Top results")[0]

    def test_virtual_only_results_store_no_evidence(
        self, repo: Repo, monkeypatch,
    ) -> None:
        from hybrid_search.memory import qa_log

        monkeypatch.setenv(qa_log.ENV_TOGGLE, "1")
        (repo.root / ".hybrid-search").mkdir(exist_ok=True)
        written = qa_log.record(
            query="가장 최근 대화가 뭐였지?",
            response=self._FakeResponse([
                self._FakeResult(
                    ".hybrid-search/qa/2026/07/old.md", "qa-file-hash",
                    node_type="qa_log",
                ),
            ]),
            cwd=str(repo.root),
            async_write=False,
        )
        assert written is not None
        assert "anchor_hash_algo" not in written.read_text(encoding="utf-8")

    def test_record_hot_path_makes_no_subprocess_calls(
        self, repo: Repo, monkeypatch,
    ) -> None:
        """Round-2 3rd-pass fix 2: evidence rides in on the results —
        the fire-and-forget write contract is restored."""
        import subprocess as _subprocess

        v1_hash = _index_hash(repo, "src/auth.py")

        def forbidden(*args, **kwargs):
            raise AssertionError("subprocess on the qa hot path")

        monkeypatch.setattr(_subprocess, "run", forbidden)
        monkeypatch.setattr(_subprocess, "Popen", forbidden)
        written = self._record(repo, v1_hash, monkeypatch)
        assert "anchor_hash_algo: index" in written.read_text(encoding="utf-8")


class TestGuardedReplace:
    """Round-2 3rd-pass fix 3: HEAD re-verified INSIDE the transaction;
    post-write mismatch rolls back."""

    def _seed(self, db: StoreDB) -> None:
        with db.transaction() as conn:
            db.replace_qa_revalidation(
                conn, "p1", [("qa-old", "aaaaaaa", "old.py")],
                projection_head="old-head",
            )

    def test_replace_sticks_when_head_stable(self, repo: Repo, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        try:
            self._seed(db)
            head = repo.git("rev-parse", "HEAD")
            result = ProjectionResult(
                rows=[("qa-new", "bbbbbbb", "new.py")], head=head, complete=True,
            )
            assert replace_projection_guarded(db, "p1", result, repo.root) is True
            assert db.get_qa_revalidations(["qa-new"]) == {"qa-new": ("bbbbbbb", "new.py")}
        finally:
            db.close()

    def test_post_write_head_move_rolls_back(self, repo: Repo, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        try:
            self._seed(db)
            head = repo.git("rev-parse", "HEAD")
            result = ProjectionResult(
                rows=[("qa-new", "bbbbbbb", "new.py")], head=head, complete=True,
            )
            calls = {"n": 0}

            def moving_git(repo_path, *argv):
                # First check (pre-write) sees the pinned head; the
                # post-write check sees a moved HEAD.
                calls["n"] += 1
                return head if calls["n"] == 1 else "moved-head"

            assert replace_projection_guarded(
                db, "p1", result, repo.root, run_git=moving_git,
            ) is False
            # Rolled back: the OLD projection survives untouched.
            assert db.get_qa_revalidations(["qa-old", "qa-new"]) == {
                "qa-old": ("aaaaaaa", "old.py"),
            }
            assert db.get_meta("qa_reval_projection_head") == "old-head"
        finally:
            db.close()

    def test_pre_write_mismatch_discards_without_touching(self, repo: Repo, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        try:
            self._seed(db)
            result = ProjectionResult(
                rows=[], head="not-current-head", complete=True,
            )
            assert replace_projection_guarded(db, "p1", result, repo.root) is False
            assert db.get_qa_revalidations(["qa-old"]) == {"qa-old": ("aaaaaaa", "old.py")}
        finally:
            db.close()


# --- error/absence separation (round-2 re-review fix 2) -----------------------------

class TestIncompleteProjection:
    def test_git_error_marks_projection_incomplete(self, repo: Repo) -> None:
        """A transient failure must NOT produce a complete-looking empty
        projection that would un-flag everything on replace."""
        from hybrid_search.memory.revalidation import _default_git

        def flaky(repo_path, *argv):
            # The commit-log lookup backs every timestamp→base query.
            if argv[0] == "log" and "--format=%cI %H" in argv:
                raise GitError("timeout")
            return _default_git(repo_path, *argv)

        repo.write("src/auth.py", "v2\n")
        repo.commit("change", T1)
        result = project_revalidations(
            repo.root, [_qa(T_QA, ["src/auth.py"])], run_git=flaky,
        )
        assert result.complete is False

    def test_path_absence_is_not_an_error(self, repo: Repo) -> None:
        """ls-tree exit 0 + empty output = clean absence — the pass
        stays complete and simply doesn't flag."""
        result = project_revalidations(
            repo.root, [_qa(T_QA, ["src/never_existed.py"])],
        )
        assert result.complete is True and result.rows == []


# --- pinned HEAD + CAS (round-2 re-review fix 3) -------------------------------------

class TestPinnedHead:
    def test_no_symbolic_head_after_capture(self, repo: Repo) -> None:
        """Every git command after the initial capture must use the
        pinned SHA — a mid-pass checkout cannot mix two HEADs."""
        from hybrid_search.memory.revalidation import _default_git

        calls: list[tuple[str, ...]] = []

        def spy(repo_path, *argv):
            calls.append(argv)
            return _default_git(repo_path, *argv)

        repo.write("src/auth.py", "v2\n")
        repo.commit("change", T1)
        ev_entries = [_qa(T_QA, ["src/auth.py"])]  # legacy path: base/diff/log
        result = project_revalidations(repo.root, ev_entries, run_git=spy)
        assert result.complete
        after_capture = calls[1:]  # calls[0] is the rev-parse HEAD capture
        for argv in after_capture:
            assert "HEAD" not in argv, f"symbolic HEAD leaked into: {argv}"

    def test_head_unchanged_cas_guard(self, repo: Repo) -> None:
        head = repo.git("rev-parse", "HEAD")
        assert head_unchanged(repo.root, head) is True
        repo.write("src/auth.py", "v2\n")
        repo.commit("moved", T1)
        assert head_unchanged(repo.root, head) is False

    def test_head_unchanged_on_broken_repo_is_false(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert head_unchanged(plain, "abc123") is False


# --- round-3 correctness regressions -------------------------------------------------

class TestNonAsciiPaths:
    """git ls-tree C-quotes non-ASCII paths by default — the HEAD-tree
    preload must use -z (unquoted) or every Korean-named file reads as
    deleted (round-3 P0)."""

    def test_same_hash_korean_path_is_clean(self, repo: Repo) -> None:
        repo.write("src/한글모듈.py", "값 = 1\n")
        repo.commit("korean file", "2026-07-02T00:00:00+00:00")
        h = _index_hash(repo, "src/한글모듈.py")
        entries = [_qa(T_QA, ["src/한글모듈.py"],
                       anchor_hashes={"src/한글모듈.py": h})]
        assert _flags(repo, entries) == []

    def test_changed_korean_path_flags(self, repo: Repo) -> None:
        repo.write("src/한글모듈.py", "값 = 1\n")
        repo.commit("korean file", "2026-07-02T00:00:00+00:00")
        h = _index_hash(repo, "src/한글모듈.py")
        entries = [_qa(T_QA, ["src/한글모듈.py"],
                       anchor_hashes={"src/한글모듈.py": h})]
        repo.write("src/한글모듈.py", "값 = 2\n")
        repo.commit("change", T1)
        rows = _flags(repo, entries)
        assert len(rows) == 1 and rows[0][2] == "src/한글모듈.py"


class TestNonMonotonicCommitDates:
    """`git log --reverse` orders by topology, not timestamp — the base
    lookup must sort by commit date or clock-skew/rebase/cherry-pick
    histories pick a wrong base (round-3 P0)."""

    def test_qa_between_skewed_dates_picks_date_order_base(
        self, repo: Repo,
    ) -> None:
        # Parent order: 07-01 (fixture) → 07-10 → 07-05. The 07-05
        # commit (later in topology, earlier by date) changes the
        # anchor to its HEAD state.
        repo.write("docs/other.md", "x\n")
        repo.commit("skewed later date", "2026-07-10T00:00:00+00:00")
        repo.write("src/auth.py", "def verify_token():\n    return 'v2'\n")
        repo.commit("skewed earlier date", "2026-07-05T00:00:00+00:00")

        # qa at 07-07: by DATE its base includes the 07-05 change, whose
        # content equals HEAD → clean. An unsorted bisect stops before
        # the 07-10 entry and lands on 07-01 → false flag.
        entries = [_qa("2026-07-07T00:00:00+00:00", ["src/auth.py"])]
        assert _flags(repo, entries) == []


class TestCauseCommitAmortised:
    def test_thousand_flags_same_anchor_constant_subprocesses(
        self, repo: Repo,
    ) -> None:
        """Round-3 P1: cause lookup is memoised per (path, since) — a
        corpus where every qa is anchored to the same stale file must
        not run one `git log` per flag."""
        from hybrid_search.memory.revalidation import _default_git

        repo.write("src/auth.py", "v2\n")
        repo.commit("change", T1)
        h = "stale-hash"
        entries = [
            _qa(T_QA, ["src/auth.py"], chunk_id=f"qa-{i}",
                anchor_hashes={"src/auth.py": h})
            for i in range(1000)
        ]
        calls = {"n": 0}

        def counting(repo_path, *argv):
            calls["n"] += 1
            return _default_git(repo_path, *argv)

        result = project_revalidations(repo.root, entries, run_git=counting)
        assert len(result.rows) == 1000
        assert calls["n"] < 10, f"subprocess grew with flag count: {calls['n']}"


# --- scale posture (round-3: projection cost vs qa corpus size) --------------------

class TestProjectionScale:
    def test_thousand_entry_corpus_stays_fast_and_subprocess_bounded(
        self, repo: Repo,
    ) -> None:
        """The pass runs on every reindex — git subprocess count must not
        grow with the qa corpus. 500 legacy (distinct timestamps) + 500
        evidence entries over 40 files: the commit log and the HEAD tree
        are each fetched ONCE; only per-unique-(base,path) lookups and
        per-changed-path cat-files remain."""
        import time as _time

        from hybrid_search.memory.revalidation import _default_git

        for i in range(40):
            repo.write(f"src/mod_{i:02d}.py", f"x = {i}\n")
        repo.commit("forty modules", "2026-07-02T00:00:00+00:00")
        repo.write("src/mod_00.py", "x = 'changed'\n")
        repo.commit("hot change", T1)

        entries = []
        for i in range(500):  # legacy: distinct timestamps
            ts = f"2026-07-0{3 + (i % 5)}T{i % 24:02d}:{i % 60:02d}:00+00:00"
            entries.append(_qa(ts, [f"src/mod_{i % 40:02d}.py"], chunk_id=f"legacy-{i}"))
        for i in range(500):  # evidence: mostly-clean hashes + some stale
            path = f"src/mod_{i % 40:02d}.py"
            h = "stale-hash" if i % 40 == 0 else compute_file_hash(repo.root / path)
            entries.append(_qa(
                T_QA, [path], chunk_id=f"ev-{i}", anchor_hashes={path: h},
            ))

        calls = {"n": 0}

        def counting(repo_path, *argv):
            calls["n"] += 1
            return _default_git(repo_path, *argv)

        start = _time.monotonic()
        result = project_revalidations(repo.root, entries, run_git=counting)
        elapsed = _time.monotonic() - start

        assert result.complete is True
        assert any(r[0].startswith("ev-") for r in result.rows)
        # Bound is generous for CI noise; the point is the ORDER: with a
        # per-timestamp rev-list this corpus would need 500+ subprocesses.
        assert calls["n"] < 150, f"subprocess count grew with corpus: {calls['n']}"
        assert elapsed < 8.0, f"projection too slow at 1k entries: {elapsed:.1f}s"


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
