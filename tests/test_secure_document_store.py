from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core.secure_document_store import SecureDocumentStore
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


def _capture_exception(function, *args):
    try:
        function(*args)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


class SecureDocumentStoreTests(unittest.TestCase):
    def test_cross_instance_same_content_import_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            first_store = SecureDocumentStore(workspace, keys)
            second_store = SecureDocumentStore(workspace, keys)
            barrier = threading.Barrier(2)
            try:
                def import_once(store):
                    barrier.wait(timeout=5)
                    try:
                        return store.import_document_bytes(
                            "same-content.pdf",
                            b"same content imported concurrently",
                        )
                    except Exception as exc:
                        return {"error": f"{type(exc).__name__}: {exc}"}

                with ThreadPoolExecutor(max_workers=2) as pool:
                    outcomes = list(pool.map(import_once, (first_store, second_store)))
                self.assertEqual([item for item in outcomes if "error" in item], [])
                self.assertEqual(
                    sorted(item["status"] for item in outcomes),
                    ["already_present", "imported"],
                )
                self.assertEqual(len(first_store.list_documents()), 1)
            finally:
                first_store.close()
                second_store.close()

    def test_shared_connection_concurrent_imports_are_complete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureDocumentStore(workspace, keys)
            try:
                def import_batch(batch_index):
                    start = batch_index * 12
                    for index_number in range(start, start + 12):
                        store.import_document_bytes(
                            f"concurrent-{index_number}.pdf",
                            f"concurrent document payload {index_number}".encode("utf-8"),
                            modified_at=float(index_number),
                        )

                with ThreadPoolExecutor(max_workers=4) as pool:
                    errors = [
                        result
                        for result in pool.map(
                            lambda batch: _capture_exception(import_batch, batch),
                            range(4),
                        )
                        if result is not None
                    ]
                self.assertEqual(errors, [])
                self.assertEqual(len(store.list_documents()), 48)
            finally:
                store.close()

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

    def test_same_name_documents_fail_closed_for_name_based_read_and_delete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            first_dir = Path(temp_dir) / "first"
            second_dir = Path(temp_dir) / "second"
            first_dir.mkdir()
            second_dir.mkdir()
            first_source = first_dir / "report.pdf"
            second_source = second_dir / "report.pdf"
            first_source.write_bytes(b"first report")
            second_source.write_bytes(b"second report")
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureDocumentStore(workspace, keys)
            try:
                store.import_document(str(first_source))
                store.import_document(str(second_source))
                with self.assertRaisesRegex(ValueError, "同名文档不唯一"):
                    store.read_document_named("report.pdf")
                with self.assertRaisesRegex(ValueError, "同名文档不唯一"):
                    store.delete_document("report.pdf")
                remaining = store.list_documents()
            finally:
                store.close()

        self.assertEqual(len(remaining), 2)

    def test_delete_restores_object_when_manifest_commit_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            source_pdf = Path(temp_dir) / "recoverable.pdf"
            source_pdf.write_bytes(b"recoverable private payload")
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureDocumentStore(workspace, keys)
            try:
                store.import_document(str(source_pdf))
                document = store.list_documents()[0]
                object_id = store.iter_documents()[0]["object_id"]
                with patch.object(
                    store,
                    "_commit_document_manifest",
                    side_effect=OSError("manifest commit interrupted"),
                ):
                    with self.assertRaisesRegex(OSError, "manifest commit interrupted"):
                        store.delete_document_object(object_id)

                self.assertEqual(store.read_document_bytes(object_id), b"recoverable private payload")
                self.assertEqual(store.list_documents()[0]["name"], document["name"])
                self.assertEqual(
                    store.delete_document_object(object_id),
                    b"recoverable private payload",
                )
                self.assertEqual(store.list_documents(), [])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
