"""Tests for cwd-based project priority boost in cross-project search."""

import pytest

from hybrid_search.search.orchestrator import (
    SearchOrchestrator,
    _weighted_interleave,
    _interleave_round_robin,
)
from hybrid_search.project import ProjectInfo


class TestWeightedInterleave:
    def test_basic_2_to_1(self):
        primary = ["p1", "p2", "p3", "p4", "p5", "p6"]
        secondary = ["s1", "s2", "s3"]
        result = _weighted_interleave(primary, secondary, primary_ratio=2)
        # Expected: p1, p2, s1, p3, p4, s2, p5, p6, s3
        assert result == ["p1", "p2", "s1", "p3", "p4", "s2", "p5", "p6", "s3"]

    def test_dedup(self):
        primary = ["a", "b", "c"]
        secondary = ["b", "d", "e"]  # "b" is duplicate
        result = _weighted_interleave(primary, secondary, primary_ratio=2)
        assert "b" in result
        assert result.count("b") == 1

    def test_empty_primary(self):
        result = _weighted_interleave([], ["s1", "s2"], primary_ratio=2)
        assert result == ["s1", "s2"]

    def test_empty_secondary(self):
        result = _weighted_interleave(["p1", "p2"], [], primary_ratio=2)
        assert result == ["p1", "p2"]

    def test_both_empty(self):
        assert _weighted_interleave([], [], primary_ratio=2) == []

    def test_ratio_1(self):
        primary = ["p1", "p2"]
        secondary = ["s1", "s2"]
        result = _weighted_interleave(primary, secondary, primary_ratio=1)
        assert result == ["p1", "s1", "p2", "s2"]


class TestDetectPrimaryProject:
    def _make_info(self, name: str, path: str) -> ProjectInfo:
        return ProjectInfo(
            id=f"id_{name}", name=name, path=path,
            last_indexed_at=None, file_count=0, chunk_count=0,
        )

    def test_cwd_inside_project(self):
        projects = [
            self._make_info("valuein", "/home/user/projects/valuein_homepage"),
            self._make_info("breeze", "/home/user/projects/breeze"),
        ]
        result = SearchOrchestrator._detect_primary_project(
            "/home/user/projects/valuein_homepage/app/dashboard", projects
        )
        assert result == "id_valuein"

    def test_cwd_is_project_root(self):
        projects = [
            self._make_info("valuein", "/home/user/projects/valuein_homepage"),
        ]
        result = SearchOrchestrator._detect_primary_project(
            "/home/user/projects/valuein_homepage", projects
        )
        assert result == "id_valuein"

    def test_cwd_no_match(self):
        projects = [
            self._make_info("valuein", "/home/user/projects/valuein_homepage"),
        ]
        result = SearchOrchestrator._detect_primary_project(
            "/home/user/other/unrelated", projects
        )
        assert result is None

    def test_cwd_matches_first_project(self):
        projects = [
            self._make_info("breeze", "/home/user/projects/breeze"),
            self._make_info("valuein", "/home/user/projects/valuein_homepage"),
        ]
        result = SearchOrchestrator._detect_primary_project(
            "/home/user/projects/breeze/src", projects
        )
        assert result == "id_breeze"


class TestRoundRobinUnchanged:
    """Verify the existing round-robin interleave still works correctly."""

    def test_basic(self):
        result = _interleave_round_robin([["a", "b"], ["c", "d"]])
        assert result == ["a", "c", "b", "d"]

    def test_uneven(self):
        result = _interleave_round_robin([["a", "b", "c"], ["d"]])
        assert result == ["a", "d", "b", "c"]

    def test_single_list(self):
        result = _interleave_round_robin([["a", "b", "c"]])
        assert result == ["a", "b", "c"]

    def test_empty(self):
        result = _interleave_round_robin([])
        assert result == []
