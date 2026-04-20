"""Tests for file scanner — index/scanner.py (crash recovery, delta detection)."""

from unittest.mock import patch
from pathlib import Path

from hybrid_search.config import IndexingConfig
from hybrid_search.index.scanner import (
    _is_changed,
    _is_sensitive_file,
    compute_file_hash,
    get_changed_files_from_git,
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
