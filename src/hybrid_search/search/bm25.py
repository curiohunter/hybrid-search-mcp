"""Tantivy-based BM25 search engine.

Indexes: content + name + qualified_name + docstring per chunk (§13).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import tantivy

logger = logging.getLogger(__name__)


@dataclass
class BM25Result:
    chunk_id: str
    score: float


class BM25Engine:
    """Tantivy BM25 full-text search index.

    Args:
        index_dir: Path to tantivy index directory.
        read_only: If True, skip writer creation and schema-mismatch recovery.
                   Use for search-only access (e.g., cross-project search).
    """

    def __init__(self, index_dir: Path, read_only: bool = False) -> None:
        self._index_dir = index_dir
        self._read_only = read_only
        index_dir.mkdir(parents=True, exist_ok=True)

        self._schema = self._build_schema()

        # Open or create index
        try:
            self._index = tantivy.Index(self._schema, path=str(index_dir))
        except Exception:
            if read_only:
                logger.warning("Cannot open BM25 index at %s in read-only mode", index_dir)
                self._index = None
                self._writer = None
                return
            # If schema mismatch, recreate (only in write mode)
            import shutil
            logger.warning("BM25 schema mismatch at %s, recreating index", index_dir)
            shutil.rmtree(index_dir, ignore_errors=True)
            index_dir.mkdir(parents=True, exist_ok=True)
            self._index = tantivy.Index(self._schema, path=str(index_dir))

        self._writer = None

    def _build_schema(self) -> tantivy.Schema:
        builder = tantivy.SchemaBuilder()
        builder.add_text_field("chunk_id", stored=True, tokenizer_name="raw")
        builder.add_text_field("name", stored=True)
        builder.add_text_field("qualified_name", stored=True)
        builder.add_text_field("content", stored=False)
        builder.add_text_field("docstring", stored=False)
        return builder.build()

    def _get_writer(self) -> tantivy.IndexWriter:
        if self._writer is None:
            self._writer = self._index.writer(heap_size=50_000_000)
        return self._writer

    def add(
        self,
        chunk_id: str,
        name: str,
        qualified_name: str,
        content: str,
        docstring: str | None = None,
    ) -> None:
        """Add a document to the index."""
        writer = self._get_writer()
        doc = tantivy.Document(
            chunk_id=chunk_id,
            name=name or "",
            qualified_name=qualified_name or "",
            content=content or "",
            docstring=docstring or "",
        )
        writer.add_document(doc)

    def delete(self, chunk_id: str) -> None:
        """Delete a document by chunk_id."""
        writer = self._get_writer()
        writer.delete_documents("chunk_id", chunk_id)

    def delete_batch(self, chunk_ids: list[str]) -> None:
        """Delete multiple documents."""
        for cid in chunk_ids:
            self.delete(cid)

    def commit(self) -> None:
        """Commit pending changes and reload the index."""
        if self._writer is not None:
            self._writer.commit()
            self._writer = None
        self._index.reload()

    def search(self, query_text: str, limit: int = 10) -> list[BM25Result]:
        """Search using BM25 scoring across all text fields."""
        if self._index is None:
            return []
        self._index.reload()
        searcher = self._index.searcher()

        # Search across all text fields
        search_fields = ["name", "qualified_name", "content", "docstring"]

        try:
            query = self._index.parse_query(query_text, search_fields)
        except Exception:
            # Escape all Tantivy special characters
            escaped = _escape_tantivy_query(query_text)
            try:
                query = self._index.parse_query(escaped, search_fields)
            except Exception:
                return []

        try:
            results = searcher.search(query, limit)
        except Exception:
            return []

        output: list[BM25Result] = []
        for score, addr in results.hits:
            doc = searcher.doc(addr)
            chunk_id = doc["chunk_id"][0]
            output.append(BM25Result(chunk_id=chunk_id, score=score))

        return output

    @property
    def count(self) -> int:
        """Approximate document count."""
        if self._index is None:
            return 0
        self._index.reload()
        searcher = self._index.searcher()
        return searcher.num_docs


# Tantivy query syntax special characters
_TANTIVY_SPECIAL = set('+-&|!(){}[]^"~*?:\\/>')


def _escape_tantivy_query(text: str) -> str:
    """Escape Tantivy query syntax special characters."""
    return "".join(f"\\{c}" if c in _TANTIVY_SPECIAL else c for c in text)
