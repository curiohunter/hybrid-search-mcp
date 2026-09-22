from __future__ import annotations

from pathlib import Path

import pytest

from hybrid_search.memory.routing_template import (
    BEGIN_RE,
    END_RE,
    USER_BEGIN,
    USER_END,
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


def test_update_installs_current_body(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    old = RoutingBlock("claude", "## Old\n\nstale").render() + "\n"
    path.write_text(old, encoding="utf-8")
    result = apply_update(path, claude_block())
    assert result.status == "update"
    assert result.written is True
    assert "+## 검색 전략" in result.diff
    assert ROUTING_BODY.strip() in path.read_text(encoding="utf-8")


def _user_region(text: str) -> str:
    return text.split(USER_BEGIN, 1)[1].split(USER_END, 1)[0]


def test_hand_written_line_inside_block_survives_update(tmp_path: Path) -> None:
    # The bug: a rule written right under the routing table (the natural spot)
    # was inside the machine-owned block, so the next reindex deleted it.
    path = tmp_path / "CLAUDE.md"
    edited = claude_block().render().replace(
        "| **정밀 조회**",
        "| **DB 맵** | \"테이블 목록\" | `db_map_tables` | Grep |\n| **정밀 조회**",
        1,
    )
    path.write_text("# Project\n\n" + edited + "\n", encoding="utf-8")

    result = apply_update(path, claude_block())
    text = path.read_text(encoding="utf-8")

    assert result.status == "update"
    assert "db_map_tables" in text
    assert "db_map_tables" in _user_region(text)
    assert result.preserved == ("| **DB 맵** | \"테이블 목록\" | `db_map_tables` | Grep |",)
    # And it stays put: the rescue is idempotent, not a every-run reshuffle.
    assert apply_update(path, claude_block()).status == "no_change"
    assert path.read_text(encoding="utf-8") == text


def test_user_region_survives_a_template_change(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    apply_update(path, claude_block())
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(USER_END, "## 우리 규칙\n- 커밋 전 스캔\n" + USER_END),
        encoding="utf-8",
    )

    result = apply_update(path, RoutingBlock("claude", ROUTING_BODY + "\n- 새 규칙 한 줄\n"))
    text = path.read_text(encoding="utf-8")

    assert result.status == "update"
    assert "- 새 규칙 한 줄" in text
    assert "## 우리 규칙" in _user_region(text)
    assert "- 커밋 전 스캔" in _user_region(text)
    # A template upgrade must not treat its own retired lines as user content.
    assert result.preserved == ()


def test_retired_template_lines_are_not_rescued(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    old_body = ROUTING_BODY + "\n- 이 줄은 다음 버전에서 사라진다\n"
    apply_update(path, RoutingBlock("claude", old_body))

    result = apply_update(path, claude_block())
    text = path.read_text(encoding="utf-8")

    assert result.preserved == ()
    assert "이 줄은 다음 버전에서 사라진다" not in text


def test_rescue_without_snapshot_prefers_keeping_lines(tmp_path: Path) -> None:
    # No snapshot (fresh clone, or an install that predates it): we cannot tell
    # a retired template line from a human one, so we keep it.
    path = tmp_path / "CLAUDE.md"
    apply_update(path, RoutingBlock("claude", ROUTING_BODY + "\n- 출처 불명 한 줄\n"))
    snapshot = path.parent / ".hybrid-search" / "runtime" / "routing-body-claude.md"
    snapshot.unlink()

    result = apply_update(path, claude_block())

    assert result.preserved == ("- 출처 불명 한 줄",)
    assert "- 출처 불명 한 줄" in _user_region(path.read_text(encoding="utf-8"))


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


def test_routing_body_covers_deferred_tool_recovery() -> None:
    # Tool Search (default-on since 2026-01) can defer the MCP tool schema in
    # tool-heavy environments; the routing block must tell the agent how to
    # load it instead of drifting to Grep.
    assert "ToolSearch" in ROUTING_BODY
    assert "select:mcp__hybrid-search__hybrid_search" in ROUTING_BODY
