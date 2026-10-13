import base64
import hashlib
import json
import os
import sqlite3
import threading
import time
from typing import Any, Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from core.skill_runtime import SUPPORTED_WORKFLOW_IDS


TASK_STATUSES = {"pending", "running", "interrupted", "completed", "failed", "cancelled"}
STEP_STATUSES = {"pending", "running", "completed", "failed"}


def _validate_workflow_input(value: Any) -> None:
    if not isinstance(value, dict):
        return
    workflow_id = str(value.get("workflow_id") or "").strip()
    if workflow_id and workflow_id not in SUPPORTED_WORKFLOW_IDS:
        raise ValueError("unsupported workflow_id")


class TaskBusyError(RuntimeError):
    pass


class TaskDefinitionMismatchError(RuntimeError):
    """Raised before recovery when the current worker cannot run a task step."""


class TaskLeaseLostError(RuntimeError):
    """Raised when a recovered worker tries to commit with an old lease epoch."""


class TaskPublicError(RuntimeError):
    """A deliberately approved, user-safe task failure message.

    Ordinary exceptions are fingerprinted before they reach durable storage.
    Handlers may use this type only when the message is known not to contain
    backend secrets or private diagnostics.
    """

    def __init__(self, public_message: str):
        message = str(public_message or "").strip()
        if not message:
            raise ValueError("public task error message is required")
        self.public_message = message[:500]
        super().__init__(self.public_message)


def _public_error_observation(error: Any) -> dict[str, Any] | None:
    """Expose bounded task-failure correlation without returning error text."""
    return _task_error_observation(error)


