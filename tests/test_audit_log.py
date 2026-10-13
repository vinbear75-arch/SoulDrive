import json
import multiprocessing
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core.audit_log import AuditLogger, append_audit_event, append_audit_event_best_effort
from core.observability import runtime_metrics


def _audit_process_worker(audit_path: str, state_path: str, barrier, worker_id: int):
    barrier.wait(timeout=10)
    logger = AuditLogger(audit_path, state_path)
    for attempt in range(5):
        logger.append_event("process.step", {"worker": worker_id, "attempt": attempt})


class AuditLogTests(unittest.TestCase):
    def test_best_effort_audit_failure_is_observable_without_raising(self):
        class FailingAuditLogger:
            def append_event(self, *_args, **_kwargs):
                raise OSError("audit-secret-query should not escape")

        before = runtime_metrics.snapshot()["audit_failures"]
        self.assertFalse(
            append_audit_event_best_effort(
                FailingAuditLogger(),
                "test.audit_failure",
                {"query": "audit-secret-query"},
            )
        )
        self.assertEqual(runtime_metrics.snapshot()["audit_failures"], before + 1)

    def test_append_audit_event_hash_chains_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.jsonl"
            state_path = Path(temp_dir) / "audit_state.json"

            first = append_audit_event(
                "runtime.unlock",
                {"hardware_sn": "SECRET-SN", "auth_level": "PRO"},
                audit_path=audit_path,
                state_path=state_path,
            )
            second = append_audit_event(
                "chat.completed",
                {"trace_id": "trace-1", "citations": [{"id": "E1"}]},
                audit_path=audit_path,
                state_path=state_path,
            )

            records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(records), 2)
        self.assertEqual(second["previous_hash"], first["event_hash"])
        self.assertNotIn("SECRET-SN", json.dumps(records, ensure_ascii=False))
        self.assertIn("hardware_sn_hash", records[0]["payload"])

    def test_state_write_failure_does_not_fork_the_audit_chain(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.jsonl"
            state_path = Path(temp_dir) / "audit_state.json"
            logger = AuditLogger(audit_path, state_path)
            logger.append_event("first", {"step": 1})

            with patch.object(
                logger,
                "_write_state",
                side_effect=[OSError("state unavailable"), None],
            ):
                with self.assertRaises(OSError):
                    logger.append_event("second", {"step": 2})
                logger.append_event("third", {"step": 3})

            report = logger.verify_chain()

        self.assertTrue(report["ready"], report)
        self.assertEqual(report["event_count"], 3)

    def test_audit_state_snapshot_failure_preserves_previous_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.jsonl"
            state_path = Path(temp_dir) / "audit_state.json"
            logger = AuditLogger(audit_path, state_path)
            logger.append_event("first", {"step": 1})
            original = state_path.read_bytes()

            with patch("core.atomic_file.os.fsync", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    logger._write_state("new-hash")

            self.assertEqual(state_path.read_bytes(), original)
            self.assertEqual(list(state_path.parent.glob(".audit_state.json.*.tmp")), [])

    def test_append_refuses_to_extend_audit_log_after_invalid_tail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.jsonl"
            state_path = Path(temp_dir) / "audit_state.json"
            logger = AuditLogger(audit_path, state_path)
            logger.append_event("first", {"step": 1})
            with audit_path.open("a", encoding="utf-8") as file:
                file.write("{broken-json\n")

            with self.assertRaisesRegex(ValueError, "audit log tail"):
                logger.append_event("second", {"step": 2})

    def test_concurrent_appenders_keep_hash_chain_intact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.jsonl"
            state_path = Path(temp_dir) / "audit_state.json"
            start = threading.Barrier(4)
            errors = []

            def worker(worker_id):
                try:
                    start.wait(timeout=5)
                    logger = AuditLogger(audit_path, state_path)
                    for attempt in range(4):
                        logger.append_event("worker.step", {"worker": worker_id, "attempt": attempt})
                except BaseException as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(worker_id,)) for worker_id in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            report = AuditLogger(audit_path, state_path).verify_chain()

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertTrue(report["ready"], report)
        self.assertEqual(report["event_count"], 16)

    def test_cross_process_appenders_keep_hash_chain_intact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.jsonl"
            state_path = Path(temp_dir) / "audit_state.json"
            context = multiprocessing.get_context("spawn")
            worker_count = 3
            barrier = context.Barrier(worker_count)
            processes = [
                context.Process(
                    target=_audit_process_worker,
                    args=(str(audit_path), str(state_path), barrier, worker_id),
                )
                for worker_id in range(worker_count)
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=30)

            report = AuditLogger(audit_path, state_path).verify_chain()

        self.assertFalse(any(process.is_alive() for process in processes))
        self.assertEqual([process.exitcode for process in processes], [0] * worker_count)
        self.assertTrue(report["ready"], report)
        self.assertEqual(report["event_count"], worker_count * 5)

    def test_verify_chain_reports_valid_audit_log(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.jsonl"
            state_path = Path(temp_dir) / "audit_state.json"
            logger = AuditLogger(audit_path, state_path)
            logger.append_event("runtime.unlock", {"auth_level": "PRO"})
            logger.append_event("chat.completed", {"citations": [{"id": "E1"}]})

            report = logger.verify_chain()

        self.assertTrue(report["ready"])
        self.assertEqual(report["event_count"], 2)
        self.assertEqual(report["checked_events"], 2)
        self.assertIsNone(report["broken_at"])
        self.assertNotIn("audit_path", report)
        self.assertTrue(report["audit_log"].startswith("audit_log:"))
        self.assertNotIn(str(audit_path), json.dumps(report, ensure_ascii=False))

    def test_verify_chain_detects_tampering(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.jsonl"
            state_path = Path(temp_dir) / "audit_state.json"
            logger = AuditLogger(audit_path, state_path)
            logger.append_event("runtime.unlock", {"auth_level": "PRO"})
            records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
            records[0]["payload"]["auth_level"] = "ADMIN"
            audit_path.write_text(json.dumps(records[0], ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

            report = logger.verify_chain()

        self.assertFalse(report["ready"])
        self.assertEqual(report["broken_at"]["reason"], "event_hash_mismatch")
        self.assertNotIn("audit_path", report)

    def test_verify_chain_detects_invalid_lines(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.jsonl"
            state_path = Path(temp_dir) / "audit_state.json"
            logger = AuditLogger(audit_path, state_path)
            logger.append_event("runtime.unlock", {"auth_level": "PRO"})
            with audit_path.open("a", encoding="utf-8") as file:
                file.write("{broken-json\n")

            report = logger.verify_chain()

        self.assertFalse(report["ready"])
        self.assertEqual(report["invalid_lines"], [2])
        self.assertNotIn("audit_path", report)

    def test_default_audit_logger_follows_app_runtime_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"SOULDRIVE_APP_ROOT": temp_dir}, clear=False):
                event = append_audit_event("runtime.unlock", {"auth_level": "PRO"})
                audit_path = Path(temp_dir) / "runtime" / "audit_log.jsonl"
                self.assertTrue(audit_path.exists())
                self.assertFalse((Path(temp_dir) / "souldrive_db").exists())

        self.assertEqual(event["event_type"], "runtime.unlock")


if __name__ == "__main__":
    unittest.main()
