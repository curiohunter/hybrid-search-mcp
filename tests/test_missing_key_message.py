"""First-run `index` without an embedding key: guidance and exit 2, not a traceback.

Synthetic project, isolated HOME — nothing touches the real ~/.hybrid-search.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from hybrid_search.config import EmbeddingConfig
from hybrid_search.index.embedder import Embedder, MissingAPIKeyError

SRC = Path(__file__).resolve().parents[1] / "src"


def test_missing_key_is_still_a_value_error(monkeypatch, tmp_path) -> None:
    """Existing ValueError handlers (search fail-open) must keep catching it."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    embedder = Embedder(EmbeddingConfig(), tmp_path / "models")
    with pytest.raises(ValueError) as info:
        embedder._get_api_key()
    assert isinstance(info.value, MissingAPIKeyError)
    assert info.value.key_env == "OPENAI_API_KEY"
    assert "OPENAI_API_KEY not found" in str(info.value)


@pytest.fixture
def project(tmp_path) -> Path:
    root = tmp_path / "demo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "shop.py").write_text(
        'def total(lines):\n    """Sum line amounts."""\n    return sum(lines)\n'
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def test_index_without_key_explains_setup(tmp_path, project) -> None:
    home = tmp_path / "home"
    home.mkdir()
    res = subprocess.run(
        [sys.executable, "-c", "from hybrid_search.cli import main; main()",
         "index", str(project)],
        cwd=project, capture_output=True, text=True, timeout=120,
        env={"HOME": str(home), "PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin",
             "HYBRID_SEARCH_NO_USAGE_LOG": "1"},
    )
    assert res.returncode == 2, res.stderr[-2000:]
    assert "Traceback" not in res.stderr
    assert "OPENAI_API_KEY is not set" in res.stderr
    assert 'backend = "ollama"' in res.stderr
    assert str(home / ".hybrid-search" / "config.toml") in res.stderr


def test_default_config_names_ollama(tmp_path) -> None:
    from hybrid_search.config import load_config

    load_config(tmp_path / "config.toml")
    text = (tmp_path / "config.toml").read_text()
    assert '"ollama"' in text
