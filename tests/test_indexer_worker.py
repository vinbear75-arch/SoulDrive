import tempfile
import unittest
from base64 import b64encode
from pathlib import Path
from unittest.mock import patch

from core import indexer_worker
import core.runtime_state as runtime_state
from core.security_context import clear_workspace_keys, get_workspace_keys
from core.workspace import SoulDriveWorkspace


class IndexerWorkerTests(unittest.TestCase):
    def test_workspace_mode_propagates_durable_run_identity(self):
        original_state_path = runtime_state.STATE_PATH
        captured = {}

        class FakeIndexer:
            def sync_workspace(self, workspace, auth_level, **kwargs):
                captured["workspace_path"] = workspace.root_path
                captured["auth_level"] = auth_level
                captured.update(kwargs)

            def close(self):
                pass

        try:
            runtime_state.STATE_PATH = None
            with tempfile.TemporaryDirectory() as temp_dir:
                workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()

                with patch("core.indexer_worker.DriveIndexer", return_value=FakeIndexer()):
                    exit_code = indexer_worker.main([
                        "--workspace-path",
                        workspace.root_path,
                        "--auth-level",
                        "PRO",
                        "--run-id",
                        "api-run-1",
                        "--idempotency-key",
                        "index-key-1",
                    ])
        finally:
            runtime_state.STATE_PATH = original_state_path

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["run_id"], "api-run-1")
        self.assertEqual(captured["idempotency_key"], "index-key-1")

    def test_workspace_mode_writes_runtime_state_inside_workspace(self):
        original_state_path = runtime_state.STATE_PATH
        captured = {}

        class FakeIndexer:
            def sync_workspace(self, workspace, auth_level):
                captured["state_path"] = runtime_state.runtime_state_path()
                captured["workspace_path"] = workspace.root_path
                captured["auth_level"] = auth_level

            def close(self):
                pass

        try:
            runtime_state.STATE_PATH = None
            with tempfile.TemporaryDirectory() as temp_dir:
                workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()

                with patch("core.indexer_worker.DriveIndexer", return_value=FakeIndexer()):
                    exit_code = indexer_worker.main([
                        "--workspace-path",
                        workspace.root_path,
                        "--auth-level",
                        "LITE",
                    ])

                expected_state_path = str(Path(workspace.runtime_path) / "runtime_state.json")
        finally:
            runtime_state.STATE_PATH = original_state_path

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["workspace_path"], workspace.root_path)
        self.assertEqual(captured["auth_level"], "LITE")
        self.assertEqual(captured["state_path"], expected_state_path)

    def test_workspace_mode_restores_workspace_keys_from_environment(self):
        original_state_path = runtime_state.STATE_PATH
        captured = {}

        class FakeIndexer:
            def sync_workspace(self, workspace, auth_level):
                captured["workspace_path"] = workspace.root_path
                captured["auth_level"] = auth_level
                captured["keys_ready"] = get_workspace_keys(workspace.root_path) is not None

            def close(self):
                pass

        key_b64 = b64encode(b"k" * 32).decode("ascii")

        try:
            runtime_state.STATE_PATH = None
            with tempfile.TemporaryDirectory() as temp_dir:
                workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()

                with patch("core.indexer_worker.DriveIndexer", return_value=FakeIndexer()), patch.dict(
                    "os.environ",
                    {"SOULDRIVE_WORKSPACE_DATA_KEY_B64": key_b64},
                    clear=False,
                ):
                    exit_code = indexer_worker.main([
                        "--workspace-path",
                        workspace.root_path,
                        "--auth-level",
                        "PRO",
                    ])
        finally:
            clear_workspace_keys(workspace.root_path)
            runtime_state.STATE_PATH = original_state_path

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["workspace_path"], workspace.root_path)
        self.assertEqual(captured["auth_level"], "PRO")
        self.assertTrue(captured["keys_ready"])


if __name__ == "__main__":
    unittest.main()
