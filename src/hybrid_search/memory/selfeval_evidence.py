"""What a turn actually did with a search result — selfeval v1.2 evidence.

Two measurement defects, each fixed by reading the transcript more closely
rather than by moving the scoring rules (plan ``2026-09-27-selfeval-v1.2``):

- **Shell reads.** An agent reads files with ``grep -n X file`` and
  ``sed -n 1,80p file`` as often as with the Read tool. v1.1 counted every
  ``grep`` as a search of its own — a betrayal even when the target was a
  result file — and ignored ``sed``/``cat`` entirely. ``shell_actions``
  splits a command into the files it reads (scored exactly like Read) and
  the searches it runs (no file target: a global search).
- **Quoted hits.** Memory-lane hits are quoted into the context with an
  instruction *not* to open them, so Read can never adopt them.
  ``quoted_adopted`` asks the only question left: did the answer carry
  something that could only have come from that hit?
"""

from __future__ import annotations

import re
import shlex

# ── Shell commands ────────────────────────────────────────────────────

_SEARCH_HEADS = frozenset({"grep", "rg", "ag", "ack", "egrep", "fgrep"})
_ALWAYS_SEARCH_HEADS = frozenset({"find", "fd"})
_SCRIPT_HEADS = frozenset({"sed", "awk"})
_READ_HEADS = frozenset({"cat", "head", "tail", "nl", "less", "more", "bat"})

# Short options that consume the next token as their value. Without these a
# `grep -A 3 foo file` would read "3" as the pattern and "foo" as a file.
_VALUE_OPTS = {
    "grep": {"-A", "-B", "-C", "-m", "-e", "-f", "-d", "-D"},
    "egrep": {"-A", "-B", "-C", "-m", "-e", "-f"},
    "fgrep": {"-A", "-B", "-C", "-m", "-e", "-f"},
    "rg": {"-A", "-B", "-C", "-m", "-e", "-f", "-g", "-t", "-T", "-j", "-M", "-E",
           "--glob", "--type", "--type-not", "--max-count", "--context"},
    "ag": {"-A", "-B", "-C", "-m", "-G", "-g"},
    "ack": {"-A", "-B", "-C", "-m"},
    "sed": {"-e", "-f"},
    "awk": {"-F", "-v", "-f"},
    "head": {"-n", "-c"},
    "tail": {"-n", "-c"},
    "nl": {"-b", "-v", "-i", "-w", "-s"},
}
# Options whose value *is* the pattern/script, so no positional one follows.
_PATTERN_OPTS = frozenset({"-e", "-f", "--regexp", "--file"})

_SEGMENT_BREAKS = frozenset({"&&", "||", ";", ";;", "&"})
_GLOB_CHARS = frozenset("*?[")


def _looks_like_file(arg: str) -> bool:
    """A target that names one file rather than a directory or a glob.

    Filesystem checks are out: retro scoring runs long after the fact, on
    trees that have moved. So this is lexical — the last path component has
    an extension (dotfiles included). Extensionless files (``Makefile``)
    read as directories; that is the documented cost.
    """
    if not arg or arg.endswith("/") or any(c in _GLOB_CHARS for c in arg):
        return False
    name = arg.rstrip("/").rsplit("/", 1)[-1]
    if name in ("", ".", ".."):
        return False
    return "." in name


_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w-]*)\1")
# Redirection operators, bare (`>`, `2>>`, `&>`) or glued to their target
# (`>out.txt`, `2>/dev/null`). Their target is where output goes, not a file
# the command reads.
_REDIRECT_RE = re.compile(r"^(\d*|&)(>>?|<<?<?|>&|<&)(.*)$")


def _strip_heredocs(command: str) -> str:
    """Drop heredoc bodies — they are stdin text, not arguments.

    Without this, ``cat > build.py <<'EOF' ... EOF`` turns every dotted word
    of the script into a "file read".
    """
    lines = command.split("\n")
    out: list[str] = []
    waiting: list[str] = []
    for line in lines:
        if waiting:
            if line.strip() == waiting[0]:
                waiting.pop(0)
            continue
        out.append(line)
        waiting.extend(m.group(2) for m in _HEREDOC_RE.finditer(line))
    return "\n".join(out)