def _task_error_observation(error: Any) -> dict[str, Any] | None:
    """Return a bounded task-error fingerprint that contains no error text."""
    if error is None or (not isinstance(error, dict) and not str(error or "")):
        return None
    if isinstance(error, dict):
        error_type = str(error.get("error_type") or "UnknownError")[:96]
        error_digest = str(error.get("error_digest") or "")
        error_chars = error.get("error_chars")
        if (
            len(error_digest) == 16
            and all(character in "0123456789abcdef" for character in error_digest.lower())
            and isinstance(error_chars, int)
            and error_chars >= 0
        ):
            observation = {
                "error_type": error_type,
                "error_digest": error_digest.lower(),
                "error_chars": error_chars,
            }
            public_message = error.get("public_message")
            if error_type == "TaskPublicError" and isinstance(public_message, str):
                public_message = public_message.strip()
                if public_message:
                    observation["public_message"] = public_message[:500]
            return observation
        error = json.dumps(error, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    else:
        try:
            decoded = json.loads(str(error))
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = None
        if isinstance(decoded, dict):
            return _task_error_observation(decoded)

    message = str(error or "")
    if not message:
        return None
    observation = {
        "error_type": (
            type(error).__name__ if isinstance(error, BaseException) else "UnknownError"
        )[:96],
        "error_digest": hashlib.sha256(message.encode("utf-8")).hexdigest()[:16],
        "error_chars": len(message),
    }
    if isinstance(error, TaskPublicError):
        observation["public_message"] = error.public_message
    return observation


def _serialize_task_error(error: Any) -> str | None:
    observation = _task_error_observation(error)
    if observation is None:
        return None
    return json.dumps(observation, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _public_error_message(error: Any) -> str | None:
    observation = _public_error_observation(error)
    if not observation:
        return None
    return str(observation.get("public_message") or "任务执行失败，请查看运行诊断。")


def process_owner_id(nonce: str | None = None) -> str:
    """Return a worker identity that distinguishes PID reuse when possible."""
    try:
        import psutil

        started_at = f"{psutil.Process(os.getpid()).create_time():.6f}"
    except Exception:
        started_at = "unknown"
    parts = [str(os.getpid()), started_at]
    if nonce:
        parts.append(str(nonce))
    return ":".join(parts)


class TaskJournal:
    def __init__(self, workspace, keys):
        self.workspace = workspace
        self.keys = keys
        self.db_path = workspace.task_journal_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.RLock()
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
                idempotency_key TEXT,
                input_digest TEXT,
                repetition_digest TEXT,
                repetition_basis TEXT NOT NULL DEFAULT 'full_inputs',
                status TEXT NOT NULL,
                current_step TEXT,
                owner_id TEXT,
                lease_epoch INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                completed_at REAL,
                cancel_requested_at REAL,
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

            CREATE TABLE IF NOT EXISTS runtime_task_step_attempts (
                task_id TEXT NOT NULL,
                step_name TEXT NOT NULL,
                attempt INTEGER NOT NULL CHECK (attempt >= 1),
                status TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                lease_epoch INTEGER,
                started_at REAL NOT NULL,
                completed_at REAL,
                error TEXT,
                PRIMARY KEY (task_id, step_name, attempt),
                FOREIGN KEY (task_id, step_name)
                    REFERENCES runtime_task_steps(task_id, step_name)
                    ON DELETE CASCADE,
                CHECK (status IN ('running', 'completed', 'failed', 'interrupted', 'cancelled'))
            );

            CREATE INDEX IF NOT EXISTS runtime_task_step_attempts_lookup_idx
            ON runtime_task_step_attempts(task_id, step_name, attempt ASC);

            CREATE INDEX IF NOT EXISTS runtime_tasks_status_idx
            ON runtime_tasks(status, updated_at DESC);
            """
        )
        columns = {
            str(row[1])
            for row in self.conn.execute("PRAGMA table_info(runtime_tasks)").fetchall()
        }
        for name, declaration in (
            ("idempotency_key", "TEXT"),
            ("input_digest", "TEXT"),
            ("repetition_digest", "TEXT"),
            ("repetition_basis", "TEXT NOT NULL DEFAULT 'full_inputs'"),
            ("cancel_requested_at", "REAL"),
            ("lease_epoch", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                self.conn.execute(f"ALTER TABLE runtime_tasks ADD COLUMN {name} {declaration}")
        self.conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS runtime_tasks_idempotency_idx
            ON runtime_tasks(kind, idempotency_key)
            WHERE idempotency_key IS NOT NULL
            """
        )
        self._sanitize_legacy_errors()
        self.conn.commit()

    def _sanitize_legacy_errors(self) -> None:
        """Migrate pre-fingerprint task errors without retaining their text."""
        for table in (
            "runtime_tasks",
            "runtime_task_steps",
            "runtime_task_step_attempts",
        ):
            rows = self.conn.execute(
                f"SELECT rowid, error FROM {table} WHERE error IS NOT NULL AND error <> ''"
            ).fetchall()
            updates = [
                (_serialize_task_error(row["error"]), row["rowid"])
                for row in rows
            ]
            if updates:
                self.conn.executemany(
                    f"UPDATE {table} SET error = ? WHERE rowid = ?",
                    updates,
                )

    def create_task(
        self,
        kind: str,
        inputs: dict[str, Any],
        steps: list[str] | tuple[str, ...],
        *,
        task_id: str | None = None,
        idempotency_key: str | None = None,
        repetition_inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_steps = [str(step).strip() for step in steps if str(step).strip()]
        if not kind or not normalized_steps or len(normalized_steps) != len(set(normalized_steps)):
            raise ValueError("task kind and unique steps are required")
        _validate_workflow_input(inputs)
        if repetition_inputs is not None and not isinstance(repetition_inputs, dict):
            raise ValueError("repetition_inputs must be an object")
        _validate_workflow_input(repetition_inputs)
        idempotency_key = str(idempotency_key or "").strip() or None
        if idempotency_key and len(idempotency_key) > 256:
            raise ValueError("idempotency_key exceeds 256 characters")
        task_id = task_id or f"task_{os.urandom(10).hex()}"
        now = time.time()
        task_input_digest = _digest({"inputs": inputs, "steps": normalized_steps})
        repetition_basis = "business_projection" if repetition_inputs is not None else "full_inputs"
        repetition_digest = _digest({
            "inputs": repetition_inputs if repetition_inputs is not None else inputs,
            "steps": normalized_steps,
        })
        input_nonce, input_ciphertext = self._encrypt(
            inputs,
            self._aad("task-input", task_id),
        )
        with self._lock, self.conn:
            insert_sql = """
                INSERT INTO runtime_tasks (
                    task_id, kind, idempotency_key, input_digest,
                    repetition_digest, repetition_basis, status,
                    created_at, updated_at, input_nonce, input_ciphertext
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            """
            insert_values = (
                task_id,
                kind,
                idempotency_key,
                task_input_digest,
                repetition_digest,
                repetition_basis,
                now,
                now,
                input_nonce,
                input_ciphertext,
            )
            if idempotency_key:
                # The unique index is the serialization point.  An atomic
                # insert avoids a check-then-insert race across worker
                # processes sharing this SQLite journal.
                inserted = self.conn.execute(
                    insert_sql.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1),
                    insert_values,
                ).rowcount == 1
                if not inserted:
                    existing = self.conn.execute(
                        """
                        SELECT task_id, input_digest
                        FROM runtime_tasks
                        WHERE kind = ? AND idempotency_key = ?
                        """,
                        (kind, idempotency_key),
                    ).fetchone()
                    if existing is not None:
                        if str(existing["input_digest"] or "") != task_input_digest:
                            raise ValueError("idempotency_key is already bound to different task input")
                        return self.get_task(str(existing["task_id"]))
                    # OR IGNORE also suppresses an unrelated constraint
                    # failure (for example an explicitly reused task_id).
                    # Re-run the strict insert so that callers still receive
                    # the original SQLite integrity error in that case.
                    self.conn.execute(insert_sql, insert_values)
            else:
                self.conn.execute(insert_sql, insert_values)
            input_digest = _digest(inputs)
            for ordinal, step_name in enumerate(normalized_steps):
                step_idempotency_key = hashlib.sha256(
                    f"{kind}:{task_id}:{input_digest}:{ordinal}:{step_name}".encode("utf-8")
                ).hexdigest()
                self.conn.execute(
                    """
                    INSERT INTO runtime_task_steps (
                        task_id, step_name, ordinal, idempotency_key
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (task_id, step_name, ordinal, step_idempotency_key),
                )
        return self.get_task(task_id)

    def repetition_guard(
        self,
        kind: str,
        inputs: dict[str, Any],
        steps: list[str] | tuple[str, ...],
        *,
        limit: int = 3,
        repetition_inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Warn when the same task input previously failed or was interrupted."""
        normalized_steps = [str(step).strip() for step in steps if str(step).strip()]
        if not kind or not normalized_steps or len(normalized_steps) != len(set(normalized_steps)):
            raise ValueError("task kind and unique steps are required")
        _validate_workflow_input(inputs)
        if repetition_inputs is not None and not isinstance(repetition_inputs, dict):
            raise ValueError("repetition_inputs must be an object")
        _validate_workflow_input(repetition_inputs)
        repetition_basis = "business_projection" if repetition_inputs is not None else "full_inputs"
        repetition_digest = _digest({
            "inputs": repetition_inputs if repetition_inputs is not None else inputs,
            "steps": normalized_steps,
        })
        return self._repetition_guard_for_digest(
            kind,
            repetition_digest,
            limit=limit,
            basis=repetition_basis,
        )

    def _repetition_guard_for_digest(
        self,
        kind: str,
        repetition_digest: str,
        *,
        limit: int = 3,
        basis: str = "full_inputs",
    ) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit), 20))
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT task_id, status, current_step, updated_at, attempt_count, error
                FROM runtime_tasks
                WHERE kind = ?
                  AND (
                      repetition_digest = ?
                      OR (
                          ? = 'full_inputs'
                          AND repetition_digest IS NULL
                          AND input_digest = ?
                      )
                  )
                  AND status IN ('failed', 'interrupted')
                ORDER BY updated_at DESC, task_id ASC
                LIMIT ?
                """,
                (
                    str(kind),
                    str(repetition_digest),
                    str(basis or "full_inputs"),
                    str(repetition_digest),
                    safe_limit,
                ),
            ).fetchall()
        matches = []
        for row in rows:
            error_text = str(row["error"] or "").strip()
            matches.append(
                {
                    "task_id": str(row["task_id"]),
                    "status": str(row["status"]),
                    "current_step": str(row["current_step"] or "") or None,
                    "updated_at": row["updated_at"],
                    "attempt_count": int(row["attempt_count"] or 0),
                    "error_digest": _digest({"error": error_text}) if error_text else None,
                }
            )
        return {
            "schema_version": "souldrive_task_repetition_guard_v0.2",
            "decision": "warn" if matches else "allow",
            "reason": "prior_failed_same_input" if matches else None,
            "kind": str(kind),
            "input_digest": str(repetition_digest),
            "repetition_digest": str(repetition_digest),
            "basis": str(basis or "full_inputs"),
            "matches": matches,
        }

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
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
                attempt_rows = self.conn.execute(
                    """
                    SELECT attempt, status, idempotency_key, lease_epoch,
                           started_at, completed_at, error
                    FROM runtime_task_step_attempts
                    WHERE task_id = ? AND step_name = ?
                    ORDER BY attempt ASC
                    """,
                    (task_id, step["step_name"]),
                ).fetchall()
                step["attempt_history"] = [dict(attempt_row) for attempt_row in attempt_rows]
            task["steps"] = steps
            normalized_task_steps = [str(step.get("step_name") or "") for step in steps]
            stored_repetition_digest = str(task.get("repetition_digest") or "").strip()
            if stored_repetition_digest:
                task["repetition_guard"] = self._repetition_guard_for_digest(
                    str(task.get("kind") or ""),
                    stored_repetition_digest,
                    basis=str(task.get("repetition_basis") or "full_inputs"),
                )
            else:
                task["repetition_guard"] = self.repetition_guard(
                    str(task.get("kind") or ""),
                    task["inputs"],
                    normalized_task_steps,
                )
            task["progress"] = {
                "completed": sum(1 for step in steps if step["status"] == "completed"),
                "total": len(steps),
            }
            task["cancellation_pending"] = (
                task["status"] == "running" and task.get("cancel_requested_at") is not None
            )
            task["recoverable"] = (
                task["status"] in {"pending", "interrupted", "failed"}
                and task.get("cancel_requested_at") is None
            )
            return task

    def public_task(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        public_result = task.get("result")
        if isinstance(public_result, dict) and "evidence_snapshot" in public_result:
            public_result = dict(public_result)
            public_result.pop("evidence_snapshot", None)
            public_result["evidence_snapshot_available"] = True
        public = {
            key: task.get(key)
            for key in (
                "task_id",
                "kind",
                "status",
                "current_step",
                "created_at",
                "updated_at",
                "started_at",
                "lease_epoch",
                "completed_at",
                "cancel_requested_at",
                "attempt_count",
                "result",
                "progress",
                "recoverable",
                "cancellation_pending",
                "repetition_guard",
            )
        }
        public["error"] = _public_error_message(task.get("error"))
        public["error_observation"] = _public_error_observation(task.get("error"))
        public["result"] = public_result
        for key in (
            "request_id",
            "plan_task_id",
            "plan_digest",
            "artifact_id",
            "project_id",
            "workflow_id",
            "source_filenames",
            "source_document_ids",
        ):
            if key in task["inputs"]:
                public[key] = task["inputs"].get(key)
        if task.get("kind") == "presentation.plan":
            public["phase"] = "outline_review"
        elif task.get("kind") == "presentation.generate":
            public["phase"] = "generation"
        return public | {
            "steps": [
                {
                    "step_name": step.get("step_name"),
                    "ordinal": step.get("ordinal"),
                    "status": step.get("status"),
                    "attempt_count": step.get("attempt_count"),
                    "started_at": step.get("started_at"),
                    "completed_at": step.get("completed_at"),
                    "error": _public_error_message(step.get("error")),
                    "error_observation": _public_error_observation(step.get("error")),
                    "attempt_history": [
                        {
                            "attempt": attempt.get("attempt"),
                            "status": attempt.get("status"),
                            "idempotency_key": attempt.get("idempotency_key"),
                            "lease_epoch": attempt.get("lease_epoch"),
                            "started_at": attempt.get("started_at"),
                            "completed_at": attempt.get("completed_at"),
                            "error": _public_error_message(attempt.get("error")),
                            "error_observation": _public_error_observation(attempt.get("error")),
                        }
                        for attempt in step.get("attempt_history") or []
                    ],
                }
                for step in task["steps"]
            ]
        }

    def list_tasks(
        self,
        *,
        kind: str | None = None,
        limit: int = 50,
        project_id: str | None = None,
        global_only: bool = False,
    ) -> list[dict[str, Any]]:
        normalized_project_id = str(project_id or "").strip() or None
        if normalized_project_id and global_only:
            raise ValueError("project_id and global_only cannot be combined")
        safe_limit = max(1, min(int(limit), 200))
        scoped = bool(normalized_project_id or global_only)
        with self._lock:
            select = "SELECT task_id, input_nonce, input_ciphertext FROM runtime_tasks"
            if kind:
                query = f"{select} WHERE kind = ? ORDER BY updated_at DESC, task_id ASC"
                params: tuple[Any, ...] = (kind,)
            else:
                query = f"{select} ORDER BY updated_at DESC, task_id ASC"
                params = ()
            if not scoped:
                query += " LIMIT ?"
                params = (*params, safe_limit)
            rows = self.conn.execute(query, params).fetchall()

        tasks = []
        for row in rows:
            if scoped:
                inputs = self._decrypt(
                    row["input_nonce"],
                    row["input_ciphertext"],
                    self._aad("task-input", row["task_id"]),
                )
                task_project_id = str(inputs.get("project_id") or "").strip()
                if normalized_project_id and task_project_id != normalized_project_id:
                    continue
                if global_only and task_project_id:
                    continue
            tasks.append(self.public_task(row["task_id"]))
            if len(tasks) >= safe_limit:
                break
        return tasks

    def claim_task(self, task_id: str, owner_id: str) -> dict[str, Any]:
        now = time.time()
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                "SELECT status, started_at, cancel_requested_at FROM runtime_tasks WHERE task_id = ?",
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
            if row["cancel_requested_at"] is not None:
                self.conn.execute(
                    """
                    UPDATE runtime_tasks
                    SET status = 'cancelled', owner_id = NULL, current_step = NULL,
                        completed_at = ?, updated_at = ?, error = ?
                    WHERE task_id = ?
                    """,
                    (now, now, _serialize_task_error("task cancelled before resume"), task_id),
                )
                return self.get_task(task_id)
            self.conn.execute(
                """
                UPDATE runtime_tasks
                SET status = 'running', owner_id = ?, updated_at = ?,
                    started_at = COALESCE(started_at, ?), attempt_count = attempt_count + 1,
                    error = NULL, lease_epoch = lease_epoch + 1
                WHERE task_id = ?
                """,
                (owner_id, now, now, task_id),
            )
        return self.get_task(task_id)

    def request_cancel(self, task_id: str) -> dict[str, Any]:
        """Request cancellation and apply it immediately when no step runs."""
        now = time.time()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT status, cancel_requested_at FROM runtime_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown task: {task_id}")
            status = str(row["status"])
            if status in {"completed", "failed", "cancelled"}:
                return self.get_task(task_id)
            if status == "running":
                self.conn.execute(
                    """
                    UPDATE runtime_tasks
                    SET cancel_requested_at = COALESCE(cancel_requested_at, ?),
                        updated_at = ?, error = ?
                    WHERE task_id = ? AND status = 'running'
                    """,
                    (now, now, _serialize_task_error("正在停止，等待当前步骤结束"), task_id),
                )
            else:
                self.conn.execute(
                    """
                    UPDATE runtime_tasks
                    SET status = 'cancelled', cancel_requested_at = COALESCE(cancel_requested_at, ?),
                        owner_id = NULL, current_step = NULL, completed_at = ?,
                        updated_at = ?, error = ?
                    WHERE task_id = ? AND status IN ('pending', 'interrupted')
                    """,
                    (now, now, now, _serialize_task_error("task cancelled before start"), task_id),
                )
        return self.get_task(task_id)

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        """Compatibility alias for callers that expose a cancel action."""
        return self.request_cancel(task_id)

    def recover_orphaned_tasks(self) -> list[str]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT task_id, owner_id, lease_epoch FROM runtime_tasks WHERE status = 'running'"
            ).fetchall()
        recovered = []
        for row in rows:
            # A live owner may be inside a long synchronous step whose
            # checkpoint timestamp cannot advance until the step returns.
            # Do not steal that lease merely because the checkpoint is old;
            # process death remains the recoverable orphan signal on this
            # single-machine journal.
            if _owner_process_alive(row["owner_id"]):
                continue
            try:
                self.interrupt_task(
                    row["task_id"],
                    "worker process ended before task completion",
                    lease_epoch=int(row["lease_epoch"] or 0),
                )
            except TaskLeaseLostError:
                continue
            recovered.append(row["task_id"])
        return recovered

    def interrupt_task(
        self,
        task_id: str,
        reason: str = "worker interrupted",
        *,
        lease_epoch: int | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            task_row = self.conn.execute(
                "SELECT status, cancel_requested_at, lease_epoch FROM runtime_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task_row is None:
                raise KeyError(f"unknown task: {task_id}")
            # Recovery may have observed an older owner before another
            # connection resumed this task. Fence that snapshot atomically.
            if lease_epoch is not None:
                self._assert_lease_epoch(task_id, lease_epoch, task_row)
                if task_row["status"] != "running":
                    raise TaskLeaseLostError(f"task is no longer running: {task_id}")
            if task_row["status"] != "running":
                return self.get_task(task_id)
            cancelled = task_row["cancel_requested_at"] is not None
            running_steps = self.conn.execute(
                """
                SELECT step_name, attempt_count
                FROM runtime_task_steps
                WHERE task_id = ? AND status = 'running'
                """,
                (task_id,),
            ).fetchall()
            self.conn.execute(
                """
                UPDATE runtime_task_steps
                SET status = 'failed', completed_at = ?, error = ?
                WHERE task_id = ? AND status = 'running'
                """,
                (now, _serialize_task_error(str(reason or "worker interrupted")[:500]), task_id),
            )
            attempt_status = "cancelled" if cancelled else "interrupted"
            serialized_reason = _serialize_task_error(str(reason or "worker interrupted")[:500])
            for step_row in running_steps:
                self.conn.execute(
                    """
                    UPDATE runtime_task_step_attempts
                    SET status = ?, completed_at = ?, error = ?
                    WHERE task_id = ? AND step_name = ? AND attempt = ?
                      AND status = 'running'
                    """,
                    (
                        attempt_status,
                        now,
                        serialized_reason,
                        task_id,
                        step_row["step_name"],
                        int(step_row["attempt_count"] or 0),
                    ),
                )
            self.conn.execute(
                """
                UPDATE runtime_tasks
                SET status = ?, owner_id = NULL, current_step = NULL, completed_at = ?,
                    updated_at = ?, error = ?, lease_epoch = lease_epoch + 1
                WHERE task_id = ? AND status = 'running'
                """,
                (
                    "cancelled" if cancelled else "interrupted",
                    now if cancelled else None,
                    now,
                    serialized_reason,
                    task_id,
                ),
            )
        return self.get_task(task_id)

    @staticmethod
    def _assert_lease_epoch(
        task_id: str,
        lease_epoch: int | None,
        task_row: sqlite3.Row,
    ) -> None:
        if lease_epoch is None:
            raise TaskLeaseLostError(f"task lease required: {task_id}")
        current_epoch = int(task_row["lease_epoch"] or 0)
        if current_epoch != int(lease_epoch):
            raise TaskLeaseLostError(
                f"task lease lost: {task_id} (expected {lease_epoch}, current {current_epoch})"
            )

    def begin_step(
        self,
        task_id: str,
        step_name: str,
        *,
        lease_epoch: int | None = None,
    ) -> bool:
        now = time.time()
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            task_row = self.conn.execute(
                "SELECT status, cancel_requested_at, lease_epoch FROM runtime_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task_row is None:
                raise KeyError(f"unknown task: {task_id}")
            self._assert_lease_epoch(task_id, lease_epoch, task_row)
            if task_row["status"] == "cancelled":
                return False
            if task_row["status"] == "running" and task_row["cancel_requested_at"] is not None:
                self.conn.execute(
                    """
                    UPDATE runtime_tasks
                    SET status = 'cancelled', owner_id = NULL, current_step = NULL,
                        completed_at = ?, updated_at = ?, error = ?
                    WHERE task_id = ? AND status = 'running'
                    """,
                    (now, now, _serialize_task_error("task cancelled before the next step"), task_id),
                )
                return False
            if task_row["status"] != "running":
                raise ValueError("task is not running")
            row = self.conn.execute(
                """
                SELECT status, attempt_count, idempotency_key FROM runtime_task_steps
                WHERE task_id = ? AND step_name = ?
                """,
                (task_id, step_name),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown task step: {step_name}")
            if row["status"] == "completed":
                return False
            attempt = int(row["attempt_count"] or 0) + 1
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
                INSERT INTO runtime_task_step_attempts (
                    task_id, step_name, attempt, status, idempotency_key,
                    lease_epoch, started_at
                ) VALUES (?, ?, ?, 'running', ?, ?, ?)
                """,
                (
                    task_id,
                    step_name,
                    attempt,
                    row["idempotency_key"],
                    int(task_row["lease_epoch"] or 0),
                    now,
                ),
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

    def complete_step(
        self,
        task_id: str,
        step_name: str,
        output: dict[str, Any],
        *,
        lease_epoch: int | None = None,
    ) -> None:
        nonce, ciphertext = self._encrypt(
            output,
            self._aad("task-step", task_id, step_name),
        )
        now = time.time()
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            task_row = self.conn.execute(
                "SELECT status, cancel_requested_at, lease_epoch FROM runtime_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task_row is None:
                raise KeyError(f"unknown task: {task_id}")
            self._assert_lease_epoch(task_id, lease_epoch, task_row)
            step_row = self.conn.execute(
                """
                SELECT status, attempt_count
                FROM runtime_task_steps
                WHERE task_id = ? AND step_name = ?
                """,
                (task_id, step_name),
            ).fetchone()
            if step_row is None:
                raise KeyError(f"unknown task step: {step_name}")
            # A durable completion is monotonic.  A late worker callback must
            # not replace a checkpoint that recovery will already reuse.
            if step_row["status"] == "completed":
                return
            if step_row["status"] != "running":
                raise ValueError("task step is not running")
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
                """
                UPDATE runtime_task_step_attempts
                SET status = 'completed', completed_at = ?, error = NULL
                WHERE task_id = ? AND step_name = ? AND attempt = ?
                  AND status = 'running'
                """,
                (now, task_id, step_name, int(step_row["attempt_count"] or 0)),
            )
            if task_row["status"] == "running" and task_row["cancel_requested_at"] is not None:
                self.conn.execute(
                    """
                    UPDATE runtime_tasks
                    SET status = 'cancelled', owner_id = NULL, current_step = NULL,
                        completed_at = ?, updated_at = ?, error = ?
                    WHERE task_id = ? AND status = 'running'
                    """,
                    (now, now, _serialize_task_error("task cancelled after current step"), task_id),
                )
            else:
                self.conn.execute(
                    "UPDATE runtime_tasks SET updated_at = ? WHERE task_id = ?",
                    (now, task_id),
                )

    def fail_step(
        self,
        task_id: str,
        step_name: str,
        error: Any,
        *,
        lease_epoch: int | None = None,
    ) -> None:
        now = time.time()
        message = str(error or "task step failed")[:500]
        serialized_message = _serialize_task_error(error if str(error or "") else message)
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            task_row = self.conn.execute(
                "SELECT status, cancel_requested_at, lease_epoch FROM runtime_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task_row is None:
                raise KeyError(f"unknown task: {task_id}")
            self._assert_lease_epoch(task_id, lease_epoch, task_row)
            step_row = self.conn.execute(
                """
                SELECT status, attempt_count
                FROM runtime_task_steps
                WHERE task_id = ? AND step_name = ?
                """,
                (task_id, step_name),
            ).fetchone()
            if step_row is None:
                raise KeyError(f"unknown task step: {step_name}")
            # Do not roll a successfully checkpointed step back to failed when
            # a stale worker reports an exception after its result was saved.
            if step_row["status"] == "completed":
                return
            self.conn.execute(
                """
                UPDATE runtime_task_steps
                SET status = 'failed', completed_at = ?, error = ?
                WHERE task_id = ? AND step_name = ?
                """,
                (now, serialized_message, task_id, step_name),
            )
            self.conn.execute(
                """
                UPDATE runtime_task_step_attempts
                SET status = 'failed', completed_at = ?, error = ?
                WHERE task_id = ? AND step_name = ? AND attempt = ?
                  AND status = 'running'
                """,
                (
                    now,
                    serialized_message,
                    task_id,
                    step_name,
                    int(step_row["attempt_count"] or 0),
                ),
            )
            status = "cancelled" if task_row["cancel_requested_at"] is not None else "failed"
            self.conn.execute(
                """
                UPDATE runtime_tasks
                SET status = ?, owner_id = NULL, current_step = NULL,
                    completed_at = CASE WHEN ? = 'cancelled' THEN ? ELSE completed_at END,
                    updated_at = ?, error = ?
                WHERE task_id = ? AND status IN ('running', 'pending', 'interrupted', 'failed')
                """,
                (status, status, now, now, serialized_message, task_id),
            )

    def complete_task(
        self,
        task_id: str,
        result: dict[str, Any],
        *,
        lease_epoch: int | None = None,
    ) -> dict[str, Any]:
        nonce, ciphertext = self._encrypt(result, self._aad("task-result", task_id))
        now = time.time()
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            task_row = self.conn.execute(
                "SELECT status, cancel_requested_at, lease_epoch FROM runtime_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task_row is None:
                raise KeyError(f"unknown task: {task_id}")
            if task_row["status"] == "completed":
                return self.get_task(task_id)
            self._assert_lease_epoch(task_id, lease_epoch, task_row)
            if task_row["status"] == "cancelled" or task_row["cancel_requested_at"] is not None:
                self.conn.execute(
                    """
                    UPDATE runtime_tasks
                    SET status = 'cancelled', owner_id = NULL, current_step = NULL,
                        completed_at = COALESCE(completed_at, ?), updated_at = ?,
                        error = COALESCE(error, ?)
                    WHERE task_id = ? AND status != 'completed'
                    """,
                    (now, now, _serialize_task_error("task cancelled before completion"), task_id),
                )
                return self.get_task(task_id)
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
        *,
        include_step_context: bool = False,
    ) -> dict[str, Any]:
        """Run unfinished steps and optionally expose their durable identity.

        The default two-argument handler contract remains compatible with
        existing callers.  When ``include_step_context`` is enabled, handlers
        receive a third mapping containing the stable step idempotency key and
        the current attempt number.  Business side effects can use that key
        for their own deduplication or result lookup; the journal itself does
        not claim exactly-once delivery to an external system.
        """
        existing = self.get_task(task_id)
        if existing["status"] == "completed":
            return existing
        missing_steps = [
            str(step.get("step_name") or "")
            for step in existing.get("steps", [])
            if step.get("status") != "completed"
            and (
                step.get("step_name") not in handlers
                or not callable(handlers.get(step.get("step_name")))
            )
        ]
        if missing_steps:
            raise TaskDefinitionMismatchError(
                "task workflow definition is incompatible with the current worker; "
                "missing step handlers: "
                + ", ".join(missing_steps)
            )
        task = self.claim_task(task_id, owner_id)
        if task["status"] == "completed":
            return task
        lease_epoch = int(task.get("lease_epoch") or 0)
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
                self.fail_step(
                    task_id,
                    step_name,
                    f"missing task step handler: {step_name}",
                    lease_epoch=lease_epoch,
                )
                return self.get_task(task_id)
            try:
                should_run = self.begin_step(task_id, step_name, lease_epoch=lease_epoch)
            except TaskLeaseLostError:
                return self.get_task(task_id)
            if not should_run:
                current = self.get_task(task_id)
                if current["status"] == "cancelled":
                    return current
                if current["status"] != "running":
                    return current
                continue
            try:
                if include_step_context:
                    step_context = {
                        "task_id": task_id,
                        "step_name": step_name,
                        "idempotency_key": str(step.get("idempotency_key") or ""),
                        "attempt": int(step.get("attempt_count") or 0) + 1,
                    }
                    output = handler(inputs, dict(outputs), step_context)
                else:
                    output = handler(inputs, dict(outputs))
                if not isinstance(output, dict):
                    raise TypeError("task step output must be an object")
            except Exception as exc:
                try:
                    self.fail_step(task_id, step_name, exc, lease_epoch=lease_epoch)
                except TaskLeaseLostError:
                    return self.get_task(task_id)
                return self.get_task(task_id)
            try:
                self.complete_step(task_id, step_name, output, lease_epoch=lease_epoch)
            except TaskLeaseLostError:
                return self.get_task(task_id)
            current = self.get_task(task_id)
            if current["status"] == "cancelled":
                return current
            outputs[step_name] = output
        try:
            result = result_builder(outputs)
            return self.complete_task(task_id, result, lease_epoch=lease_epoch)
        except TaskLeaseLostError:
            return self.get_task(task_id)
        except Exception as exc:
            # Finalization is not a step.  Do not turn the last completed
            # side-effecting step back into ``failed`` merely because the
            # result envelope could not be assembled.  Keeping its checkpoint
            # intact makes a retry resume at finalization instead of repeating
            # the side effect.
            now = time.time()
            serialized_message = _serialize_task_error(exc if str(exc) else "task finalization failed")
            with self._lock, self.conn:
                self.conn.execute("BEGIN IMMEDIATE")
                current = self.conn.execute(
                    "SELECT lease_epoch FROM runtime_tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if current is None:
                    raise KeyError(f"unknown task: {task_id}")
                try:
                    self._assert_lease_epoch(task_id, lease_epoch, current)
                except TaskLeaseLostError:
                    return self.get_task(task_id)
                self.conn.execute(
                    """
                    UPDATE runtime_tasks
                    SET status = CASE
                            WHEN cancel_requested_at IS NOT NULL THEN 'cancelled'
                            ELSE 'failed'
                        END,
                        owner_id = NULL,
                        current_step = NULL,
                        completed_at = CASE
                            WHEN cancel_requested_at IS NOT NULL THEN COALESCE(completed_at, ?)
                            ELSE completed_at
                        END,
                        updated_at = ?,
                        error = ?
                    WHERE task_id = ? AND status IN ('running', 'pending', 'interrupted', 'failed')
                    """,
                    (now, now, serialized_message, task_id),
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
    owner_parts = str(owner_id or "").split(":")
    try:
        process_id = int(owner_parts[0])
    except (TypeError, ValueError):
        return False
    try:
        import psutil

        if not psutil.pid_exists(process_id):
            return False
        if len(owner_parts) >= 3 and owner_parts[1] != "unknown":
            actual_started_at = f"{psutil.Process(process_id).create_time():.6f}"
            return actual_started_at == owner_parts[1]
        return True
    except Exception:
        try:
            os.kill(process_id, 0)
            return True
        except OSError:
            return False
