"""Durable idempotency receipts for local index runs."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from typing import Any


INDEX_RUN_STATUSES = frozenset({"starting", "queued", "scanning", "indexing", "completed", "failed"})
_INDEX_RUN_STATUS_ORDER = {
    "starting": 0,
    "queued": 1,
    "scanning": 2,
    "indexing": 3,
    "completed": 4,
    "failed": 4,
}
_INDEX_RUN_TERMINAL_STATUSES = frozenset({"completed", "failed"})


def index_run_error_observation(error: Any) -> dict[str, Any]:
    """Return a stable, non-sensitive observation for a persisted run error."""
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
            return {
                "error_type": error_type,
                "error_digest": error_digest.lower(),
                "error_chars": error_chars,
            }
        error = json.dumps(error, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    else:
        text = str(error or "")
        try:
            decoded = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = None
        if isinstance(decoded, dict):
            return index_run_error_observation(decoded)

    text = str(error or "")
    return {
        "error_type": "UnknownError",
        "error_digest": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "error_chars": len(text),
    }


def _serialize_index_run_error(error: Any) -> str | None:
    if error is None or str(error or "") == "":
        return None
    return json.dumps(
        index_run_error_observation(error),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class IndexRunInputConflict(ValueError):
    pass


class IndexRunJournal:
    """Small local receipt store; it contains no source text or key material."""

    def __init__(self, workspace):
        self.db_path = os.path.join(workspace.runtime_path, "index_run_journal.sqlite")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.connection = sqlite3.connect(self.db_path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS index_run_requests (
                idempotency_key TEXT PRIMARY KEY,
                input_digest TEXT NOT NULL,
                run_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS index_run_requests_updated_idx "
            "ON index_run_requests(updated_at DESC)"
        )
        self._sanitize_legacy_errors()
        self.connection.commit()

    def _sanitize_legacy_errors(self) -> None:
        rows = self.connection.execute(
            "SELECT rowid, error FROM index_run_requests WHERE error IS NOT NULL"
        ).fetchall()
        updates = [
            (_serialize_index_run_error(row["error"]), row["rowid"])
            for row in rows
        ]
        if updates:
            self.connection.executemany(
                "UPDATE index_run_requests SET error = ? WHERE rowid = ?",
                updates,
            )

    def close(self) -> None:
        self.connection.close()

    def get(self, idempotency_key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM index_run_requests WHERE idempotency_key = ?",
            (str(idempotency_key),),
        ).fetchone()
        return dict(row) if row is not None else None

    def create_or_get(
        self,
        idempotency_key: str,
        input_digest: str,
        run_id: str,
    ) -> dict[str, Any]:
        key = str(idempotency_key or "").strip()
        if not key or len(key) > 256:
            raise ValueError("idempotency_key must contain 1-256 characters")
        now = time.time()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO index_run_requests(
                    idempotency_key, input_digest, run_id, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'starting', ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (key, str(input_digest), str(run_id), now, now),
            )
            row = self.get(key)
            if row is None:
                raise RuntimeError("index run receipt was not persisted")
            if str(row["input_digest"]) != str(input_digest):
                raise IndexRunInputConflict("idempotency key is already bound to a different index request")
            return row

    def update(
        self,
        idempotency_key: str,
        *,
        status: str,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        normalized_status = str(status or "").strip()
        if normalized_status not in INDEX_RUN_STATUSES:
            raise ValueError(f"unsupported index run status: {normalized_status}")
        incoming_order = _INDEX_RUN_STATUS_ORDER[normalized_status]
        allowed_current_statuses = tuple(
            current_status
            for current_status, current_order in _INDEX_RUN_STATUS_ORDER.items()
            if current_status not in _INDEX_RUN_TERMINAL_STATUSES
            and current_order <= incoming_order
        )
        placeholders = ", ".join("?" for _ in allowed_current_statuses)
        with self.connection:
            self.connection.execute(
                f"""
                UPDATE index_run_requests
                SET status = ?, error = ?, updated_at = ?
                WHERE idempotency_key = ?
                  AND status IN ({placeholders})
                """,
                (
                    normalized_status,
                    _serialize_index_run_error(error),
                    time.time(),
                    str(idempotency_key),
                    *allowed_current_statuses,
                ),
            )
        return self.get(str(idempotency_key))


def index_run_input_digest(workspace_root: str, auth_level: str) -> str:
    payload = {
        "workspace_root": os.path.normcase(os.path.abspath(str(workspace_root))),
        "auth_level": str(auth_level or "PRO"),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
