"""`setup` embedding step: one real call decides whether a choice is saved.

No network — the opener is injected. Keys are synthetic.
"""

from __future__ import annotations

import io
import json
import stat
import urllib.error
from pathlib import Path

import pytest

from hybrid_search import embedding_setup as es
from hybrid_search import providers
from hybrid_search.config import load_config

FAKE_KEY = "sk-test-0123456789abcdef"


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self) -> bytes:
        return self._raw


def _ok_opener(width: int, seen: list | None = None):
    def opener(request, timeout):
        if seen is not None:
            seen.append(request)
        return _Resp({"data": [{"embedding": [0.0] * width}]})

    return opener


def _http_error(code: int, body: dict):
    def opener(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, code, "err", {}, io.BytesIO(json.dumps(body).encode()),
        )

    return opener


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", providers.PROVIDER_ENV):
        monkeypatch.delenv(name, raising=False)
    config_path = tmp_path / ".hybrid-search" / "config.toml"
    load_config(config_path)  # writes the default file
    return config_path, tmp_path / ".env.local"


class TestProbe:
    def test_success_reports_provider_model_and_width(self) -> None:
        seen: list = []
        result = es.probe(es.Choice("openai", key=FAKE_KEY), opener=_ok_opener(1536, seen))
        assert result.ok
        assert "text-embedding-3-small" in result.detail
        assert seen[0].full_url == "https://api.openai.com/v1/embeddings"
        assert seen[0].get_header("Authorization") == f"Bearer {FAKE_KEY}"

    def test_gemini_asks_for_the_index_width(self) -> None:
        seen: list = []
        es.probe(es.Choice("gemini", key="AIzaFake"), opener=_ok_opener(1536, seen))
        assert json.loads(seen[0].data)["dimensions"] == 1536

    def test_ollama_uses_the_given_address_without_a_key(self) -> None:
        seen: list = []
        choice = es.Choice("ollama", base_url="http://10.0.0.5:11434/v1")
        assert es.probe(choice, opener=_ok_opener(1024, seen)).ok
        assert seen[0].full_url == "http://10.0.0.5:11434/v1/embeddings"

    def test_rejected_key_never_appears_in_the_message(self) -> None:
        body = {"error": {"code": "invalid_api_key",
                          "message": f"Incorrect API key provided: {FAKE_KEY}"}}
        result = es.probe(es.Choice("openai", key=FAKE_KEY), opener=_http_error(401, body))
        assert not result.ok
        assert "rejected the key" in result.detail
        assert FAKE_KEY not in result.detail
        assert "sk-" not in result.detail

    def test_masked_key_shapes_are_scrubbed_too(self) -> None:
        body = {"error": {"message": "Incorrect API key provided: sk-abc***wxyz"}}
        result = es.probe(es.Choice("openai", key=FAKE_KEY), opener=_http_error(401, body))
        assert "sk-abc" not in result.detail

    def test_quota_error_points_at_billing(self) -> None:
        body = {"error": {"code": "insufficient_quota", "message": "You exceeded"}}
        result = es.probe(es.Choice("openai", key=FAKE_KEY), opener=_http_error(429, body))
        assert "insufficient_quota" in result.detail
        assert "billing" in result.detail

    def test_missing_ollama_model_says_how_to_pull_it(self) -> None:
        body = {"error": {"message": "model not found"}}
        result = es.probe(es.Choice("ollama"), opener=_http_error(404, body))
        assert "ollama pull qwen3-embedding:0.6b" in result.detail

    def test_unreachable_ollama_asks_if_it_is_running(self) -> None:
        def opener(request, timeout):
            raise urllib.error.URLError("Connection refused")

        result = es.probe(es.Choice("ollama"), opener=opener)
        assert not result.ok
        assert "ollama serve" in result.detail

    def test_wrong_width_is_a_failure(self) -> None:
        result = es.probe(es.Choice("openai", key=FAKE_KEY), opener=_ok_opener(768))
        assert not result.ok
        assert "768" in result.detail

    def test_non_embedding_answer_is_a_failure(self) -> None:
        def opener(request, timeout):
            return _Resp({"hello": "world"})

        assert not es.probe(es.Choice("openai", key=FAKE_KEY), opener=opener).ok


