"""QA Reflector (WS3) — same-topic qa logs consolidate into one note.

The qa corpus is append-only, so a topic accumulates near-duplicate and
stale entries that compete for the memory slot (Hindsight's failure
modes, observed in the 2026-08-31 field report). The Reflector follows
the repo's synthesize-wiki pattern — the LLM is the coding agent, not an
API call:

  1. ``qa-reflect --prepare``   clusters the corpus, writes one context
     file per cluster to ``_reflection_input/`` plus a manifest.
  2. The agent reads each context file and writes the consolidated note
     body to ``_reflection_output/<cluster>.md``.
  3. ``qa-reflect --finalize``  validates against the manifest and
     installs notes under ``.hybrid-search/qa/consolidated/``.

Poisoning defenses (master plan D14 / 불변식 4), by construction:

- **Provenance cannot be forged**: finalize writes ``sources:`` from its
  own manifest, never from agent output. An output naming an unknown
  cluster is rejected.
- **Originals are preserved**: nothing is deleted or archived here. The
  note is just the newest same-topic qa entry, so the EXISTING
  index-time ``compute_supersession`` maps every source to it on the
  next reindex — no new DB write path, and a newer real turn on the
  topic will supersede the note in turn.
- **Idempotent** (D6): a cluster's identity is the hash of its member
  ids. A hash that already has a consolidated note is skipped, as is a
  cluster whose newest member IS a consolidated note.

Clustering reuses the strict index-time matcher from ``supersession`` —
one calibration source, not a second set of magic numbers.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from hybrid_search.memory import reader
from hybrid_search.memory.supersession import (
    _is_machine_payload,
    _parse_timestamp,
    _strict_group_indices,
    _topic_item,
)

logger = logging.getLogger(__name__)

INPUT_DIRNAME = ".hybrid-search/_reflection_input"
OUTPUT_DIRNAME = ".hybrid-search/_reflection_output"
CONSOLIDATED_DIRNAME = ".hybrid-search/qa/consolidated"
MANIFEST_NAME = "manifest.json"

# Clusters below this size have nothing to consolidate.
_MIN_CLUSTER_SIZE = 2
# A member contributes to consolidation only if it actually carries an
# answer. Meta-recall turns ("what did we just do") and search-only
# records have queries but no content — a note built from them would be
# exactly the fossilized junk F11 demotes at query time.
_MIN_ANSWER_CHARS = 40
# Context files cap member count (newest first) and per-member excerpt
# size so one chatty topic cannot blow the agent's context budget.
_MAX_MEMBERS_PER_CLUSTER = 6
_MEMBER_EXCERPT_CHARS = 2500
# chars-per-token for the cost estimate printed by --prepare
# (measured 2.38 on this corpus — 2026-08-27 field note).
_CHARS_PER_TOKEN = 2.38

_INSTRUCTIONS = """\
<!-- reflector task — read the member Q&As below and write ONE
consolidated note to {output_path}

The note must:
- state the topic's CURRENT answer (newest member wins on conflicts;
  note explicitly what older members got wrong or what changed),
- keep only insights: decisions, reasons, constraints, gotchas —
  never restate tool logs or transcripts,
- be self-contained prose (a future agent sees only this note first),
- start with frontmatter exactly:

---
cluster: {cluster_id}
---

