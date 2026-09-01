import tempfile
import unittest
from pathlib import Path

from core.secure_document_store import SecureDocumentStore
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


class SecureDocumentStoreTests(unittest.TestCase):
    def test_import_document_stores_encrypted_payload_without_plaintext_pdf_copy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            source_pdf = Path(temp_dir) / "paper.pdf"
            source_pdf.write_bytes(b"%PDF encrypted payload")
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureDocumentStore(workspace, keys)
            try:
                item = store.import_document(str(source_pdf))
                listed = store.list_documents()
                encrypted_files = list(Path(workspace.documents_path).rglob("*.sdoc"))
                plaintext_pdf = list(Path(workspace.documents_path).rglob("*.pdf"))
            finally:
                store.close()

        self.assertEqual(item["status"], "imported")
        self.assertEqual(item["name"], "paper.pdf")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["name"], "paper.pdf")
        self.assertEqual(len(encrypted_files), 1)
        self.assertEqual(len(plaintext_pdf), 0)

    def test_import_image_stores_encrypted_payload_and_media_type(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            source_image = Path(temp_dir) / "screen.png"
            source_image.write_bytes(b"private image bytes")
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureDocumentStore(workspace, keys)
            try:
                item = store.import_document(str(source_image))
                listed = store.list_documents()
                encrypted_files = list(Path(workspace.documents_path).rglob("*.sdoc"))
                plaintext_images = list(Path(workspace.documents_path).rglob("*.png"))
            finally:
                store.close()

        self.assertEqual(item["status"], "imported")
        self.assertEqual(item["media_type"], "image/png")
        self.assertEqual(listed[0]["media_type"], "image/png")
        self.assertEqual(len(encrypted_files), 1)
        self.assertEqual(plaintext_images, [])

    def test_delete_document_removes_manifest_and_encrypted_object(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            source_pdf = Path(temp_dir) / "obsolete.pdf"
            source_pdf.write_bytes(b"obsolete private payload")
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureDocumentStore(workspace, keys)
            try:
                store.import_document(str(source_pdf))
                deleted = store.delete_document("obsolete.pdf")
                remaining = store.list_documents()
                encrypted_files = list(Path(workspace.documents_path).rglob("*.sdoc"))
            finally:
                store.close()

        self.assertEqual(deleted, b"obsolete private payload")
        self.assertEqual(remaining, [])
        self.assertEqual(encrypted_files, [])


if __name__ == "__main__":
    unittest.main()