class TestNormalizeBaseUrl:
    @pytest.mark.parametrize("raw", [
        "10.0.0.5:11434", "http://10.0.0.5:11434", "http://10.0.0.5:11434/",
        "http://10.0.0.5:11434/v1", " http://10.0.0.5:11434/v1/ ",
    ])
    def test_spellings_converge(self, raw: str) -> None:
        assert es.normalize_base_url(raw) == "http://10.0.0.5:11434/v1"

    def test_empty_means_provider_default(self) -> None:
        assert es.normalize_base_url("  ") == ""

    @pytest.mark.parametrize("raw", ['http://h"\nx = 1', "ftp://host", "http://"])
    def test_rejects_what_would_break_the_config(self, raw: str) -> None:
        with pytest.raises(ValueError):
            es.normalize_base_url(raw)


class TestSaveKey:
    def test_new_file_is_owner_only(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env.local"
        es.save_key("OPENAI_API_KEY", FAKE_KEY, env_file)
        assert env_file.read_text() == f"OPENAI_API_KEY={FAKE_KEY}\n"
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600

    def test_replaces_its_own_line_and_keeps_the_rest(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env.local"
        env_file.write_text("OTHER=1\nOPENAI_API_KEY=old\nLAST=2")
        es.save_key("OPENAI_API_KEY", FAKE_KEY, env_file)
        assert env_file.read_text() == f"OTHER=1\nLAST=2\nOPENAI_API_KEY={FAKE_KEY}\n"


class TestSaveBackend:
    def test_default_config_switches_to_ollama(self, paths) -> None:
        config_path, _ = paths
        assert es.save_backend(config_path, "ollama", "http://10.0.0.5:11434/v1")
        loaded = load_config(config_path).embedding
        assert loaded.backend == "ollama"
        assert loaded.base_url == "http://10.0.0.5:11434/v1"
        assert "# backend: which OpenAI-shaped provider" in config_path.read_text()

    def test_leaving_ollama_drops_its_address(self, paths) -> None:
        config_path, _ = paths
        es.save_backend(config_path, "ollama", "http://10.0.0.5:11434/v1")
        es.save_backend(config_path, "openai", "")
        loaded = load_config(config_path).embedding
        assert loaded.backend == "openai"
        assert loaded.base_url == ""

    def test_same_choice_twice_changes_nothing(self, paths) -> None:
        config_path, _ = paths
        es.save_backend(config_path, "gemini", "")
        assert not es.save_backend(config_path, "gemini", "")

    def test_other_sections_are_untouched(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text('[search]\nbackend = "keep"\n\n[embedding]\nbatch_size = 7\n')
        es.save_backend(config_path, "gemini", "")
        text = config_path.read_text()
        assert '[search]\nbackend = "keep"' in text
        assert 'backend = "gemini"\nbatch_size = 7' in text

    def test_config_without_the_section_gains_one(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text('[general]\nlog_level = "info"\n')
        es.save_backend(config_path, "gemini", "")
        assert load_config(config_path).embedding.backend == "gemini"


class TestConfigure:
    def _run(self, paths, **kwargs):
        config_path, env_file = paths
        lines: list[str] = []
        outcome = es.configure(
            config=load_config(config_path).embedding,
            config_path=config_path, env_file=env_file, out=lines.append, **kwargs,
        )
        return outcome, "\n".join(lines)

    def test_no_terminal_and_no_key_explains_without_network(self, paths) -> None:
        def opener(request, timeout):
            raise AssertionError("must not touch the network")

        outcome, text = self._run(paths, opener=opener)
        assert outcome.ok and not outcome.configured
        assert "setup --backend openai" in text

    def test_no_terminal_with_a_key_does_not_verify(self, paths, monkeypatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)

        def opener(request, timeout):
            raise AssertionError("must not touch the network")

        outcome, text = self._run(paths, opener=opener)
        assert outcome.configured
        assert FAKE_KEY not in text

    def test_prompt_verifies_then_saves_key_and_backend(self, paths) -> None:
        config_path, env_file = paths
        answers = iter(["3"])
        outcome, text = self._run(
            paths, interactive=True, ask=lambda _p: next(answers),
            ask_secret=lambda _p: "AIzaSyntheticKey", opener=_ok_opener(1536),
        )
        assert outcome.configured and outcome.backend == "gemini"
        assert env_file.read_text() == "GEMINI_API_KEY=AIzaSyntheticKey\n"
        assert load_config(config_path).embedding.backend == "gemini"
        assert "AIzaSyntheticKey" not in text
        assert "verified" in text

    def test_rejected_key_is_not_saved(self, paths) -> None:
        config_path, env_file = paths
        before = config_path.read_text()
        body = {"error": {"code": "invalid_api_key", "message": "no"}}
        outcome, text = self._run(
            paths, interactive=True, ask=lambda _p: "1",
            ask_secret=lambda _p: FAKE_KEY, opener=_http_error(401, body),
        )
        assert outcome.ok and not outcome.configured
        assert not env_file.exists()
        assert config_path.read_text() == before
        assert text.count("rejected the key") == es.MAX_ATTEMPTS

    def test_skip_saves_nothing(self, paths) -> None:
        _, env_file = paths
        outcome, _ = self._run(paths, interactive=True, ask=lambda _p: "s")
        assert outcome.ok and not outcome.configured
        assert not env_file.exists()

    def test_bad_menu_answer_asks_again(self, paths) -> None:
        config_path, _ = paths
        answers = iter(["9", "2", ""])
        outcome, _ = self._run(
            paths, interactive=True, ask=lambda _p: next(answers),
            opener=_ok_opener(1024),
        )
        assert outcome.backend == "ollama"
        assert load_config(config_path).embedding.base_url == ""

    def test_working_existing_setup_is_not_asked_again(self, paths, monkeypatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)

        def ask(_prompt):
            raise AssertionError("must not prompt")

        outcome, text = self._run(
            paths, interactive=True, ask=ask, opener=_ok_opener(1536),
        )
        assert outcome.configured
        assert "verified" in text

    def test_flag_takes_key_from_env_and_persists_it(self, paths, monkeypatch) -> None:
        _, env_file = paths
        monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
        outcome, text = self._run(paths, backend="openai", opener=_ok_opener(1536))
        assert outcome.ok and outcome.configured
        assert env_file.read_text() == f"OPENAI_API_KEY={FAKE_KEY}\n"
        assert FAKE_KEY not in text

    def test_flag_without_a_key_fails(self, paths) -> None:
        outcome, text = self._run(paths, backend="gemini")
        assert not outcome.ok
        assert "GEMINI_API_KEY is not set" in text

    def test_flag_with_unreachable_ollama_fails_and_saves_nothing(self, paths) -> None:
        config_path, _ = paths
        before = config_path.read_text()

        def opener(request, timeout):
            raise urllib.error.URLError("Connection refused")

        outcome, _ = self._run(
            paths, backend="ollama", base_url="10.0.0.5:11434", opener=opener,
        )
        assert not outcome.ok
        assert config_path.read_text() == before

    def test_switching_provider_mentions_the_rebuild(self, paths) -> None:
        outcome, text = self._run(
            paths, backend="ollama", opener=_ok_opener(1024), has_index=True,
        )
        assert outcome.ok
        assert "index . --force" in text

    def test_first_install_is_not_told_to_rebuild(self, paths) -> None:
        _, text = self._run(paths, backend="ollama", opener=_ok_opener(1024))
        assert "--force" not in text


def test_home_env_local_is_found_from_outside_home(tmp_path, monkeypatch) -> None:
    home, elsewhere = tmp_path / "home", tmp_path / "volume" / "project"
    home.mkdir()
    elsewhere.mkdir(parents=True)
    (home / ".env.local").write_text(f"OPENAI_API_KEY={FAKE_KEY}\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(elsewhere)
    assert providers.load_dotenv_key("OPENAI_API_KEY") == FAKE_KEY
