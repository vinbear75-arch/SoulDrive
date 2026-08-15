import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.runtime_state as runtime_state
from core.workspace import SoulDriveWorkspace


class RuntimeStateTests(unittest.TestCase):
    def test_default_runtime_state_is_locked_and_isolated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            with patch.object(runtime_state, "STATE_PATH", state_path):
                first = runtime_state.get_runtime_state()
                first["indexing"]["failures"].append({"source_filename": "leak.pdf"})
                second = runtime_state.get_runtime_state()

        self.assertTrue(second["locked"])
        self.assertEqual(second["auth_level"], "NONE")
        self.assertEqual(second["indexing"]["failures"], [])

    def test_default_runtime_state_path_uses_app_runtime_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"SOULDRIVE_APP_ROOT": temp_dir}, clear=False), patch.object(
                runtime_state,
                "STATE_PATH",
                None,
            ):
                runtime_state.lock_runtime("test lock")
                state_path = Path(runtime_state.runtime_state_path())

        self.assertEqual(state_path, Path(temp_dir) / "runtime" / "runtime_state.json")
        self.assertFalse((Path(temp_dir) / "souldrive_db").exists())

    def test_runtime_state_merges_indexing_without_shared_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            with patch.object(runtime_state, "STATE_PATH", state_path):
                runtime_state.set_runtime_state(indexing={"status": "indexing"})
                state = runtime_state.get_runtime_state()

        self.assertEqual(state["indexing"]["status"], "indexing")
        self.assertEqual(state["indexing"]["processed_files"], 0)
        self.assertEqual(state["indexing"]["failures"], [])
        self.assertEqual(state["indexing"]["succeeded_files"], 0)
        self.assertEqual(state["indexing"]["failure_summary"], {})
        self.assertEqual(state["indexing"]["chunk_count"], 0)
        self.assertEqual(state["indexing"]["observability"]["schema_version"], "indexing_observability_v4")
        self.assertEqual(state["indexing"]["observability"]["documents"], [])

    def test_runtime_unlock_marks_hardware_only_until_software_unlock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            with patch.object(runtime_state, "STATE_PATH", state_path):
                state = runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)

        self.assertTrue(state["locked"])
        self.assertTrue(state["hardware_mounted"])
        self.assertFalse(state["software_unlocked"])
        self.assertEqual(state["security_reason"], "workspace passphrase required")

    def test_runtime_unlock_resets_stale_indexing_progress(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            with patch.object(runtime_state, "STATE_PATH", state_path):
                runtime_state.set_runtime_state(
                    indexing={
                        "status": "indexing",
                        "total_files": 2,
                        "processed_files": 1,
                        "succeeded_files": 1,
                    }
                )
                state = runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)

        self.assertEqual(state["indexing"]["status"], "idle")
        self.assertEqual(state["indexing"]["total_files"], 0)
        self.assertEqual(state["indexing"]["processed_files"], 0)
        self.assertEqual(state["indexing"]["succeeded_files"], 0)

    def test_mark_software_unlocked_allows_sensitive_workflows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            with patch.object(runtime_state, "STATE_PATH", state_path):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                state = runtime_state.mark_software_unlocked()

        self.assertFalse(state["locked"])
        self.assertTrue(state["software_unlocked"])
        self.assertEqual(state["auth_level"], "HARDWARE_PLUS_PASSWORD")

    def test_unlock_preserves_existing_workspace_when_drive_is_missing(self):
        from core.mcp_server import RuntimeRequest, runtime_unlock

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            with patch.object(runtime_state, "STATE_PATH", state_path):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                result = _run_async(runtime_unlock(RuntimeRequest(auth_level="PRO")))

        self.assertEqual(result["active_drive"], temp_dir)
        self.assertEqual(result["workspace_path"], workspace.root_path)

    def test_runtime_unlock_rejects_uninitialized_active_drive(self):
        from core.mcp_server import RuntimeRequest, runtime_unlock

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            candidate_drive = str(Path(temp_dir) / "uninitialized")
            Path(candidate_drive).mkdir()
            with patch.object(runtime_state, "STATE_PATH", state_path):
                result = _run_async(runtime_unlock(RuntimeRequest(active_drive=candidate_drive)))

        self.assertEqual(result.status_code, 400)

    def test_runtime_unlock_rejects_missing_removable_workspace(self):
        from core.mcp_server import RuntimeRequest, runtime_unlock

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            with patch.object(runtime_state, "STATE_PATH", state_path):
                result = _run_async(runtime_unlock(RuntimeRequest(auth_level="PRO")))

        self.assertEqual(result.status_code, 423)

    def test_public_runtime_state_redacts_local_paths(self):
        from core.mcp_server import public_runtime_state

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            with patch.object(runtime_state, "STATE_PATH", state_path):
                runtime_state.unlock_runtime("PRO", "SECRET-SN", temp_dir, workspace.root_path)
                runtime_state.mark_software_unlocked()
                public_state = public_runtime_state()

        serialized = str(public_state)
        self.assertNotIn(temp_dir, serialized)
        self.assertNotIn("SECRET-SN", serialized)
        self.assertEqual(public_state["active_drive"], "mounted removable storage")
        self.assertEqual(public_state["workspace_path"], "SoulDrive workspace mounted")


def _run_async(coroutine):
    import asyncio

    return asyncio.run(coroutine)


if __name__ == "__main__":
    unittest.main()
