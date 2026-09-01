import tempfile
import unittest
from pathlib import Path

from core.document_usage import document_weight, record_document_hits
from core.indexer import DriveIndexer
from core.retrieval import SearchCandidate, build_citations, source_key
from scripts.audit_eval_leakage import audit_records


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

    def test_eval_audit_rejects_cross_split_identity_and_missing_split(self):
        report = audit_records([
            {"id": "a", "split": "dev", "doc_id": "doc-1", "question": "q"},
            {"id": "b", "split": "sealed", "doc_id": "doc-1", "question": "r"},
        ])
        self.assertFalse(report["pass"])
        self.assertEqual(report["cross_split_identity_leaks"], ["doc-1"])


if __name__ == "__main__":
    unittest.main()
