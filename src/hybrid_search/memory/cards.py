"""Memory cards — compact semantic memories promoted from qa logs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from hybrid_search.memory import reader as qa_reader

CARD_DIRNAME = ".hybrid-search/memory/cards"
FRONTMATTER_DELIM = "---"


@dataclass(frozen=True)
class MemoryCard:
    path: Path
    summary: str
    query: str
    source_ids: tuple[str, ...]
    topics: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    followups: tuple[str, ...] = ()
    confidence: str = "medium"
    status: str = "active"
    timestamp: datetime | None = None

    @property
    def id(self) -> str:
        parts = self.path.parts
        if len(parts) >= 3 and parts[-3].isdigit() and parts[-2].isdigit():
            return f"{parts[-3]}-{parts[-2]}-{self.path.stem}"
        return self.path.stem


def card_dir(project_root: Path) -> Path:
    return project_root / CARD_DIRNAME


def _yaml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _yaml_scalar(value: str) -> str:
    return f'"{_yaml_escape(value)}"'


def _yaml_list(values: Iterable[str]) -> str:
    vals = [v for v in values if v]
    if not vals:
        return "[]"
    return "[" + ", ".join(_yaml_scalar(v) for v in vals) + "]"


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


_PATH_RE = re.compile(r"(?P<path>(?:[\w.-]+/)+[\w.@-]+\.[A-Za-z0-9]+)(?::\d+(?:-\d+)?)?")
_BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(.+)")


def _extract_files(text: str) -> tuple[str, ...]:
    seen: list[str] = []
    for match in _PATH_RE.finditer(text):
        path = match.group("path")
        if path not in seen:
            seen.append(path)
        if len(seen) >= 12:
            break
    return tuple(seen)


def _extract_topics(query: str, body: str) -> tuple[str, ...]:
    raw = re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}|[가-힣]{2,}", f"{query} {body}")[:80]
    stop = {
        "the", "and", "with", "from", "that", "this", "what", "when", "where",
        "어떻게", "무엇", "확인", "프로젝트", "메모리", "구조",
    }
    out: list[str] = []
    for tok in raw:
        low = tok.lower()
        if low in stop:
            continue
        if low not in out:
            out.append(low)
        if len(out) >= 8:
            break
    return tuple(out)


def _extract_section(body: str, heading: str) -> str:
    marker = f"## {heading}"
    start = body.find(marker)
    if start < 0:
        return ""
    rest = body[start + len(marker):].lstrip()
    end = rest.find("\n## ")
    return (rest[:end] if end >= 0 else rest).strip()


def _summary_from_body(query: str, body: str) -> str:
    excerpt = _extract_section(body, "Answer excerpt") or body
    for para in re.split(r"\n\s*\n", excerpt):
        clean = " ".join(para.split())
        if clean and not clean.startswith("#"):
            return clean[:500].rstrip()
    return query[:500].rstrip()


def _extract_decisions(body: str) -> tuple[str, ...]:
    out: list[str] = []
    for line in body.splitlines():
        m = _BULLET_RE.match(line)
        candidate = m.group(1).strip() if m else line.strip()
        low = candidate.lower()
        if any(k in low for k in ("decision", "decide", "chose", "must", "should", "결정", "해야", "금지")):
            out.append(candidate[:240])
        if len(out) >= 8:
            break
    return tuple(out)


def _extract_followups(body: str) -> tuple[str, ...]:
    out: list[str] = []
    for line in body.splitlines():
        m = _BULLET_RE.match(line)
        candidate = m.group(1).strip() if m else line.strip()
        low = candidate.lower()
        if any(k in low for k in ("next", "todo", "follow", "남은", "다음", "해야")):
            out.append(candidate[:240])
        if len(out) >= 6:
            break
    return tuple(out)


def _build_card_content(
    *,
    query: str,
    summary: str,
    source_id: str,
    source_path: Path,
    topics: tuple[str, ...],
    files: tuple[str, ...],
    decisions: tuple[str, ...],
    followups: tuple[str, ...],
) -> str:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        FRONTMATTER_DELIM,
        "type: memory_card",
        f"query: {_yaml_scalar(query)}",
        f"summary: {_yaml_scalar(summary)}",
        f"source_ids: {_yaml_list([source_id])}",
        f"source_paths: {_yaml_list([str(source_path)])}",
        f"topics: {_yaml_list(topics)}",
        f"files: {_yaml_list(files)}",
        f"decisions: {_yaml_list(decisions)}",
        f"followups: {_yaml_list(followups)}",
        "confidence: medium",
        "status: active",
        f"timestamp: {ts}",
        FRONTMATTER_DELIM,
        "",
        "## Summary",
        "",
        summary,
        "",
    ]
    if decisions:
        lines.extend(["## Decisions", ""])
        lines.extend(f"- {d}" for d in decisions)
        lines.append("")
    if files:
        lines.extend(["## Files", ""])
        lines.extend(f"- `{f}`" for f in files)
        lines.append("")
    if followups:
        lines.extend(["## Followups", ""])
        lines.extend(f"- {f}" for f in followups)
        lines.append("")
    lines.extend([
        "## Evidence",
        "",
        f"- qa: `{source_id}`",
        f"- path: `{source_path}`",
        "",
        "## When to use",
        "",
        f"Use when answering follow-up questions related to: {query}",
    ])
    return "\n".join(lines).rstrip() + "\n"


def create_card_from_qa(project_root: Path, qa_id: str) -> Path | None:
    idx = qa_reader.find_qa_by_id(project_root, qa_id)
    if idx is None:
        return None
    qa_path = idx.path
    body = qa_reader.read_qa_body(qa_path)
    summary = _summary_from_body(idx.query, body)
    files = _extract_files(body)
    topics = _extract_topics(idx.query, body)
    decisions = _extract_decisions(body)
    followups = _extract_followups(body)
    source_id = idx.id
    content = _build_card_content(
        query=idx.query,
        summary=summary,
        source_id=source_id,
        source_path=qa_path.relative_to(project_root),
        topics=topics,
        files=files,
        decisions=decisions,
        followups=followups,
    )
    now = datetime.now(timezone.utc)
    stem = f"{now.strftime('%d-%H%M%S')}-{_hash_text(idx.query + source_id)}"
    path = card_dir(project_root) / f"{now.year:04d}" / f"{now.month:02d}" / f"{stem}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return path


def _existing_source_ids(project_root: Path) -> set[str]:
    out: set[str] = set()
    for card in iter_cards(project_root):
        out.update(card.source_ids)
    return out


def compact_qa_to_cards(
    project_root: Path,
    *,
    since: str | None = None,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, object]:
    """Promote qa logs without existing cards into deterministic memory cards."""
    cutoff: datetime | None = None
    if since:
        cutoff = datetime.now(timezone.utc) - qa_reader.parse_duration(since)
    existing = _existing_source_ids(project_root)
    candidates: list[qa_reader.QAIndex] = []
    for idx in qa_reader.iter_qa_indexes(project_root):
        if idx.id in existing:
            continue
        if cutoff and (idx.timestamp is None or idx.timestamp < cutoff):
            continue
        candidates.append(idx)
    if limit is not None:
        candidates = candidates[:limit]
    if dry_run:
        return {
            "created": 0,
            "candidates": len(candidates),
            "paths": [],
        }
    created: list[str] = []
    for idx in candidates:
        path = create_card_from_qa(project_root, idx.id)
        if path is not None:
            created.append(str(path))
    return {
        "created": len(created),
        "candidates": len(candidates),
        "paths": created,
    }


def iter_card_files(project_root: Path) -> Iterator[Path]:
    root = card_dir(project_root)
    if not root.is_dir():
        return
    files = [p for p in root.rglob("*.md") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    yield from files


def _parse_list(raw: str) -> tuple[str, ...]:
    val = raw.strip()
    if val.startswith("[") and val.endswith("]"):
        val = val[1:-1]
    out: list[str] = []
    for part in val.split(","):
        item = part.strip().strip('"')
        if item:
            out.append(item.replace('\\"', '"').replace("\\\\", "\\"))
    return tuple(out)


def _parse_frontmatter(block: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip().strip('"')
    return out


def parse_card(path: Path) -> MemoryCard | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    block, _ = qa_reader._split_frontmatter(text)
    if not block:
        return None
    fm = _parse_frontmatter(block)
    if fm.get("type") != "memory_card":
        return None
    try:
        ts = datetime.fromisoformat(fm.get("timestamp", ""))
    except ValueError:
        ts = None
    return MemoryCard(
        path=path,
        summary=fm.get("summary", ""),
        query=fm.get("query", ""),
        source_ids=_parse_list(fm.get("source_ids", "")),
        topics=_parse_list(fm.get("topics", "")),
        files=_parse_list(fm.get("files", "")),
        decisions=_parse_list(fm.get("decisions", "")),
        followups=_parse_list(fm.get("followups", "")),
        confidence=fm.get("confidence", "medium"),
        status=fm.get("status", "active"),
        timestamp=ts,
    )


def iter_cards(project_root: Path) -> Iterator[MemoryCard]:
    for path in iter_card_files(project_root):
        card = parse_card(path)
        if card is not None:
            yield card


def find_card_by_id(project_root: Path, token: str) -> Path | None:
    tok = token.strip()
    if not tok:
        return None
    for path in iter_card_files(project_root):
        card = parse_card(path)
        candidates = {path.stem}
        if card is not None:
            candidates.add(card.id)
        if tok in candidates or path.stem.endswith(tok):
            return path
    return None


def _procedural_candidate_lines(card: MemoryCard) -> list[str]:
    signals = [*card.decisions, *card.followups, card.summary]
    out: list[str] = []
    keywords = (
        "next", "should", "must", "when", "after", "before", "verify",
        "다음", "해야", "반드시", "이후", "전에", "검증",
    )
    for text in signals:
        low = text.lower()
        if any(k in low for k in keywords):
            line = text.strip()
            if line and line not in out:
                out.append(line)
        if len(out) >= 10:
            break
    return out


def write_procedural_candidates(project_root: Path) -> Path | None:
    """Write reviewed-by-human procedural memory candidates from active cards."""
    rows: list[tuple[MemoryCard, str]] = []
    for card in iter_cards(project_root):
        if card.status != "active":
            continue
        for line in _procedural_candidate_lines(card):
            rows.append((card, line))
    if not rows:
        return None
    path = project_root / ".hybrid-search" / "memory" / "procedural-candidates.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Procedural Memory Candidates",
        "",
        "Review these before applying them to AGENTS.md, CLAUDE.md, or a skill.",
        "",
    ]
    seen: set[str] = set()
    for card, line in rows:
        if line in seen:
            continue
        seen.add(line)
        lines.append(f"- [ ] {line}")
        lines.append(f"  - source: `{card.id}`")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def export_facts(project_root: Path) -> Path | None:
    """Export lightweight temporal facts from memory cards to facts.jsonl."""
    rows: list[dict[str, object]] = []
    for card in iter_cards(project_root):
        if card.status != "active":
            continue
        facts = card.decisions or (card.summary,)
        for fact in facts:
            fact = fact.strip()
            if not fact:
                continue
            rows.append({
                "subject": card.topics[0] if card.topics else card.query[:80],
                "predicate": "notes",
                "object": fact,
                "valid_from": card.timestamp.isoformat() if card.timestamp else None,
                "valid_to": None,
                "source": f"memory_card:{card.id}",
            })
    if not rows:
        return None
    path = project_root / ".hybrid-search" / "memory" / "facts.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def iter_facts(project_root: Path) -> Iterator[dict[str, object]]:
    path = project_root / ".hybrid-search" / "memory" / "facts.jsonl"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            yield payload