Everything after the frontmatter is the note body. Provenance is added
by finalize from the manifest — do not add a sources list yourself. -->
"""


@dataclass(frozen=True)
class Cluster:
    cluster_id: str  # sha1[:8] over sorted member ids
    member_ids: tuple[str, ...]
    member_paths: tuple[str, ...]
    representative_query: str  # newest member's query, verbatim


def _qa_id(project_root: Path, path: Path) -> str:
    return str(path.relative_to(project_root / reader.QA_DIRNAME))


def _cluster_hash(member_ids: list[str]) -> str:
    joined = "\n".join(sorted(member_ids))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:8]


def _frontmatter_value(content: str, key: str) -> str | None:
    from hybrid_search.memory.supersession import _frontmatter_value as fv

    return fv(content, key)


def _existing_sources_hashes(project_root: Path) -> set[str]:
    out: set[str] = set()
    cdir = project_root / CONSOLIDATED_DIRNAME
    if not cdir.is_dir():
        return out
    for p in cdir.glob("*.md"):
        try:
            h = _frontmatter_value(p.read_text(encoding="utf-8"), "sources_hash")
        except OSError:
            continue
        if h:
            out.add(h)
    return out


def _answer_text(content: str) -> str:
    if "## Answer excerpt" not in content:
        return ""
    text = content.split("## Answer excerpt", 1)[1]
    return text.split("## Top results", 1)[0].strip()


def _is_low_value(content: str) -> bool:
    """Entries whose consolidation could only produce junk."""
    # Lazy import: the meta-recall predicate lives with the query-time
    # demotion it powers; pulling the search stack here is CLI-only cost.
    from hybrid_search.memory.quality import is_junk_query
    from hybrid_search.search.orchestrator import _is_meta_recall_text

    query = _frontmatter_value(content, "query") or ""
    if is_junk_query(query) or _is_meta_recall_text(query):
        return True
    return len(_answer_text(content)) < _MIN_ANSWER_CHARS


def collect_clusters(project_root: Path) -> list[Cluster]:
    """Strict same-topic clusters of the active qa corpus, consolidation-worthy only."""
    entries: list[tuple[Path, str, datetime | None]] = []
    for path in reader.iter_qa_files(project_root):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if _is_machine_payload(content) or _is_low_value(content):
            continue
        entries.append((path, content, _parse_timestamp(content)))
    if len(entries) < _MIN_CLUSTER_SIZE:
        return []

    # Newest-first so complete-link groups seed on the newest member —
    # same ordering contract as compute_supersession.
    entries.sort(
        key=lambda e: ((e[2] is None), -(e[2].timestamp() if e[2] else 0.0), str(e[0]))
    )
    items = [_topic_item(content) for _, content, _ in entries]

    done_hashes = _existing_sources_hashes(project_root)
    clusters: list[Cluster] = []
    for group in _strict_group_indices(items):
        if len(group) < _MIN_CLUSTER_SIZE:
            continue
        members = [entries[i] for i in group]
        dated = [m for m in members if m[2] is not None]
        if not dated:
            continue  # no trustworthy newest — same refusal as supersession
        newest = max(dated, key=lambda m: (m[2], str(m[0])))
        if _frontmatter_value(newest[1], "memory_type") == "consolidated":
            continue  # this topic already ends in a consolidated note
        members = members[:_MAX_MEMBERS_PER_CLUSTER]
        ids = [_qa_id(project_root, p) for p, _, _ in members]
        cid = _cluster_hash(ids)
        if cid in done_hashes:
            continue
        clusters.append(
            Cluster(
                cluster_id=cid,
                member_ids=tuple(ids),
                member_paths=tuple(str(p) for p, _, _ in members),
                representative_query=_frontmatter_value(newest[1], "query") or "",
            )
        )
    return clusters


def _member_excerpt(content: str) -> str:
    """Question + answer excerpt, trimmed — drop the Top-results dump."""
    cut = content.split("## Top results", 1)[0]
    return cut[:_MEMBER_EXCERPT_CHARS]


def prepare(project_root: Path) -> dict:
    """Write per-cluster context files + manifest. Returns a summary."""
    clusters = collect_clusters(project_root)
    in_dir = project_root / INPUT_DIRNAME
    out_dir = project_root / OUTPUT_DIRNAME
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, dict] = {}
    total_chars = 0
    for c in clusters:
        parts = [
            _INSTRUCTIONS.format(
                output_path=str(out_dir / f"{c.cluster_id}.md"),
                cluster_id=c.cluster_id,
            )
        ]
        for qa_id, p in zip(c.member_ids, c.member_paths):
            try:
                content = Path(p).read_text(encoding="utf-8")
            except OSError:
                continue
            parts.append(f"\n\n===== member {qa_id} =====\n{_member_excerpt(content)}")
        text = "".join(parts)
        (in_dir / f"{c.cluster_id}.md").write_text(text, encoding="utf-8")
        total_chars += len(text)
        manifest[c.cluster_id] = {
            "member_ids": list(c.member_ids),
            "representative_query": c.representative_query,
        }
    (in_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "clusters": len(clusters),
        "input_dir": str(in_dir),
        "output_dir": str(out_dir),
        "est_input_tokens": int(total_chars / _CHARS_PER_TOKEN),
    }


def _yaml_escape(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def finalize(project_root: Path, *, now: datetime | None = None) -> dict:
    """Validate agent outputs against the manifest and install notes."""
    in_dir = project_root / INPUT_DIRNAME
    out_dir = project_root / OUTPUT_DIRNAME
    manifest_path = in_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        return {"installed": 0, "rejected": [], "error": "no manifest — run --prepare first"}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"installed": 0, "rejected": [], "error": "manifest unreadable"}

    now = now or datetime.now(timezone.utc)
    cdir = project_root / CONSOLIDATED_DIRNAME
    installed = 0
    rejected: list[str] = []
    for out_file in sorted(out_dir.glob("*.md")) if out_dir.is_dir() else []:
        try:
            raw = out_file.read_text(encoding="utf-8")
        except OSError:
            rejected.append(f"{out_file.name}: unreadable")
            continue
        cid = _frontmatter_value(raw, "cluster")
        entry = manifest.get(cid or "")
        if entry is None:
            rejected.append(f"{out_file.name}: unknown cluster id {cid!r}")
            continue
        body = raw.split("---", 2)[-1].strip()
        if not body:
            rejected.append(f"{out_file.name}: empty body")
            continue
        cdir.mkdir(parents=True, exist_ok=True)
        note_path = cdir / f"{now.date().isoformat()}-{cid}.md"
        sources = "\n".join(f"  - {mid}" for mid in entry["member_ids"])
        note = (
            "---\n"
            f"query: {_yaml_escape(entry['representative_query'])}\n"
            f"timestamp: {now.isoformat(timespec='seconds')}\n"
            "trigger: reflector\n"
            "memory_type: consolidated\n"
            f"sources_hash: {cid}\n"
            "sources:\n"
            f"{sources}\n"
            "---\n\n"
            # The body sits under the same heading real qa answers use, so
            # the topic matcher's answer path and quality checks see the
            # note exactly like any other qa entry.
            "## Answer excerpt\n\n"
            f"{body}\n"
        )
        note_path.write_text(note, encoding="utf-8")
        installed += 1
        out_file.unlink()
        input_file = in_dir / out_file.name
        if input_file.is_file():
            input_file.unlink()
    return {"installed": installed, "rejected": rejected, "dir": str(cdir)}
