"""First-run embedding provider setup: ask, verify with a real call, save.

`setup` used to leave this to the user — append a key to ``~/.env.local``
by hand, edit ``config.toml`` for Ollama — and nothing checked the result
until the first index failed minutes later. Here one real embedding call
decides whether a choice is saved at all, so a wrong key or an unreachable
Ollama is reported at the moment it is entered.

The key value never reaches stdout or a log. Provider error bodies can
quote it back, so every message built from one is scrubbed first.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from hybrid_search import providers
from hybrid_search.config import EmbeddingConfig
from hybrid_search.index.embedder import _resolve_dim, _resolve_model

PROBE_TEXT = "memory layer setup check"
PROBE_TIMEOUT = 20.0
MAX_ATTEMPTS = 3

# Menu order is the order the prompt lists them in.
MENU: tuple[str, ...] = ("openai", "ollama", "gemini")

_SECRET_SHAPES = re.compile(r"\b(sk-[A-Za-z0-9_\-*.]{4,}|AIza[A-Za-z0-9_\-*.]{4,})")
_EMBEDDING_SECTION = re.compile(r"(?ms)^\[embedding\]\n(.*?)(?=^\[|\Z)")
_BACKEND_LINE = re.compile(r"(?m)^backend\s*=.*\n?")
_BASE_URL_LINE = re.compile(r"(?m)^base_url\s*=.*\n?")
_URL_SAFE = re.compile(r"^[A-Za-z0-9._~:/\[\]@\-]+$")


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    # Always safe to print: built from status codes and scrubbed bodies.
    detail: str


@dataclass(frozen=True)
class Choice:
    backend: str
    key: str = ""
    base_url: str = ""


@dataclass(frozen=True)
class Outcome:
    """What `configure` ended with — `ok` is False only on a hard failure."""

    ok: bool
    configured: bool
    backend: str = ""


def _scrub(text: str, key: str) -> str:
    cleaned = text.replace(key, "***") if key else text
    return _SECRET_SHAPES.sub("***", cleaned)


def normalize_base_url(raw: str) -> str:
    """`host:11434`, `http://host:11434/` and `…/v1` all mean the same thing."""
    value = raw.strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"http://{value}"
    if not _URL_SAFE.match(value):
        raise ValueError("the address contains characters a URL cannot hold")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"not a usable address: {raw.strip()!r}")
    value = value.rstrip("/")
    return value if value.endswith("/v1") else f"{value}/v1"


def _error_summary(body: str, key: str) -> str:
    """Provider's own error code and message, scrubbed and kept short."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return _scrub(body.strip(), key)[:200]
    if isinstance(parsed, list) and parsed:
        parsed = parsed[0]
    err = parsed.get("error", parsed) if isinstance(parsed, dict) else {}
    if not isinstance(err, dict):
        return _scrub(str(err), key)[:200]
    code = err.get("code") or err.get("status") or err.get("type") or ""
    message = _scrub(str(err.get("message", "")), key)[:200]
    return f"{code}: {message}".strip(": ")


def _explain_http(
    spec: providers.ProviderSpec, model: str, code: int, body: str, key: str,
) -> str:
    summary = _error_summary(body, key)
    if code in (401, 403):
        return f"{spec.name} rejected the key (HTTP {code}). {summary}"
    if code == 404 and not spec.requires_key:
        return (
            f"{spec.name} is running but has no model `{model}`. "
            f"Run `ollama pull {model}` on that machine, then try again."
        )
    if code == 429:
        return (
            f"{spec.name} accepted the key but refused the request (HTTP 429). "
            f"{summary} — on a new account this usually means billing is not "
            "set up yet."
        )
    return f"{spec.name} returned HTTP {code}. {summary}"