def _drop_redirects(tokens: list[str]) -> list[str]:
    out: list[str] = []
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        m = _REDIRECT_RE.match(tok)
        if m:
            skip_next = not m.group(3)  # bare operator: target is the next token
            continue
        out.append(tok)
    return out


def _tokenize(command: str) -> list[str] | None:
    lexer = shlex.shlex(
        _strip_heredocs(command).replace("\n", " ; "),
        posix=True,
        punctuation_chars=";&|",
    )
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return _drop_redirects(list(lexer))
    except ValueError:
        return None


def _first_stages(tokens: list[str]) -> list[list[str]]:
    """The first pipeline stage of every ``&&``/``||``/``;`` segment.

    A grep *after* a pipe filters another command's output — it is not a
    search of the codebase, so later stages are dropped.
    """
    stages: list[list[str]] = []
    current: list[str] = []
    in_pipe_tail = False
    for tok in tokens:
        if tok in _SEGMENT_BREAKS:
            if current:
                stages.append(current)
            current, in_pipe_tail = [], False
        elif tok in ("|", "|&"):
            if current:
                stages.append(current)
            current, in_pipe_tail = [], True
        elif not in_pipe_tail:
            current.append(tok)
    if current:
        stages.append(current)
    return stages


def _positionals(head: str, args: list[str]) -> tuple[list[str], bool]:
    """(positional args, whether a pattern/script came in through an option)."""
    value_opts = _VALUE_OPTS.get(head, set())
    out: list[str] = []
    pattern_given = False
    skip_next = False
    only_positional = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if only_positional or not arg.startswith("-") or arg == "-":
            out.append(arg)
            continue
        if arg == "--":
            only_positional = True
            continue
        opt = arg.split("=", 1)[0]
        if opt in _PATTERN_OPTS or (head in _SCRIPT_HEADS and opt in ("-e", "-f")):
            pattern_given = True
        if arg in value_opts:
            skip_next = True
    return out, pattern_given


def _stage_actions(stage: list[str]) -> tuple[list[str], list[str]]:
    # Drop leading env assignments and a stray "(" from subshell syntax.
    while stage and ("=" in stage[0] and not stage[0].startswith("-")):
        stage = stage[1:]
    if not stage:
        return [], []
    head = stage[0].lstrip("(").rsplit("/", 1)[-1]
    args = stage[1:]
    raw = " ".join(stage)[:120]
    if head in _ALWAYS_SEARCH_HEADS:
        return [], [raw]
    if head not in _SEARCH_HEADS | _SCRIPT_HEADS | _READ_HEADS:
        return [], []
    positionals, pattern_given = _positionals(head, args)
    if head in _SEARCH_HEADS | _SCRIPT_HEADS and not pattern_given:
        positionals = positionals[1:]  # the pattern / script itself
    files = [p for p in positionals if _looks_like_file(p)]
    if head in _SEARCH_HEADS and (not files or len(files) != len(positionals)):
        # No target, or a directory/glob among them: a search of its own.
        return files, [raw]
    return files, []


def shell_actions(command: str) -> tuple[list[str], list[str]]:
    """(files read, searches run) for one Bash command.

    Files read are scored exactly like Read targets: a result path is an
    adoption, anything else an outside read. A search is a command that
    names no file — the agent looking on its own.

    Unparseable commands (unbalanced quotes, heredocs gone wrong) fall back
    to the v1.1 prefix rule so a quoting accident never hides a search.
    """
    cmd = (command or "").strip().lstrip("!")
    tokens = _tokenize(cmd)
    if tokens is None:
        return [], ([cmd[:120]] if _legacy_is_search(cmd) else [])
    files: list[str] = []
    searches: list[str] = []
    for stage in _first_stages(tokens):
        f, s = _stage_actions(stage)
        files.extend(f)
        searches.extend(s)
    return files, searches


_LEGACY_PREFIXES = ("rg ", "grep ", "ag ", "ack ", "find ", "fd ")


