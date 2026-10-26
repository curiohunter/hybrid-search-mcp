import json
import sqlite3

from hybrid_search import notices


def _settings(tmp_path, body):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(body))
    return path


def test_retention_warns_when_unset(tmp_path):
    assert "default 30" in notices.retention_notice(tmp_path / "missing.json")


def test_retention_warns_when_not_raised(tmp_path):
    assert "is 30" in notices.retention_notice(_settings(tmp_path, {"cleanupPeriodDays": 30}))


def test_retention_silent_when_raised(tmp_path):
    assert notices.retention_notice(_settings(tmp_path, {"cleanupPeriodDays": 3650})) is None


def test_retention_ignores_non_integer(tmp_path):
    assert notices.claude_cleanup_days(_settings(tmp_path, {"cleanupPeriodDays": True})) is None


def _db(tmp_path, rows):
    path = tmp_path / "store.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE modules (id TEXT)")
    conn.executemany("INSERT INTO modules VALUES (?)", [(str(i),) for i in range(rows)])
    conn.commit()
    conn.close()
    return path


def test_modules_notice_on_zero(tmp_path):
    assert "0 modules" in notices.modules_notice(_db(tmp_path, 0))


def test_modules_notice_silent_when_present(tmp_path):
    assert notices.modules_notice(_db(tmp_path, 3)) is None


def test_modules_notice_silent_without_index(tmp_path):
    assert notices.modules_notice(tmp_path / "none.db") is None
