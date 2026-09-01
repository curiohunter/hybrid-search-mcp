"""reflector — qa consolidation with provenance by construction (WS3)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from hybrid_search.memory import reflector


_BODY = "환불 정산은 tuition-service의 settle 단계에서 처리되고, 환불 사유는 별도 원장 행으로 남긴다는 결정이 있었다."


def _write_qa(root: Path, name: str, query: str, ts: str, body: str = _BODY,
              extra_fm: str = "") -> Path:
    p = root / ".hybrid-search/qa/2026/08" / f"{name}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\n"
        f'query: "{query}"\n'
        f"timestamp: {ts}\n"
        f"{extra_fm}"
        "---\n\n"
        "## Answer excerpt\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return p


# Identical queries always satisfy the strict matcher — cluster formation
# is supersession's calibrated concern, not re-tested here.
_Q = "환불 정산 로직이 어디서 처리되는지 알려줘"


class TestCollectClusters:
    def test_same_topic_entries_form_a_cluster(self, tmp_path):
        _write_qa(tmp_path, "01-000001-aaaa0001", _Q, "2026-08-01T00:00:00+00:00")
        _write_qa(tmp_path, "02-000002-bbbb0002", _Q, "2026-08-02T00:00:00+00:00")
        clusters = reflector.collect_clusters(tmp_path)
        assert len(clusters) == 1
        assert len(clusters[0].member_ids) == 2
        assert clusters[0].representative_query == _Q

    def test_singletons_are_not_clusters(self, tmp_path):
        _write_qa(tmp_path, "01-000001-aaaa0001", _Q, "2026-08-01T00:00:00+00:00")
        _write_qa(tmp_path, "02-000002-bbbb0002", "완전히 다른 주제 CI 파이프라인 캐시",
                  "2026-08-02T00:00:00+00:00")
        assert reflector.collect_clusters(tmp_path) == []

    def test_machine_payloads_are_excluded(self, tmp_path):
        _write_qa(tmp_path, "01-000001-aaaa0001", "<task-notification>x</task-notification>",
                  "2026-08-01T00:00:00+00:00")
        _write_qa(tmp_path, "02-000002-bbbb0002", "<task-notification>x</task-notification>",
                  "2026-08-02T00:00:00+00:00")
        assert reflector.collect_clusters(tmp_path) == []

    def test_meta_recall_and_answerless_entries_are_excluded(self, tmp_path):
        # Meta-recall queries with no answer content — the real-corpus
        # dry run surfaced exactly this cluster shape; consolidating it
        # would mint the fossilized junk F11 demotes at query time.
        _write_qa(tmp_path, "01-000001-aaaa0001",
                  "클로드 코드와 내가 가장 최근에 한 일이 뭐지",
                  "2026-08-01T00:00:00+00:00", body="")
        _write_qa(tmp_path, "02-000002-bbbb0002",
                  "클로드 코드와 내가 오늘 가장 최근에 나눈 대화와 작업은 무엇이었지",
                  "2026-08-02T00:00:00+00:00", body="")
        assert reflector.collect_clusters(tmp_path) == []

    def test_topic_ending_in_consolidated_note_is_skipped(self, tmp_path):
        _write_qa(tmp_path, "01-000001-aaaa0001", _Q, "2026-08-01T00:00:00+00:00")
        _write_qa(tmp_path, "02-000002-bbbb0002", _Q, "2026-08-02T00:00:00+00:00",
                  extra_fm="memory_type: consolidated\n")
        assert reflector.collect_clusters(tmp_path) == []


class TestPrepareFinalize:
    def _prepare_two(self, tmp_path):
        _write_qa(tmp_path, "01-000001-aaaa0001", _Q, "2026-08-01T00:00:00+00:00",
                  body="옛 답변 — 환불 정산은 클라이언트 컴포넌트에서 직접 계산해서 처리한다고 안내했던 기록.")
        _write_qa(tmp_path, "02-000002-bbbb0002", _Q, "2026-08-02T00:00:00+00:00",
                  body="새 답변 — 환불 정산은 tuition-service의 settle 단계로 이동했고 원장 행으로 남긴다.")
        return reflector.prepare(tmp_path)

    def test_prepare_writes_context_and_manifest(self, tmp_path):
        summary = self._prepare_two(tmp_path)
        assert summary["clusters"] == 1
        assert summary["est_input_tokens"] > 0
        in_dir = tmp_path / reflector.INPUT_DIRNAME
        manifest = json.loads((in_dir / reflector.MANIFEST_NAME).read_text())
        (cid, entry), = manifest.items()
        assert len(entry["member_ids"]) == 2
        ctx = (in_dir / f"{cid}.md").read_text()
        assert "옛 답변" in ctx and "새 답변" in ctx
        assert cid in ctx  # instructions carry the cluster id

    def test_finalize_installs_note_with_manifest_provenance(self, tmp_path):
        self._prepare_two(tmp_path)
        in_dir = tmp_path / reflector.INPUT_DIRNAME
        (cid,) = [k for k in json.loads((in_dir / reflector.MANIFEST_NAME).read_text())]
        out_dir = tmp_path / reflector.OUTPUT_DIRNAME
        (out_dir / f"{cid}.md").write_text(
            f"---\ncluster: {cid}\nsources:\n  - forged/entry.md\n---\n\n통합된 답.\n"
        )
        result = reflector.finalize(
            tmp_path, now=datetime(2026, 9, 1, tzinfo=timezone.utc)
        )
        assert result["installed"] == 1 and result["rejected"] == []
        note = (tmp_path / reflector.CONSOLIDATED_DIRNAME / f"2026-09-01-{cid}.md").read_text()
        # Provenance comes from the manifest — the forged sources are ignored.
        assert "forged/entry.md" not in note
        assert "aaaa0001" in note and "bbbb0002" in note
        assert "memory_type: consolidated" in note
        assert f"sources_hash: {cid}" in note
        assert "통합된 답." in note
        assert "## Answer excerpt" in note
        # Processed input/output files are consumed.
        assert not (out_dir / f"{cid}.md").exists()
        assert not (in_dir / f"{cid}.md").exists()

    def test_finalize_rejects_unknown_cluster_and_empty_body(self, tmp_path):
        self._prepare_two(tmp_path)
        out_dir = tmp_path / reflector.OUTPUT_DIRNAME
        (out_dir / "deadbeef.md").write_text("---\ncluster: deadbeef\n---\n\n내용\n")
        in_dir = tmp_path / reflector.INPUT_DIRNAME
        (cid,) = [k for k in json.loads((in_dir / reflector.MANIFEST_NAME).read_text())]
        (out_dir / f"{cid}.md").write_text(f"---\ncluster: {cid}\n---\n\n   \n")
        result = reflector.finalize(tmp_path)
        assert result["installed"] == 0
        assert len(result["rejected"]) == 2

    def test_finalize_without_prepare_errors_cleanly(self, tmp_path):
        result = reflector.finalize(tmp_path)
        assert result["installed"] == 0
        assert "manifest" in result["error"]

    def test_consolidated_topic_is_idempotent_on_next_prepare(self, tmp_path):
        self._prepare_two(tmp_path)
        in_dir = tmp_path / reflector.INPUT_DIRNAME
        (cid,) = [k for k in json.loads((in_dir / reflector.MANIFEST_NAME).read_text())]
        (tmp_path / reflector.OUTPUT_DIRNAME / f"{cid}.md").write_text(
            f"---\ncluster: {cid}\n---\n\n통합.\n"
        )
        reflector.finalize(tmp_path, now=datetime(2026, 9, 1, tzinfo=timezone.utc))
        # The note now lives inside qa/ and is the topic's newest entry —
        # a fresh prepare must find nothing to do.
        summary = reflector.prepare(tmp_path)
        assert summary["clusters"] == 0