def _legacy_is_search(command: str) -> bool:
    """The v1.1 rule — kept for unparseable input and for retro comparison."""
    cmd = (command or "").strip().lstrip("!")
    for segment in cmd.replace(";", "&&").split("&&"):
        head = segment.strip().lstrip("(").strip()
        if head.startswith(_LEGACY_PREFIXES):
            return True
    return False


# ── Quoted memory hits ───────────────────────────────────────────────

# Node types rendered as "quoted — no file to open" (hook_runtime).
QUOTED_NODE_TYPES = frozenset({"qa_log", "conv_turn", "memory_card", "commit"})
# Pre-fetch rows only carry paths. Wiki pages live under .hybrid-search/ but
# are rendered as openable file hits, so they are not quoted.
_QUOTED_PATH_PREFIXES = (".hybrid-search/", ".conversations/", ".git-history/")
_OPENABLE_MEMORY_PREFIXES = (".hybrid-search/wiki/",)

_ID_PATTERNS = (
    re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b"),  # hash
    re.compile(r"#\d+\b"),                                               # PR/issue
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),                                # date
    re.compile(r"\b\d+\.\d+\.\d+\b"),                                    # version
    re.compile(r"`([^`\n]{3,80})`"),                                     # code span
    re.compile(r"(?<![\w./-])(?=[\w./-]*[_./])[A-Za-z_][\w./-]{3,}(?<![./-])"),
)
_WORD_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_SHINGLE_WORDS = 3
_SHINGLE_MIN_CHARS = 12
_MIN_IDS = 1
_MIN_SHINGLES = 2


def is_quoted_path(path: str) -> bool:
    p = (path or "").strip()
    p = p[2:] if p.startswith("./") else p
    return p.startswith(_QUOTED_PATH_PREFIXES) and not p.startswith(_OPENABLE_MEMORY_PREFIXES)


def _ids(text: str) -> set[str]:
    found: set[str] = set()
    for pat in _ID_PATTERNS:
        for m in pat.finditer(text or ""):
            token = (m.group(1) if m.groups() else m.group(0)).strip()
            if len(token) >= 3:
                found.add(token.lower())
    return found


def _normalize(text: str) -> str:
    return " ".join(_WORD_RE.sub(" ", (text or "").lower()).split())


def _shingles(text: str) -> set[str]:
    words = _normalize(text).split()
    out: set[str] = set()
    for i in range(len(words) - _SHINGLE_WORDS + 1):
        s = " ".join(words[i : i + _SHINGLE_WORDS])
        if len(s) >= _SHINGLE_MIN_CHARS:
            out.add(s)
    return out


def quoted_adopted(excerpt: str, answer: str, excluded: str) -> bool:
    """True when ``answer`` carries evidence unique to ``excerpt``.

    Evidence is an id (hash, ``#N``, date, version, code span, dotted or
    underscored identifier) or a 3-word phrase from the excerpt. Anything
    that also appears in ``excluded`` — the prompt, the query, other tool
    output in the same window — is dropped: if the answer could have got it
    from there, the hit can't claim it.
    """
    if not excerpt or not answer:
        return False
    answer_l = (answer or "").lower()
    excluded_l = (excluded or "").lower()
    ids = [i for i in _ids(excerpt) if i not in excluded_l]
    if sum(1 for i in ids if i in answer_l) >= _MIN_IDS:
        return True
    excluded_norm = _normalize(excluded)
    answer_norm = _normalize(answer)
    phrases = [s for s in _shingles(excerpt) if s not in excluded_norm]
    return sum(1 for s in phrases if s in answer_norm) >= _MIN_SHINGLES


def best_quoted_rank(quoted: list[dict], answer: str, excluded: str) -> int | None:
    """Best (lowest) rank among quoted hits the answer adopted, else None."""
    ranks = [
        q["rank"]
        for q in quoted
        if isinstance(q.get("rank"), int)
        and quoted_adopted(q.get("excerpt") or "", answer, excluded)
    ]
    return min(ranks) if ranks else None
