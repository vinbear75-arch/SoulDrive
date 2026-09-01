import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.indexer import DriveIndexer
from core.secure_document_store import SecureDocumentStore
from core.security_context import clear_workspace_keys, set_workspace_keys
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


class _FakeCollection:
    def get(self, where=None, include=None, ids=None):
        return {"ids": [], "metadatas": []}

    def delete(self, ids):
        return None


class _FakeKnowledgeBase:
    def __init__(self, *args, **kwargs):
        self.collection = _FakeCollection()
        self.ingested = []

    def ingest_chunks(self, document_hash, chunks):
        self.ingested.append((document_hash, chunks))

    def delete_document_indexes(self, document_hash):
        return None

    def close(self):
        return None


class _RecordingParser:
    def __init__(self):
        self.paths = []
        self.contents = []

    def parse_and_chunk(self, pdf_path: str):
        path = Path(pdf_path)
        self.paths.append(path)
        self.contents.append(path.read_bytes())
        return [SimpleNamespace(page_content="secure text", metadata={})]


class SecureIndexerDocumentsTests(unittest.TestCase):
    def test_indexer_reads_encrypted_documents_through_deleted_temp_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            source_pdf = Path(temp_dir) / "private.pdf"
            source_pdf.write_bytes(b"%PDF private payload")
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            set_workspace_keys(workspace.root_path, keys)
            store = SecureDocumentStore(workspace, keys)
            try:
                store.import_document(str(source_pdf))
            finally:
                store.close()

            parser = _RecordingParser()
            indexer = DriveIndexer()
            indexer.parser = parser
            with patch("core.knowledge_base.LocalKnowledgeBase", _FakeKnowledgeBase):
                try:
                    indexer.sync_workspace(workspace, auth_level="BASIC")
                finally:
                    indexer.close()
                    clear_workspace_keys(workspace.root_path)

            plaintext_documents = list(Path(workspace.documents_path).rglob("*.pdf"))
            runtime_pdfs = list(Path(workspace.runtime_path).rglob("*.pdf"))

        self.assertEqual(parser.contents, [b"%PDF private payload"])
        self.assertEqual(len(parser.paths), 1)
        self.assertFalse(parser.paths[0].exists())
        self.assertEqual(plaintext_documents, [])
        self.assertEqual(runtime_pdfs, [])

    def test_indexer_dispatches_encrypted_image_and_removes_plaintext_temp_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            source_image = Path(temp_dir) / "screen.png"
            source_image.write_bytes(b"private image payload")
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            set_workspace_keys(workspace.root_path, keys)
            store = SecureDocumentStore(workspace, keys)
            try:
                store.import_document(str(source_image))
            finally:
                store.close()

            parser = _RecordingParser()
            indexer = DriveIndexer()
            indexer.image_parser = parser
            with patch("core.knowledge_base.LocalKnowledgeBase", _FakeKnowledgeBase):
                try:
                    indexer.sync_workspace(workspace, auth_level="BASIC")
                finally:
                    indexer.close()
                    clear_workspace_keys(workspace.root_path)

            runtime_images = list(Path(workspace.runtime_path).rglob("*.png"))

        self.assertEqual(parser.contents, [b"private image payload"])
        self.assertEqual(parser.paths[0].suffix, ".png")
        self.assertFalse(parser.paths[0].exists())
        self.assertEqual(runtime_images, [])


if __name__ == "__main__":
    unittest.main()
