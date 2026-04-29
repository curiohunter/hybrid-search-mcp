from __future__ import annotations

from pathlib import Path

from hybrid_search.memory import cards, qa_log


class _Resp:
    query_type = "NL_EN"
    effective_bm25_weight = 0.5
    query_time_ms = 1.0
    total_chunks_searched = 10
    results = []


def _write_turn(root: Path) -> Path:
    (root / ".git").mkdir(exist_ok=True)
    path = qa_log.record_turn(
        query="Why did we choose Codex Stop as the qa writer?",
        cwd=str(root),
        answer_chars=180,
        answer_excerpt=(
            "Decision: Codex Stop is the only automatic qa_log writer. "
            "UserPromptSubmit writes pending prompt only. "
            "Next: create memory cards from useful turns. "
            "See src/hybrid_search/codex_hooks.py."
        ),
        trigger="codex_stop_hook",
        client="codex",
    )
    assert path is not None
    return path


def test_create_card_from_qa(tmp_path: Path) -> None:
    qa_path = _write_turn(tmp_path)
    card_path = cards.create_card_from_qa(tmp_path, qa_path.stem)
    assert card_path is not None
    text = card_path.read_text(encoding="utf-8")
    assert "type: memory_card" in text
    assert "## Summary" in text
    assert "Codex Stop is the only automatic qa_log writer" in text
    assert "src/hybrid_search/codex_hooks.py" in text


def test_iter_and_parse_cards(tmp_path: Path) -> None:
    qa_path = _write_turn(tmp_path)
    card_path = cards.create_card_from_qa(tmp_path, qa_path.stem)
    assert card_path is not None
    parsed = cards.parse_card(card_path)
    assert parsed is not None
    assert parsed.type == "memory_card"
    assert parsed.source_ids
    assert "src/hybrid_search/codex_hooks.py" in parsed.files
    assert list(cards.iter_cards(tmp_path))[0].path == card_path


def test_find_card_by_id(tmp_path: Path) -> None:
    qa_path = _write_turn(tmp_path)
    card_path = cards.create_card_from_qa(tmp_path, qa_path.stem)
    assert card_path is not None
    found = cards.find_card_by_id(tmp_path, card_path.stem)
    assert found == card_path


def test_compact_qa_to_cards_skips_existing_source(tmp_path: Path) -> None:
    qa_path = _write_turn(tmp_path)
    first = cards.compact_qa_to_cards(tmp_path)
    second = cards.compact_qa_to_cards(tmp_path)
    assert first["created"] == 1
    assert second["created"] == 0
    assert second["candidates"] == 0
    assert len(list(cards.iter_cards(tmp_path))) == 1
    assert cards.find_card_by_id(tmp_path, qa_path.stem) is None  # card ids differ from qa ids


def test_compact_dry_run_writes_nothing(tmp_path: Path) -> None:
    _write_turn(tmp_path)
    result = cards.compact_qa_to_cards(tmp_path, dry_run=True)
    assert result["created"] == 0
    assert result["candidates"] == 1
    assert list(cards.iter_cards(tmp_path)) == []


def test_write_procedural_candidates(tmp_path: Path) -> None:
    _write_turn(tmp_path)
    cards.compact_qa_to_cards(tmp_path)
    path = cards.write_procedural_candidates(tmp_path)
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "Procedural Memory Candidates" in text
    assert "Next: create memory cards" in text
    assert "- [ ]" in text


def test_export_and_iter_facts(tmp_path: Path) -> None:
    _write_turn(tmp_path)
    cards.compact_qa_to_cards(tmp_path)
    path = cards.export_facts(tmp_path)
    assert path is not None
    facts = list(cards.iter_facts(tmp_path))
    assert facts
    assert facts[0]["predicate"] == "notes"
    assert "Codex Stop" in str(facts[0]["object"])


def test_create_domain_term_from_qa(tmp_path: Path) -> None:
    qa_path = _write_turn(tmp_path)
    card_path = cards.create_card_from_qa(tmp_path, qa_path.stem, card_type="domain_term")

    assert card_path is not None
    text = card_path.read_text(encoding="utf-8")
    assert "type: domain_term" in text
    parsed = cards.parse_card(card_path)
    assert parsed is not None
    assert parsed.type == "domain_term"
