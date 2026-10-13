import json
import tempfile
import threading
import unittest
import types
import sys
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from core import mcp_server
from core.index_run_journal import (
    IndexRunJournal,
    index_run_error_observation,
    index_run_input_digest,
)
import core.runtime_state as runtime_state
from core.security_context import set_workspace_keys
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


class IndexRunIdempotencyTests(unittest.TestCase):
    def test_index_receipt_status_is_monotonic_and_terminal_state_is_fenced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            digest = index_run_input_digest(workspace.root_path, "PRO")
            journal = IndexRunJournal(workspace)
            try:
                journal.create_or_get("monotonic-index-key", digest, "run-monotonic")
                journal.update("monotonic-index-key", status="queued")
                journal.update("monotonic-index-key", status="indexing")
                journal.update("monotonic-index-key", status="scanning")
                self.assertEqual(journal.get("monotonic-index-key")["status"], "indexing")

                journal.update("monotonic-index-key", status="completed")
                journal.update(
                    "monotonic-index-key",
                    status="failed",
                    error="late stale worker failure",
                )
                journal.update("monotonic-index-key", status="indexing")
                stored = journal.get("monotonic-index-key")
            finally:
                journal.close()

        self.assertEqual(stored["status"], "completed")
        self.assertIsNone(stored["error"])

    def test_receipt_survives_journal_reopen_and_terminal_projection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            digest = index_run_input_digest(workspace.root_path, "PRO")
            journal = IndexRunJournal(workspace)
            created = journal.create_or_get("persistent-index-key", digest, "run-persistent")
            journal.update("persistent-index-key", status="completed")
            journal.close()

            reopened = IndexRunJournal(workspace)
            stored = reopened.get("persistent-index-key")
            reopened.close()

        self.assertEqual(created["run_id"], "run-persistent")
        self.assertEqual(stored["input_digest"], digest)
        self.assertEqual(stored["status"], "completed")

    def test_idle_worker_state_projects_as_completed_noop_receipt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            journal = IndexRunJournal(workspace)
            digest = index_run_input_digest(workspace.root_path, "PRO")
            journal.create_or_get("idle-index-key", digest, "run-idle")
            journal.update("idle-index-key", status="queued")
            journal.close()

            idle_state = {
                "workspace_path": workspace.root_path,
                "indexing": {
                    "idempotency_key": "idle-index-key",
                    "status": "idle",
                },
            }
            with patch.object(mcp_server, "_workspace_from_state", return_value=workspace):
                mcp_server._sync_index_run_receipt(idle_state)

            reopened = IndexRunJournal(workspace)
            stored = reopened.get("idle-index-key")
            reopened.close()

        self.assertEqual(stored["status"], "completed")

    def test_blocked_worker_state_projects_as_failed_receipt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            journal = IndexRunJournal(workspace)
            digest = index_run_input_digest(workspace.root_path, "PRO")
            journal.create_or_get("blocked-index-key", digest, "run-blocked")
            journal.update("blocked-index-key", status="queued")
            journal.close()

            blocked_state = {
                "workspace_path": workspace.root_path,
                "indexing": {
                    "idempotency_key": "blocked-index-key",
                    "status": "blocked",
                    "failures": [{"reason": "insufficient disk space"}],
                },
            }
            with patch.object(mcp_server, "_workspace_from_state", return_value=workspace):
                mcp_server._sync_index_run_receipt(blocked_state)

            reopened = IndexRunJournal(workspace)
            stored = reopened.get("blocked-index-key")
            reopened.close()

        self.assertEqual(stored["status"], "failed")

    def test_concurrent_same_key_creates_one_durable_receipt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            digest = index_run_input_digest(workspace.root_path, "PRO")
            barrier = threading.Barrier(2)
            results = []
            errors = []

            def submit(run_id):
                journal = IndexRunJournal(workspace)
                try:
                    barrier.wait(timeout=10)
                    results.append(journal.create_or_get("concurrent-index-key", digest, run_id))
                except Exception as exc:
                    errors.append(exc)
                finally:
                    journal.close()

            threads = [
                threading.Thread(target=submit, args=(f"concurrent-run-{index}",))
                for index in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len({row["run_id"] for row in results}), 1)

    def test_persisted_worker_identity_is_detected_after_sidecar_pointer_is_lost(self):
        fake_process = types.SimpleNamespace(
            is_running=lambda: True,
            status=lambda: "running",
            create_time=lambda: 123.456,
            cmdline=lambda: [
                "python",
                "-m",
                "core.indexer_worker",
                "--workspace-path",
                "C:/workspace",
            ],
        )
        fake_psutil = types.SimpleNamespace(Process=lambda pid: fake_process)
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            runtime_state,
            "STATE_PATH",
            str(Path(temp_dir) / "runtime_state.json"),
        ), patch.object(mcp_server, "indexer_process", None), patch.dict(
            sys.modules,
            {"psutil": fake_psutil},
        ):
            runtime_state.set_runtime_state(
                workspace_path="C:/workspace",
                indexing={
                    "status": "indexing",
                    "worker_pid": 8123,
                    "worker_started_at": 123.456,
                },
            )

            active = mcp_server._indexer_worker_is_active()

        self.assertTrue(active)

    def test_stale_indexer_run_cannot_overwrite_current_run(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            runtime_state,
            "STATE_PATH",
            str(Path(temp_dir) / "runtime_state.json"),
        ):
            runtime_state.update_indexing_status(
                status="indexing",
                run_id="new-run",
                current_file="new.pdf",
            )
            runtime_state.update_indexing_status(
                expected_run_id="old-run",
                status="completed",
                run_id="old-run",
                current_file=None,
                finished_at=123.0,
            )
            state = runtime_state.get_runtime_state()

        self.assertEqual(state["indexing"]["run_id"], "new-run")
        self.assertEqual(state["indexing"]["status"], "indexing")
        self.assertEqual(state["indexing"]["current_file"], "new.pdf")

    def test_same_index_run_does_not_regress_when_parent_update_arrives_late(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            runtime_state,
            "STATE_PATH",
            str(Path(temp_dir) / "runtime_state.json"),
        ):
            runtime_state.update_indexing_status(
                status="scanning",
                run_id="same-run",
                discovered_files=3,
            )
            runtime_state.update_indexing_status(
                status="queued",
                run_id="same-run",
                worker_pid=1234,
            )
            state = runtime_state.get_runtime_state()

        self.assertEqual(state["indexing"]["status"], "scanning")
        self.assertEqual(state["indexing"]["discovered_files"], 3)
        self.assertIsNone(state["indexing"]["worker_pid"])

    def test_stale_indexer_run_cannot_reopen_locked_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            runtime_state,
            "STATE_PATH",
            str(Path(temp_dir) / "runtime_state.json"),
        ):
            runtime_state.lock_runtime("test lock")
            runtime_state.update_indexing_status(
                expected_run_id="stale-run",
                status="indexing",
                run_id="stale-run",
            )
            state = runtime_state.get_runtime_state()

        self.assertEqual(state["indexing"]["status"], "locked")
        self.assertIsNone(state["indexing"]["run_id"])

    def test_same_key_replays_without_starting_a_second_worker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "index-passphrase")
            set_workspace_keys(workspace.root_path, unlock_keystore(workspace, "index-passphrase"))
            state_path = str(Path(temp_dir) / "runtime_state.json")
            fake_process = Mock()
            fake_process.poll.return_value = None
            def start_worker(*_args, **_kwargs):
                mcp_server.indexer_process = fake_process
                return fake_process
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server,
                "current_workspace",
                return_value=workspace,
            ), patch.object(
                mcp_server,
                "_start_indexer_worker",
                side_effect=start_worker,
            ) as start_worker_mock, patch.object(
                mcp_server,
                "indexer_process",
                None,
            ), patch.dict(
                "os.environ",
                {"SOULDRIVE_API_TOKEN": "test-token"},
                clear=False,
            ):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                runtime_state.mark_software_unlocked()
                client = TestClient(mcp_server.app)
                first = client.post(
                    "/index/run",
                    headers={"X-SoulDrive-Token": "test-token", "Idempotency-Key": "index-request-1"},
                )
                second = client.post(
                    "/index/run",
                    headers={"X-SoulDrive-Token": "test-token", "Idempotency-Key": "index-request-1"},
                )

        self.assertEqual(first.status_code, 202)
        self.assertTrue(first.json()["started"])
        self.assertEqual(second.status_code, 202)
        self.assertTrue(second.json()["replayed"])
        self.assertEqual(first.json()["run_id"], second.json()["run_id"])
        start_worker_mock.assert_called_once()

    def test_completed_runtime_state_is_projected_to_receipt_and_replayed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "index-passphrase")
            set_workspace_keys(workspace.root_path, unlock_keystore(workspace, "index-passphrase"))
            state_path = str(Path(temp_dir) / "runtime_state.json")
            fake_process = Mock()
            fake_process.poll.return_value = None
            def start_worker(*_args, **_kwargs):
                mcp_server.indexer_process = fake_process
                return fake_process
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server,
                "current_workspace",
                return_value=workspace,
            ), patch.object(
                mcp_server,
                "_start_indexer_worker",
                side_effect=start_worker,
            ) as start_worker_mock, patch.object(
                mcp_server,
                "indexer_process",
                None,
            ), patch.dict(
                "os.environ",
                {"SOULDRIVE_API_TOKEN": "test-token"},
                clear=False,
            ):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                runtime_state.mark_software_unlocked()
                client = TestClient(mcp_server.app)
                first = client.post(
                    "/index/run",
                    headers={"X-SoulDrive-Token": "test-token", "Idempotency-Key": "index-request-3"},
                )
                run_id = first.json()["run_id"]
                runtime_state.update_indexing_status(status="completed", run_id=run_id)
                fake_process.poll.return_value = 0
                client.get("/runtime/status")
                replay = client.post(
                    "/index/run",
                    headers={"X-SoulDrive-Token": "test-token", "Idempotency-Key": "index-request-3"},
                )

        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["status"], "completed")
        start_worker_mock.assert_called_once()

    def test_failed_runtime_state_replay_does_not_expose_raw_failure_reason(self):
        secret = "parser backend=redis://user:password@private-host secret=index-token"
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "index-passphrase")
            set_workspace_keys(workspace.root_path, unlock_keystore(workspace, "index-passphrase"))
            state_path = str(Path(temp_dir) / "runtime_state.json")
            fake_process = Mock()
            fake_process.poll.return_value = None

            def start_worker(*_args, **_kwargs):
                mcp_server.indexer_process = fake_process
                return fake_process

            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server, "current_workspace", return_value=workspace
            ), patch.object(mcp_server, "_start_indexer_worker", side_effect=start_worker), patch.object(
                mcp_server, "indexer_process", None
            ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                runtime_state.mark_software_unlocked()
                client = TestClient(mcp_server.app)
                first = client.post(
                    "/index/run",
                    headers={"X-SoulDrive-Token": "test-token", "Idempotency-Key": "index-request-failed"},
                )
                run_id = first.json()["run_id"]
                runtime_state.update_indexing_status(
                    status="failed",
                    run_id=run_id,
                    failures=[{
                        "source_filename": "private.pdf",
                        "error_code": "INDEXING_FAILED",
                        "category": "unknown",
                        "reason": secret,
                    }],
                )
                replay = client.post(
                    "/index/run",
                    headers={"X-SoulDrive-Token": "test-token", "Idempotency-Key": "index-request-failed"},
                )
                journal = IndexRunJournal(workspace)
                stored = journal.get("index-request-failed")
                journal.close()

        self.assertEqual(first.status_code, 202)
        self.assertEqual(replay.status_code, 200)
        payload = replay.json()
        self.assertTrue(payload["replayed"])
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error"], "索引任务失败，请查看运行诊断。")
        self.assertEqual(payload["error_observation"]["error_type"], "UnknownError")
        self.assertRegex(payload["error_observation"]["error_digest"], r"^[0-9a-f]{16}$")
        self.assertIsNotNone(stored)
        self.assertNotIn(secret, stored["error"] or "")
        self.assertEqual(payload["error_observation"], json.loads(stored["error"]))
        self.assertNotIn(secret, replay.text)
        self.assertNotIn("index-token", replay.text)

    def test_legacy_raw_failure_receipt_is_sanitized_on_reopen(self):
        secret = "legacy backend=redis://user:password@private-host secret=index-token"
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            digest = index_run_input_digest(workspace.root_path, "PRO")
            journal = IndexRunJournal(workspace)
            journal.create_or_get("legacy-index-key", digest, "legacy-run")
            journal.connection.execute(
                "UPDATE index_run_requests SET status = 'failed', error = ? WHERE idempotency_key = ?",
                (secret, "legacy-index-key"),
            )
            journal.connection.commit()
            journal.close()

            reopened = IndexRunJournal(workspace)
            stored = reopened.get("legacy-index-key")
            raw = reopened.connection.execute(
                "SELECT error FROM index_run_requests WHERE idempotency_key = ?",
                ("legacy-index-key",),
            ).fetchone()["error"]
            reopened.close()

        self.assertIsNotNone(stored)
        self.assertNotIn(secret, raw)
        self.assertEqual(index_run_error_observation(raw), json.loads(raw))

    def test_same_key_with_changed_index_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "index-passphrase")
            set_workspace_keys(workspace.root_path, unlock_keystore(workspace, "index-passphrase"))
            state_path = str(Path(temp_dir) / "runtime_state.json")
            fake_process = Mock()
            fake_process.poll.return_value = None
            def start_worker(*_args, **_kwargs):
                mcp_server.indexer_process = fake_process
                return fake_process
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server,
                "current_workspace",
                return_value=workspace,
            ), patch.object(
                mcp_server,
                "_start_indexer_worker",
                side_effect=start_worker,
            ), patch.object(
                mcp_server,
                "indexer_process",
                None,
            ), patch.dict(
                "os.environ",
                {"SOULDRIVE_API_TOKEN": "test-token"},
                clear=False,
            ):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                runtime_state.mark_software_unlocked()
                client = TestClient(mcp_server.app)
                first = client.post(
                    "/index/run",
                    headers={"X-SoulDrive-Token": "test-token", "Idempotency-Key": "index-request-2"},
                )
                runtime_state.set_runtime_state(auth_level="LOCAL")
                second = client.post(
                    "/index/run",
                    headers={"X-SoulDrive-Token": "test-token", "Idempotency-Key": "index-request-2"},
                )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 409)
        self.assertIn("different index request", second.json()["error"])


if __name__ == "__main__":
    unittest.main()
