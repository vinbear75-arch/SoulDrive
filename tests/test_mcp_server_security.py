import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from core import mcp_server
import core.runtime_state as runtime_state
from core.security_context import get_workspace_keys
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore


class McpServerSecurityTests(unittest.TestCase):
    def test_tools_call_returns_bad_request_for_invalid_arguments(self):
        with patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
            response = TestClient(mcp_server.app).post(
                "/tools/call",
                headers={"X-SoulDrive-Token": "test-token"},
                json={"name": "souldrive.audit_recent", "arguments": {"limit": 1000}},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("limit", response.json()["error"])

    def test_storage_health_does_not_expose_raw_backend_exception(self):
        secret_error = "redis://user:password@private-host:6379/0"
        with patch.object(mcp_server, "current_workspace", side_effect=RuntimeError(secret_error)):
            result = mcp_server._storage_health()

        self.assertEqual(result["status"], "error")
        self.assertNotIn(secret_error, str(result))
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertRegex(result["error_digest"], r"^[0-9a-f]{16}$")

    def test_storage_health_sanitizes_nested_backend_errors_and_paths(self):
        secret_error = "postgresql://user:password@private-host:5432/db"
        fake_store = Mock()
        fake_store.health.return_value = {
            "schema_version": "storage_health_v1",
            "degraded": True,
            "file_archive": {
                "status": "degraded",
                "root": r"D:\private\workspace",
                "sqlite_index_error": r"D:\private\workspace\sessions.sqlite: malformed",
                "archive_read_error": "record contains secret=archive-token",
            },
            "redis": {"enabled": True, "status": "error", "error": secret_error},
            "postgres": {"enabled": True, "status": "ok", "error": None},
            "memory_vector_rehydrate": {
                "status": "error",
                "error": "embedding backend secret=vector-token",
            },
            "outbox": {"pending": 1, "path": r"D:\private\workspace\outbox.sqlite"},
        }
        with patch.object(mcp_server, "current_workspace", return_value=object()), patch.object(
            mcp_server, "_conversation_store", return_value=fake_store
        ):
            result = mcp_server._storage_health()
            response = TestClient(mcp_server.app).get("/health")

        self.assertNotIn(secret_error, str(result))
        self.assertNotIn("archive-token", str(result))
        self.assertNotIn("vector-token", str(result))
        self.assertNotIn(r"D:\private\workspace", str(result))
        self.assertNotIn("root", result["file_archive"])
        self.assertNotIn("path", result["outbox"])
        self.assertEqual(
            result["redis"]["error_observations"]["error"]["error_type"],
            "UnknownError",
        )
        self.assertRegex(
            result["memory_vector_rehydrate"]["error_observations"]["error"]["error_digest"],
            r"^[0-9a-f]{16}$",
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(secret_error, response.text)
        self.assertNotIn("archive-token", response.text)
        self.assertNotIn(r"D:\private\workspace", response.text)

    def test_security_init_requires_no_recovery_acknowledgement(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server,
                "current_workspace",
                return_value=workspace,
            ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                response = TestClient(mcp_server.app).post(
                    "/security/init",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={"passphrase": "secret-passphrase", "acknowledge_no_recovery": False},
                )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "no recovery acknowledgement required")

    def test_security_init_and_unlock_enable_sensitive_workflows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server,
                "current_workspace",
                return_value=workspace,
            ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False), patch.object(
                mcp_server,
                "_start_indexer_worker",
                return_value=Mock(),
            ) as start_indexer:
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                client = TestClient(mcp_server.app)
                init_response = client.post(
                    "/security/init",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={"passphrase": "secret-passphrase", "acknowledge_no_recovery": True},
                )
                lock_response = client.post(
                    "/security/lock",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={},
                )
                unlock_response = client.post(
                    "/security/unlock",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={"passphrase": "secret-passphrase"},
                )
                state = runtime_state.get_runtime_state()

        self.assertEqual(init_response.status_code, 200)
        self.assertEqual(lock_response.status_code, 200)
        self.assertEqual(unlock_response.status_code, 200)
        self.assertFalse(state["locked"])
        self.assertTrue(state["software_unlocked"])
        start_indexer.assert_not_called()

    def test_security_init_keeps_completed_state_when_audit_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()

            class FailingAuditLogger:
                def append_event(self, *_args, **_kwargs):
                    raise OSError("audit sink failed for secret-passphrase")

            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server,
                "current_workspace",
                return_value=workspace,
            ), patch.object(
                mcp_server,
                "current_audit_logger",
                return_value=FailingAuditLogger(),
            ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                response = TestClient(mcp_server.app).post(
                    "/security/init",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={"passphrase": "secret-passphrase", "acknowledge_no_recovery": True},
                )
                state = runtime_state.get_runtime_state()
                keystore_exists = Path(workspace.keystore_path).exists()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["software_unlocked"])
        self.assertTrue(state["software_unlocked"])
        self.assertTrue(keystore_exists)
        self.assertIn("审计记录写入失败", response.json()["warning"])
        self.assertNotIn("secret-passphrase", response.json()["warning"])

    def test_security_unlock_rejects_wrong_passphrase(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server,
                "current_workspace",
                return_value=workspace,
            ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                client = TestClient(mcp_server.app)
                client.post(
                    "/security/init",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={"passphrase": "secret-passphrase", "acknowledge_no_recovery": True},
                )
                response = client.post(
                    "/security/unlock",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={"passphrase": "wrong-passphrase"},
                )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "incorrect passphrase")

    def test_security_unlock_keeps_403_when_failure_audit_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()

            class FailingAuditLogger:
                def append_event(self, *_args, **_kwargs):
                    raise OSError("audit sink failed for secret-passphrase")

            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server,
                "current_workspace",
                return_value=workspace,
            ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                client = TestClient(mcp_server.app)
                initialized = client.post(
                    "/security/init",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={"passphrase": "secret-passphrase", "acknowledge_no_recovery": True},
                )
                self.assertEqual(initialized.status_code, 200)
                client.post("/security/lock", headers={"X-SoulDrive-Token": "test-token"}, json={})
                with patch.object(mcp_server, "current_audit_logger", return_value=FailingAuditLogger()):
                    response = client.post(
                        "/security/unlock",
                        headers={"X-SoulDrive-Token": "test-token"},
                        json={"passphrase": "wrong-passphrase"},
                    )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "incorrect passphrase")
        self.assertIn("审计记录写入失败", response.json()["warning"])
        self.assertNotIn("secret-passphrase", response.json()["warning"])

    def test_document_delete_authorization_keeps_403_when_failure_audit_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()

            class FailingAuditLogger:
                def append_event(self, *_args, **_kwargs):
                    raise OSError("audit sink failed for secret-passphrase")

            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server,
                "current_workspace",
                return_value=workspace,
            ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                client = TestClient(mcp_server.app)
                initialized = client.post(
                    "/security/init",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={"passphrase": "secret-passphrase", "acknowledge_no_recovery": True},
                )
                self.assertEqual(initialized.status_code, 200)
                with patch.object(mcp_server, "current_audit_logger", return_value=FailingAuditLogger()):
                    response = client.post(
                        "/documents/delete-authorize",
                        headers={"X-SoulDrive-Token": "test-token"},
                        json={"passphrase": "wrong-passphrase"},
                    )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "incorrect passphrase")
        self.assertIn("审计记录写入失败", response.json()["warning"])
        self.assertNotIn("secret-passphrase", response.json()["warning"])

    def test_security_unlock_does_not_start_secure_index_after_unlock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server,
                "current_workspace",
                return_value=workspace,
            ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False), patch.object(
                mcp_server,
                "_start_indexer_worker",
                return_value=Mock(),
            ) as start_indexer:
                runtime_state.unlock_runtime("HARDWARE_PLUS_PASSWORD", "SN-1", temp_dir, workspace.root_path)
                client = TestClient(mcp_server.app)
                client.post(
                    "/security/init",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={"passphrase": "secret-passphrase", "acknowledge_no_recovery": True},
                )
                client.post(
                    "/security/lock",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={},
                )
                start_indexer.reset_mock()
                response = client.post(
                    "/security/unlock",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={"passphrase": "secret-passphrase"},
                )

        self.assertEqual(response.status_code, 200)
        start_indexer.assert_not_called()

    def test_security_lock_clears_workspace_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server,
                "current_workspace",
                return_value=workspace,
            ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                client = TestClient(mcp_server.app)
                init_response = client.post(
                    "/security/init",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={"passphrase": "secret-passphrase", "acknowledge_no_recovery": True},
                )
                self.assertEqual(init_response.status_code, 200)
                self.assertIsNotNone(get_workspace_keys(workspace.root_path))

                lock_response = client.post(
                    "/security/lock",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={},
                )

        self.assertEqual(lock_response.status_code, 200)
        self.assertIsNone(get_workspace_keys(workspace.root_path))

    def test_security_lock_removes_secure_index_temp_dirs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            leftover = Path(workspace.runtime_path) / "secure-index-leftover"
            leftover.mkdir(parents=True)
            (leftover / "leak.pdf").write_bytes(b"%PDF leftover")
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server,
                "current_workspace",
                return_value=workspace,
            ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                client = TestClient(mcp_server.app)
                response = client.post(
                    "/security/lock",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={},
                )
                self.assertEqual(response.status_code, 200)
                self.assertFalse(leftover.exists())

    def test_runtime_lock_clears_workspace_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server,
                "current_workspace",
                return_value=workspace,
            ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                client = TestClient(mcp_server.app)
                init_response = client.post(
                    "/security/init",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={"passphrase": "secret-passphrase", "acknowledge_no_recovery": True},
                )
                self.assertEqual(init_response.status_code, 200)
                self.assertIsNotNone(get_workspace_keys(workspace.root_path))

                lock_response = client.post(
                    "/runtime/lock",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={"reason": "storage device removed"},
                )

        self.assertEqual(lock_response.status_code, 200)
        self.assertIsNone(get_workspace_keys(workspace.root_path))

    def test_security_status_requires_passphrase_when_memory_keys_are_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server,
                "current_workspace",
                return_value=workspace,
            ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                runtime_state.mark_software_unlocked()
                response = TestClient(mcp_server.app).get(
                    "/security/status",
                    headers={"X-SoulDrive-Token": "test-token"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["software_unlocked"])
        self.assertEqual(response.json()["reason"], "workspace passphrase required")

    def test_index_run_rejects_stale_unlocked_state_without_memory_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server,
                "current_workspace",
                return_value=workspace,
            ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                runtime_state.mark_software_unlocked()
                response = TestClient(mcp_server.app).post(
                    "/index/run",
                    headers={"X-SoulDrive-Token": "test-token"},
                )

        self.assertEqual(response.status_code, 423)
        self.assertEqual(response.json()["reason"], "workspace passphrase required")

    def test_graphrag_diagnostics_rejects_stale_unlocked_state_without_memory_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server,
                "current_workspace",
                return_value=workspace,
            ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                runtime_state.mark_software_unlocked()
                response = TestClient(mcp_server.app).get(
                    "/diagnostics/graphrag",
                    headers={"X-SoulDrive-Token": "test-token"},
                )

        self.assertEqual(response.status_code, 423)
        self.assertEqual(response.json()["status"], "locked")
        self.assertEqual(response.json()["reason"], "workspace passphrase required")


if __name__ == "__main__":
    unittest.main()
