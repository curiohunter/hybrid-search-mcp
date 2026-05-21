from __future__ import annotations

import subprocess
from pathlib import Path

from hybrid_search.config import IndexingConfig
from hybrid_search.index.scanner import parse_git_diff_name_status
from hybrid_search.search.in_flight import (
    InFlightFile,
    InFlightOverlay,
    collect_in_flight_overlay,
    merge_in_flight_results,
    score_in_flight_files,
)
from hybrid_search.search.orchestrator import HybridResult


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _commit_all(repo: Path) -> None:
    _git(repo, "add", ".")
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test User",
            "commit",
            "-m",
            "initial",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _result(path: str, node_type: str = "function") -> HybridResult:
    return HybridResult(
        chunk_id=f"idx:{path}",
        rrf_score=0.02,
        bm25_rank=1,
        vector_rank=None,
        file_path=path,
        project="demo",
        name=Path(path).name,
        qualified_name=path,
        node_type=node_type,
        start_line=1,
        end_line=2,
        content="old indexed content",
        snippet="old indexed content",
    )


def test_parse_git_diff_rename_marks_old_deleted_and_new_added() -> None:
    diff = parse_git_diff_name_status(
        "A\tsrc/new.py\nM\tsrc/edit.py\nD\tsrc/gone.py\nR100\tsrc/old.py\tsrc/renamed.py\n"
    )

    assert diff.added == ["src/new.py", "src/renamed.py"]
    assert diff.modified == ["src/edit.py"]
    assert diff.deleted == ["src/gone.py", "src/old.py"]
    assert diff.renamed == [("src/old.py", "src/renamed.py")]


def test_collects_modified_deleted_and_renamed_from_temp_git_repo(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("def old_name():\n    return 1\n", encoding="utf-8")
    (tmp_path / "src/delete_me.py").write_text("delete me\n", encoding="utf-8")
    (tmp_path / "src/old_name.py").write_text("def renamed_old():\n    pass\n", encoding="utf-8")
    _commit_all(tmp_path)

    (tmp_path / "src/app.py").write_text(
        "def createPayssamV2Payment():\n    return '/api/v2/payment/request'\n",
        encoding="utf-8",
    )
    (tmp_path / "src/delete_me.py").unlink()
    _git(tmp_path, "mv", "src/old_name.py", "src/new_name.py")
    (tmp_path / "src/new_name.py").write_text("def renamed_new():\n    pass\n", encoding="utf-8")

    overlay = collect_in_flight_overlay(tmp_path)

    assert "src/delete_me.py" in overlay.deleted_paths
    assert "src/old_name.py" in overlay.deleted_paths
    assert {f.relative_path for f in overlay.files} == {"src/app.py", "src/new_name.py"}
    assert any("createPayssamV2Payment" in f.content for f in overlay.files)


def test_collect_skips_binary_and_truncates_oversized_text(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    (tmp_path / "binary.py").write_bytes(b"print('ok')\x00more")
    (tmp_path / "large.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(tmp_path)

    (tmp_path / "binary.py").write_bytes(b"print('changed')\x00more")
    (tmp_path / "large.py").write_text("needle = 'match'\n" * 50, encoding="utf-8")

    overlay = collect_in_flight_overlay(tmp_path, max_bytes_per_file=40)

    assert [f.relative_path for f in overlay.files] == ["large.py"]
    assert overlay.files[0].truncated is True
    assert len(overlay.files[0].content.encode("utf-8")) <= 40


def test_collect_respects_indexability_rules(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    (tmp_path / "ignored.py").write_text("value = 1\n", encoding="utf-8")
    _commit_all(tmp_path)
    (tmp_path / "ignored.py").write_text("value = 2\n", encoding="utf-8")

    overlay = collect_in_flight_overlay(
        tmp_path,
        indexing_config=IndexingConfig(exclude_patterns=("ignored.py",)),
    )

    assert overlay.files == []


def test_scores_path_match_above_generic_content_overlap() -> None:
    overlay = InFlightOverlay(
        files=[
            InFlightFile(
                relative_path="src/payments/createPayssamV2Payment.ts",
                status="modified",
                content="export const endpoint = '/api/v2/payment/request'",
                content_hash="a",
            ),
            InFlightFile(
                relative_path="src/other.ts",
                status="modified",
                content="createPayssamV2Payment endpoint endpoint endpoint",
                content_hash="b",
            ),
        ],
        deleted_paths=set(),
    )

    results = score_in_flight_files(
        overlay,
        query="createPayssamV2Payment endpoint",
        project_name="demo",
        project_id="p1",
    )

    assert results[0].file_path == "src/payments/createPayssamV2Payment.ts"
    assert results[0].node_type == "in_flight_file"
    assert results[0].chunk_id.startswith("ephemeral:p1:")
    assert "in-flight" in (results[0].trust_meta or "")
    assert results[0].snippet.startswith("[in-flight]")


def test_score_applies_file_pattern_to_dirty_files() -> None:
    overlay = InFlightOverlay(
        files=[
            InFlightFile(
                relative_path="src/app.py",
                status="modified",
                content="phase five overlay notes",
                content_hash="a",
            ),
            InFlightFile(
                relative_path="docs/plan.md",
                status="modified",
                content="phase five overlay notes",
                content_hash="b",
            ),
        ],
        deleted_paths=set(),
    )

    results = score_in_flight_files(
        overlay,
        query="phase five overlay",
        project_name="demo",
        project_id="p1",
        file_pattern="docs/*",
    )

    assert [r.file_path for r in results] == ["docs/plan.md"]


def test_score_applies_exclude_pattern_to_dirty_files() -> None:
    overlay = InFlightOverlay(
        files=[
            InFlightFile(
                relative_path="src/generated/client.py",
                status="modified",
                content="generated endpoint overlay",
                content_hash="a",
            ),
            InFlightFile(
                relative_path="src/app.py",
                status="modified",
                content="generated endpoint overlay",
                content_hash="b",
            ),
        ],
        deleted_paths=set(),
    )

    results = score_in_flight_files(
        overlay,
        query="generated endpoint overlay",
        project_name="demo",
        project_id="p1",
        exclude_pattern="src/generated/*",
    )

    assert [r.file_path for r in results] == ["src/app.py"]


def test_camel_case_identifier_content_match_beats_generic_overlap() -> None:
    overlay = InFlightOverlay(
        files=[
            InFlightFile(
                relative_path="src/auth.py",
                status="modified",
                content="def signInHandler():\n    return True\n",
                content_hash="a",
            ),
            InFlightFile(
                relative_path="src/generic.py",
                status="modified",
                content="auth handler login",
                content_hash="b",
            ),
        ],
        deleted_paths=set(),
    )

    results = score_in_flight_files(
        overlay,
        query="signInHandler auth handler",
        project_name="demo",
        project_id="p1",
    )

    assert results[0].file_path == "src/auth.py"
    assert results[0].rrf_score > results[1].rrf_score


def test_merge_suppresses_deleted_and_replaces_stale_same_file() -> None:
    dirty = _result("src/app.py")
    dirty.node_type = "in_flight_file"
    dirty.trust_meta = "[in-flight dirty worktree; not indexed]"

    merged = merge_in_flight_results(
        [_result("src/app.py"), _result("src/deleted.py"), _result("src/keep.py")],
        [dirty],
        deleted_paths={"src/deleted.py"},
        limit=10,
    )

    assert [r.file_path for r in merged] == ["src/app.py", "src/keep.py"]
    assert merged[0].node_type == "in_flight_file"
