import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import mcp_server
import core.runtime_state as runtime_state
from core.parent_doc_store import ParentDocumentStore
from core.secure_document_store import SecureDocumentStore
from core.secure_vector_store import SecureVectorStore
from core.security_context import clear_workspace_keys, set_workspace_keys
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


class McpServerPapersTests(unittest.TestCase):
    def test_document_delete_requires_one_passphrase_authorization_and_removes_secure_document(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            source_pdf = Path(temp_dir) / "obsolete.pdf"
            source_pdf.write_bytes(b"obsolete private payload")
            state_path = str(Path(temp_dir) / "runtime_state.json")

            class FakeCollection:
                def get(self, **_kwargs):
                    return {"ids": ["obsolete-child"], "metadatas": [{"parent_id": "obsolete-parent"}]}

            class FakeKnowledgeBase:
                collection = FakeCollection()

                def __init__(self):
                    self.deleted_hashes = []
                    self.deleted_records = []

                def delete_document_indexes(self, document_hash):
                    self.deleted_hashes.append(document_hash)

                def delete_index_records(self, *, child_ids, parent_ids):
                    self.deleted_records.append((child_ids, parent_ids))

            fake_kb = FakeKnowledgeBase()
            original_kb = mcp_server.kb
            original_workspace_path = mcp_server.loaded_workspace_path
            try:
                with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                    mcp_server, "ensure_runtime_loaded",
                ), patch(
                    "core.mcp_server.current_workspace", return_value=workspace,
                ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
                    runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                    client = TestClient(mcp_server.app)
                    init_response = client.post(
                        "/security/init",
                        json={"passphrase": "secret-passphrase", "acknowledge_no_recovery": True},
                        headers={"X-SoulDrive-Token": "test-token"},
                    )
                    document_store = SecureDocumentStore(workspace, unlock_keystore(workspace, "secret-passphrase"))
                    try:
                        document_store.import_document(str(source_pdf))
                    finally:
                        document_store.close()
                    mcp_server.kb = fake_kb
                    mcp_server.loaded_workspace_path = workspace.root_path

                    authorized = client.post(
                        "/documents/delete-authorize",
                        json={"passphrase": "secret-passphrase"},
                        headers={"X-SoulDrive-Token": "test-token"},
                    )
                    token = authorized.json()["authorization"]
                    deleted = client.post(
                        "/documents/delete",
                        json={"name": "obsolete.pdf", "authorization": token},
                        headers={"X-SoulDrive-Token": "test-token"},
                    )
                    listed = client.get("/documents/list", headers={"X-SoulDrive-Token": "test-token"})
            finally:
                mcp_server.kb = original_kb
                mcp_server.loaded_workspace_path = original_workspace_path

        self.assertEqual(init_response.status_code, 200)
        self.assertEqual(authorized.status_code, 200)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(listed.json()["documents"], [])
        self.assertEqual(len(fake_kb.deleted_hashes), 1)
        self.assertEqual(fake_kb.deleted_records, [(["obsolete-child"], ["obsolete-parent"])])

    def test_chat_request_defaults_to_six_evidence_items(self):
        request = mcp_server.ChatRequest(query="hello")

        self.assertEqual(request.top_k, 6)

    def test_documents_list_reads_only_workspace_documents_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            managed = Path(workspace.papers_path) / "managed.pdf"
            loose = Path(temp_dir) / "loose.pdf"
            managed.write_bytes(b"%PDF managed")
            loose.write_bytes(b"%PDF loose")

            store = ParentDocumentStore(workspace.parent_doc_path)
            try:
                store.upsert_parent(
                    "managed_parent_0",
                    "content",
                    {"source_filename": "managed.pdf"},
                )
            finally:
                store.close()
            state_path = str(Path(temp_dir) / "runtime_state.json")

            with patch.object(runtime_state, "STATE_PATH", state_path), patch("core.mcp_server.current_workspace", return_value=workspace), patch.dict(
                "os.environ",
                {"SOULDRIVE_API_TOKEN": "test-token"},
                clear=False,
            ):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                runtime_state.mark_software_unlocked()
                response = TestClient(mcp_server.app).get(
                    "/documents/list",
                    headers={"X-SoulDrive-Token": "test-token"},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["document_count"], 1)
        self.assertEqual(payload["documents"][0]["name"], "managed.pdf")
        self.assertTrue(payload["documents"][0]["indexed"])

    def test_documents_import_copies_pdf_into_active_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            source_dir = Path(temp_dir) / "incoming"
            source_dir.mkdir()
            source_pdf = source_dir / "paper.pdf"
            source_pdf.write_bytes(b"%PDF imported")
            state_path = str(Path(temp_dir) / "runtime_state.json")

            with patch.object(runtime_state, "STATE_PATH", state_path), patch(
                "core.mcp_server.current_workspace",
                return_value=workspace,
            ), patch.dict(
                "os.environ",
                {"SOULDRIVE_API_TOKEN": "test-token"},
                clear=False,
            ):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                client = TestClient(mcp_server.app)
                init_response = client.post(
                    "/security/init",
                    json={"passphrase": "secret-passphrase", "acknowledge_no_recovery": True},
                    headers={"X-SoulDrive-Token": "test-token"},
                )
                response = client.post(
                    "/documents/import",
                    json={"source_paths": [str(source_pdf)]},
                    headers={"X-SoulDrive-Token": "test-token"},
                )
                encrypted_document_exists = bool(list(Path(workspace.documents_path).rglob("*.sdoc")))
                imported_pdf_exists = bool(list(Path(workspace.documents_path).rglob("*.pdf")))

        self.assertEqual(init_response.status_code, 200)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["imported_count"], 1)
        self.assertEqual(payload["items"][0]["status"], "imported")
        self.assertEqual(payload["items"][0]["name"], "paper.pdf")
        self.assertTrue(encrypted_document_exists)
        self.assertFalse(imported_pdf_exists)

    def test_documents_import_accepts_encrypted_image_and_exposes_media_type(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            source_image = Path(temp_dir) / "screen.png"
            source_image.write_bytes(b"private image")
            state_path = str(Path(temp_dir) / "runtime_state.json")

            with patch.object(runtime_state, "STATE_PATH", state_path), patch(
                "core.mcp_server.current_workspace",
                return_value=workspace,
            ), patch.dict(
                "os.environ",
                {"SOULDRIVE_API_TOKEN": "test-token"},
                clear=False,
            ):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                client = TestClient(mcp_server.app)
                client.post(
                    "/security/init",
                    json={"passphrase": "secret-passphrase", "acknowledge_no_recovery": True},
                    headers={"X-SoulDrive-Token": "test-token"},
                )
                import_response = client.post(
                    "/documents/import",
                    json={"source_paths": [str(source_image)]},
                    headers={"X-SoulDrive-Token": "test-token"},
                )
                list_response = client.get(
                    "/documents/list",
                    headers={"X-SoulDrive-Token": "test-token"},
                )

        self.assertEqual(import_response.status_code, 200)
        self.assertEqual(import_response.json()["items"][0]["media_type"], "image/png")
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.json()["documents"][0]["media_type"], "image/png")

    def test_documents_list_counts_encrypted_indexed_documents(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            source_pdf = Path(temp_dir) / "secure.pdf"
            source_pdf.write_bytes(b"%PDF secure")
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            set_workspace_keys(workspace.root_path, keys)
            try:
                document_store = SecureDocumentStore(workspace, keys)
                try:
                    document_store.import_document(str(source_pdf))
                finally:
                    document_store.close()

                vector_store = SecureVectorStore(workspace.secure_vector_store_path, keys)
                try:
                    vector_store.add_chunks(
                        ids=["secure-child-1"],
                        documents=["indexed content"],
                        metadatas=[{"source_filename": "secure.pdf", "document_hash": "hash-1"}],
                        embeddings=[[1.0]],
                    )
                finally:
                    vector_store.close()

                state_path = str(Path(temp_dir) / "runtime_state.json")
                with patch.object(runtime_state, "STATE_PATH", state_path), patch(
                    "core.mcp_server.current_workspace",
                    return_value=workspace,
                ), patch.dict(
                    "os.environ",
                    {"SOULDRIVE_API_TOKEN": "test-token"},
                    clear=False,
                ):
                    runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                    runtime_state.mark_software_unlocked()
                    response = TestClient(mcp_server.app).get(
                        "/documents/list",
                        headers={"X-SoulDrive-Token": "test-token"},
                    )
            finally:
                clear_workspace_keys(workspace.root_path)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["document_count"], 1)
        self.assertEqual(payload["indexed_count"], 1)
        self.assertTrue(payload["documents"][0]["indexed"])

    def test_documents_list_rejects_before_software_unlock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            source_pdf = Path(temp_dir) / "private.pdf"
            source_pdf.write_bytes(b"%PDF private")
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            set_workspace_keys(workspace.root_path, keys)
            try:
                store = SecureDocumentStore(workspace, keys)
                try:
                    store.import_document(str(source_pdf))
                finally:
                    store.close()
            finally:
                clear_workspace_keys(workspace.root_path)

            state_path = str(Path(temp_dir) / "runtime_state.json")
            with patch.object(runtime_state, "STATE_PATH", state_path), patch(
                "core.mcp_server.current_workspace",
                return_value=workspace,
            ), patch.dict(
                "os.environ",
                {"SOULDRIVE_API_TOKEN": "test-token"},
                clear=False,
            ):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                response = TestClient(mcp_server.app).get(
                    "/documents/list",
                    headers={"X-SoulDrive-Token": "test-token"},
                )

        self.assertEqual(response.status_code, 423)
        payload = response.json()
        self.assertEqual(payload["status"], "locked")
        self.assertNotIn("document_count", payload)
        self.assertNotIn("documents", payload)

    def test_papers_list_keeps_legacy_response_shape(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            legacy = Path(workspace.papers_path) / "legacy.pdf"
            legacy.write_bytes(b"%PDF legacy")
            state_path = str(Path(temp_dir) / "runtime_state.json")

            with patch.object(runtime_state, "STATE_PATH", state_path), patch("core.mcp_server.current_workspace", return_value=workspace), patch.dict(
                "os.environ",
                {"SOULDRIVE_API_TOKEN": "test-token"},
                clear=False,
            ):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                runtime_state.mark_software_unlocked()
                response = TestClient(mcp_server.app).get(
                    "/papers/list",
                    headers={"X-SoulDrive-Token": "test-token"},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("paper_count", payload)
        self.assertIn("papers", payload)

    def test_papers_import_rejects_non_pdf_without_copying(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            source_txt = Path(temp_dir) / "notes.txt"
            source_txt.write_text("not a pdf", encoding="utf-8")
            state_path = str(Path(temp_dir) / "runtime_state.json")

            with patch.object(runtime_state, "STATE_PATH", state_path), patch(
                "core.mcp_server.current_workspace",
                return_value=workspace,
            ), patch.dict(
                "os.environ",
                {"SOULDRIVE_API_TOKEN": "test-token"},
                clear=False,
            ):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                runtime_state.mark_software_unlocked()
                response = TestClient(mcp_server.app).post(
                    "/papers/import",
                    json={"source_paths": [str(source_txt)]},
                    headers={"X-SoulDrive-Token": "test-token"},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["imported_count"], 0)
        self.assertEqual(payload["items"][0]["status"], "rejected")
        self.assertEqual(payload["items"][0]["error_code"], "UNSUPPORTED_FILE_TYPE")
        self.assertFalse((Path(workspace.papers_path) / "notes.txt").exists())

    def test_documents_list_rejects_when_no_removable_workspace_is_active(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.dict(
                "os.environ",
                {"SOULDRIVE_API_TOKEN": "test-token"},
                clear=False,
            ):
                response = TestClient(mcp_server.app).get(
                    "/documents/list",
                    headers={"X-SoulDrive-Token": "test-token"},
                )

        self.assertEqual(response.status_code, 423)


if __name__ == "__main__":
    unittest.main()
