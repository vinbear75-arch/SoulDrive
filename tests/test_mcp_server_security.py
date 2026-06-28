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


if __name__ == "__main__":
    unittest.main()
