import base64
import hashlib
import json
import os
import sqlite3
import time
from typing import Any, Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


TASK_STATUSES = {"pending", "running", "interrupted", "completed", "failed", "cancelled"}
STEP_STATUSES = {"pending", "running", "completed", "failed"}


class TaskBusyError(RuntimeError):
    pass


class TaskJournal:
    def __init__(self, workspace, keys):
        self.workspace = workspace
        self.keys = keys
        self.db_path = workspace.task_journal_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            PRAGMA journal_mode = WAL;
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS runtime_tasks (
                task_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                current_step TEXT,
                owner_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                completed_at REAL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                input_nonce TEXT NOT NULL,
                input_ciphertext TEXT NOT NULL,
                result_nonce TEXT,
                result_ciphertext TEXT,
                CHECK (status IN ('pending', 'running', 'interrupted', 'completed', 'failed', 'cancelled'))
            );

            CREATE TABLE IF NOT EXISTS runtime_task_steps (
                task_id TEXT NOT NULL REFERENCES runtime_tasks(task_id) ON DELETE CASCADE,
                step_name TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                idempotency_key TEXT NOT NULL,
                started_at REAL,
                completed_at REAL,
                error TEXT,
                output_nonce TEXT,
                output_ciphertext TEXT,
                PRIMARY KEY (task_id, step_name),
                UNIQUE (task_id, ordinal),
                CHECK (status IN ('pending', 'running', 'completed', 'failed'))
            );

            CREATE INDEX IF NOT EXISTS runtime_tasks_status_idx
            ON runtime_tasks(status, updated_at DESC);
            """
        )
        self.conn.commit()

    def create_task(
        self,
        kind: str,
        inputs: dict[str, Any],
        steps: list[str] | tuple[str, ...],
        *,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_steps = [str(step).strip() for step in steps if str(step).strip()]
        if not kind or not normalized_steps or len(normalized_steps) != len(set(normalized_steps)):
            raise ValueError("task kind and unique steps are required")
        task_id = task_id or f"task_{os.urandom(10).hex()}"
        now = time.time()
        input_nonce, input_ciphertext = self._encrypt(
            inputs,
            self._aad("task-input", task_id),
        )
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO runtime_tasks (
                    task_id, kind, status, created_at, updated_at,
                    input_nonce, input_ciphertext
                ) VALUES (?, ?, 'pending', ?, ?, ?, ?)
                """,
                (task_id, kind, now, now, input_nonce, input_ciphertext),
            )
            input_digest = _digest(inputs)
            for ordinal, step_name in enumerate(normalized_steps):
                idempotency_key = hashlib.sha256(
                    f"{kind}:{task_id}:{input_digest}:{ordinal}:{step_name}".encode("utf-8")
                ).hexdigest()
                self.conn.execute(
                    """
                    INSERT INTO runtime_task_steps (
                        task_id, step_name, ordinal, idempotency_key
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (task_id, step_name, ordinal, idempotency_key),
                )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM runtime_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown task: {task_id}")
        task = dict(row)
        task["inputs"] = self._decrypt(
            task.pop("input_nonce"),
            task.pop("input_ciphertext"),
            self._aad("task-input", task_id),
        )
        result_nonce = task.pop("result_nonce")
        result_ciphertext = task.pop("result_ciphertext")
        task["result"] = (
            self._decrypt(result_nonce, result_ciphertext, self._aad("task-result", task_id))
            if result_nonce and result_ciphertext
            else None
        )
        step_rows = self.conn.execute(
            """
            SELECT * FROM runtime_task_steps
            WHERE task_id = ?
            ORDER BY ordinal ASC
            """,
            (task_id,),
        ).fetchall()
        steps = []
        for step_row in step_rows:
            step = dict(step_row)
            output_nonce = step.pop("output_nonce")
            output_ciphertext = step.pop("output_ciphertext")
            step["output"] = (
                self._decrypt(
                    output_nonce,
                    output_ciphertext,
                    self._aad("task-step", task_id, step["step_name"]),
                )
                if output_nonce and output_ciphertext
                else None
            )
            steps.append(step)
        task["steps"] = steps
        task["progress"] = {
            "completed": sum(1 for step in steps if step["status"] == "completed"),
            "total": len(steps),
        }
        task["recoverable"] = task["status"] in {"pending", "interrupted", "failed"}
        return task

    def public_task(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        return {
            key: task.get(key)
            for key in (
                "task_id",
                "kind",
                "status",
                "current_step",
                "created_at",
                "updated_at",
                "started_at",
                "completed_at",
                "attempt_count",
                "error",
                "result",
                "progress",
                "recoverable",
            )
        } | {
            "steps": [
                {
                    key: step.get(key)
                    for key in (
                        "step_name",
                        "ordinal",
                        "status",
                        "attempt_count",
                        "started_at",
                        "completed_at",
                        "error",
                    )
                }
                for step in task["steps"]
            ]
        }

    def list_tasks(self, *, kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if kind:
            rows = self.conn.execute(
                "SELECT task_id FROM runtime_tasks WHERE kind = ? ORDER BY updated_at DESC LIMIT ?",
                (kind, max(1, min(int(limit), 200))),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT task_id FROM runtime_tasks ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [self.public_task(row[0]) for row in rows]

    def claim_task(self, task_id: str, owner_id: str) -> dict[str, Any]:
        now = time.time()
        with self.conn:
            row = self.conn.execute(
                "SELECT status, started_at FROM runtime_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown task: {task_id}")
            if row["status"] == "completed":
                return self.get_task(task_id)
            if row["status"] == "cancelled":
                raise ValueError("cancelled task cannot be resumed")
            if row["status"] == "running":
                raise TaskBusyError(f"task already running: {task_id}")
            self.conn.execute(
                """
                UPDATE runtime_tasks
                SET status = 'running', owner_id = ?, updated_at = ?,
                    started_at = COALESCE(started_at, ?), attempt_count = attempt_count + 1,
                    error = NULL
                WHERE task_id = ?
                """,
                (owner_id, now, now, task_id),
            )
        return self.get_task(task_id)

    def recover_orphaned_tasks(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT task_id, owner_id, updated_at FROM runtime_tasks WHERE status = 'running'"
        ).fetchall()
        recovered = []
        for row in rows:
            if _owner_process_alive(row["owner_id"]) and time.time() - float(row["updated_at"] or 0) < 900:
                continue
            self.interrupt_task(row["task_id"], "worker process ended before task completion")
            recovered.append(row["task_id"])
        return recovered

    def interrupt_task(self, task_id: str, reason: str = "worker interrupted") -> dict[str, Any]:
        now = time.time()
        with self.conn:
            self.conn.execute(
                """
                UPDATE runtime_task_steps
                SET status = 'failed', completed_at = ?, error = ?
                WHERE task_id = ? AND status = 'running'
                """,
                (now, reason[:500], task_id),
            )
            self.conn.execute(
                """
                UPDATE runtime_tasks
                SET status = 'interrupted', owner_id = NULL, updated_at = ?, error = ?
                WHERE task_id = ? AND status = 'running'
                """,
                (now, reason[:500], task_id),
            )
        return self.get_task(task_id)

    def begin_step(self, task_id: str, step_name: str) -> bool:
        now = time.time()
        with self.conn:
            row = self.conn.execute(
                """
                SELECT status FROM runtime_task_steps
                WHERE task_id = ? AND step_name = ?
                """,
                (task_id, step_name),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown task step: {step_name}")
            if row["status"] == "completed":
                return False
            self.conn.execute(
                """
                UPDATE runtime_task_steps
                SET status = 'running', started_at = ?, completed_at = NULL,
                    attempt_count = attempt_count + 1, error = NULL
                WHERE task_id = ? AND step_name = ?
                """,
                (now, task_id, step_name),
            )
            self.conn.execute(
                """
                UPDATE runtime_tasks
                SET current_step = ?, updated_at = ?
                WHERE task_id = ? AND status = 'running'
                """,
                (step_name, now, task_id),
            )
        return True

    def complete_step(self, task_id: str, step_name: str, output: dict[str, Any]) -> None:
        nonce, ciphertext = self._encrypt(
            output,
            self._aad("task-step", task_id, step_name),
        )
        now = time.time()
        with self.conn:
            self.conn.execute(
                """
                UPDATE runtime_task_steps
                SET status = 'completed', completed_at = ?, error = NULL,
                    output_nonce = ?, output_ciphertext = ?
                WHERE task_id = ? AND step_name = ?
                """,
                (now, nonce, ciphertext, task_id, step_name),
            )
            self.conn.execute(
                "UPDATE runtime_tasks SET updated_at = ? WHERE task_id = ?",
                (now, task_id),
            )

    def fail_step(self, task_id: str, step_name: str, error: str) -> None:
        now = time.time()
        message = str(error or "task step failed")[:500]
        with self.conn:
            self.conn.execute(
                """
                UPDATE runtime_task_steps
                SET status = 'failed', completed_at = ?, error = ?
                WHERE task_id = ? AND step_name = ?
                """,
                (now, message, task_id, step_name),
            )
            self.conn.execute(
                """
                UPDATE runtime_tasks
                SET status = 'failed', owner_id = NULL, updated_at = ?, error = ?
                WHERE task_id = ?
                """,
                (now, message, task_id),
            )

    def complete_task(self, task_id: str, result: dict[str, Any]) -> dict[str, Any]:
        nonce, ciphertext = self._encrypt(result, self._aad("task-result", task_id))
        now = time.time()
        with self.conn:
            pending = self.conn.execute(
                "SELECT COUNT(*) FROM runtime_task_steps WHERE task_id = ? AND status != 'completed'",
                (task_id,),
            ).fetchone()[0]
            if pending:
                raise ValueError("cannot complete task with unfinished steps")
            self.conn.execute(
                """
                UPDATE runtime_tasks
                SET status = 'completed', current_step = NULL, owner_id = NULL,
                    updated_at = ?, completed_at = ?, error = NULL,
                    result_nonce = ?, result_ciphertext = ?
                WHERE task_id = ?
                """,
                (now, now, nonce, ciphertext, task_id),
            )
        return self.get_task(task_id)

    def run_steps(
        self,
        task_id: str,
        owner_id: str,
        handlers: dict[str, Callable[[dict[str, Any], dict[str, dict[str, Any]]], dict[str, Any]]],
        result_builder: Callable[[dict[str, dict[str, Any]]], dict[str, Any]],
    ) -> dict[str, Any]:
        task = self.claim_task(task_id, owner_id)
        if task["status"] == "completed":
            return task
        inputs = task["inputs"]
        outputs = {
            step["step_name"]: step["output"]
            for step in task["steps"]
            if step["status"] == "completed" and step["output"] is not None
        }
        for step in task["steps"]:
            step_name = step["step_name"]
            if step["status"] == "completed":
                continue
            handler = handlers.get(step_name)
            if handler is None:
                self.fail_step(task_id, step_name, f"missing task step handler: {step_name}")
                return self.get_task(task_id)
            self.begin_step(task_id, step_name)
            try:
                output = handler(inputs, dict(outputs))
                if not isinstance(output, dict):
                    raise TypeError("task step output must be an object")
            except Exception as exc:
                self.fail_step(task_id, step_name, str(exc))
                return self.get_task(task_id)
            self.complete_step(task_id, step_name, output)
            outputs[step_name] = output
        try:
            result = result_builder(outputs)
            return self.complete_task(task_id, result)
        except Exception as exc:
            current_step = self.get_task(task_id).get("current_step") or "finalize"
            if current_step != "finalize":
                self.fail_step(task_id, current_step, str(exc))
            else:
                now = time.time()
                with self.conn:
                    self.conn.execute(
                        "UPDATE runtime_tasks SET status = 'failed', owner_id = NULL, updated_at = ?, error = ? WHERE task_id = ?",
                        (now, str(exc)[:500], task_id),
                    )
            return self.get_task(task_id)

    def _encrypt(self, payload: dict[str, Any], aad: bytes) -> tuple[str, str]:
        nonce = os.urandom(12)
        plaintext = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ciphertext = AESGCM(self.keys.metadata_key).encrypt(nonce, plaintext, aad)
        return _b64(nonce), _b64(ciphertext)

    def _decrypt(self, nonce: str, ciphertext: str, aad: bytes) -> dict[str, Any]:
        try:
            plaintext = AESGCM(self.keys.metadata_key).decrypt(
                _unb64(nonce),
                _unb64(ciphertext),
                aad,
            )
        except (InvalidTag, ValueError, TypeError) as exc:
            raise ValueError("task checkpoint authentication failed") from exc
        payload = json.loads(plaintext.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("task checkpoint payload must be an object")
        return payload

    @staticmethod
    def _aad(kind: str, task_id: str, step_name: str | None = None) -> bytes:
        parts = ["SoulDrive", "task-journal-v1", kind, task_id]
        if step_name:
            parts.append(step_name)
        return ":".join(parts).encode("utf-8")


def _digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))


def _owner_process_alive(owner_id: str | None) -> bool:
    try:
        process_id = int(str(owner_id or "").split(":", 1)[0])
    except (TypeError, ValueError):
        return False
    try:
        import psutil

        return psutil.pid_exists(process_id)
    except Exception:
        try:
            os.kill(process_id, 0)
            return True
        except OSError:
            return False
