"""Tests for file scanner — index/scanner.py (crash recovery, delta detection)."""

from unittest.mock import patch
from pathlib import Path

from hybrid_search.config import IndexingConfig
from hybrid_search.index.scanner import (
    _is_changed,
    _is_sensitive_file,
    compute_file_hash,
    excluded_paths_summary,
    get_changed_files_from_git,
    parse_git_diff_name_status,
    scan_project,
    scan_project_subset,
)
from hybrid_search.storage.db import FileRecord, StoreDB


class TestIsChanged:
    """_is_changed() delta detection tests."""

    def test_empty_hash_triggers_reindex(self, tmp_path: Path) -> None:
        """file_hash="" (partial write from crash) should always return True."""
        f = tmp_path / "test.py"
        f.write_text("print('hello')")
        rec = FileRecord(
            id="f1", project_id="p1", relative_path="test.py",
            file_hash="",  # <-- crash marker
            file_size=int(f.stat().st_size),
            file_mtime=str(f.stat().st_mtime),
        )
        assert _is_changed(f, rec) is True

    def test_matching_hash_not_changed(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("print('hello')")
        real_hash = compute_file_hash(f)
        stat = f.stat()
        rec = FileRecord(
            id="f1", project_id="p1", relative_path="test.py",
            file_hash=real_hash,
            file_size=stat.st_size,
            file_mtime=str(stat.st_mtime),
        )
        assert _is_changed(f, rec) is False

    def test_different_size_triggers_hash_check(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("print('hello')")
        rec = FileRecord(
            id="f1", project_id="p1", relative_path="test.py",
            file_hash="fakehash",
            file_size=999,  # different size
            file_mtime=str(f.stat().st_mtime),
        )
        assert _is_changed(f, rec) is True

    def test_missing_file_returns_changed(self, tmp_path: Path) -> None:
        missing = tmp_path / "gone.py"
        rec = FileRecord(
            id="f1", project_id="p1", relative_path="gone.py",
            file_hash="abc",
        )
        assert _is_changed(missing, rec) is True


class TestComputeFileHashFrontmatter:
    """Q6: Markdown YAML frontmatter stripped before hashing."""

    def test_md_frontmatter_does_not_affect_hash(self, tmp_path: Path) -> None:
        body = "# Heading\n\nBody text.\n"
        with_fm = tmp_path / "a.md"
        with_fm.write_text(
            "---\nreviewed: 2026-04-20\nstatus: fresh\n---\n" + body,
            encoding="utf-8",
        )
        plain = tmp_path / "b.md"
        plain.write_text(body, encoding="utf-8")

        assert compute_file_hash(with_fm) == compute_file_hash(plain)

    def test_frontmatter_edit_keeps_hash_stable(self, tmp_path: Path) -> None:
        body = "# Module\n\nContent.\n"
        f = tmp_path / "page.md"
        f.write_text("---\nreviewed: 2026-04-01\n---\n" + body, encoding="utf-8")
        before = compute_file_hash(f)
        f.write_text("---\nreviewed: 2026-04-20\nstatus: fresh\n---\n" + body, encoding="utf-8")
        after = compute_file_hash(f)

        assert before == after

    def test_body_edit_changes_hash(self, tmp_path: Path) -> None:
        f = tmp_path / "page.md"
        f.write_text("---\nr: 1\n---\n# v1\n", encoding="utf-8")
        before = compute_file_hash(f)
        f.write_text("---\nr: 1\n---\n# v2\n", encoding="utf-8")
        after = compute_file_hash(f)

        assert before != after

    def test_md_without_frontmatter_hashes_body(self, tmp_path: Path) -> None:
        """No frontmatter means nothing is stripped."""
        import hashlib

        body = b"# Plain\n\nNo frontmatter here.\n"
        f = tmp_path / "plain.md"
        f.write_bytes(body)

        assert compute_file_hash(f) == hashlib.sha256(body).hexdigest()

    def test_body_level_horizontal_rule_preserved(self, tmp_path: Path) -> None:
        """A `---` horizontal rule in the body must not be treated as frontmatter."""
        body = "# Heading\n\n---\n\nSection.\n"
        f = tmp_path / "page.md"
        f.write_text(body, encoding="utf-8")
        fm_wrapped = tmp_path / "page_fm.md"
        fm_wrapped.write_text("---\nr: 1\n---\n" + body, encoding="utf-8")

        assert compute_file_hash(f) == compute_file_hash(fm_wrapped)

    def test_non_md_file_unaffected(self, tmp_path: Path) -> None:
        """Non-.md files must hash the full bytes, even if they start with `---`."""
        import hashlib

        content = b"---\nkey: value\n---\nprint('hi')\n"
        f = tmp_path / "weird.py"
        f.write_bytes(content)

        assert compute_file_hash(f) == hashlib.sha256(content).hexdigest()

    def test_crlf_frontmatter_stripped(self, tmp_path: Path) -> None:
        body = b"# Heading\r\n\r\nBody.\r\n"
        f = tmp_path / "win.md"
        f.write_bytes(b"---\r\nkey: value\r\n---\r\n" + body)
        plain = tmp_path / "plain.md"
        plain.write_bytes(body)

        assert compute_file_hash(f) == compute_file_hash(plain)

    def test_unclosed_frontmatter_not_stripped(self, tmp_path: Path) -> None:
        """Opening `---` without a closing `---` is not a frontmatter block."""
        import hashlib

        content = b"---\nno closing delimiter\n# Heading\n"
        f = tmp_path / "broken.md"
        f.write_bytes(content)

        assert compute_file_hash(f) == hashlib.sha256(content).hexdigest()


class TestGitDiff:
    def test_parses_name_status_output(self, tmp_path: Path) -> None:
        completed = type(
            "Proc",
            (),
            {
                "returncode": 0,
                "stdout": "A\tnew.py\nM\tsrc/app.py\nD\told.py\nR100\tbefore.py\tafter.py\n",
                "stderr": "",
            },
        )()
        with patch("hybrid_search.index.scanner.subprocess.run", return_value=completed):
            result = get_changed_files_from_git(tmp_path)

        assert result is not None
        assert result.added == ["new.py", "after.py"]
        assert result.modified == ["src/app.py"]
        assert result.deleted == ["old.py", "before.py"]
        assert result.renamed == [("before.py", "after.py")]


class TestParseGitDiffNameStatus:
    """M3: public parser used by both subprocess and env-var fast paths."""

    def test_empty_string_yields_empty_result(self) -> None:
        result = parse_git_diff_name_status("")
        assert result.added == []
        assert result.modified == []
        assert result.deleted == []
        assert result.renamed == []

    def test_parses_all_status_kinds(self) -> None:
        raw = "A\tnew.py\nM\tsrc/app.py\nD\told.py\nR100\tbefore.py\tafter.py\n"
        result = parse_git_diff_name_status(raw)
        assert result.added == ["new.py", "after.py"]
        assert result.modified == ["src/app.py"]
        assert result.deleted == ["old.py", "before.py"]
        assert result.renamed == [("before.py", "after.py")]

    def test_blank_lines_skipped(self) -> None:
        raw = "\nA\ta.py\n\n\nM\tb.py\n\n"
        result = parse_git_diff_name_status(raw)
        assert result.added == ["a.py"]
        assert result.modified == ["b.py"]

    def test_unknown_status_codes_ignored(self) -> None:
        """T (type change), U (unmerged), etc. are ignored without error."""
        raw = "T\ttype-changed.py\nU\tconflict.py\nA\tfine.py\n"
        result = parse_git_diff_name_status(raw)
        assert result.added == ["fine.py"]
        assert result.modified == []
        assert result.deleted == []

    def test_rename_similarity_scores_accepted(self) -> None:
        """``R050`` (50% similarity), ``R100`` (exact rename) — both valid."""
        raw = "R050\told/path.py\tnew/path.py\nR100\tverbatim.py\tmoved.py\n"
        result = parse_git_diff_name_status(raw)
        assert result.renamed == [
            ("old/path.py", "new/path.py"),
            ("verbatim.py", "moved.py"),
        ]
        # Each rename counts as (delete old, add new)
        assert set(result.deleted) == {"old/path.py", "verbatim.py"}
        assert set(result.added) == {"new/path.py", "moved.py"}


class TestSubsetScan:
    def test_detects_added_changed_deleted_from_subset(self, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        project_root = tmp_path / "project"
        project_root.mkdir()

        existing = project_root / "existing.py"
        existing.write_text("print('new')\n")
        added = project_root / "added.py"
        added.write_text("print('added')\n")

        with db.transaction() as conn:
            db.upsert_file(
                conn,
                FileRecord(
                    id="f-existing",
                    project_id="p1",
                    relative_path="existing.py",
                    file_hash="old-hash",
                    file_size=1,
                    file_mtime="0",
                    language="python",
                ),
            )
            db.upsert_file(
                conn,
                FileRecord(
                    id="f-deleted",
                    project_id="p1",
                    relative_path="deleted.py",
                    file_hash="old-hash",
                    file_size=1,
                    file_mtime="0",
                    language="python",
                ),
            )

        result = scan_project_subset(
            project_root,
            "p1",
            db,
            IndexingConfig(),
            changed_paths=["existing.py", "added.py", "ignored.txt"],
            deleted_paths=["deleted.py"],
        )

        assert [p.name for p in result.added] == ["added.py"]
        assert [p.name for p in result.changed] == ["existing.py"]
        assert result.deleted == ["deleted.py"]


# ---------------------------------------------------------------------------
# Q5 — sensitive file filter
# ---------------------------------------------------------------------------


class TestIsSensitiveFile:
    """Basename + full-path pattern matching for credential-like files."""

    def test_env_variants_blocked(self) -> None:
        for name in (".env", ".env.local", ".env.production", ".envrc"):
            assert _is_sensitive_file(Path(name)), name

    def test_credential_yaml_json_blocked(self) -> None:
        for name in (
            "credentials.json",
            "app-credentials.yaml",
            "secrets.yml",
            "api-secrets.toml",
            "service-account.json",
            "service-account-prod.json",
        ):
            assert _is_sensitive_file(Path(name)), name

    def test_keys_and_certs_blocked(self) -> None:
        for name in ("my.pem", "prod.key", "cert.p12", "server.crt", "id_rsa", "id_ed25519.pub"):
            assert _is_sensitive_file(Path(name)), name

    def test_shell_cred_stores_blocked(self) -> None:
        for name in (".netrc", ".pgpass", ".htpasswd"):
            assert _is_sensitive_file(Path(name)), name

    def test_ssh_id_under_dotssh_blocked(self) -> None:
        """Located under .ssh/ — must catch even with full path."""
        for p in ("home/user/.ssh/id_rsa", "root/.ssh/id_ed25519"):
            assert _is_sensitive_file(Path(p)), p

    def test_aws_gcloud_credentials_by_path(self) -> None:
        assert _is_sensitive_file(Path("home/me/.aws/credentials"))
        assert _is_sensitive_file(Path("root/.gcloud/legacy_credentials/me/foo"))

    def test_source_files_not_blocked(self) -> None:
        """Legit source code must not be mistaken for secrets."""
        for name in (
            "src/auth/PasswordReset.tsx",
            "app/components/TokenManager.ts",
            "tests/test_password.py",
            "docs/secrets.md",           # .md extension → docs, not creds
            "components/secret-ingredient.ts",
            "auth/credential_helpers.ts",  # .ts not in cred extensions
        ):
            assert not _is_sensitive_file(Path(name)), name


class TestScanProjectSkipsSensitive:
    """scan_project must silently drop sensitive files from the walk."""

    def test_full_scan_omits_credentials_and_env(self, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        project_root = tmp_path / "project"
        project_root.mkdir()
        # Legitimate indexable files
        (project_root / "main.py").write_text("print('x')\n")
        (project_root / "config.json").write_text("{}")
        # Sensitive files that match supported extensions — must be skipped
        (project_root / "credentials.json").write_text('{"api_key": "super-secret"}')
        (project_root / "secrets.yaml").write_text("token: super-secret\n")
        (project_root / "service-account.json").write_text('{"client_email": "x"}')
        # .env has suffix .env (not a supported ext) — extension filter already blocks
        (project_root / ".env").write_text("API_KEY=xxx")

        result = scan_project(project_root, "p1", db, IndexingConfig())
        names = {p.name for p in result.added}
        assert "main.py" in names
        assert "config.json" in names
        assert "credentials.json" not in names
        assert "secrets.yaml" not in names
        assert "service-account.json" not in names


class TestContentNoiseFilter:
    """Phase 1 router plan — content-heavy corpus noise is skipped by default."""

    def test_binary_content_extensions_are_skipped(self, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / "app.py").write_text("print('x')\n")
        (project_root / "guide.pdf").write_bytes(b"%PDF-1.7")
        (project_root / "book.epub").write_bytes(b"epub")

        cfg = IndexingConfig(supported_extensions=(".py", ".pdf", ".epub"))
        result = scan_project(project_root, "p1", db, cfg)
        names = {p.name for p in result.added}

        assert "app.py" in names
        assert "guide.pdf" not in names
        assert "book.epub" not in names

    def test_oversized_content_markdown_skipped_only_under_content_roots(
        self, tmp_path: Path,
    ) -> None:
        db = StoreDB(tmp_path / "store.db")
        project_root = tmp_path / "project"
        learning = project_root / "docs" / "learning"
        plans = project_root / "docs" / "plans"
        learning.mkdir(parents=True)
        plans.mkdir(parents=True)
        large = "x" * 32
        (learning / "book.md").write_text(large)
        (plans / "design.md").write_text(large)

        cfg = IndexingConfig(content_md_max_bytes=8)
        result = scan_project(project_root, "p1", db, cfg)
        rels = {str(p.relative_to(project_root)) for p in result.added}

        assert "docs/learning/book.md" not in rels
        assert "docs/plans/design.md" in rels

    def test_oversized_markdown_skipped_under_nested_content_root(
        self, tmp_path: Path,
    ) -> None:
        db = StoreDB(tmp_path / "store.db")
        project_root = tmp_path / "project"
        learning = project_root / "docs" / "valueinmath_docs" / "학습"
        analysis = project_root / "docs" / "valueinmath_docs" / "분석"
        learning.mkdir(parents=True)
        analysis.mkdir(parents=True)
        (learning / "worksheet.md").write_text("x" * 32)
        (analysis / "report.md").write_text("x" * 32)

        cfg = IndexingConfig(
            content_md_max_bytes=8,
            content_roots=("학습", "분석"),
        )
        result = scan_project(project_root, "p1", db, cfg)

        assert [p.name for p in result.added] == []

    def test_allow_paths_override_content_filter(self, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        project_root = tmp_path / "project"
        learning = project_root / "docs" / "learning"
        learning.mkdir(parents=True)
        (learning / "keep.md").write_text("x" * 32)

        cfg = IndexingConfig(
            content_md_max_bytes=8,
            content_allow_paths=("docs/learning/keep.md",),
        )
        result = scan_project(project_root, "p1", db, cfg)

        assert [p.name for p in result.added] == ["keep.md"]

    def test_include_content_disables_content_filter(self, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / "guide.pdf").write_bytes(b"%PDF-1.7")

        cfg = IndexingConfig(supported_extensions=(".pdf",), include_content=True)
        result = scan_project(project_root, "p1", db, cfg)

        assert [p.name for p in result.added] == ["guide.pdf"]

    def test_excluded_paths_summary_counts_reasons(self, tmp_path: Path) -> None:
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / ".hybrid-search-ignore").write_text("generated/\n")
        (project_root / "guide.pdf").write_bytes(b"%PDF-1.7")
        learning = project_root / "docs" / "learning"
        learning.mkdir(parents=True)
        (learning / "book.md").write_text("x" * 32)
        generated = project_root / "generated"
        generated.mkdir()
        (generated / "schema.py").write_text("# generated\n")

        summary = excluded_paths_summary(
            project_root,
            IndexingConfig(content_md_max_bytes=8),
        )

        assert summary.counts["extension"] == 1
        assert summary.counts["oversize_md"] == 1
        assert summary.counts["manual"] == 1


# ---------------------------------------------------------------------------
# Q10 — .hybrid-search-ignore + upward walk to .git boundary
# ---------------------------------------------------------------------------


class TestHybridSearchIgnore:
    """`.hybrid-search-ignore` patterns collected from project_root upward.

    Walk stops at the directory containing ``.git`` (included) or at the
    filesystem root, whichever comes first.
    """

    @staticmethod
    def _seed_git_root(root: Path) -> None:
        (root / ".git").mkdir()

    def test_local_ignore_excludes_matching_files(self, tmp_path: Path) -> None:
        """Basic: ``.hybrid-search-ignore`` at project root drops matching paths."""
        db = StoreDB(tmp_path / "store.db")
        project_root = tmp_path / "project"
        project_root.mkdir()
        self._seed_git_root(project_root)

        (project_root / ".hybrid-search-ignore").write_text("build/\n*.generated.ts\n")

        (project_root / "main.py").write_text("print(1)\n")
        (project_root / "app.generated.ts").write_text("export const x = 1\n")
        (project_root / "build").mkdir()
        (project_root / "build" / "out.js").write_text("console.log(1)\n")
        (project_root / "src").mkdir()
        (project_root / "src" / "keep.ts").write_text("export {}\n")

        result = scan_project(project_root, "p1", db, IndexingConfig())
        names = {p.name for p in result.added}
        assert "main.py" in names
        assert "keep.ts" in names
        assert "app.generated.ts" not in names
        assert "out.js" not in names

    def test_parent_ignore_walked_up_for_subfolder(self, tmp_path: Path) -> None:
        """Monorepo case: parent ignore file applies when scanning a subfolder."""
        db = StoreDB(tmp_path / "store.db")
        monorepo = tmp_path / "monorepo"
        monorepo.mkdir()
        self._seed_git_root(monorepo)
        (monorepo / ".hybrid-search-ignore").write_text("generated/\n")

        subproject = monorepo / "packages" / "foo"
        subproject.mkdir(parents=True)
        (subproject / "keep.py").write_text("x = 1\n")
        (subproject / "generated").mkdir()
        (subproject / "generated" / "schema.py").write_text("# auto\n")

        result = scan_project(subproject, "p1", db, IndexingConfig())
        names = {p.name for p in result.added}
        assert "keep.py" in names
        assert "schema.py" not in names

    def test_walk_stops_at_git_boundary(self, tmp_path: Path) -> None:
        """Ancestor ignore above the ``.git`` root must NOT affect the scan."""
        db = StoreDB(tmp_path / "store.db")
        outer = tmp_path / "outer"
        outer.mkdir()
        # Outer ignore that should NOT apply — above the git boundary.
        (outer / ".hybrid-search-ignore").write_text("*.py\n")

        repo = outer / "repo"
        repo.mkdir()
        self._seed_git_root(repo)
        (repo / "keep.py").write_text("x = 1\n")

        result = scan_project(repo, "p1", db, IndexingConfig())
        names = {p.name for p in result.added}
        assert "keep.py" in names  # outer's *.py pattern must not bleed in

    def test_combines_with_gitignore(self, tmp_path: Path) -> None:
        """Both `.gitignore` and `.hybrid-search-ignore` contribute patterns."""
        db = StoreDB(tmp_path / "store.db")
        project_root = tmp_path / "project"
        project_root.mkdir()
        self._seed_git_root(project_root)

        (project_root / ".gitignore").write_text("dist/\n")
        (project_root / ".hybrid-search-ignore").write_text("vendor/\n")

        (project_root / "app.py").write_text("x = 1\n")
        (project_root / "dist").mkdir()
        (project_root / "dist" / "bundle.js").write_text("x\n")
        (project_root / "vendor").mkdir()
        (project_root / "vendor" / "lib.py").write_text("y\n")

        result = scan_project(project_root, "p1", db, IndexingConfig())
        names = {p.name for p in result.added}
        assert "app.py" in names
        assert "bundle.js" not in names
        assert "lib.py" not in names

    def test_comments_and_blank_lines_ignored(self, tmp_path: Path) -> None:
        """Blank lines and ``#`` comments are handled by pathspec (no-op)."""
        db = StoreDB(tmp_path / "store.db")
        project_root = tmp_path / "project"
        project_root.mkdir()
        self._seed_git_root(project_root)

        (project_root / ".hybrid-search-ignore").write_text(
            "\n"
            "# comment — should be ignored as a pattern\n"
            "*.log\n"
            "\n"
        )
        (project_root / "real.py").write_text("x = 1\n")
        (project_root / "noise.log").write_text("log\n")  # .log not in supported ext anyway

        # Force .log into supported ext so we verify the pattern, not the ext filter.
        cfg = IndexingConfig(supported_extensions=[".py", ".log"])
        result = scan_project(project_root, "p1", db, cfg)
        names = {p.name for p in result.added}
        assert "real.py" in names
        assert "noise.log" not in names

    def test_missing_ignore_file_is_noop(self, tmp_path: Path) -> None:
        """No ignore file anywhere → scan behaves like before."""
        db = StoreDB(tmp_path / "store.db")
        project_root = tmp_path / "project"
        project_root.mkdir()
        self._seed_git_root(project_root)

        (project_root / "a.py").write_text("x = 1\n")
        (project_root / "b.py").write_text("y = 2\n")

        result = scan_project(project_root, "p1", db, IndexingConfig())
        names = {p.name for p in result.added}
        assert names == {"a.py", "b.py"}

    def test_oversized_ignore_file_skipped_gracefully(self, tmp_path: Path) -> None:
        """Ignore file above 64KB is skipped (safety) — indexing continues."""
        db = StoreDB(tmp_path / "store.db")
        project_root = tmp_path / "project"
        project_root.mkdir()
        self._seed_git_root(project_root)

        # >64KB of pattern noise; if we actually read it, `*.py` inside would
        # wipe out sources. We expect the file to be skipped, so `keep.py`
        # must survive.
        big = "# padding\n" * 10_000 + "*.py\n"
        (project_root / ".hybrid-search-ignore").write_text(big)
        assert len(big.encode()) > 64 * 1024

        (project_root / "keep.py").write_text("x = 1\n")
        result = scan_project(project_root, "p1", db, IndexingConfig())
        names = {p.name for p in result.added}
        assert "keep.py" in names

    def test_subset_scan_respects_hybrid_search_ignore(self, tmp_path: Path) -> None:
        """`scan_project_subset` (used by git-diff fast path) honors the patterns too."""
        db = StoreDB(tmp_path / "store.db")
        project_root = tmp_path / "project"
        project_root.mkdir()
        self._seed_git_root(project_root)

        (project_root / ".hybrid-search-ignore").write_text("generated/\n")
        (project_root / "keep.py").write_text("x = 1\n")
        (project_root / "generated").mkdir()
        (project_root / "generated" / "schema.py").write_text("# auto\n")

        result = scan_project_subset(
            project_root,
            "p1",
            db,
            IndexingConfig(),
            changed_paths=["keep.py", "generated/schema.py"],
        )
        added_names = [p.name for p in result.added]
        assert "keep.py" in added_names
        assert "schema.py" not in added_names  # filtered by ignore spec


class TestIndexQALogsOptIn:
    """Sprint 3 — opt-in self-indexing of the Memory Layer."""

    def _seed(self, tmp_path: Path) -> tuple[Path, StoreDB]:
        db = StoreDB(tmp_path / "store.db")
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / ".git").mkdir()
        # Matches what installers write — would normally hide memory dirs.
        (project_root / ".gitignore").write_text(
            ".hybrid-search/qa/\n.hybrid-search/memory/\n"
        )

        qa_dir = project_root / ".hybrid-search" / "qa" / "2026" / "04"
        qa_dir.mkdir(parents=True)
        (qa_dir / "21-000000-deadbeef.md").write_text(
            "---\nquery: \"x\"\n---\n\n# Q: x\n"
        )
        card_dir = project_root / ".hybrid-search" / "memory" / "cards" / "2026" / "04"
        card_dir.mkdir(parents=True)
        (card_dir / "21-000001-feedbeef.md").write_text(
            "---\ntype: memory_card\n---\n\n## Summary\n\nx\n"
        )
        (project_root / "main.py").write_text("x = 1\n")
        return project_root, db

    def test_qa_logs_walked_by_default(self, tmp_path: Path) -> None:
        # Memory Layer default-on — qa logs are indexed out of the box.
        project_root, db = self._seed(tmp_path)
        result = scan_project(project_root, "p1", db, IndexingConfig())
        names = [p.name for p in result.added]
        assert "main.py" in names
        assert "21-000000-deadbeef.md" in names
        assert "21-000001-feedbeef.md" in names

    def test_qa_logs_skipped_when_opted_out(self, tmp_path: Path) -> None:
        project_root, db = self._seed(tmp_path)
        result = scan_project(
            project_root, "p1", db, IndexingConfig(index_qa_logs=False)
        )
        names = [p.name for p in result.added]
        assert "main.py" in names
        assert "21-000000-deadbeef.md" not in names
        assert "21-000001-feedbeef.md" not in names

    def test_subset_scan_respects_opt_out(self, tmp_path: Path) -> None:
        # Post-commit fast-path goes through scan_project_subset — same toggle.
        project_root, db = self._seed(tmp_path)
        rel = ".hybrid-search/qa/2026/04/21-000000-deadbeef.md"

        on = scan_project_subset(
            project_root, "p1", db, IndexingConfig(), changed_paths=[rel]
        )
        assert "21-000000-deadbeef.md" in [p.name for p in on.added]

        off = scan_project_subset(
            project_root,
            "p1",
            db,
            IndexingConfig(index_qa_logs=False),
            changed_paths=[rel],
        )
        assert [p.name for p in off.added] == []

    def test_qa_archive_is_not_indexed(self, tmp_path: Path) -> None:
        project_root, db = self._seed(tmp_path)
        archive = project_root / ".hybrid-search" / "qa-archive" / "2026" / "04"
        archive.mkdir(parents=True)
        (archive / "21-000002-archived.md").write_text("# archived\n")

        result = scan_project(project_root, "p1", db, IndexingConfig())

        assert "21-000002-archived.md" not in [p.name for p in result.added]