def probe(
    choice: Choice,
    *,
    config: EmbeddingConfig | None = None,
    opener: Callable = urllib.request.urlopen,
    timeout: float = PROBE_TIMEOUT,
) -> ProbeResult:
    """Embed one short string with ``choice`` and check the vector's width."""
    spec = providers.PROVIDERS[choice.backend]
    cfg = replace(config or EmbeddingConfig(), backend=choice.backend)
    model = _resolve_model(cfg, spec)
    dim = _resolve_dim(cfg, spec)
    base = (choice.base_url or spec.base_url).rstrip("/")
    body: dict = {"model": model, "input": [PROBE_TEXT]}
    if spec.supports_dimensions:
        body["dimensions"] = dim
    key = choice.key or ("local" if not spec.requires_key else "")
    request = urllib.request.Request(
        f"{base}/embeddings",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with opener(request, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        width = len(data["data"][0]["embedding"])
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        return ProbeResult(False, _explain_http(spec, model, exc.code, text, key))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = _scrub(str(getattr(exc, "reason", exc)), key)
        hint = "" if spec.requires_key else " Is `ollama serve` running there?"
        return ProbeResult(False, f"could not reach {base} ({reason}).{hint}")
    except (KeyError, IndexError, TypeError, ValueError):
        return ProbeResult(False, f"{base} answered, but not with an embedding.")
    if width != dim:
        return ProbeResult(
            False,
            f"{model} returned {width}-wide vectors; the index expects {dim}.",
        )
    return ProbeResult(True, f"{spec.name} · {model} · {width} dims")


def save_key(key_env: str, value: str, env_file: Path) -> None:
    """Set ``key_env`` in ``env_file``, replacing an older line for it."""
    line = f"{key_env}={value}"
    old_lines = env_file.read_text().splitlines() if env_file.exists() else []
    kept = [ln for ln in old_lines if not ln.strip().startswith(f"{key_env}=")]
    text = "\n".join([*kept, line]) + "\n"
    if env_file.exists():
        env_file.write_text(text)
        return
    # A new secrets file is created owner-only; an existing one keeps the
    # permissions its owner chose.
    fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)


def _embedding_lines(backend: str, base_url: str) -> str:
    lines = f'backend = "{backend}"\n'
    return lines + (f'base_url = "{base_url}"\n' if base_url else "")


def render_backend(text: str, backend: str, base_url: str) -> str:
    """``text`` (config.toml) with [embedding] pointing at ``backend``.

    ``base_url`` is rewritten with it: an Ollama address left behind would
    send the next provider's requests — and its key — to the wrong host.
    """
    new_lines = _embedding_lines(backend, base_url)
    match = _EMBEDDING_SECTION.search(text)
    if match is None:
        sep = "\n\n" if text.strip() else ""
        return f"{text.rstrip()}{sep}[embedding]\n{new_lines}"
    section = _BASE_URL_LINE.sub("", match.group(1))
    if _BACKEND_LINE.search(section):
        section = _BACKEND_LINE.sub(lambda _m: new_lines, section, count=1)
    else:
        section = new_lines + section
    return f"{text[:match.start(1)]}{section}{text[match.end(1):]}"


def save_backend(config_path: Path, backend: str, base_url: str) -> bool:
    """Write the provider choice to config.toml. True when bytes changed."""
    old = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    new = render_backend(old, backend, base_url)
    tomllib.loads(new)  # never leave a config the loader cannot read
    if new == old:
        return False
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(new, encoding="utf-8")
    return True


def persist(choice: Choice, *, config_path: Path, env_file: Path) -> None:
    spec = providers.PROVIDERS[choice.backend]
    if spec.requires_key and choice.key:
        save_key(spec.key_env, choice.key, env_file)
    save_backend(config_path, choice.backend, choice.base_url)


def current_choice(config: EmbeddingConfig) -> Choice | None:
    """The provider already configured, or None when it has no key yet."""
    spec = providers.resolve(config.backend)
    key = providers.api_key(spec) if spec.requires_key else ""
    if spec.requires_key and not key:
        return None
    return Choice(spec.name, key=key, base_url=(config.base_url or "").rstrip("/"))


def _menu_text() -> str:
    labels = {
        "openai": "OpenAI — API key",
        "ollama": "Ollama — local or self-hosted, no key",
        "gemini": "Gemini — API key",
    }
    rows = [f"  {i}) {labels[name]}" for i, name in enumerate(MENU, start=1)]
    return "\n".join([
        "",
        "Embedding provider — the index needs one to build its vectors.",
        *rows,
        "  s) Skip for now (search stays keyword-only until this is set)",
    ])


def _ask_choice(ask: Callable[[str], str], ask_secret: Callable[[str], str]) -> Choice | None:
    answer = ask("Choice [1]: ").strip().lower() or "1"
    if answer in ("s", "skip", "q"):
        return None
    if not answer.isdigit() or not 1 <= int(answer) <= len(MENU):
        raise ValueError(f"pick 1-{len(MENU)} or s")
    backend = MENU[int(answer) - 1]
    spec = providers.PROVIDERS[backend]
    if not spec.requires_key:
        raw = ask(f"Ollama address [{spec.base_url}]: ")
        return Choice(backend, base_url=normalize_base_url(raw))
    key = ask_secret(f"{spec.key_env} (input is hidden): ").strip()
    if not key or any(ch.isspace() for ch in key):
        raise ValueError("that does not look like a key")
    return Choice(backend, key=key)


def prompt(
    *,
    config: EmbeddingConfig,
    ask: Callable[[str], str] = input,
    ask_secret: Callable[[str], str] = getpass.getpass,
    out: Callable[[str], None] = print,
    opener: Callable = urllib.request.urlopen,
) -> Choice | None:
    """Ask until one choice passes a real call, or the user skips."""
    out(_menu_text())
    for _ in range(MAX_ATTEMPTS):
        try:
            choice = _ask_choice(ask, ask_secret)
        except ValueError as exc:
            out(f"  ✗ {exc}")
            continue
        except (EOFError, KeyboardInterrupt):
            out("")
            return None
        if choice is None:
            return None
        out("  checking with one real embedding call…")
        result = probe(choice, config=config, opener=opener)
        if result.ok:
            out(f"  ✓ verified: {result.detail}")
            return choice
        out(f"  ✗ {result.detail}")
    out("  Giving up after three tries — nothing was saved.")
    return None


def choice_from_flags(backend: str, base_url: str, config: EmbeddingConfig) -> Choice:
    """`setup --backend …`: the key comes from the environment, never argv."""
    spec = providers.PROVIDERS[backend]
    if not spec.requires_key:
        return Choice(backend, base_url=normalize_base_url(base_url or config.base_url))
    key = providers.api_key(spec)
    if not key:
        raise LookupError(spec.key_env)
    return Choice(backend, key=key)


def manual_help(config_path: Path) -> str:
    """What to do when setup could not ask (no terminal, or `--yes`)."""
    return (
        "Embedding provider is not set up yet — indexing needs one.\n"
        "  - In your own terminal: `hybrid-search-mcp setup` asks, verifies\n"
        "    the answer with a real call, and saves it (key input is hidden).\n"
        "  - Without a terminal (agents, CI): pass the choice as a flag —\n"
        "      OPENAI_API_KEY=... hybrid-search-mcp setup --backend openai\n"
        "      GEMINI_API_KEY=... hybrid-search-mcp setup --backend gemini\n"
        "      hybrid-search-mcp setup --backend ollama "
        "--base-url http://localhost:11434\n"
        f"Config: {config_path} · keys: ~/.env.local"
    )


def _report_saved(
    choice: Choice, previous_backend: str, config_path: Path, env_file: Path,
    out: Callable[[str], None], has_index: bool,
) -> None:
    spec = providers.PROVIDERS[choice.backend]
    if spec.requires_key:
        out(f"  saved: {spec.key_env} → {env_file}, backend → {config_path}")
        exported = os.environ.get(spec.key_env, "")
        if exported and exported != choice.key:
            out(
                f"  note: {spec.key_env} is also exported in this shell with a "
                "different value, and the environment wins — unset or update it."
            )
    else:
        out(f"  saved: backend → {config_path}")
    if has_index and previous_backend != choice.backend:
        out(
            f"  note: projects already indexed with {previous_backend} need a "
            "rebuild — `hybrid-search-mcp index . --force`."
        )


def _configure_from_flags(
    backend: str, base_url: str, config: EmbeddingConfig, config_path: Path,
    env_file: Path, out: Callable[[str], None], opener: Callable, has_index: bool,
) -> Outcome:
    try:
        choice = choice_from_flags(backend, base_url, config)
    except LookupError as exc:
        out(f"Embedding provider: {exc.args[0]} is not set — nothing to verify.")
        out(manual_help(config_path))
        return Outcome(ok=False, configured=False)
    except ValueError as exc:
        out(f"Embedding provider: {exc}")
        return Outcome(ok=False, configured=False)
    result = probe(choice, config=config, opener=opener)
    if not result.ok:
        out(f"Embedding provider NOT verified: {result.detail}")
        return Outcome(ok=False, configured=False)
    out(f"Embedding provider verified: {result.detail}")
    persist(choice, config_path=config_path, env_file=env_file)
    _report_saved(choice, config.backend, config_path, env_file, out, has_index)
    return Outcome(ok=True, configured=True, backend=choice.backend)


def configure(
    *,
    config: EmbeddingConfig,
    config_path: Path,
    env_file: Path,
    backend: str = "",
    base_url: str = "",
    interactive: bool = False,
    # Whether any project is indexed yet: switching provider strands those
    # vectors, and a first-time user has none to be warned about.
    has_index: bool = False,
    ask: Callable[[str], str] = input,
    ask_secret: Callable[[str], str] = getpass.getpass,
    out: Callable[[str], None] = print,
    opener: Callable = urllib.request.urlopen,
) -> Outcome:
    """`setup`'s embedding step. Network is touched only when asked to verify:
    an explicit ``backend`` flag, or a terminal the user is sitting at."""
    if backend:
        return _configure_from_flags(
            backend, base_url, config, config_path, env_file, out, opener, has_index,
        )
    existing = current_choice(config)
    if not interactive:
        if existing is None:
            out(manual_help(config_path))
            return Outcome(ok=True, configured=False)
        out(f"Embedding provider: {existing.backend} (configured, not verified here)")
        return Outcome(ok=True, configured=True, backend=existing.backend)
    if existing is not None:
        result = probe(existing, config=config, opener=opener)
        if result.ok:
            out(f"Embedding provider verified: {result.detail}")
            return Outcome(ok=True, configured=True, backend=existing.backend)
        out(f"Embedding provider is configured but not working: {result.detail}")
    choice = prompt(config=config, ask=ask, ask_secret=ask_secret, out=out, opener=opener)
    if choice is None:
        out("Embedding provider skipped — run `hybrid-search-mcp setup` again to set it.")
        return Outcome(ok=True, configured=False)
    persist(choice, config_path=config_path, env_file=env_file)
    _report_saved(choice, config.backend, config_path, env_file, out, has_index)
    return Outcome(ok=True, configured=True, backend=choice.backend)
