import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from core import mcp_server
import core.runtime_state as runtime_state
from core.security_context import get_workspace_keys
from core.workspace import SoulDriveWorkspace


class McpServerShutdownTests(unittest.TestCase):
    def tearDown(self):
        if hasattr(mcp_server.app.state, "shutdown_handler"):
            delattr(mcp_server.app.state, "shutdown_handler")

    def test_shutdown_uses_registered_sidecar_handler(self):
        handler = Mock()
        mcp_server.app.state.shutdown_handler = handler

        with patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
            response = TestClient(mcp_server.app).post(
                "/shutdown",
                headers={"X-SoulDrive-Token": "test-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "shutdown_requested")
        handler.assert_called_once_with("api shutdown requested")

    def test_shutdown_calls_handler_when_audit_fails(self):
        handler = Mock()

        class FailingAuditLogger:
            def append_event(self, *_args, **_kwargs):
                raise OSError("audit sink failed for shutdown-secret")

        mcp_server.app.state.shutdown_handler = handler
        with patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False), patch.object(
            mcp_server, "current_audit_logger", return_value=FailingAuditLogger()
        ):
            response = TestClient(mcp_server.app).post(
                "/shutdown",
                headers={"X-SoulDrive-Token": "test-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "shutdown_requested")
        self.assertIn("审计记录写入失败", response.json()["warning"])
        self.assertNotIn("shutdown-secret", response.json()["warning"])
        handler.assert_called_once_with("api shutdown requested")

    def test_shutdown_clears_workspace_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            handler = Mock()
            mcp_server.app.state.shutdown_handler = handler

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

                response = client.post(
                    "/shutdown",
                    headers={"X-SoulDrive-Token": "test-token"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(get_workspace_keys(workspace.root_path))
        handler.assert_called_once_with("api shutdown requested")


if __name__ == "__main__":
    unittest.main()
