import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.task_journal import (
    TaskJournal,
    TaskDefinitionMismatchError,
    TaskLeaseLostError,
    TaskPublicError,
    _owner_process_alive,
    process_owner_id,
)
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import derive_workspace_keys


class TaskJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.workspace = SoulDriveWorkspace.from_drive(self.temp_dir.name).ensure()
        self.keys = derive_workspace_keys(b"x" * 32)

    def test_resume_skips_completed_steps(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)
        task = journal.create_task("test.workflow", {"value": 2}, ["first", "second"])
        calls = []
        claim = journal.claim_task(task["task_id"], "999999:first")
        journal.begin_step(task["task_id"], "first", lease_epoch=claim["lease_epoch"])
        journal.complete_step(
            task["task_id"],
            "first",
            {"value": 3},
            lease_epoch=claim["lease_epoch"],
        )
        journal.interrupt_task(task["task_id"], "simulated interruption")

        result = journal.run_steps(
            task["task_id"],
            "999998:resume",
            {
                "first": lambda _inputs, _outputs: calls.append("first") or {"value": 99},
                "second": lambda _inputs, outputs: calls.append("second") or {"value": outputs["first"]["value"] + 1},
            },
            lambda outputs: {"answer": outputs["second"]["value"]},
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["answer"], 4)
        self.assertEqual(calls, ["second"])
        self.assertEqual(result["progress"], {"completed": 2, "total": 2})

    def test_missing_step_handler_does_not_turn_definition_mismatch_into_failure(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)
        task = journal.create_task("test.workflow", {"value": 2}, ["first", "second"])

        with self.assertRaisesRegex(TaskDefinitionMismatchError, "first"):
            journal.run_steps(
                task["task_id"],
                "worker-new-code",
                {"second": lambda _inputs, _outputs: {"ok": True}},
                lambda outputs: outputs,
            )

        stored = journal.get_task(task["task_id"])
        self.assertEqual(stored["status"], "pending")
        self.assertEqual(
            {step["status"] for step in stored["steps"]},
            {"pending"},
        )

    def test_completed_step_does_not_require_its_old_handler_during_recovery(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)
        task = journal.create_task("test.workflow", {"value": 2}, ["first", "second"])
        claim = journal.claim_task(task["task_id"], "worker-old-code")
        journal.begin_step(task["task_id"], "first", lease_epoch=claim["lease_epoch"])
        journal.complete_step(
            task["task_id"],
            "first",
            {"value": 3},
            lease_epoch=claim["lease_epoch"],
        )
        journal.interrupt_task(task["task_id"], "simulated interruption")

        result = journal.run_steps(
            task["task_id"],
            "worker-new-code",
            {"second": lambda _inputs, outputs: {"value": outputs["first"]["value"] + 1}},
            lambda outputs: {"answer": outputs["second"]["value"]},
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["answer"], 4)

    def test_public_task_preserves_workflow_identity_from_encrypted_inputs(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)
        task = journal.create_task(
            "meeting.actions",
            {
                "project_id": "project-a",
                "workflow_id": "meeting_actions",
                "title": "确认负责人",
            },
            ["collect"],
        )

        public = journal.public_task(task["task_id"])

        self.assertEqual(public["project_id"], "project-a")
        self.assertEqual(public["workflow_id"], "meeting_actions")

    def test_create_task_rejects_unknown_workflow_identity(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)

        with self.assertRaisesRegex(ValueError, "unsupported workflow_id"):
            journal.create_task(
                "unknown.actions",
                {"workflow_id": "unknown_window", "title": "未知窗口任务"},
                ["run"],
            )

    def test_checkpoint_payload_is_not_stored_in_plaintext(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)
        task = journal.create_task("test.workflow", {"secret_value": "private input"}, ["first"])
        raw = Path(self.workspace.task_journal_path).read_bytes()

        self.assertNotIn(b"private input", raw)
        self.assertEqual(journal.get_task(task["task_id"])["inputs"]["secret_value"], "private input")

    def test_task_failure_persists_only_a_bounded_error_observation(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)
        secret = "private task failure secret=worker-token path=C:\\private\\run.log"
        task = journal.create_task("test.workflow", {"value": 1}, ["first"])

        failed = journal.run_steps(
            task["task_id"],
            "worker-1",
            {
                "first": lambda _inputs, _outputs: (_ for _ in ()).throw(RuntimeError(secret)),
            },
            lambda outputs: outputs["first"],
        )

        raw = Path(self.workspace.task_journal_path).read_bytes()
        self.assertNotIn(secret.encode("utf-8"), raw)
        row = journal.conn.execute(
            "SELECT error FROM runtime_tasks WHERE task_id = ?",
            (task["task_id"],),
        ).fetchone()
        observation = json.loads(row["error"])
        self.assertEqual(observation["error_type"], "RuntimeError")
        self.assertEqual(observation["error_chars"], len(secret))
        self.assertNotIn(secret, str(failed))

        journal.close()
        reopened = TaskJournal(self.workspace, self.keys)
        self.addCleanup(reopened.close)
        reopened_task = reopened.get_task(task["task_id"])
        self.assertNotIn(secret, str(reopened_task))
        self.assertEqual(json.loads(reopened_task["error"]), observation)

    def test_explicit_public_task_error_survives_without_raw_diagnostics(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)
        secret = "private task failure secret=worker-token path=C:\\private\\run.log"
        public_message = "selected documents are not indexed: public-name.pdf"
        task = journal.create_task("test.workflow", {"value": 1}, ["first"])

        failed = journal.run_steps(
            task["task_id"],
            "worker-1",
            {
                "first": lambda _inputs, _outputs: (_ for _ in ()).throw(
                    TaskPublicError(public_message)
                ),
            },
            lambda outputs: outputs["first"],
        )

        raw = Path(self.workspace.task_journal_path).read_bytes()
        self.assertNotIn(secret.encode("utf-8"), raw)
        self.assertIn(public_message, str(failed))
        observation = json.loads(failed["error"])
        self.assertEqual(observation["error_type"], "TaskPublicError")
        self.assertEqual(observation["public_message"], public_message)
        self.assertEqual(
            journal.public_task(task["task_id"])["error"],
            public_message,
        )

        journal.close()
        reopened = TaskJournal(self.workspace, self.keys)
        self.addCleanup(reopened.close)
        self.assertEqual(
            reopened.public_task(task["task_id"])["error"],
            public_message,
        )

    def test_legacy_task_errors_are_sanitized_on_reopen(self):
        journal = TaskJournal(self.workspace, self.keys)
        task = journal.create_task("test.workflow", {"value": 1}, ["first"])
        claim = journal.claim_task(task["task_id"], "worker-1")
        journal.begin_step(task["task_id"], "first", lease_epoch=claim["lease_epoch"])
        secret = "legacy task error secret=old-token path=C:\\private\\legacy.log"
        journal.conn.execute(
            "UPDATE runtime_tasks SET error = ? WHERE task_id = ?",
            (secret, task["task_id"]),
        )
        journal.conn.execute(
            "UPDATE runtime_task_steps SET error = ? WHERE task_id = ? AND step_name = ?",
            (secret, task["task_id"], "first"),
        )
        journal.conn.execute(
            "UPDATE runtime_task_step_attempts SET error = ? WHERE task_id = ? AND step_name = ?",
            (secret, task["task_id"], "first"),
        )
        journal.conn.commit()
        journal.close()

        reopened = TaskJournal(self.workspace, self.keys)
        self.addCleanup(reopened.close)
        rows = reopened.conn.execute(
            """
            SELECT error FROM runtime_tasks WHERE task_id = ?
            UNION ALL
            SELECT error FROM runtime_task_steps WHERE task_id = ?
            UNION ALL
            SELECT error FROM runtime_task_step_attempts WHERE task_id = ?
            """,
            (task["task_id"], task["task_id"], task["task_id"]),
        ).fetchall()
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertNotIn(secret, row["error"])
            self.assertEqual(json.loads(row["error"])["error_chars"], len(secret))

    def test_task_idempotency_reuses_task_and_rejects_input_conflict(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)

        first = journal.create_task(
            "test.workflow",
            {"value": 1},
            ["first"],
            idempotency_key="same-request",
        )
        repeated = journal.create_task(
            "test.workflow",
            {"value": 1},
            ["first"],
            idempotency_key="same-request",
        )

        self.assertEqual(repeated["task_id"], first["task_id"])
        with self.assertRaisesRegex(ValueError, "idempotency_key"):
            journal.create_task(
                "test.workflow",
                {"value": 2},
                ["first"],
                idempotency_key="same-request",
            )

    def test_concurrent_same_idempotency_key_creates_one_task_across_connections(self):
        first_journal = TaskJournal(self.workspace, self.keys)
        second_journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(first_journal.close)
        self.addCleanup(second_journal.close)
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def create(journal):
            try:
                barrier.wait(timeout=5)
                results.append(
                    journal.create_task(
                        "test.workflow",
                        {"value": 1},
                        ["first"],
                        idempotency_key="concurrent-request",
                    )
                )
            except BaseException as exc:  # pragma: no cover - assertion below reports it
                errors.append(exc)

        threads = [
            threading.Thread(target=create, args=(first_journal,)),
            threading.Thread(target=create, args=(second_journal,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(errors, errors)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 2)
        self.assertEqual({item["task_id"] for item in results}, {results[0]["task_id"]})
        count = first_journal.conn.execute(
            "SELECT COUNT(*) FROM runtime_tasks WHERE kind = ? AND idempotency_key = ?",
            ("test.workflow", "concurrent-request"),
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_repetition_guard_warns_without_returning_failed_error_text(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)
        task = journal.create_task("test.workflow", {"value": 1}, ["side_effect"])

        def failed_step(_inputs, _outputs):
            raise RuntimeError("secret failure details")

        result = journal.run_steps(
            task["task_id"],
            "worker-1",
            {"side_effect": failed_step},
            lambda outputs: outputs["side_effect"],
        )

        self.assertEqual(result["status"], "failed")
        guard = journal.repetition_guard(
            "test.workflow",
            {"value": 1},
            ["side_effect"],
        )
        self.assertEqual(guard["decision"], "warn")
        self.assertEqual(guard["matches"][0]["task_id"], task["task_id"])
        self.assertRegex(guard["matches"][0]["error_digest"], r"^[0-9a-f]{64}$")
        self.assertNotIn("secret failure details", str(guard))

        public = journal.public_task(task["task_id"])
        self.assertNotIn("secret failure details", str(public))
        self.assertEqual(public["error"], "任务执行失败，请查看运行诊断。")
        self.assertEqual(public["error_observation"]["error_type"], "RuntimeError")
        self.assertRegex(public["error_observation"]["error_digest"], r"^[0-9a-f]{16}$")
        self.assertNotIn("secret failure details", str(public["steps"]))
        self.assertEqual(
            public["steps"][0]["attempt_history"][0]["error"],
            "任务执行失败，请查看运行诊断。",
        )

        allowed = journal.repetition_guard(
            "test.workflow",
            {"value": 2},
            ["side_effect"],
        )
        self.assertEqual(allowed["decision"], "allow")
        self.assertEqual(journal.public_task(task["task_id"])["repetition_guard"]["decision"], "warn")

    def test_repetition_guard_persists_business_digest_across_restart(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)
        first = journal.create_task(
            "presentation.plan",
            {
                "request_id": "request-run-1",
                "task_id": "task-run-1",
                "source_filenames": ["A.pdf"],
            },
            ["side_effect"],
            task_id="task-run-1",
            repetition_inputs={
                "project_id": "paper",
                "source_filenames": ["A.pdf"],
                "title": "same business request",
            },
        )

        failed = journal.run_steps(
            first["task_id"],
            "worker-1",
            {"side_effect": lambda _inputs, _outputs: (_ for _ in ()).throw(RuntimeError("transient"))},
            lambda outputs: outputs["side_effect"],
        )
        self.assertEqual(failed["status"], "failed")
        journal.close()

        reopened = TaskJournal(self.workspace, self.keys)
        self.addCleanup(reopened.close)
        repeated = reopened.create_task(
            "presentation.plan",
            {
                "request_id": "request-run-2",
                "task_id": "task-run-2",
                "source_filenames": ["A.pdf"],
            },
            ["side_effect"],
            task_id="task-run-2",
            repetition_inputs={
                "project_id": "paper",
                "source_filenames": ["A.pdf"],
                "title": "same business request",
            },
        )

        guard = repeated["repetition_guard"]
        self.assertEqual(guard["decision"], "warn")
        self.assertEqual(guard["basis"], "business_projection")
        self.assertEqual(guard["matches"][0]["task_id"], first["task_id"])

        changed = reopened.create_task(
            "presentation.plan",
            {
                "request_id": "request-run-3",
                "task_id": "task-run-3",
                "source_filenames": ["B.pdf"],
            },
            ["side_effect"],
            task_id="task-run-3",
            repetition_inputs={
                "project_id": "paper",
                "source_filenames": ["B.pdf"],
                "title": "same business request",
            },
        )
        self.assertEqual(changed["repetition_guard"]["decision"], "allow")

    def test_repetition_guard_falls_back_to_legacy_full_input_digest(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)
        task = journal.create_task("test.workflow", {"value": 7}, ["side_effect"])
        failed = journal.run_steps(
            task["task_id"],
            "worker-1",
            {"side_effect": lambda _inputs, _outputs: (_ for _ in ()).throw(RuntimeError("legacy"))},
            lambda outputs: outputs["side_effect"],
        )
        self.assertEqual(failed["status"], "failed")
        journal.conn.execute(
            "UPDATE runtime_tasks SET repetition_digest = NULL, repetition_basis = 'full_inputs' WHERE task_id = ?",
            (task["task_id"],),
        )
        journal.conn.commit()

        guard = journal.repetition_guard("test.workflow", {"value": 7}, ["side_effect"])
        self.assertEqual(guard["decision"], "warn")
        self.assertEqual(guard["matches"][0]["task_id"], task["task_id"])

    def test_process_owner_identity_rejects_pid_reuse(self):
        self.assertTrue(_owner_process_alive(process_owner_id("current-worker")))
        self.assertFalse(_owner_process_alive(f"{os.getpid()}:0.0:reused-pid"))

    def test_list_tasks_uses_stable_task_id_tie_breaker(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)

        newest_id = journal.create_task("test.workflow", {"name": "task-b"}, ["step"])['task_id']
        oldest_id = journal.create_task("test.workflow", {"name": "task-a"}, ["step"])['task_id']
        journal.conn.execute(
            "UPDATE runtime_tasks SET updated_at = ? WHERE task_id IN (?, ?)",
            (100.0, newest_id, oldest_id),
        )
        journal.conn.commit()

        listed = journal.list_tasks(kind="test.workflow", limit=10)

        self.assertEqual([item["task_id"] for item in listed], sorted([newest_id, oldest_id]))

    def test_list_tasks_applies_project_filter_before_limit(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)

        project_task = journal.create_task(
            "test.workflow",
            {"project_id": "project-a", "name": "target"},
            ["step"],
        )["task_id"]
        foreign_task = journal.create_task(
            "test.workflow",
            {"project_id": "project-b", "name": "foreign"},
            ["step"],
        )["task_id"]
        journal.conn.execute(
            "UPDATE runtime_tasks SET updated_at = ? WHERE task_id = ?",
            (100.0, project_task),
        )
        journal.conn.execute(
            "UPDATE runtime_tasks SET updated_at = ? WHERE task_id = ?",
            (200.0, foreign_task),
        )
        journal.conn.commit()

        listed = journal.list_tasks(
            kind="test.workflow",
            limit=1,
            project_id="project-a",
        )

        self.assertEqual([item["task_id"] for item in listed], [project_task])

    def test_step_context_exposes_stable_idempotency_key_across_retry(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)
        task = journal.create_task("test.workflow", {"value": 1}, ["side_effect"])
        contexts = []

        def side_effect(_inputs, _outputs, context):
            contexts.append(dict(context))
            if len(contexts) == 1:
                raise RuntimeError("transient side effect failure")
            return {"ok": True}

        first = journal.run_steps(
            task["task_id"],
            "worker-1",
            {"side_effect": side_effect},
            lambda outputs: outputs["side_effect"],
            include_step_context=True,
        )
        resumed = journal.run_steps(
            task["task_id"],
            "worker-2",
            {"side_effect": side_effect},
            lambda outputs: outputs["side_effect"],
            include_step_context=True,
        )

        self.assertEqual(first["status"], "failed")
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(contexts[0]["task_id"], task["task_id"])
        self.assertEqual(contexts[0]["step_name"], "side_effect")
        self.assertTrue(contexts[0]["idempotency_key"])
        self.assertEqual(contexts[0]["idempotency_key"], contexts[1]["idempotency_key"])
        self.assertEqual([context["attempt"] for context in contexts], [1, 2])

    def test_step_attempt_history_survives_retry_and_reopen(self):
        journal = TaskJournal(self.workspace, self.keys)
        task = journal.create_task("test.workflow", {"value": 1}, ["side_effect"])
        calls = {"count": 0}

        def side_effect(_inputs, _outputs, _context):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("first attempt failed")
            return {"ok": True}

        first = journal.run_steps(
            task["task_id"],
            "worker-1",
            {"side_effect": side_effect},
            lambda outputs: outputs["side_effect"],
            include_step_context=True,
        )
        self.assertEqual(first["status"], "failed")
        journal.close()

        reopened = TaskJournal(self.workspace, self.keys)
        self.addCleanup(reopened.close)
        resumed = reopened.run_steps(
            task["task_id"],
            "worker-2",
            {"side_effect": side_effect},
            lambda outputs: outputs["side_effect"],
            include_step_context=True,
        )

        history = resumed["steps"][0]["attempt_history"]
        self.assertEqual([item["attempt"] for item in history], [1, 2])
        self.assertEqual([item["status"] for item in history], ["failed", "completed"])
        first_error = json.loads(history[0]["error"])
        self.assertEqual(first_error["error_type"], "RuntimeError")
        self.assertEqual(first_error["error_chars"], len("first attempt failed"))
        self.assertIsNone(history[1]["error"])
        self.assertEqual(
            history[0]["idempotency_key"],
            history[1]["idempotency_key"],
        )

    def test_completed_step_is_monotonic_against_late_complete_or_failure(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)
        task = journal.create_task("test.workflow", {}, ["side_effect"])
        claim = journal.claim_task(task["task_id"], "worker")
        journal.begin_step(task["task_id"], "side_effect", lease_epoch=claim["lease_epoch"])
        journal.complete_step(
            task["task_id"],
            "side_effect",
            {"value": "first"},
            lease_epoch=claim["lease_epoch"],
        )

        journal.fail_step(
            task["task_id"],
            "side_effect",
            "late worker failure",
            lease_epoch=claim["lease_epoch"],
        )
        journal.complete_step(
            task["task_id"],
            "side_effect",
            {"value": "late"},
            lease_epoch=claim["lease_epoch"],
        )

        current = journal.get_task(task["task_id"])
        self.assertEqual(current["steps"][0]["status"], "completed")
        self.assertEqual(current["steps"][0]["output"], {"value": "first"})

    def test_pending_cancel_is_terminal_and_survives_reopen(self):
        journal = TaskJournal(self.workspace, self.keys)
        task = journal.create_task("test.workflow", {}, ["first"])

        cancelled = journal.request_cancel(task["task_id"])

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse(cancelled["recoverable"])
        self.assertIsNotNone(cancelled["cancel_requested_at"])
        with self.assertRaisesRegex(ValueError, "cancelled task"):
            journal.run_steps(task["task_id"], "worker", {"first": lambda *_: {}}, lambda _: {})
        journal.close()

        reopened = TaskJournal(self.workspace, self.keys)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get_task(task["task_id"])["status"], "cancelled")
        self.assertFalse(reopened.get_task(task["task_id"])["recoverable"])

    def test_running_cancel_waits_for_current_step_boundary(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)
        task = journal.create_task("test.workflow", {}, ["first", "second"])
        entered = threading.Event()
        release = threading.Event()
        holder = {}

        def first_step(_inputs, _outputs):
            entered.set()
            self.assertTrue(release.wait(2))
            return {"value": 1}

        def worker():
            holder["result"] = journal.run_steps(
                task["task_id"],
                "worker-thread",
                {"first": first_step, "second": lambda *_: {"value": 2}},
                lambda outputs: {"value": outputs["second"]["value"]},
            )

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(entered.wait(2))
        pending = journal.request_cancel(task["task_id"])
        self.assertEqual(pending["status"], "running")
        self.assertTrue(pending["cancellation_pending"])
        self.assertFalse(pending["recoverable"])
        release.set()
        thread.join(2)

        self.assertFalse(thread.is_alive())
        result = holder["result"]
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["steps"][0]["status"], "completed")
        self.assertEqual(result["steps"][1]["status"], "pending")
        self.assertIsNone(result["result"])

    def test_completed_task_wins_a_late_cancel_request(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)
        task = journal.create_task("test.workflow", {}, ["first"])

        completed = journal.run_steps(
            task["task_id"],
            "worker",
            {"first": lambda *_: {"value": 1}},
            lambda outputs: outputs["first"],
        )
        after_cancel = journal.request_cancel(task["task_id"])

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(after_cancel["status"], "completed")
        self.assertIsNone(after_cancel["cancel_requested_at"])

    def test_finalize_failure_preserves_completed_step_for_resume(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)
        task = journal.create_task("test.workflow", {}, ["side_effect"])
        calls = []

        def side_effect(_inputs, _outputs):
            calls.append("side_effect")
            return {"artifact_id": "artifact-1"}

        first = journal.run_steps(
            task["task_id"],
            "worker-1",
            {"side_effect": side_effect},
            lambda _outputs: (_ for _ in ()).throw(RuntimeError("transient finalize failure")),
        )

        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["steps"][0]["status"], "completed")
        self.assertEqual(first["progress"], {"completed": 1, "total": 1})

        resumed = journal.run_steps(
            task["task_id"],
            "worker-2",
            {"side_effect": side_effect},
            lambda outputs: {"artifact_id": outputs["side_effect"]["artifact_id"]},
        )

        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["result"], {"artifact_id": "artifact-1"})
        self.assertEqual(calls, ["side_effect"])

    def test_dead_worker_is_marked_interrupted_and_can_resume_from_checkpoint(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)
        task = journal.create_task("test.workflow", {}, ["first", "second"])
        claim = journal.claim_task(task["task_id"], "not-a-live-process:worker")
        journal.begin_step(task["task_id"], "first", lease_epoch=claim["lease_epoch"])

        recovered = journal.recover_orphaned_tasks()

        self.assertEqual(recovered, [task["task_id"]])
        self.assertEqual(journal.get_task(task["task_id"])["status"], "interrupted")
        attempt_history = journal.get_task(task["task_id"])["steps"][0]["attempt_history"]
        self.assertEqual(attempt_history[0]["status"], "interrupted")
        interrupted_error = json.loads(attempt_history[0]["error"])
        self.assertEqual(interrupted_error["error_type"], "UnknownError")
        self.assertEqual(
            interrupted_error["error_chars"],
            len("worker process ended before task completion"),
        )

        calls = []
        resumed = journal.run_steps(
            task["task_id"],
            "worker-after-restart",
            {
                "first": lambda *_: calls.append("first") or {"value": 1},
                "second": lambda _inputs, outputs: calls.append("second") or {"value": outputs["first"]["value"] + 1},
            },
            lambda outputs: {"value": outputs["second"]["value"]},
        )

        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["result"], {"value": 2})
        self.assertEqual(calls, ["first", "second"])

    def test_live_worker_is_not_recovered_only_for_a_stale_checkpoint(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)
        task = journal.create_task("test.workflow", {}, ["long_step"])
        owner_id = f"{os.getpid()}:live-worker"
        claim = journal.claim_task(task["task_id"], owner_id)
        journal.begin_step(task["task_id"], "long_step", lease_epoch=claim["lease_epoch"])
        journal.conn.execute(
            "UPDATE runtime_tasks SET updated_at = ? WHERE task_id = ?",
            (time.time() - 901, task["task_id"]),
        )
        journal.conn.commit()

        self.assertEqual(journal.recover_orphaned_tasks(), [])
        self.assertEqual(journal.get_task(task["task_id"])["status"], "running")

    def test_stale_recovery_scan_cannot_interrupt_a_replacement_worker(self):
        journal = TaskJournal(self.workspace, self.keys)
        replacement = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)
        self.addCleanup(replacement.close)
        task = journal.create_task("test.workflow", {}, ["side_effect"])
        old_claim = journal.claim_task(task["task_id"], "not-a-live-process:old")
        journal.begin_step(task["task_id"], "side_effect", lease_epoch=old_claim["lease_epoch"])
        new_claim = {}

        def replace_after_scan(owner_id):
            if not new_claim:
                replacement.interrupt_task(task["task_id"], "another recovery won")
                new_claim.update(replacement.claim_task(task["task_id"], process_owner_id("replacement")))
                replacement.begin_step(
                    task["task_id"], "side_effect", lease_epoch=new_claim["lease_epoch"],
                )
            return _owner_process_alive(owner_id)

        with patch("core.task_journal._owner_process_alive", side_effect=replace_after_scan):
            recovered = journal.recover_orphaned_tasks()

        current = replacement.get_task(task["task_id"])
        self.assertEqual(current["status"], "running")
        self.assertEqual(current["lease_epoch"], new_claim["lease_epoch"])
        self.assertEqual(current["steps"][0]["status"], "running")
        self.assertEqual(current["steps"][0]["attempt_history"][-1]["status"], "running")
        self.assertEqual(recovered, [])
        replacement.complete_step(
            task["task_id"], "side_effect", {"artifact_id": "new"},
            lease_epoch=new_claim["lease_epoch"],
        )
        completed = replacement.complete_task(
            task["task_id"], {"artifact_id": "new"}, lease_epoch=new_claim["lease_epoch"],
        )
        self.assertEqual(completed["result"], {"artifact_id": "new"})

    def test_stale_recovery_scan_does_not_report_another_recovery_as_its_own(self):
        journal = TaskJournal(self.workspace, self.keys)
        replacement = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)
        self.addCleanup(replacement.close)
        task = journal.create_task("test.workflow", {}, ["side_effect"])
        old_claim = journal.claim_task(task["task_id"], "not-a-live-process:old")
        journal.begin_step(task["task_id"], "side_effect", lease_epoch=old_claim["lease_epoch"])

        def recover_after_scan(owner_id):
            replacement.interrupt_task(task["task_id"], "another recovery won")
            return _owner_process_alive(owner_id)

        with patch("core.task_journal._owner_process_alive", side_effect=recover_after_scan):
            recovered = journal.recover_orphaned_tasks()

        self.assertEqual(recovered, [])
        current = journal.get_task(task["task_id"])
        self.assertEqual(current["status"], "interrupted")
        self.assertEqual(current["lease_epoch"], old_claim["lease_epoch"] + 1)
        self.assertEqual(len(current["steps"][0]["attempt_history"]), 1)

    def test_stale_worker_cannot_commit_after_lease_recovery(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)
        task = journal.create_task("test.workflow", {}, ["side_effect"])

        old_claim = journal.claim_task(task["task_id"], "not-a-live-process:old")
        old_epoch = old_claim["lease_epoch"]
        self.assertTrue(journal.begin_step(task["task_id"], "side_effect", lease_epoch=old_epoch))

        self.assertEqual(journal.recover_orphaned_tasks(), [task["task_id"]])
        new_claim = journal.claim_task(task["task_id"], "not-a-live-process:new")
        new_epoch = new_claim["lease_epoch"]
        self.assertGreater(new_epoch, old_epoch)
        self.assertTrue(journal.begin_step(task["task_id"], "side_effect", lease_epoch=new_epoch))

        with self.assertRaises(TaskLeaseLostError):
            journal.complete_step(
                task["task_id"],
                "side_effect",
                {"artifact_id": "old"},
                lease_epoch=old_epoch,
            )

        journal.complete_step(
            task["task_id"],
            "side_effect",
            {"artifact_id": "new"},
            lease_epoch=new_epoch,
        )
        with self.assertRaises(TaskLeaseLostError):
            journal.complete_task(
                task["task_id"],
                {"artifact_id": "old"},
                lease_epoch=old_epoch,
            )

        completed = journal.complete_task(
            task["task_id"],
            {"artifact_id": "new"},
            lease_epoch=new_epoch,
        )
        self.assertEqual(completed["result"], {"artifact_id": "new"})

    def test_worker_without_lease_epoch_cannot_commit_after_lease_recovery(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)
        task = journal.create_task("test.workflow", {}, ["side_effect"])

        old_claim = journal.claim_task(task["task_id"], "not-a-live-process:old")
        self.assertTrue(
            journal.begin_step(
                task["task_id"],
                "side_effect",
                lease_epoch=old_claim["lease_epoch"],
            )
        )
        self.assertEqual(journal.recover_orphaned_tasks(), [task["task_id"]])

        new_claim = journal.claim_task(task["task_id"], "not-a-live-process:new")
        self.assertTrue(
            journal.begin_step(
                task["task_id"],
                "side_effect",
                lease_epoch=new_claim["lease_epoch"],
            )
        )
        with self.assertRaises(TaskLeaseLostError):
            journal.complete_step(task["task_id"], "side_effect", {"artifact_id": "unfenced"})

        current = journal.get_task(task["task_id"])
        self.assertEqual(current["steps"][0]["status"], "running")

    def test_interrupted_task_fences_old_worker_before_reclaim(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)
        task = journal.create_task("test.workflow", {}, ["side_effect"])

        old_claim = journal.claim_task(task["task_id"], "not-a-live-process:old")
        old_epoch = old_claim["lease_epoch"]
        self.assertTrue(journal.begin_step(task["task_id"], "side_effect", lease_epoch=old_epoch))
        self.assertEqual(journal.recover_orphaned_tasks(), [task["task_id"]])

        with self.assertRaises(TaskLeaseLostError):
            journal.complete_step(
                task["task_id"],
                "side_effect",
                {"artifact_id": "old"},
                lease_epoch=old_epoch,
            )
        with self.assertRaises(TaskLeaseLostError):
            journal.complete_task(
                task["task_id"],
                {"artifact_id": "old"},
                lease_epoch=old_epoch,
            )

        current = journal.get_task(task["task_id"])
        self.assertEqual(current["status"], "interrupted")
        self.assertEqual(current["steps"][0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
