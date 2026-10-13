import base64
import hashlib
import json
import os
import re
import sqlite3
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.skill_runtime import SUPPORTED_WORKFLOW_IDS
from core.local_file_lock import local_file_lock


CONVERSATION_SCHEMA_VERSION = 2
CONVERSATION_AAD = b"SoulDrive conversation v2"
OUTBOX_AAD = b"SoulDrive conversation outbox v1"
OUTBOX_CLAIM_LEASE_SECONDS = 300.0
REQUEST_RECEIPT_LEASE_SECONDS = 120.0
_SUMMARY_IDENTITY_FIELDS = (
    "session_id",
    "version",
    "summary",
    "covered_sequence_start",
    "covered_sequence_end",
    "source_message_ids",
    "input_sequence_start",
    "input_sequence_end",
    "input_message_count",
    "excerpt_message_ids",
    "excerpt_truncated",
    "excerpt_status",
)
_MEMORY_STATUS_RANK = {
    "active": 0,
    "archived": 1,
    "revoked": 1,
    "deleted": 2,
}
_MEMORY_IDENTITY_IGNORED_FIELDS = frozenset({
    "archived_at",
    "created_at",
    "last_used_at",
    "schema_version",
    "updated_at",
})


def memory_write_is_stale(current: dict[str, Any], incoming: dict[str, Any]) -> bool:
    """Return whether an incoming durable-memory state must not overwrite current."""
    current_version = int(current.get("record_version") or 1)
    incoming_version = int(incoming.get("record_version") or 1)
    if current_version != incoming_version:
        return current_version > incoming_version
    current_rank = _MEMORY_STATUS_RANK.get(str(current.get("status") or "active").strip().lower(), 0)
    incoming_rank = _MEMORY_STATUS_RANK.get(str(incoming.get("status") or "active").strip().lower(), 0)
    return current_rank > incoming_rank


def memory_identity_digest(memory: dict[str, Any]) -> str:
    """Bind one memory version to its semantic content, excluding runtime timestamps."""
    identity = {
        str(key): value
        for key, value in memory.items()
        if str(key) not in _MEMORY_IDENTITY_IGNORED_FIELDS and str(key) != "status"
    }
    return hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def memory_version_conflict(current: dict[str, Any], incoming: dict[str, Any]) -> bool:
    """Return whether equal-version, equal-lifecycle memories disagree semantically."""
    if int(current.get("record_version") or 1) != int(incoming.get("record_version") or 1):
        return False
    current_rank = _MEMORY_STATUS_RANK.get(str(current.get("status") or "active").strip().lower(), 0)
    incoming_rank = _MEMORY_STATUS_RANK.get(str(incoming.get("status") or "active").strip().lower(), 0)
    return current_rank == incoming_rank and memory_identity_digest(current) != memory_identity_digest(incoming)


def summary_identity_digest(summary: dict[str, Any]) -> str:
    """Bind one summary version to its semantic input and output payload."""
    identity = {
        field: summary.get(field)
        for field in _SUMMARY_IDENTITY_FIELDS
    }
    return hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def outbox_identity_digest(event: dict[str, Any]) -> str:
    """Bind one outbox event ID to its immutable dispatch payload."""
    event_type = str(event.get("event_type") or "")
    event_payload = _outbox_identity_value(
        event.get("payload") or {},
        ignore_updated_at=event_type in {"redis.session_message", "postgres.session_message"},
    )
    if event_type in {"redis.session_message", "postgres.session_message"} and isinstance(event_payload, dict):
        # Session metadata is a rebuildable projection hint.  It may be empty
        # on the first write and populated on a retry after the archive creates
        # the manifest; the message record and archive path are the immutable
        # dispatch identity.
        event_payload = {
            key: value
            for key, value in event_payload.items()
            if key != "session_metadata"
        }
    identity = {
        "event_type": event_type,
        "aggregate_id": str(event.get("aggregate_id") or ""),
        "payload": event_payload,
    }
    return hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _outbox_has_dispatch_identity(event: dict[str, Any]) -> bool:
    return bool(
        event.get("event_type")
        or event.get("aggregate_id")
        or "payload" in event
    )


def _outbox_identity_value(value: Any, *, ignore_updated_at: bool = False) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _outbox_identity_value(item, ignore_updated_at=ignore_updated_at)
            for key, item in value.items()
            if not (ignore_updated_at and str(key) == "updated_at")
        }
    if isinstance(value, list):
        return [_outbox_identity_value(item, ignore_updated_at=ignore_updated_at) for item in value]
    if isinstance(value, tuple):
        return [_outbox_identity_value(item, ignore_updated_at=ignore_updated_at) for item in value]
    return value


