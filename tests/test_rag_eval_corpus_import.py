import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.keyword_index import KeywordIndex
from core.workspace import SoulDriveWorkspace
from scripts import check_rag_eval_workspace, import_rag_eval_corpus


class RagEvalCorpusImportTests(unittest.TestCase):
    def test_dry_run_reports_documents_without_importing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = SoulDriveWorkspace(str(root / "SoulDrive")).ensure()
            corpus_dir, manifest_path = _write_fixture_corpus(root)

            report = import_rag_eval_corpus.import_corpus(
                workspace_path=workspace.root_path,
                corpus_dir=corpus_dir,
                manifest_path=manifest_path,
                passphrase=None,
                dry_run=True,
                sync_index=False,
            )

            self.assertEqual(report["expected_count"], 2)
            self.assertEqual(report["imported_count"], 0)
            self.assertEqual(report["already_present_count"], 0)
            self.assertEqual(report["dry_run_count"], 2)
            self.assertEqual(list(Path(workspace.papers_path).glob("*.pdf")), [])

    def test_import_copies_manifest_pdfs_into_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = SoulDriveWorkspace(str(root / "SoulDrive")).ensure()
            corpus_dir, manifest_path = _write_fixture_corpus(root)

            report = import_rag_eval_corpus.import_corpus(
                workspace_path=workspace.root_path,
                corpus_dir=corpus_dir,
                manifest_path=manifest_path,
                passphrase=None,
                dry_run=False,
                sync_index=False,
            )

            self.assertEqual(report["expected_count"], 2)
            self.assertEqual(report["imported_count"], 2)
            self.assertEqual(report["rejected_count"], 0)
            self.assertEqual(sorted(path.name for path in Path(workspace.papers_path).glob("*.pdf")), ["alpha.pdf", "beta.pdf"])

    def test_index_can_skip_graph_extraction_for_retrieval_baseline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = SoulDriveWorkspace(str(root / "SoulDrive")).ensure()
            corpus_dir, manifest_path = _write_fixture_corpus(root)
            captured = {}

            class FakeIndexer:
                def sync_workspace(self, indexed_workspace, auth_level="PRO"):
                    captured["workspace"] = indexed_workspace.root_path
                    captured["auth_level"] = auth_level

            with patch.object(import_rag_eval_corpus, "DriveIndexer", return_value=FakeIndexer()):
                report = import_rag_eval_corpus.import_corpus(
                    workspace_path=workspace.root_path,
                    corpus_dir=corpus_dir,
                    manifest_path=manifest_path,
                    passphrase=None,
                    dry_run=False,
                    sync_index=True,
                    skip_graph=True,
                )

        self.assertEqual(report["index_status"], "completed")
        self.assertEqual(captured["workspace"], workspace.root_path)
        self.assertEqual(captured["auth_level"], "BASIC")

    def test_workspace_check_reports_manifest_documents_missing_from_index(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = SoulDriveWorkspace(str(root / "SoulDrive")).ensure()
            _, manifest_path = _write_fixture_corpus(root)

            with patch.object(check_rag_eval_workspace, "plain_paper_names", return_value=["alpha.pdf", "beta.pdf"]), patch.object(
                check_rag_eval_workspace,
                "indexed_source_names",
                return_value=["alpha.pdf"],
            ):
                report = check_rag_eval_workspace.build_report(
                    workspace_path=workspace.root_path,
                    manifest_path=manifest_path,
                    passphrase=None,
                )

        self.assertEqual(report["present_count"], 2)
        self.assertEqual(report["missing_count"], 0)
        self.assertEqual(report["indexed_count"], 1)
        self.assertEqual(report["missing_indexed_count"], 1)
        self.assertEqual(report["missing_indexed_documents"], ["beta.pdf"])

    def test_workspace_check_reads_current_keyword_index_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace(
                str(Path(temp_dir) / "SoulDrive")
            ).ensure()
            index = KeywordIndex(workspace.keyword_index_path)
            index.upsert_document(
                "alpha-child",
                "alpha content",
                {"source_filename": "alpha.pdf"},
            )
            index.close()

            names = check_rag_eval_workspace.plain_indexed_source_names(
                workspace
            )

        self.assertEqual(names, ["alpha.pdf"])


def _write_fixture_corpus(root: Path):
    corpus_dir = root / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "alpha.pdf").write_bytes(b"%PDF-1.4\nalpha")
    (corpus_dir / "beta.pdf").write_bytes(b"%PDF-1.4\nbeta")
    manifest_path = root / "corpus_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": "test",
                "documents": [
                    {"doc_id": "SDOC-T01", "local_filename": "alpha.pdf"},
                    {"doc_id": "SDOC-T02", "local_filename": "beta.pdf"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return corpus_dir, manifest_path


if __name__ == "__main__":
    unittest.main()
