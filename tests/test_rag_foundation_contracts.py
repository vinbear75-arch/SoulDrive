import json
import multiprocessing
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core.document_usage import document_weight, forget_document, record_document_hits
from core.indexer import DriveIndexer
from core.retrieval import SearchCandidate, build_citations, source_key
from scripts.audit_eval_leakage import audit_records


def _document_usage_process_worker(workspace_path: str, barrier, worker_id: int):
    barrier.wait(timeout=10)
    for attempt in range(3):
        record_document_hits(workspace_path, [{"document_id": f"asset-{worker_id}"}])


class RagFoundationContractTests(unittest.TestCase):
    def test_document_id_is_stable_for_unregistered_file(self):
        first = DriveIndexer._stable_document_id("D:/workspace/report.pdf")
        second = DriveIndexer._stable_document_id("d:\\workspace\\REPORT.pdf")
        self.assertEqual(first, second)

    def test_citations_expose_identity_and_source_key_prefers_document_id(self):
        metadata = {"document_id": "asset-1", "source_filename": "old-name.pdf", "project_id": "p1"}
        candidate = SearchCandidate(id="chunk-1", content="evidence", metadata=metadata)
        citation = build_citations([candidate])[0]
        self.assertEqual(citation["document_id"], "asset-1")
        self.assertEqual(source_key(metadata), "asset-1")

    def test_usage_ledger_uses_stable_document_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            record_document_hits(temp_dir, [{"document_id": "asset-1", "source_filename": "old.pdf"}])
            self.assertGreater(document_weight(temp_dir, {"document_id": "asset-1", "indexed_at": 0}), 0)
            self.assertTrue((Path(temp_dir) / "runtime" / "document_usage.json").read_text(encoding="utf-8").find("asset-1") >= 0)

    def test_usage_ledger_keeps_previous_snapshot_when_flush_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            record_document_hits(temp_dir, [{"document_id": "asset-old"}])
            path = Path(temp_dir) / "runtime" / "document_usage.json"
            original = path.read_bytes()
            with patch("core.document_usage.os.fsync", side_effect=OSError("usage disk full")):
                with self.assertRaisesRegex(OSError, "usage disk full"):
                    record_document_hits(temp_dir, [{"document_id": "asset-new"}])

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_forget_document_keeps_previous_snapshot_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            record_document_hits(temp_dir, [{"document_id": "asset-to-forget"}])
            path = Path(temp_dir) / "runtime" / "document_usage.json"
            original = path.read_bytes()
            with patch("core.document_usage.os.replace", side_effect=OSError("usage replace denied")):
                with self.assertRaisesRegex(OSError, "usage replace denied"):
                    forget_document(temp_dir, "asset-to-forget")

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_usage_ledger_merges_concurrent_thread_updates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            record_document_hits(temp_dir, [{"document_id": "asset-1"}])
            start = threading.Barrier(4)
            errors = []

            def worker():
                try:
                    start.wait(timeout=5)
                    record_document_hits(temp_dir, [{"document_id": "asset-1"}])
                except BaseException as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            path = Path(temp_dir) / "runtime" / "document_usage.json"
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(payload["asset-1"]["citation_count"], 5)

    def test_usage_ledger_merges_concurrent_process_updates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            context = multiprocessing.get_context("spawn")
            worker_count = 3
            barrier = context.Barrier(worker_count)
            processes = [
                context.Process(
                    target=_document_usage_process_worker,
                    args=(temp_dir, barrier, worker_id),
                )
                for worker_id in range(worker_count)
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=30)

            path = Path(temp_dir) / "runtime" / "document_usage.json"
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertFalse(any(process.is_alive() for process in processes))
        self.assertEqual([process.exitcode for process in processes], [0] * worker_count)
        self.assertEqual(
            sum(item["citation_count"] for item in payload.values()),
            worker_count * 3,
        )

    def test_eval_audit_rejects_cross_split_identity_and_missing_split(self):
        report = audit_records([
            {"id": "a", "split": "dev", "doc_id": "doc-1", "question": "q"},
            {"id": "b", "split": "sealed", "doc_id": "doc-1", "question": "r"},
        ])
        self.assertFalse(report["pass"])
        self.assertEqual(report["cross_split_identity_leaks"], ["doc-1"])


if __name__ == "__main__":
    unittest.main()