class ConversationStore:
    """完整会话事实源；加密 JSONL 追加保存，SQLite 仅作本机索引/降级。"""

    def __init__(self, path: str, key: bytes):
        self.path = path
        self.key = key
        self._lock = threading.RLock()
        self._closed = False
        self.last_index_error: str | None = None
        self.archive_read_error: str | None = None
        self.archive_root = Path(path).with_name("conversations")
        self.outbox_path = self.archive_root.parent / "outbox.jsonl.enc"
        self.memory_root = self.archive_root.parent / "memories"
        self.archive_root.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._init_sqlite()

    def _init_sqlite(self):
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT,
                session_id TEXT NOT NULL,
                sequence INTEGER,
                role TEXT NOT NULL,
                nonce BLOB NOT NULL,
                content BLOB NOT NULL,
                metadata_nonce BLOB,
                metadata_ciphertext BLOB,
                request_id TEXT,
                created_at REAL NOT NULL
            )"""
        )
        columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(messages)").fetchall()
        }
        for name, declaration in (
            ("message_id", "TEXT"),
            ("sequence", "INTEGER"),
            ("metadata_nonce", "BLOB"),
            ("metadata_ciphertext", "BLOB"),
            ("request_id", "TEXT"),
        ):
            if name not in columns:
                self.connection.execute(f"ALTER TABLE messages ADD COLUMN {name} {declaration}")
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_session_sequence ON messages(session_id, sequence)"
        )
        self.connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_message_id ON messages(message_id) WHERE message_id IS NOT NULL"
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS request_receipts (
                session_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                input_digest TEXT NOT NULL,
                status TEXT NOT NULL,
                owner_id TEXT,
                lease_token TEXT,
                lease_expires_at REAL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                response_message_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (session_id, request_id),
                CHECK (status IN ('in_flight', 'completed', 'failed'))
            )"""
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_request_receipts_lease ON request_receipts(status, lease_expires_at)"
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS outbox_delivery_claims (
                event_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                owner_id TEXT,
                claim_token TEXT,
                claim_until REAL,
                error TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL,
                CHECK (status IN ('pending', 'in_flight', 'completed', 'failed', 'cancelled'))
            )"""
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_outbox_delivery_claims_lease "
            "ON outbox_delivery_claims(status, claim_until, updated_at, event_id)"
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS projection_checkpoints (
                checkpoint_name TEXT PRIMARY KEY,
                nonce BLOB NOT NULL,
                ciphertext BLOB NOT NULL,
                updated_at REAL NOT NULL
            )"""
        )
        self.connection.commit()

    def close(self):
        with self._lock:
            if not self._closed:
                self.connection.close()
                self._closed = True

    def create_session(
        self,
        session_id: str,
        *,
        project_id: str | None = None,
        title: str,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        """Create the durable session manifest before its first message."""
        session_id = _safe_session_id(session_id)
        project_id = str(project_id or "").strip() or None
        title = str(title or "").strip()[:120]
        workflow_id = str(workflow_id or "").strip() or None
        if not title:
            raise ValueError("title is required")
        if workflow_id is not None and workflow_id not in SUPPORTED_WORKFLOW_IDS:
            raise ValueError("unsupported workflow_id")
        with self._lock, local_file_lock(self._session_lock_path(session_id)):
            path = self._manifest_path(session_id)
            if path.exists():
                existing = self.session_metadata(session_id)
                if existing and existing.get("project_id") != project_id:
                    raise ValueError("session belongs to another project")
                if existing:
                    existing_title = str(existing.get("title") or "").strip()
                    existing_workflow_id = str(existing.get("workflow_id") or "").strip() or None
                    if existing_title != title or existing_workflow_id != workflow_id:
                        raise ValueError(
                            "session already exists with different title or workflow_id"
                        )
                return existing or {}
            now = time.time()
            payload = {
                "schema_version": CONVERSATION_SCHEMA_VERSION,
                "session_id": session_id,
                "project_id": project_id,
                "title": title,
                "workflow_id": workflow_id,
                "created_at": now,
                "updated_at": now,
                "latest_sequence": 0,
            }
            self._write_manifest(path, payload)
            return payload

    def session_metadata(self, session_id: str) -> dict[str, Any] | None:
        safe_session_id = _safe_session_id(session_id)
        path = self.archive_root / safe_session_id / "manifest.json"
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if str(payload.get("session_id") or "").strip() != safe_session_id:
            raise ValueError("session manifest identity mismatch")
        workflow_id = str(payload.get("workflow_id") or "").strip() or None
        if workflow_id is not None and workflow_id not in SUPPORTED_WORKFLOW_IDS:
            raise ValueError("unsupported workflow_id")
        return payload

    def list_sessions(self, project_id: str | None = None) -> list[dict[str, Any]]:
        project_id = str(project_id or "").strip() or None
        sessions = []
        for path in self.archive_root.glob("*/manifest.json"):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(manifest, dict):
                continue
            if str(manifest.get("session_id") or "").strip() != path.parent.name:
                continue
            workflow_id = str(manifest.get("workflow_id") or "").strip() or None
            if workflow_id is not None and workflow_id not in SUPPORTED_WORKFLOW_IDS:
                continue
            if manifest.get("project_id") == project_id:
                sessions.append(manifest)
        return sorted(
            sessions,
            key=lambda item: (
                -float(item.get("updated_at") or 0),
                str(item.get("session_id") or ""),
            ),
        )

    def delete_session(self, session_id: str, *, project_id: str | None = None) -> dict[str, Any] | None:
        """Delete one project's complete conversation archive and local index."""
        safe_session_id = _safe_session_id(session_id)
        manifest_path = self.archive_root / safe_session_id / "manifest.json"
        with self._lock, local_file_lock(self._session_lock_path(safe_session_id)):
            if not manifest_path.exists():
                return None
            metadata = self.session_metadata(safe_session_id)
            if metadata is None:
                return None
            expected_project_id = str(project_id or "").strip()
            if expected_project_id and metadata.get("project_id") != expected_project_id:
                raise ValueError("session belongs to another project")
            self.connection.execute("DELETE FROM messages WHERE session_id = ?", (safe_session_id,))
            self.connection.execute("DELETE FROM request_receipts WHERE session_id = ?", (safe_session_id,))
            self.connection.commit()
            shutil.rmtree(manifest_path.parent, ignore_errors=False)
            return metadata

    def append(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        *,
        message_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        session_id = _safe_session_id(session_id)
        role = str(role or "").strip().lower()
        if role == "ai":
            role = "assistant"
        if role not in {"user", "assistant", "system", "tool"}:
            raise ValueError("unsupported conversation role")
        content = str(content or "")
        if not content:
            raise ValueError("conversation content is required")

        session_messages_path = self._session_messages_path(session_id)
        with self._lock, local_file_lock(self._session_lock_path(session_id)):
            if message_id:
                existing = self.find_message(message_id)
                if existing is not None:
                    if str(existing.get("session_id") or "") != session_id:
                        raise ValueError("message_id belongs to another session")
                    existing_role = str(existing.get("role") or "").strip().lower()
                    requested_role = "assistant" if role == "ai" else role
                    existing_metadata = existing.get("metadata")
                    if not isinstance(existing_metadata, dict):
                        existing_metadata = {}
                    if (
                        existing_role != requested_role
                        or str(existing.get("content") or "") != content
                        or str(existing.get("request_id") or "") != str(request_id or "")
                        or existing_metadata != dict(metadata or {})
                    ):
                        raise ValueError("message_id is already bound to a different message")
                    return existing
            archive_max = max(
                (int(item.get("sequence") or 0) for item in self._read_archive(session_id)),
                default=0,
            )
            sqlite_max = int(
                self.connection.execute(
                    "SELECT COALESCE(MAX(sequence), MAX(id), 0) FROM messages WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
                or 0
            )
            sequence = max(archive_max, sqlite_max) + 1
            record = {
                "schema_version": CONVERSATION_SCHEMA_VERSION,
                "message_id": message_id or f"msg_{uuid.uuid4().hex}",
                "session_id": session_id,
                "sequence": sequence,
                "role": role,
                "content": content,
                "metadata": dict(metadata or {}),
                "request_id": request_id,
                "created_at": time.time(),
            }
            self._append_encrypted_line(
                session_messages_path,
                record,
                self._session_aad(session_id),
            )
            content_nonce, encrypted_content = _seal_text(
                content,
                self.key,
                self._sqlite_aad(record["message_id"], "content"),
            )
            metadata_nonce, encrypted_metadata = _seal_json(
                record["metadata"],
                self.key,
                self._sqlite_aad(record["message_id"], "metadata"),
            )
            try:
                self.connection.execute(
                    """INSERT OR IGNORE INTO messages (
                        message_id, session_id, sequence, role, nonce, content,
                        metadata_nonce, metadata_ciphertext, request_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        record["message_id"],
                        session_id,
                        sequence,
                        role,
                        content_nonce,
                        encrypted_content,
                        metadata_nonce,
                        encrypted_metadata,
                        request_id,
                        record["created_at"],
                    ),
                )
                self.connection.commit()
                self.last_index_error = None
            except sqlite3.Error as exc:
                # ponytail: SQLite is a rebuildable local index; JSONL is the fact source.
                self.last_index_error = str(exc)
                self.connection.rollback()
            self._update_manifest(session_id, sequence)
            return record

    def recent(self, session_id: str, limit: int = 8) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        with self._lock:
            return self._records_for_session(session_id)[-safe_limit:]

    def load_before(self, session_id: str, before_sequence: int, limit: int = 8) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        with self._lock:
            records = [
                item for item in self._records_for_session(session_id)
                if int(item.get("sequence") or 0) < int(before_sequence)
            ]
            return records[-safe_limit:]

    def find_message(self, message_id: str) -> dict[str, Any] | None:
        target = str(message_id or "").strip()
        if not target:
            return None
        with self._lock:
            row = self.connection.execute(
                "SELECT session_id FROM messages WHERE message_id = ? LIMIT 1",
                (target,),
            ).fetchone()
            if row is not None:
                return next(
                    (
                        record
                        for record in self._read_sqlite(str(row[0]), limit=100000)
                        if str(record.get("message_id") or "") == target
                    ),
                    None,
                )

            # A single unreadable historical archive must not prevent a new
            # idempotent request from being persisted. The file remains the
            # source of truth and the health endpoint exposes the degraded
            # archive so the operator can repair it separately.
            for path in self.archive_root.glob("*/messages.jsonl.enc"):
                session_id = path.parent.name
                try:
                    records = self._read_encrypted_lines(path, self._session_aad(session_id))
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    self.archive_read_error = f"{path}: {exc}"
                    continue
                for record in records:
                    if str(record.get("message_id") or "") == target:
                        return record
            return None

    def find_message_by_request_id(
        self,
        session_id: str,
        request_id: str,
        *,
        role: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the newest archived message for a retryable request key."""
        safe_session_id = _safe_session_id(session_id)
        target_request_id = str(request_id or "").strip()
        target_role = str(role or "").strip().lower() or None
        target_status = str(status or "").strip().lower() or None
        if not target_request_id:
            return None
        with self._lock:
            records = self._records_for_session(safe_session_id)
            for record in reversed(records):
                if str(record.get("request_id") or "") != target_request_id:
                    continue
                if target_role and str(record.get("role") or "").lower() != target_role:
                    continue
                if target_status and str((record.get("metadata") or {}).get("status") or "").lower() != target_status:
                    continue
                return record
        return None

    def claim_request(
        self,
        session_id: str,
        request_id: str,
        query: str,
        *,
        owner_id: str,
        input_digest: str | None = None,
        lease_seconds: float = REQUEST_RECEIPT_LEASE_SECONDS,
    ) -> dict[str, Any]:
        """Atomically claim one request key before expensive chat work starts.

        The receipt stores a caller-provided canonical input digest when one is
        available, not the input itself.  An active lease makes concurrent
        callers retryable instead of allowing both to enter model generation;
        an expired lease can be taken over after a process crash.  This is
        local at-least-once execution, not exactly-once execution of an
        external model or side effect.
        """
        safe_session_id = _safe_session_id(session_id)
        target_request_id = str(request_id or "").strip()
        request_query = str(query or "")
        owner = str(owner_id or "").strip()
        if not target_request_id or len(target_request_id) > 128:
            raise ValueError("request_id is required and must be at most 128 characters")
        if not request_query:
            raise ValueError("query is required")
        if not owner or len(owner) > 256:
            raise ValueError("owner_id is required and must be at most 256 characters")
        try:
            safe_lease_seconds = min(max(float(lease_seconds), 0.001), 3600.0)
        except (TypeError, ValueError):
            safe_lease_seconds = REQUEST_RECEIPT_LEASE_SECONDS
        normalized_input_digest = str(input_digest or "").strip()
        if not normalized_input_digest:
            normalized_input_digest = hashlib.sha256(request_query.encode("utf-8")).hexdigest()
        if len(normalized_input_digest) > 256:
            raise ValueError("input_digest must be at most 256 characters")
        now = time.time()
        lease_token = uuid.uuid4().hex
        lease_expires_at = now + safe_lease_seconds
        with self._lock, self.connection:
            # ponytail: one SQLite write lock is enough for this single-PC
            # receipt; a distributed lease is only needed for multi-host workers.
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                """SELECT input_digest, status, owner_id, lease_expires_at,
                          attempt_count, response_message_id
                     FROM request_receipts
                     WHERE session_id = ? AND request_id = ?""",
                (safe_session_id, target_request_id),
            ).fetchone()
            if row is None:
                self.connection.execute(
                    """INSERT INTO request_receipts
                       (session_id, request_id, input_digest, status, owner_id,
                        lease_token, lease_expires_at, attempt_count, created_at, updated_at)
                       VALUES (?, ?, ?, 'in_flight', ?, ?, ?, 1, ?, ?)""",
                    (
                        safe_session_id,
                        target_request_id,
                        normalized_input_digest,
                        owner,
                        lease_token,
                        lease_expires_at,
                        now,
                        now,
                    ),
                )
                return {
                    "status": "claimed",
                    "lease_token": lease_token,
                    "attempt_count": 1,
                    "response_message_id": None,
                }

            status = str(row["status"] or "")
            current_expiry = float(row["lease_expires_at"] or 0.0)
            stored_digest = str(row["input_digest"] or "")
            if stored_digest != normalized_input_digest:
                # Receipts created before full-input binding stored only the
                # query digest.  An active legacy lease must still serialize
                # concurrent callers; a completed legacy receipt is rejected
                # conservatively because its old context cannot be recovered.
                legacy_query_digest = hashlib.sha256(request_query.encode("utf-8")).hexdigest()
                if stored_digest == legacy_query_digest and status == "in_flight" and current_expiry > now:
                    return {
                        "status": "in_flight",
                        "attempt_count": int(row["attempt_count"] or 0),
                        "retry_after_ms": max(0, int((current_expiry - now) * 1000)),
                    }
                return {"status": "conflict", "reason": "request_id is already bound to another input"}

            if status == "completed":
                return {
                    "status": "completed",
                    "attempt_count": int(row["attempt_count"] or 0),
                    "response_message_id": row["response_message_id"],
                    "input_digest": stored_digest,
                }

            if status == "in_flight" and current_expiry > now:
                return {
                    "status": "in_flight",
                    "attempt_count": int(row["attempt_count"] or 0),
                    "retry_after_ms": max(0, int((current_expiry - now) * 1000)),
                }

            self.connection.execute(
                """UPDATE request_receipts
                   SET status = 'in_flight', owner_id = ?, lease_token = ?,
                       lease_expires_at = ?, attempt_count = attempt_count + 1,
                       updated_at = ?
                   WHERE session_id = ? AND request_id = ?""",
                (
                    owner,
                    lease_token,
                    lease_expires_at,
                    now,
                    safe_session_id,
                    target_request_id,
                ),
            )
            return {
                "status": "claimed",
                "lease_token": lease_token,
                "attempt_count": int(row["attempt_count"] or 0) + 1,
                "response_message_id": None,
            }

    def renew_request(
        self,
        session_id: str,
        request_id: str,
        *,
        owner_id: str,
        lease_token: str,
        lease_seconds: float = REQUEST_RECEIPT_LEASE_SECONDS,
    ) -> bool:
        safe_session_id = _safe_session_id(session_id)
        try:
            safe_lease_seconds = min(max(float(lease_seconds), 0.001), 3600.0)
        except (TypeError, ValueError):
            safe_lease_seconds = REQUEST_RECEIPT_LEASE_SECONDS
        now = time.time()
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """UPDATE request_receipts
                   SET lease_expires_at = ?, updated_at = ?
                   WHERE session_id = ? AND request_id = ? AND status = 'in_flight'
                     AND owner_id = ? AND lease_token = ?""",
                (
                    now + safe_lease_seconds,
                    now,
                    safe_session_id,
                    str(request_id or "").strip(),
                    str(owner_id or "").strip(),
                    str(lease_token or "").strip(),
                ),
            )
            return bool(cursor.rowcount)

    def complete_request(
        self,
        session_id: str,
        request_id: str,
        *,
        owner_id: str,
        lease_token: str,
        response_message_id: str | None = None,
    ) -> bool:
        safe_session_id = _safe_session_id(session_id)
        now = time.time()
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """UPDATE request_receipts
                   SET status = 'completed', owner_id = NULL, lease_token = NULL,
                       lease_expires_at = NULL, response_message_id = ?,
                       updated_at = ?
                   WHERE session_id = ? AND request_id = ? AND status = 'in_flight'
                     AND owner_id = ? AND lease_token = ?""",
                (
                    str(response_message_id or "").strip() or None,
                    now,
                    safe_session_id,
                    str(request_id or "").strip(),
                    str(owner_id or "").strip(),
                    str(lease_token or "").strip(),
                ),
            )
            if cursor.rowcount:
                return True
            row = self.connection.execute(
                "SELECT status FROM request_receipts WHERE session_id = ? AND request_id = ?",
                (safe_session_id, str(request_id or "").strip()),
            ).fetchone()
            return bool(row is not None and str(row["status"] or "") == "completed")

    def fail_request(
        self,
        session_id: str,
        request_id: str,
        *,
        owner_id: str,
        lease_token: str,
        error: str,
    ) -> bool:
        _ = error
        safe_session_id = _safe_session_id(session_id)
        now = time.time()
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """UPDATE request_receipts
                   SET status = 'failed', owner_id = NULL, lease_token = NULL,
                       lease_expires_at = NULL, updated_at = ?
                   WHERE session_id = ? AND request_id = ? AND status = 'in_flight'
                     AND owner_id = ? AND lease_token = ?""",
                (
                    now,
                    safe_session_id,
                    str(request_id or "").strip(),
                    str(owner_id or "").strip(),
                    str(lease_token or "").strip(),
                ),
            )
            return bool(cursor.rowcount)

    def write_summary(
        self,
        session_id: str,
        summary: str,
        *,
        version: int,
        covered_sequence_start: int,
        covered_sequence_end: int,
        source_message_ids: list[str],
        input_sequence_start: int | None = None,
        input_sequence_end: int | None = None,
        input_message_count: int | None = None,
        excerpt_message_ids: list[str] | None = None,
        excerpt_truncated: bool | None = None,
        excerpt_status: str | None = None,
    ) -> dict[str, Any]:
        session_id = _safe_session_id(session_id)
        payload = {
            "schema_version": 1,
            "session_id": session_id,
            "version": int(version),
            "summary": str(summary or "").strip(),
            "covered_sequence_start": int(covered_sequence_start),
            "covered_sequence_end": int(covered_sequence_end),
            "source_message_ids": list(source_message_ids),
            "input_sequence_start": (
                int(input_sequence_start)
                if input_sequence_start is not None
                else int(covered_sequence_start)
            ),
            "input_sequence_end": (
                int(input_sequence_end)
                if input_sequence_end is not None
                else int(covered_sequence_end)
            ),
            "input_message_count": (
                int(input_message_count)
                if input_message_count is not None
                else len(source_message_ids)
            ),
            "excerpt_message_ids": (
                list(excerpt_message_ids)
                if excerpt_message_ids is not None
                else None
            ),
            "excerpt_truncated": excerpt_truncated,
            "excerpt_status": str(excerpt_status or "unknown"),
            "created_at": time.time(),
        }
        normalized_version = int(version)
        if normalized_version <= 0:
            raise ValueError("summary version must be positive")
        payload["identity_digest"] = summary_identity_digest(payload)
        with self._lock, local_file_lock(self._session_lock_path(session_id)):
            manifest_path = self._manifest_path(session_id)
            current_manifest: dict[str, Any] = {}
            if manifest_path.exists():
                try:
                    loaded_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if isinstance(loaded_manifest, dict):
                        current_manifest = loaded_manifest
                except (OSError, ValueError, json.JSONDecodeError):
                    current_manifest = {}
            current_summary = current_manifest.get("latest_summary") or {}
            current_version = int(current_summary.get("version") or 0)
            target = self._summary_path(session_id, normalized_version)
            if current_version > normalized_version:
                # A delayed durable replay must never move the manifest
                # checkpoint backwards.  Return the committed latest payload
                # so downstream projections can replay that version instead.
                latest_target = self._summary_path(session_id, current_version)
                if latest_target.exists():
                    try:
                        latest_payload = self._read_encrypted_file(
                            latest_target,
                            self._session_aad(session_id) + b":summary",
                        )
                    except (OSError, ValueError, KeyError, json.JSONDecodeError):
                        latest_payload = None
                    if isinstance(latest_payload, dict):
                        return latest_payload
                raise ValueError("latest summary checkpoint is unavailable")
            if current_version == normalized_version and target.exists():
                try:
                    existing_payload = self._read_encrypted_file(
                        target,
                        self._session_aad(session_id) + b":summary",
                    )
                except (OSError, ValueError, KeyError, json.JSONDecodeError):
                    existing_payload = None
                if isinstance(existing_payload, dict):
                    # The first committed writer owns a version.  A retry with
                    # the same version must replay it instead of replacing it;
                    # a changed payload is a conflict, not a valid retry.
                    if summary_identity_digest(existing_payload) != summary_identity_digest(payload):
                        raise ValueError("summary version conflict")
                    return existing_payload
            self._write_encrypted_file(target, payload, self._session_aad(session_id) + b":summary")
            self._update_manifest(session_id, int(covered_sequence_end), latest_summary=payload)
        return payload

    def latest_summary(self, session_id: str) -> dict[str, Any] | None:
        manifest_path = self._manifest_path(session_id)
        if not manifest_path.exists():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            latest = manifest.get("latest_summary") or {}
            version = int(latest.get("version") or 0)
            if version <= 0:
                return None
            return self._read_encrypted_file(
                self._summary_path(session_id, version),
                self._session_aad(session_id) + b":summary",
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None

    def append_outbox(self, event: dict[str, Any]) -> None:
        payload = {
            "schema_version": 1,
            "event_id": str(event.get("event_id") or f"evt_{uuid.uuid4().hex}"),
            "status": str(event.get("status") or "pending"),
            "created_at": time.time(),
            **event,
        }
        with self._lock, local_file_lock(f"{self.outbox_path}.lock"):
            existing = self._outbox_latest_snapshots_locked().get(payload["event_id"])
            if existing is not None and not _outbox_has_dispatch_identity(payload):
                preserved_created_at = existing.get("created_at")
                payload = {**existing, **payload}
                if preserved_created_at is not None:
                    payload["created_at"] = preserved_created_at
            if existing is not None and outbox_identity_digest(existing) != outbox_identity_digest(payload):
                raise ValueError("outbox event conflict")
            payload["identity_digest"] = outbox_identity_digest(payload)
            self._append_encrypted_line(self.outbox_path, payload, OUTBOX_AAD)

    def _outbox_latest_snapshots_locked(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        bases: dict[str, dict[str, Any]] = {}
        for item in self._read_encrypted_lines(self.outbox_path, OUTBOX_AAD):
            event_id = str(item.get("event_id") or "")
            if not event_id:
                continue
            if _outbox_has_dispatch_identity(item):
                bases[event_id] = item
            snapshot = dict(bases.get(event_id) or {})
            snapshot.update(item)
            latest[event_id] = snapshot
        return latest

    def pending_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 1000))
        with self._lock:
            latest = self._outbox_latest_snapshots_locked()
            now = time.time()
            delivery_rows = []
            if not self._closed:
                delivery_rows = self.connection.execute(
                    "SELECT event_id, status, claim_until FROM outbox_delivery_claims"
                ).fetchall()
            delivery_state = {
                str(row[0]): (str(row[1] or "pending"), row[2])
                for row in delivery_rows
            }
            pending = [
                item
                for item in latest.values()
                if str(item.get("status") or "pending") == "pending"
                and self._outbox_delivery_is_claimable(
                    delivery_state.get(str(item.get("event_id") or "")),
                    now,
                )
            ]
            return sorted(
                pending,
                key=lambda item: (
                    float(item.get("created_at") or 0.0),
                    str(item.get("event_id") or ""),
                ),
            )[:safe_limit]

    @staticmethod
    def _outbox_delivery_is_claimable(
        state: tuple[str, float | None] | None,
        now: float,
    ) -> bool:
        if state is None:
            return True
        status, claim_until = state
        if status in {"completed", "cancelled"}:
            return False
        if status == "in_flight" and claim_until is not None and float(claim_until) > now:
            return False
        return True

    def claim_outbox(
        self,
        event_id: str,
        *,
        owner_id: str | None = None,
        lease_seconds: float = OUTBOX_CLAIM_LEASE_SECONDS,
    ) -> dict[str, Any] | None:
        """Atomically claim one local outbox event across SQLite connections."""
        normalized_event_id = str(event_id or "").strip()
        if not normalized_event_id:
            return None
        owner = str(owner_id or "local-replay").strip()[:160] or "local-replay"
        safe_lease_seconds = max(1.0, min(float(lease_seconds), 86400.0))
        now = time.time()
        claim_until = now + safe_lease_seconds
        claim_token = f"{owner}:{uuid.uuid4().hex}"
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                latest = self._outbox_latest_snapshots_locked().get(normalized_event_id)
                if latest is None or str(latest.get("status") or "pending") != "pending":
                    self.connection.rollback()
                    return None
                row = self.connection.execute(
                    "SELECT status, claim_until FROM outbox_delivery_claims WHERE event_id = ?",
                    (normalized_event_id,),
                ).fetchone()
                if row is not None and not self._outbox_delivery_is_claimable(
                    (str(row[0] or "pending"), row[1]), now
                ):
                    self.connection.rollback()
                    return None
                self.connection.execute(
                    """INSERT INTO outbox_delivery_claims
                        (event_id, status, owner_id, claim_token, claim_until, error, updated_at)
                    VALUES (?, 'in_flight', ?, ?, ?, '', ?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        status = 'in_flight', owner_id = excluded.owner_id,
                        claim_token = excluded.claim_token, claim_until = excluded.claim_until,
                        error = '', updated_at = excluded.updated_at""",
                    (normalized_event_id, owner, claim_token, claim_until, now),
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return {
            "event_id": normalized_event_id,
            "owner_id": owner,
            "claim_token": claim_token,
            "claim_until": claim_until,
        }

    def mark_outbox(
        self,
        event_id: str,
        status: str,
        error: str | None = None,
        *,
        claim_token: str | None = None,
    ) -> bool:
        normalized_event_id = str(event_id or "").strip()
        normalized_status = str(status or "pending").strip().lower() or "pending"
        normalized_error = str(error or "")[:1000]
        now = time.time()
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                row = self.connection.execute(
                    "SELECT status, claim_token, claim_until FROM outbox_delivery_claims WHERE event_id = ?",
                    (normalized_event_id,),
                ).fetchone()
                if claim_token:
                    if (
                        row is None
                        or str(row[0] or "") != "in_flight"
                        or str(row[1] or "") != str(claim_token)
                        or row[2] is not None and float(row[2]) <= now
                    ):
                        self.connection.rollback()
                        return False
                elif row is not None and str(row[0] or "") == "in_flight":
                    self.connection.rollback()
                    return False
                elif row is not None and str(row[0] or "") in {"completed", "cancelled"}:
                    if str(row[0] or "") == normalized_status:
                        self.connection.rollback()
                        return True
                    self.connection.rollback()
                    return False

                latest = self._outbox_latest_snapshots_locked().get(normalized_event_id)
                updated = dict(latest or {"event_id": normalized_event_id})
                updated.update({
                    "event_id": normalized_event_id,
                    "status": normalized_status,
                    "error": normalized_error,
                })
                self.append_outbox(updated)
                self.connection.execute(
                    """INSERT INTO outbox_delivery_claims
                        (event_id, status, owner_id, claim_token, claim_until, error, updated_at)
                    VALUES (?, ?, NULL, NULL, NULL, ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        status = excluded.status, owner_id = NULL, claim_token = NULL,
                        claim_until = NULL, error = excluded.error, updated_at = excluded.updated_at""",
                    (normalized_event_id, normalized_status, normalized_error, now),
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return True

    def cancel_memory_outbox(self, memory_id: str) -> int:
        """Fence pending memory upserts before a project archive is deleted."""
        normalized_memory_id = str(memory_id or "").strip()
        if not normalized_memory_id or not self.outbox_path.exists():
            return 0
        with self._lock:
            latest = self._outbox_latest_snapshots_locked()
            cancelled = 0
            for item in latest.values():
                if str(item.get("status") or "pending") != "pending":
                    continue
                payload = item.get("payload") or {}
                aggregate_id = str(
                    item.get("aggregate_id")
                    or payload.get("memory_id")
                    or (payload.get("memory") or {}).get("memory_id")
                    or ""
                ).strip()
                if aggregate_id != normalized_memory_id:
                    continue
                if self.mark_outbox(
                    str(item.get("event_id") or ""),
                    "cancelled",
                    "project_deleted",
                ):
                    cancelled += 1
            return cancelled

    @staticmethod
    def _projection_checkpoint_aad(checkpoint_name: str) -> bytes:
        return b"SoulDrive projection checkpoint v1:" + checkpoint_name.encode("utf-8")

    def read_projection_checkpoint(self, checkpoint_name: str) -> dict[str, Any] | None:
        checkpoint_name = str(checkpoint_name or "").strip()
        if not checkpoint_name or len(checkpoint_name) > 128:
            return None
        with self._lock:
            row = self.connection.execute(
                "SELECT nonce, ciphertext FROM projection_checkpoints WHERE checkpoint_name = ?",
                (checkpoint_name,),
            ).fetchone()
        if row is None:
            return None
        try:
            plaintext = AESGCM(self.key).decrypt(
                row[0],
                row[1],
                self._projection_checkpoint_aad(checkpoint_name),
            )
            value = json.loads(plaintext.decode("utf-8"))
        except (InvalidTag, TypeError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def write_projection_checkpoint(self, checkpoint_name: str, value: dict[str, Any]) -> None:
        checkpoint_name = str(checkpoint_name or "").strip()
        if not checkpoint_name or len(checkpoint_name) > 128:
            raise ValueError("projection checkpoint name is invalid")
        nonce, ciphertext = _seal_json(
            dict(value),
            self.key,
            self._projection_checkpoint_aad(checkpoint_name),
        )
        with self._lock, self.connection:
            self.connection.execute(
                """INSERT INTO projection_checkpoints (
                    checkpoint_name, nonce, ciphertext, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(checkpoint_name) DO UPDATE SET
                    nonce = excluded.nonce,
                    ciphertext = excluded.ciphertext,
                    updated_at = excluded.updated_at""",
                (checkpoint_name, nonce, ciphertext, time.time()),
            )

    def archive_memory(self, memory: dict[str, Any]) -> str:
        memory_id = str(memory.get("memory_id") or memory.get("skill_id") or "").strip()
        if not memory_id or not re.fullmatch(r"[A-Za-z0-9._-]{1,160}", memory_id):
            raise ValueError("memory_id is required")
        target = self.memory_root / f"{memory_id}.json.enc"
        payload = {"schema_version": 1, "archived_at": time.time(), **memory}
        with self._lock, local_file_lock(self._memory_lock_path(memory_id)):
            if target.exists():
                try:
                    current = self._read_encrypted_file(
                        target,
                        b"SoulDrive durable memory v1:" + memory_id.encode(),
                    )
                except (OSError, ValueError, KeyError, json.JSONDecodeError):
                    # A corrupted archive is handled by the normal overwrite
                    # path; the authoritative encrypted learning index still
                    # decides whether the new payload is valid.
                    pass
                else:
                    if memory_write_is_stale(current, memory):
                        return str(target)
                    if memory_version_conflict(current, memory):
                        raise ValueError("memory version conflict")
            self._write_encrypted_file(target, payload, b"SoulDrive durable memory v1:" + memory_id.encode())
        return str(target)

    def archived_memories(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self.memory_root.exists():
            return []
        records = []
        with self._lock:
            for path in sorted(self.memory_root.glob("*.json.enc"), key=lambda item: item.stat().st_mtime, reverse=True):
                memory_id = path.name.removesuffix(".json.enc")
                try:
                    records.append(
                        self._read_encrypted_file(
                            path,
                            b"SoulDrive durable memory v1:" + memory_id.encode(),
                        )
                    )
                except (OSError, ValueError, KeyError):
                    continue
                if len(records) >= max(1, min(int(limit), 1000)):
                    break
        return records

    def archived_memories_page(
        self,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Read a stable memory-id page for bounded derived-index replay."""
        safe_limit = max(1, min(int(limit), 1000))
        normalized_cursor = str(cursor or "").strip()
        if not self.memory_root.exists():
            return {"records": [], "next_cursor": None, "has_more": False}
        paths = sorted(self.memory_root.glob("*.json.enc"), key=lambda item: item.name)
        records: list[dict[str, Any]] = []
        last_scanned: str | None = None
        has_more = False
        with self._lock:
            for index, path in enumerate(paths):
                memory_id = path.name.removesuffix(".json.enc")
                if normalized_cursor and memory_id <= normalized_cursor:
                    continue
                last_scanned = memory_id
                try:
                    records.append(
                        self._read_encrypted_file(
                            path,
                            b"SoulDrive durable memory v1:" + memory_id.encode(),
                        )
                    )
                except (OSError, ValueError, KeyError):
                    pass
                if len(records) >= safe_limit:
                    has_more = any(
                        candidate.name.removesuffix(".json.enc") > memory_id
                        for candidate in paths[index + 1 :]
                    )
                    break
        return {
            "records": records,
            "next_cursor": last_scanned if has_more else None,
            "has_more": has_more,
        }

    def read_memory(self, memory_id: str) -> dict[str, Any] | None:
        safe_id = str(memory_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,160}", safe_id):
            return None
        path = self.memory_root / f"{safe_id}.json.enc"
        if not path.exists():
            return None
        with self._lock:
            return self._read_encrypted_file(
                path,
                b"SoulDrive durable memory v1:" + safe_id.encode(),
            )

    def memory_ids_for_project(self, project_id: str) -> list[str]:
        """Return readable durable-memory IDs whose persisted scope is a project."""
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id or not self.memory_root.exists():
            return []
        memory_ids: list[str] = []
        with self._lock:
            for path in sorted(self.memory_root.glob("*.json.enc"), key=lambda item: item.name):
                memory_id = path.name.removesuffix(".json.enc")
                try:
                    record = self._read_encrypted_file(
                        path,
                        b"SoulDrive durable memory v1:" + memory_id.encode(),
                    )
                except (OSError, ValueError, KeyError, json.JSONDecodeError):
                    continue
                record_scope = str(record.get("scope") or "").strip().lower()
                if (
                    record_scope in {"", "project"}
                    and str(record.get("project_id") or "").strip() == normalized_project_id
                ):
                    memory_ids.append(memory_id)
        return memory_ids

    def delete_memory(self, memory_id: str, *, project_id: str | None = None) -> dict[str, Any] | None:
        """Permanently remove one durable memory archive after scope validation."""
        safe_memory_id = str(memory_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,160}", safe_memory_id):
            return None
        target = self.memory_root / f"{safe_memory_id}.json.enc"
        with self._lock, local_file_lock(self._memory_lock_path(safe_memory_id)):
            if not target.exists():
                return None
            record = self.read_memory(safe_memory_id)
            if record is None:
                return None
            expected_project_id = str(project_id or "").strip()
            record_scope = str(record.get("scope") or "").strip().lower()
            if expected_project_id and (
                record_scope not in {"", "project"}
                or str(record.get("project_id") or "").strip() != expected_project_id
            ):
                raise ValueError("memory belongs to another project")
            target.unlink()
            self.cancel_memory_outbox(safe_memory_id)
            return record

    def _session_messages_path(self, session_id: str) -> Path:
        path = self.archive_root / session_id / "messages.jsonl.enc"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _session_lock_path(self, session_id: str) -> Path:
        return self.archive_root / ".locks" / f"{session_id}.lock"

    def _memory_lock_path(self, memory_id: str) -> Path:
        return self.memory_root / ".locks" / f"{memory_id}.lock"

    def _manifest_path(self, session_id: str) -> Path:
        path = self.archive_root / session_id / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _summary_path(self, session_id: str, version: int) -> Path:
        path = self.archive_root / session_id / "summaries" / f"summary-{version}.json.enc"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _session_aad(self, session_id: str) -> bytes:
        return CONVERSATION_AAD + b":" + session_id.encode("utf-8")

    def _sqlite_aad(self, message_id: str, field: str) -> bytes:
        return CONVERSATION_AAD + b":" + message_id.encode("utf-8") + b":" + field.encode("utf-8")

    def _legacy_sqlite_aad(self, session_id: str, field: str) -> bytes:
        return CONVERSATION_AAD + b":" + session_id.encode("utf-8") + b":" + field.encode("utf-8")

    def _append_encrypted_line(self, path: Path, payload: dict[str, Any], aad: bytes) -> None:
        nonce = os.urandom(12)
        ciphertext = AESGCM(self.key).encrypt(
            nonce,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            aad,
        )
        encoded = json.dumps(
            {"nonce": _b64(nonce), "ciphertext": _b64(ciphertext)},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _read_encrypted_lines(self, path: Path, aad: bytes) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            envelope = json.loads(line)
            try:
                plaintext = AESGCM(self.key).decrypt(
                    _unb64(envelope["nonce"]),
                    _unb64(envelope["ciphertext"]),
                    aad,
                )
            except InvalidTag as exc:
                # A stale outbox/archive entry must not make all sessions
                # unreadable after a workspace key rotation.
                self.archive_read_error = f"{path}: encrypted conversation record authentication failed"
                continue
            try:
                records.append(json.loads(plaintext.decode("utf-8")))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                self.archive_read_error = f"{path}: invalid conversation record"
        return records

    def _read_archive(self, session_id: str) -> list[dict[str, Any]]:
        return sorted(
            self._read_encrypted_lines(self._session_messages_path(session_id), self._session_aad(session_id)),
            key=lambda item: int(item.get("sequence") or 0),
        )

    def _records_for_session(self, session_id: str) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for record in [
            *self._read_archive(session_id),
            *self._read_sqlite(session_id, limit=100000),
        ]:
            key = str(record.get("message_id") or f"{session_id}:{record.get('sequence')}")
            merged[key] = record
        return sorted(
            merged.values(),
            key=lambda item: (int(item.get("sequence") or 0), float(item.get("created_at") or 0)),
        )

    def _read_archive_for_all_sessions(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if not self.archive_root.exists():
            return records
        for path in self.archive_root.glob("*/messages.jsonl.enc"):
            session_id = path.parent.name
            records.extend(self._read_encrypted_lines(path, self._session_aad(session_id)))
        return records

    def _read_sqlite(self, session_id: str, limit: int = 8) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT id AS sqlite_id, message_id, session_id, sequence, role, nonce, content,
                      metadata_nonce, metadata_ciphertext, request_id, created_at
                 FROM messages WHERE session_id = ? ORDER BY COALESCE(sequence, id) DESC LIMIT ?""",
            (session_id, limit),
        ).fetchall()
        result = []
        for row in reversed(rows):
            message_id = str(row["message_id"] or f"legacy_{row['sqlite_id']}")
            try:
                content = _decrypt_with_legacy_aad(
                    self.key,
                    row["nonce"],
                    row["content"],
                    [
                        self._sqlite_aad(message_id, "content"),
                        self._legacy_sqlite_aad(str(row["session_id"]), "content"),
                    ],
                ).decode("utf-8")
                metadata = (
                    json.loads(
                        _decrypt_with_legacy_aad(
                            self.key,
                            row["metadata_nonce"],
                            row["metadata_ciphertext"],
                            [
                                self._sqlite_aad(message_id, "metadata"),
                                self._legacy_sqlite_aad(str(row["session_id"]), "metadata"),
                            ],
                        ).decode("utf-8")
                    )
                    if row["metadata_nonce"] and row["metadata_ciphertext"]
                    else {}
                )
            except (InvalidTag, ValueError) as exc:
                # Legacy rows may have been sealed with a previous workspace
                # key. Keep readable rows available instead of taking the
                # entire session API down.
                self.last_index_error = str(exc)
                continue
            result.append(
                {
                    "schema_version": CONVERSATION_SCHEMA_VERSION,
                    "message_id": message_id,
                    "session_id": session_id,
                    "sequence": int(row["sequence"] or row["sqlite_id"]),
                    "role": row["role"],
                    "content": content,
                    "metadata": metadata,
                    "request_id": row["request_id"],
                    "created_at": row["created_at"],
                }
            )
        return result

    def _write_encrypted_file(self, path: Path, payload: dict[str, Any], aad: bytes) -> None:
        nonce = os.urandom(12)
        ciphertext = AESGCM(self.key).encrypt(
            nonce,
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            aad,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps({"nonce": _b64(nonce), "ciphertext": _b64(ciphertext)}, ensure_ascii=False))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _read_encrypted_file(self, path: Path, aad: bytes) -> dict[str, Any]:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        try:
            plaintext = AESGCM(self.key).decrypt(
                _unb64(envelope["nonce"]),
                _unb64(envelope["ciphertext"]),
                aad,
            )
        except InvalidTag as exc:
            raise ValueError("encrypted conversation file authentication failed") from exc
        return json.loads(plaintext.decode("utf-8"))

    def _update_manifest(self, session_id: str, sequence: int, latest_summary: dict[str, Any] | None = None):
        path = self._manifest_path(session_id)
        current = {}
        if path.exists():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                current = {}
        current.update(
            {
                "schema_version": CONVERSATION_SCHEMA_VERSION,
                "session_id": session_id,
                "latest_sequence": max(int(current.get("latest_sequence") or 0), int(sequence)),
                "updated_at": time.time(),
            }
        )
        if latest_summary is not None:
            current["latest_summary"] = {
                "version": int(latest_summary["version"]),
                "covered_sequence_start": int(latest_summary["covered_sequence_start"]),
                "covered_sequence_end": int(latest_summary["covered_sequence_end"]),
            }
        self._write_manifest(path, current)

    @staticmethod
    def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)


def _safe_session_id(value: str) -> str:
    session_id = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", session_id):
        raise ValueError("invalid session_id")
    return session_id


def _seal_text(value: str, key: bytes, aad: bytes) -> tuple[bytes, bytes]:
    nonce = os.urandom(12)
    return nonce, AESGCM(key).encrypt(nonce, value.encode("utf-8"), aad)


def _seal_json(value: dict[str, Any], key: bytes, aad: bytes) -> tuple[bytes, bytes]:
    nonce = os.urandom(12)
    return nonce, AESGCM(key).encrypt(
        nonce,
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        aad,
    )


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(str(value).encode("ascii"))


def _decrypt_with_legacy_aad(
    key: bytes,
    nonce: bytes,
    ciphertext: bytes,
    aads: list[bytes],
) -> bytes:
    last_error: InvalidTag | None = None
    for aad in aads:
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, aad)
        except InvalidTag as exc:
            last_error = exc
    raise ValueError("encrypted conversation index authentication failed") from last_error
