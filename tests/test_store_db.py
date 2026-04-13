from pathlib import Path

from hybrid_search.storage.db import ChunkRecord, FileRecord, StoreDB


def test_upsert_file_does_not_delete_existing_chunks(tmp_path: Path) -> None:
    db = StoreDB(tmp_path / "store.db")
    conn = db._conn
    file_id = "file1"

    db.upsert_file(
        conn,
        FileRecord(
            id=file_id,
            project_id="project1",
            relative_path="src/example.py",
            file_hash="",
            file_size=10,
            file_mtime="1",
            language="python",
            chunk_count=0,
        ),
    )
    db.insert_chunks(
        conn,
        [
            ChunkRecord(
                id="chunk1",
                file_id=file_id,
                project_id="project1",
                content="print('hello')",
            )
        ],
    )

    db.upsert_file(
        conn,
        FileRecord(
            id=file_id,
            project_id="project1",
            relative_path="src/example.py",
            file_hash="abc123",
            file_size=10,
            file_mtime="1",
            language="python",
            chunk_count=1,
        ),
    )

    assert db.get_file_count("project1") == 1
    assert db.get_chunk_count("project1") == 1
