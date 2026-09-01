import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core import sidecar_runtime
from core.project_catalog import ProjectCatalog
import core.runtime_state as runtime_state


class SidecarRuntimeTests(unittest.TestCase):
    def test_configure_runtime_streams_handles_gui_mode_without_console(self):
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        original_stream = sidecar_runtime._RUNTIME_LOG_STREAM

        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                with patch.dict("os.environ", {"SOULDRIVE_APP_ROOT": temp_dir}):
                    sys.stdout = None
                    sys.stderr = None

                    sidecar_runtime.configure_runtime_streams()
                    print("local sidecar log check")

                    self.assertIsNotNone(sys.stdout)
                    self.assertIs(sys.stdout, sys.stderr)
                    self.assertTrue((Path(temp_dir) / "runtime" / "sidecar.log").exists())
            finally:
                stream = sidecar_runtime._RUNTIME_LOG_STREAM
                sys.stdout = original_stdout
                sys.stderr = original_stderr
                sidecar_runtime._RUNTIME_LOG_STREAM = original_stream
                if stream is not None:
                    stream.close()

    def test_uvicorn_log_config_writes_to_runtime_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict("os.environ", {"SOULDRIVE_APP_ROOT": temp_dir}):
                config = sidecar_runtime.uvicorn_log_config()

        default_handler = config["handlers"]["default"]

        self.assertEqual(default_handler["class"], "logging.FileHandler")
        self.assertTrue(default_handler["filename"].endswith(str(Path("runtime") / "sidecar.log")))
        self.assertEqual(config["loggers"]["uvicorn.access"]["handlers"], [])

    def test_parent_pid_from_env_ignores_invalid_values(self):
        for raw_pid in ("", "abc", "-1", str(sidecar_runtime.os.getpid())):
            with patch.dict("os.environ", {"SOULDRIVE_PARENT_PID": raw_pid}, clear=False):
                self.assertIsNone(sidecar_runtime.parent_pid_from_env())

    def test_parent_process_is_alive_returns_false_for_missing_pid(self):
        self.assertFalse(sidecar_runtime.parent_process_is_alive(99999999))

    def test_parent_exit_grace_seconds_is_bounded(self):
        with patch.dict("os.environ", {"SOULDRIVE_PARENT_EXIT_GRACE_SECONDS": "30"}, clear=False):
            self.assertEqual(sidecar_runtime.parent_exit_grace_seconds(), 30.0)
        with patch.dict("os.environ", {"SOULDRIVE_PARENT_EXIT_GRACE_SECONDS": "999"}, clear=False):
            self.assertEqual(sidecar_runtime.parent_exit_grace_seconds(), 300.0)

    def test_local_pc_mode_starts_parent_exit_monitor_when_tauri_passes_parent_pid(self):
        shutdown_event = sidecar_runtime.threading.Event()
        shutdown_event.set()
        with patch.dict(
            "os.environ",
            {"SOULDRIVE_PC_ONLY": "1", "SOULDRIVE_PARENT_PID": "99999999"},
            clear=True,
        ):
            monitor = sidecar_runtime.start_parent_exit_monitor(
                shutdown_event,
                Mock(),
            )

        self.assertIsNotNone(monitor)
        monitor.join(timeout=0.1)

    def test_removable_watch_is_enabled_by_default(self):
        with patch.dict("os.environ", {"SOULDRIVE_WATCH_REMOVABLE": "1"}, clear=False):
            self.assertTrue(sidecar_runtime.removable_watch_enabled())

        with patch.dict("os.environ", {}, clear=True):
            self.assertTrue(sidecar_runtime.removable_watch_enabled())

        with patch.dict("os.environ", {"SOULDRIVE_WATCH_REMOVABLE": "0"}, clear=True):
            self.assertFalse(sidecar_runtime.removable_watch_enabled())

    def test_workspace_mode_prefers_explicit_local_workspace(self):
        with patch.dict(
            "os.environ",
            {"SOULDRIVE_WORKSPACE": "D:\\private-workspace"},
            clear=True,
        ):
            self.assertEqual(sidecar_runtime.workspace_mode(), "local")

        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(sidecar_runtime.workspace_mode(), "removable")

    def test_pc_only_mode_always_uses_local_workspace(self):
        with patch.dict(
            "os.environ",
            {
                "SOULDRIVE_PC_ONLY": "1",
                "SOULDRIVE_WORKSPACE_MODE": "removable",
            },
            clear=True,
        ):
            self.assertEqual(sidecar_runtime.workspace_mode(), "local")

        with patch.dict(
            "os.environ",
            {
                "SOULDRIVE_WORKSPACE": "D:\\private-workspace",
                "SOULDRIVE_WORKSPACE_MODE": "removable",
            },
            clear=True,
        ):
            self.assertEqual(sidecar_runtime.workspace_mode(), "removable")

    def test_workspace_mode_rejects_unknown_mode(self):
        with patch.dict("os.environ", {"SOULDRIVE_WORKSPACE_MODE": "cloud"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "local.*removable"):
                sidecar_runtime.workspace_mode()

    def test_initialize_local_workspace_mounts_request_scoped_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_path = str(Path(temp_dir) / "pc-workspace")
            with patch.dict("os.environ", {"SOULDRIVE_WORKSPACE": workspace_path}, clear=True), patch.object(
                sidecar_runtime,
                "use_workspace_runtime_state",
            ) as use_state, patch.object(sidecar_runtime, "unlock_runtime") as unlock:
                workspace = sidecar_runtime.initialize_local_workspace()

            self.assertEqual(workspace.root_path, workspace_path)
            self.assertTrue(Path(workspace.manifest_path).exists())
            use_state.assert_called_once_with(workspace_path)
            unlock.assert_called_once_with(
                "LOCAL",
                None,
                None,
                workspace_path,
                reason="local PC workspace ready",
            )

    def test_initialize_local_workspace_still_requires_passphrase_unlock(self):
        previous_state_path = runtime_state.STATE_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_path = str(Path(temp_dir) / "app-data" / "workspace")
            try:
                with patch.dict(
                    "os.environ",
                    {
                        "SOULDRIVE_WORKSPACE": workspace_path,
                        "SOULDRIVE_WORKSPACE_MODE": "local",
                    },
                    clear=True,
                ):
                    workspace = sidecar_runtime.initialize_local_workspace()
                    state = runtime_state.get_runtime_state()

                self.assertEqual(Path(workspace.root_path).resolve(), Path(workspace_path).resolve())
                self.assertNotEqual(Path(workspace.root_path).resolve(), Path.cwd().resolve())
                self.assertTrue(state["hardware_mounted"])
                self.assertTrue(state["locked"])
                self.assertFalse(state["software_unlocked"])
                self.assertEqual(state["workspace_path"], workspace_path)
                self.assertEqual(state["security_reason"], "workspace passphrase required")
            finally:
                runtime_state.STATE_PATH = previous_state_path

    def test_local_workspace_restart_keeps_existing_project_catalog(self):
        previous_state_path = runtime_state.STATE_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_path = str(Path(temp_dir) / "pc-workspace")
            try:
                with patch.dict("os.environ", {"SOULDRIVE_WORKSPACE": workspace_path}, clear=True):
                    first_workspace = sidecar_runtime.initialize_local_workspace()
                    catalog = ProjectCatalog(first_workspace)
                    try:
                        catalog.create_project("重启保留项目", project_id="persistent-project")
                    finally:
                        catalog.close()

                    restarted_workspace = sidecar_runtime.initialize_local_workspace()

                restarted_catalog = ProjectCatalog(restarted_workspace)
                try:
                    row = restarted_catalog.conn.execute(
                        "SELECT name FROM projects WHERE project_id = ?", ("persistent-project",)
                    ).fetchone()
                finally:
                    restarted_catalog.close()

                self.assertEqual(Path(restarted_workspace.root_path).resolve(), Path(workspace_path).resolve())
                self.assertIsNotNone(row)
                self.assertEqual(row["name"], "重启保留项目")
            finally:
                runtime_state.STATE_PATH = previous_state_path

    def test_development_workspace_bypass_requires_an_explicit_flag(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(sidecar_runtime.development_workspace_bypass_enabled())
        with patch.dict("os.environ", {"SOULDRIVE_DEV_BYPASS_WORKSPACE_LOCK": "1"}, clear=True):
            self.assertTrue(sidecar_runtime.development_workspace_bypass_enabled())

    def test_frozen_indexer_command_forces_process_exit_after_worker_returns(self):
        with patch.object(sidecar_runtime.sys, "frozen", True, create=True):
            with patch("core.indexer_worker.main", return_value=7) as worker_main:
                with patch.object(sidecar_runtime.os, "_exit") as exit_process:
                    result = sidecar_runtime.main(["indexer", "D:\\drive", "LITE"])

        worker_main.assert_called_once_with(["D:\\drive", "LITE"])
        exit_process.assert_called_once_with(7)
        self.assertEqual(result, 7)

    def test_frozen_indexer_command_exits_nonzero_when_worker_raises(self):
        with patch.object(sidecar_runtime.sys, "frozen", True, create=True):
            with patch("core.indexer_worker.main", side_effect=RuntimeError("index failed")):
                with patch.object(sidecar_runtime.os, "_exit", side_effect=SystemExit) as exit_process:
                    with self.assertRaises(SystemExit):
                        sidecar_runtime.main(["indexer", "D:\\drive", "LITE"])

        exit_process.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
