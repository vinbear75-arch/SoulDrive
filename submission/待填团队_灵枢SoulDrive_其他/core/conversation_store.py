import base64
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


CONVERSATION_SCHEMA_VERSION = 2
CONVERSATION_AAD = b"SoulDrive conversation v2"
OUTBOX_AAD = b"SoulDrive conversation outbox v1"


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
        with self._lock:
            path = self._manifest_path(session_id)
            if path.exists():
                existing = self.session_metadata(session_id)
                if existing and existing.get("project_id") not in {None, project_id}:
                    raise ValueError("session belongs to another project")
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
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def list_sessions(self, project_id: str | None = None) -> list[dict[str, Any]]:
        project_id = str(project_id or "").strip() or None
        sessions = []
        for path in self.archive_root.glob("*/manifest.json"):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if manifest.get("project_id") == project_id:
                sessions.append(manifest)
        return sorted(sessions, key=lambda item: float(item.get("updated_at") or 0), reverse=True)

    def delete_session(self, session_id: str, *, project_id: str | None = None) -> dict[str, Any] | None:
        """Delete one project's complete conversation archive and local index."""
        safe_session_id = _safe_session_id(session_id)
        manifest_path = self.archive_root / safe_session_id / "manifest.json"
        with self._lock:
            if not manifest_path.exists():
                return None
            metadata = self.session_metadata(safe_session_id)
            if metadata is None:
                return None
            expected_project_id = str(project_id or "").strip()
            if expected_project_id and metadata.get("project_id") != expected_project_id:
                raise ValueError("session belongs to another project")
            self.connection.execute("DELETE FROM messages WHERE session_id = ?", (safe_session_id,))
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

        with self._lock:
            if message_id:
                existing = self.find_message(message_id)
                if existing is not None:
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
                self._session_messages_path(session_id),
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

    def write_summary(
        self,
        session_id: str,
        summary: str,
        *,
        version: int,
        covered_sequence_start: int,
        covered_sequence_end: int,
        source_message_ids: list[str],
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
            "created_at": time.time(),
        }
        target = self._summary_path(session_id, int(version))
        with self._lock:
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
        with self._lock:
            self._append_encrypted_line(self.outbox_path, payload, OUTBOX_AAD)

    def pending_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            latest: dict[str, dict[str, Any]] = {}
            for item in self._read_encrypted_lines(self.outbox_path, OUTBOX_AAD):
                latest[str(item.get("event_id") or "")] = item
            return [
                item
                for item in latest.values()
                if str(item.get("status") or "pending") == "pending"
            ][-max(1, min(int(limit), 1000)) :]

    def mark_outbox(self, event_id: str, status: str, error: str | None = None) -> None:
        self.append_outbox(
            {
                "event_id": event_id,
                "status": status,
                "error": str(error or "")[:1000],
            }
        )

    def archive_memory(self, memory: dict[str, Any]) -> str:
        memory_id = str(memory.get("memory_id") or memory.get("skill_id") or "").strip()
        if not memory_id or not re.fullmatch(r"[A-Za-z0-9._-]{1,160}", memory_id):
            raise ValueError("memory_id is required")
        target = self.memory_root / f"{memory_id}.json.enc"
        payload = {"schema_version": 1, "archived_at": time.time(), **memory}
        with self._lock:
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

    def _session_messages_path(self, session_id: str) -> Path:
        path = self.archive_root / session_id / "messages.jsonl.enc"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

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
