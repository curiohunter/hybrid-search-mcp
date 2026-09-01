"""rationale chunks (WS4a) — long design narrative becomes its own chunk."""

from __future__ import annotations

from pathlib import Path

from hybrid_search.index.ast_chunker import chunk_code_file

PROJECT_ROOT = Path("/repo")
PROJECT_ID = "p"

_LONG_WHY = (
    "bf16이 Python 3.14 ARM에서 simsimd NEON 커널과 만나 SIGBUS로 죽는다. "
    "그래서 f32로 고정한다. usearch 파일 헤더에 scalar kind가 영속되므로 "
    "기존 인덱스는 패키지 업그레이드만으로는 낫지 않고 파일 자체를 다시 "
    "써야 한다. 벡터는 손실 없이 BF16에서 F32로 확장해 재적재한다. "
    "이 결정을 되돌리려면 디스크 인덱스 전체 재빌드가 필요하고, 락으로 "
    "동시 쓰기를 막은 상태에서만 안전하다. 마이그레이션은 원자적 재작성으로 "
    "수행하며 실패 시 원본 파일이 그대로 남는다."
)


def _chunks(src: str, language: str = "python", filename: str = "mod.py"):
    return chunk_code_file(
        PROJECT_ROOT / filename, PROJECT_ROOT, PROJECT_ID, language, source=src
    )


def _rationales(chunks):
    return [c for c in chunks if c.node_type == "rationale"]


class TestPythonRationale:
    def test_long_docstring_emits_rationale_chunk(self):
        src = f'def migrate():\n    """{_LONG_WHY}"""\n    return 1\n'
        chunks = _chunks(src)
        rats = _rationales(chunks)
        assert len(rats) == 1
        r = rats[0]
        assert "SIGBUS" in r.content
        assert r.qualified_name.endswith("::rationale")
        assert r.name == "migrate"
        # The code chunk keeps its own copy — synthesis beside raw.
        code = [c for c in chunks if c.node_type == "function"]
        assert any("SIGBUS" in (c.docstring or "") for c in code)

    def test_rationale_id_differs_from_code_chunk(self):
        src = f'def migrate():\n    """{_LONG_WHY}"""\n    return 1\n'
        ids = [c.id for c in _chunks(src)]
        assert len(ids) == len(set(ids))

    def test_short_docstring_emits_nothing(self):
        src = 'def f():\n    """Return the tail of X."""\n    return 1\n'
        assert _rationales(_chunks(src)) == []

    def test_consecutive_hash_comments_group_into_narrative(self):
        comment_lines = "\n".join(
            f"    # {line}" for line in _LONG_WHY.split(". ")
        )
        src = f"def f():\n{comment_lines}\n    return 1\n"
        rats = _rationales(_chunks(src))
        assert len(rats) == 1
        assert "SIGBUS" in rats[0].content

    def test_scattered_one_liners_do_not_qualify(self):
        src = (
            "def f():\n"
            "    # set up\n"
            "    a = 1\n"
            "    # loop over items\n"
            "    b = 2\n"
            "    # return result\n"
            "    return a + b\n"
        )
        assert _rationales(_chunks(src)) == []

    def test_class_uses_docstring_only_no_method_double_capture(self):
        src = (
            "class C:\n"
            f'    """{_LONG_WHY}"""\n'
            "    def m(self):\n"
            f'        """{_LONG_WHY} 메서드 쪽 사본이다."""\n'
            "        return 1\n"
        )
        rats = _rationales(_chunks(src))
        # One for the class docstring, one for the method — no third from
        # the class re-collecting the method's narrative.
        assert len(rats) == 2
        assert {r.node_type for r in rats} == {"rationale"}

    def test_embedding_input_is_narrative_not_code(self):
        src = f'def migrate():\n    """{_LONG_WHY}"""\n    return compute(1, 2)\n'
        r = _rationales(_chunks(src))[0]
        assert "SIGBUS" in r.embedding_input
        assert "compute(1, 2)" not in r.embedding_input


class TestModuleRationale:
    def test_module_docstring_emits_module_rationale(self):
        src = f'"""{_LONG_WHY}"""\n\ndef f():\n    return 1\n'
        rats = _rationales(_chunks(src))
        assert len(rats) == 1
        assert rats[0].name == "mod.py (module)"
        assert rats[0].qualified_name.endswith("::module::rationale")
        assert "SIGBUS" in rats[0].content

    def test_top_level_comment_block_between_defs_is_captured(self):
        comment = "\n".join(f"# {s}" for s in _LONG_WHY.split(". "))
        src = f"def a():\n    return 1\n\n{comment}\ndef b():\n    return 2\n"
        rats = _rationales(_chunks(src))
        assert len(rats) == 1
        assert "SIGBUS" in rats[0].content

    def test_short_module_docstring_emits_nothing(self):
        src = '"""One-line module summary."""\n\ndef f():\n    return 1\n'
        assert _rationales(_chunks(src)) == []

    def test_function_body_comments_not_double_counted_at_module_level(self):
        comment = "\n".join(f"    # {s}" for s in _LONG_WHY.split(". "))
        src = f"def f():\n{comment}\n    return 1\n"
        rats = _rationales(_chunks(src))
        # Exactly one — from the function walk, not a second module copy.
        assert len(rats) == 1
        assert "(module)" not in rats[0].name


class TestTypescriptRationale:
    def test_consecutive_line_comments_emit_rationale(self):
        comment_lines = "\n".join(
            f"  // {line}" for line in _LONG_WHY.split(". ")
        )
        src = f"function f() {{\n{comment_lines}\n  return 1;\n}}\n"
        rats = _rationales(_chunks(src, language="typescript", filename="mod.ts"))
        assert len(rats) == 1
        assert "SIGBUS" in rats[0].content
