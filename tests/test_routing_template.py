from __future__ import annotations

from pathlib import Path

import pytest

from hybrid_search.memory.routing_template import (
    BEGIN_RE,
    END_RE,
    LEGACY_AGENTS_MARKER,
    LEGACY_CLAUDE_MARKER,
    ROUTING_BODY,
    RoutingBlock,
    agents_block,
    apply_update,
    claude_block,
    plan_update,
)


def test_fresh_install_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    result = apply_update(path, claude_block())
    assert result.status == "fresh_install"
    assert result.written is True
    text = path.read_text(encoding="utf-8")
    assert BEGIN_RE.search(text)
    assert END_RE.search(text)


def test_fresh_install_file_without_markers_appends(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    path.write_text("# Project\n\nIntro\n", encoding="utf-8")
    apply_update(path, claude_block())
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Project\n\nIntro\n\n")
    assert "<!-- BEGIN hybrid-search-mcp routing v1 -->" in text


def test_fresh_install_h1_only(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    path.write_text("# Project\n", encoding="utf-8")
    apply_update(path, claude_block())
    assert path.read_text(encoding="utf-8").startswith("# Project\n\n")


def test_no_change_returns_no_diff(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    path.write_text(claude_block().render() + "\n", encoding="utf-8")
    result = apply_update(path, claude_block())
    assert result.status == "no_change"
    assert result.diff == ""
    assert result.written is False


def test_update_replaces_existing_v1_body(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    old = RoutingBlock("claude", "## Old\n\nstale").render() + "\n"
    path.write_text(old, encoding="utf-8")
    result = apply_update(path, claude_block())
    assert result.status == "update"
    assert result.written is True
    assert "-## Old" in result.diff
    assert "+## 검색 전략" in result.diff
    assert "stale" not in path.read_text(encoding="utf-8")


def test_migrates_legacy_claude_at_same_position(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    path.write_text(
        "# Project\n\n"
        f"{LEGACY_CLAUDE_MARKER}\n"
        "## 검색 전략 — old\n"
        "legacy body\n\n"
        "## Keep\n"
        "after\n",
        encoding="utf-8",
    )
    result = apply_update(path, claude_block())
    text = path.read_text(encoding="utf-8")
    assert result.status == "migrate_legacy"
    assert LEGACY_CLAUDE_MARKER not in text
    assert text.startswith("# Project\n\n<!-- BEGIN hybrid-search-mcp routing v1 -->")
    assert "## Keep\nafter" in text
    assert apply_update(path, claude_block()).status == "no_change"


def test_migrates_legacy_agents(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text(
        "# Rules\n\n"
        f"{LEGACY_AGENTS_MARKER}\n"
        "## Hybrid Search Memory\n"
        "- old\n\n"
        "Keep\n",
        encoding="utf-8",
    )
    result = apply_update(path, agents_block())
    text = path.read_text(encoding="utf-8")
    assert result.status == "migrate_legacy"
    assert LEGACY_AGENTS_MARKER not in text
    assert "<!-- BEGIN hybrid-search-mcp routing v1 -->" in text
    assert "Keep\n" in text


@pytest.mark.parametrize(
    "content,msg",
    [
        ("<!-- BEGIN hybrid-search-mcp routing v1 -->\nbody\n", "only BEGIN marker found"),
        ("body\n<!-- END hybrid-search-mcp routing v1 -->\n", "only END marker found"),
    ],
)
def test_corrupted_marker_raises(tmp_path: Path, content: str, msg: str) -> None:
    path = tmp_path / "CLAUDE.md"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(RuntimeError, match=msg):
        apply_update(path, claude_block())


def test_force_corrupted_rewrites_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    path.write_text("prefix\n<!-- BEGIN hybrid-search-mcp routing v1 -->\nold\n", encoding="utf-8")
    result = apply_update(path, claude_block(), force=True)
    text = path.read_text(encoding="utf-8")
    assert result.written is True
    assert text.count("hybrid-search-mcp routing v1") == 2
    assert "prefix" in text


def test_preserves_bytes_outside_marker_pair(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    before = "alpha\n\n"
    after = "\n\nomega\n"
    path.write_text(before + RoutingBlock("claude", "## Old").render() + after, encoding="utf-8")
    apply_update(path, claude_block())
    text = path.read_text(encoding="utf-8")
    assert text.startswith(before)
    assert text.endswith(after)


def test_agents_path_uses_same_algorithm(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    result = apply_update(path, agents_block())
    assert result.status == "fresh_install"
    assert ROUTING_BODY.strip() in path.read_text(encoding="utf-8")


def test_dry_run_returns_diff_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    path.write_text("# Project\n", encoding="utf-8")
    result = apply_update(path, claude_block(), dry_run=True)
    assert result.status == "fresh_install"
    assert result.written is False
    assert "--- " in result.diff
    assert path.read_text(encoding="utf-8") == "# Project\n"


def test_plan_version_mismatch() -> None:
    existing = (
        "<!-- BEGIN hybrid-search-mcp routing v2 -->\n"
        "body\n"
        "<!-- END hybrid-search-mcp routing v2 -->"
    )
    assert plan_update(existing, claude_block()).status == "version_mismatch"
