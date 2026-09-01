import base64
import hashlib
import json
import os
import struct
import threading
import time
from typing import Any, Callable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.conversation_store import ConversationStore
from core.conversation_compaction import build_recoverable_summary, next_summary_version
from core.logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"
DEFAULT_POSTGRES_URL = "postgresql://souldrive:souldrive@127.0.0.1:5432/souldrive"


class PostgresStateStore:
    def __init__(self, dsn: str):
        import psycopg
        from psycopg.rows import dict_row

        self.connection = psycopg.connect(
            dsn,
            connect_timeout=int(os.environ.get("SOULDRIVE_POSTGRES_CONNECT_TIMEOUT", "1")),
            autocommit=True,
            row_factory=dict_row,
        )
        self.lock = threading.RLock()
        self.dsn = dsn
        self.setup()

    def setup(self):
        statements = (
            """
            CREATE TABLE IF NOT EXISTS souldrive_sessions (
                session_id TEXT PRIMARY KEY,
                project_id TEXT,
                title TEXT NOT NULL DEFAULT '新对话',
                archive_path TEXT NOT NULL,
                latest_sequence INTEGER NOT NULL DEFAULT 0,
                summary_version INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS souldrive_session_messages (
                message_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES souldrive_sessions(session_id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                role TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                archive_path TEXT NOT NULL,
                metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at DOUBLE PRECISION NOT NULL,
                UNIQUE(session_id, sequence)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS souldrive_session_summaries (
                session_id TEXT NOT NULL REFERENCES souldrive_sessions(session_id) ON DELETE CASCADE,
                version INTEGER NOT NULL,
                covered_sequence_start INTEGER NOT NULL,
                covered_sequence_end INTEGER NOT NULL,
                summary_hash TEXT NOT NULL,
                archive_path TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                PRIMARY KEY(session_id, version)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS souldrive_memory_records (
                memory_id TEXT PRIMARY KEY,
                project_id TEXT,
                session_id TEXT,
                memory_type TEXT NOT NULL,
                status TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                archive_path TEXT NOT NULL,
                metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS souldrive_outbox_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                payload_json JSONB NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
            )
            """,
            "ALTER TABLE souldrive_sessions ADD COLUMN IF NOT EXISTS title TEXT NOT NULL DEFAULT '新对话'",
            "CREATE INDEX IF NOT EXISTS idx_souldrive_messages_session ON souldrive_session_messages(session_id, sequence)",
            "CREATE INDEX IF NOT EXISTS idx_souldrive_outbox_status ON souldrive_outbox_events(status, created_at)",
        )
        with self.lock, self.connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)

    def upsert_session(
        self,
        session_id: str,
        *,
        project_id: str | None,
        archive_path: str,
        latest_sequence: int,
        created_at: float,
        title: str | None = None,
    ):
        now = time.time()
        with self.lock, self.connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO souldrive_sessions
                   (session_id, project_id, title, archive_path, latest_sequence, created_at, updated_at)
                   VALUES (%s, %s, COALESCE(NULLIF(%s, ''), '新对话'), %s, %s, %s, %s)
                   ON CONFLICT (session_id) DO UPDATE SET
                     project_id = COALESCE(EXCLUDED.project_id, souldrive_sessions.project_id),
                     archive_path = EXCLUDED.archive_path,
                     latest_sequence = GREATEST(souldrive_sessions.latest_sequence, EXCLUDED.latest_sequence),
                     updated_at = EXCLUDED.updated_at""",
                (session_id, project_id, str(title or ""), archive_path, latest_sequence, created_at, now),
            )

    def delete_session(self, session_id: str) -> None:
        with self.lock, self.connection.cursor() as cursor:
            cursor.execute("DELETE FROM souldrive_sessions WHERE session_id = %s", (str(session_id),))

    def record_message(self, record: dict[str, Any], archive_path: str):
        with self.lock, self.connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO souldrive_session_messages
                   (message_id, session_id, sequence, role, content_hash, archive_path, metadata_json, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                   ON CONFLICT (message_id) DO NOTHING""",
                (
                    record["message_id"],
                    record["session_id"],
                    int(record["sequence"]),
                    record["role"],
                    _content_hash(str(record.get("content") or "")),
                    archive_path,
                    json.dumps(record.get("metadata") or {}, ensure_ascii=False),
                    float(record["created_at"]),
                ),
            )

    def record_summary(self, summary: dict[str, Any], archive_path: str):
        with self.lock, self.connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO souldrive_session_summaries
                   (session_id, version, covered_sequence_start, covered_sequence_end, summary_hash, archive_path, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (session_id, version) DO UPDATE SET
                     summary_hash = EXCLUDED.summary_hash,
                     archive_path = EXCLUDED.archive_path""",
                (
                    summary["session_id"],
                    int(summary["version"]),
                    int(summary["covered_sequence_start"]),
                    int(summary["covered_sequence_end"]),
                    _content_hash(str(summary.get("summary") or "")),
                    archive_path,
                    float(summary["created_at"]),
                ),
            )
            cursor.execute(
                "UPDATE souldrive_sessions SET summary_version = GREATEST(summary_version, %s), updated_at = %s WHERE session_id = %s",
                (int(summary["version"]), time.time(), summary["session_id"]),
            )

    def upsert_memory(self, memory: dict[str, Any], archive_path: str, project_id: str | None = None, session_id: str | None = None):
        memory_id = str(memory.get("memory_id") or memory.get("skill_id") or "")
        now = time.time()
        metadata = {
            "title": memory.get("title"),
            "subtype": memory.get("subtype"),
            "source_candidate_id": memory.get("source_candidate_id"),
            "tags": memory.get("tags") or [],
        }
        with self.lock, self.connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO souldrive_memory_records
                   (memory_id, project_id, session_id, memory_type, status, content_hash, archive_path, metadata_json, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                   ON CONFLICT (memory_id) DO UPDATE SET
                     project_id = COALESCE(EXCLUDED.project_id, souldrive_memory_records.project_id),
                     status = EXCLUDED.status,
                     content_hash = EXCLUDED.content_hash,
                     archive_path = EXCLUDED.archive_path,
                     metadata_json = EXCLUDED.metadata_json,
                     updated_at = EXCLUDED.updated_at""",
                (
                    memory_id,
                    project_id,
                    session_id,
                    "memory" if memory.get("memory_id") else "skill",
                    str(memory.get("status") or "active"),
                    _content_hash(str(memory.get("content") or memory.get("text") or "")),
                    archive_path,
                    json.dumps(metadata, ensure_ascii=False),
                    float(memory.get("created_at") or now),
                    now,
                ),
            )

    def enqueue(self, event: dict[str, Any]):
        with self.lock, self.connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO souldrive_outbox_events
                   (event_id, event_type, aggregate_id, payload_json, status, created_at, updated_at)
                   VALUES (%s, %s, %s, %s::jsonb, 'pending', %s, %s)
                   ON CONFLICT (event_id) DO NOTHING""",
                (
                    event["event_id"],
                    event["event_type"],
                    event["aggregate_id"],
                    json.dumps(event.get("payload") or {}, ensure_ascii=False),
                    float(event.get("created_at") or time.time()),
                    time.time(),
                ),
            )

    def mark_outbox(self, event_id: str, status: str, error: str = ""):
        with self.lock, self.connection.cursor() as cursor:
            cursor.execute(
                "UPDATE souldrive_outbox_events SET status = %s, attempts = attempts + 1, last_error = %s, updated_at = %s WHERE event_id = %s",
                (status, error[:1000], time.time(), event_id),
            )

    def pending_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 1000))
        with self.lock, self.connection.cursor() as cursor:
            cursor.execute(
                """SELECT event_id, event_type, aggregate_id, payload_json,
                          status, attempts, last_error, created_at, updated_at
                     FROM souldrive_outbox_events
                     WHERE status = 'pending'
                     ORDER BY created_at ASC
                     LIMIT %s""",
                (safe_limit,),
            )
            return list(cursor.fetchall())

    def health(self) -> dict[str, Any]:
        with self.lock, self.connection.cursor() as cursor:
            cursor.execute("SELECT 1 AS ok")
            cursor.fetchone()
            cursor.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM souldrive_sessions) AS session_count, "
                "(SELECT COUNT(*) FROM souldrive_session_messages) AS message_count, "
                "(SELECT COUNT(*) FROM souldrive_session_summaries) AS summary_count, "
                "(SELECT COUNT(*) FROM souldrive_memory_records) AS memory_count, "
                "(SELECT COUNT(*) FROM souldrive_outbox_events WHERE status = 'pending') AS pending_outbox"
            )
            counts = dict(cursor.fetchone())
        return {"enabled": True, "status": "ok", "dsn": _redact_dsn(self.dsn), **counts}

    def close(self):
        with self.lock:
            self.connection.close()


class RedisSessionStore:
    def __init__(self, url: str, key: bytes, *, prefix: str = "souldrive", max_messages: int = 32, ttl_seconds: int = 604800):
        import redis

        self.client = redis.Redis.from_url(
            url,
            decode_responses=False,
            socket_connect_timeout=0.8,
            socket_timeout=0.8,
            health_check_interval=30,
        )
        self.client.ping()
        self.key = key
        self.url = url
        self.prefix = prefix
        self.max_messages = max(4, min(int(max_messages), 200))
        self.ttl_seconds = max(60, min(int(ttl_seconds), 2_592_000))
        self.vector_index_name = f"{prefix}:memory_idx"
        self.vector_set_key = f"{prefix}:memory_vectors"
        self._vector_dimension: int | None = None
        self._vector_index_error: str | None = None
        self._vector_backend: str | None = None
        self._restore_vector_set_state()

    def _restore_vector_set_state(self) -> None:
        """Recover a Redis 8 Vector Set's dimension after a process restart."""
        try:
            raw = self.client.execute_command("VINFO", self.vector_set_key)
        except Exception:
            return
        values = list(raw or [])
        for index in range(0, len(values) - 1, 2):
            key = values[index].decode("utf-8", errors="replace") if isinstance(values[index], bytes) else str(values[index])
            if key != "vector-dim":
                continue
            try:
                dimension = int(values[index + 1])
            except (TypeError, ValueError):
                return
            if dimension > 0:
                self._vector_dimension = dimension
                self._vector_backend = "vectorset"
                self._vector_index_error = None
            return

    def _session_key(self, session_id: str) -> str:
        return f"{self.prefix}:session:{session_id}:messages"

    def _seen_key(self, session_id: str) -> str:
        return f"{self.prefix}:session:{session_id}:message_ids"

    def _summary_key(self, session_id: str) -> str:
        return f"{self.prefix}:session:{session_id}:summary"

    def _metadata_key(self, session_id: str) -> str:
        return f"{self.prefix}:session:{session_id}:metadata"

    def set_session_metadata(self, metadata: dict[str, Any]) -> None:
        session_id = str(metadata.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        self.client.setex(
            self._metadata_key(session_id),
            self.ttl_seconds,
            _seal(metadata, self.key, f"metadata:{session_id}".encode()),
        )

    def delete_session(self, session_id: str) -> None:
        session_id = str(session_id or "").strip()
        if not session_id:
            return
        self.client.delete(
            self._session_key(session_id),
            self._seen_key(session_id),
            self._summary_key(session_id),
            self._metadata_key(session_id),
        )

    def append(self, record: dict[str, Any]):
        session_id = str(record["session_id"])
        message_id = str(record["message_id"])
        if not self.client.sadd(self._seen_key(session_id), message_id):
            return False
        token = _seal(record, self.key, f"session:{session_id}".encode())
        pipe = self.client.pipeline()
        pipe.rpush(self._session_key(session_id), token)
        pipe.ltrim(self._session_key(session_id), -self.max_messages, -1)
        pipe.expire(self._session_key(session_id), self.ttl_seconds)
        pipe.expire(self._seen_key(session_id), self.ttl_seconds)
        pipe.execute()
        return True

    def recent(self, session_id: str, limit: int = 8) -> list[dict[str, Any]]:
        values = self.client.lrange(self._session_key(session_id), -max(1, min(int(limit), self.max_messages)), -1)
        return [_unseal(value, self.key, f"session:{session_id}".encode()) for value in values]

    def set_summary(self, session_id: str, summary: dict[str, Any]):
        self.client.setex(
            self._summary_key(session_id),
            self.ttl_seconds,
            _seal(summary, self.key, f"summary:{session_id}".encode()),
        )

    def get_summary(self, session_id: str) -> dict[str, Any] | None:
        value = self.client.get(self._summary_key(session_id))
        return _unseal(value, self.key, f"summary:{session_id}".encode()) if value else None

    def ensure_vector_index(self, dimension: int) -> bool:
        dimension = int(dimension)
        if dimension <= 0:
            return False
        if self._vector_backend == "vectorset" and self._vector_dimension == dimension:
            return True
        if self._vector_backend == "vectorset" and self._vector_dimension is not None:
            self._vector_index_error = (
                f"vector dimension mismatch: expected {self._vector_dimension}, got {dimension}"
            )
            return False
        try:
            self.client.execute_command("FT.INFO", self.vector_index_name)
            self._vector_dimension = dimension
            self._vector_index_error = None
            self._vector_backend = "redisearch"
            return True
        except Exception:
            pass
        try:
            self.client.execute_command(
                "FT.CREATE", self.vector_index_name, "ON", "HASH", "PREFIX", "1", f"{self.prefix}:memory:",
                "SCHEMA", "session_id", "TAG", "memory_type", "TAG", "memory_id", "TAG",
                "embedding", "VECTOR", "HNSW", "6", "TYPE", "FLOAT32", "DIM", dimension,
                "DISTANCE_METRIC", "COSINE",
            )
            self._vector_dimension = dimension
            self._vector_index_error = None
            self._vector_backend = "redisearch"
            return True
        except Exception as exc:
            redisearch_error = exc
        try:
            # Redis 8 Vector Sets are the native replacement when RediSearch is absent.
            self.client.execute_command("COMMAND INFO", "VADD")
            self._vector_dimension = dimension
            self._vector_index_error = None
            self._vector_backend = "vectorset"
            return True
        except Exception as exc:
            self._vector_index_error = f"{redisearch_error}; {exc}"
            logger.info("Redis vector index unavailable: %s", self._vector_index_error)
            return False

    def put_vector(self, *, memory_id: str, session_id: str | None, memory_type: str, vector: list[float], archive_path: str) -> bool:
        if not self.ensure_vector_index(len(vector)):
            return False
        if self._vector_backend == "vectorset":
            try:
                self.client.execute_command(
                    "VADD", self.vector_set_key, "FP32",
                    struct.pack(f"<{len(vector)}f", *(float(value) for value in vector)), memory_id,
                    "SETATTR", json.dumps({
                        "memory_id": memory_id,
                        "session_id": session_id or "",
                        "memory_type": memory_type,
                        "archive_path": archive_path,
                    }, ensure_ascii=False, separators=(",", ":")),
                )
                self.client.expire(self.vector_set_key, self.ttl_seconds)
                return True
            except Exception as exc:
                self._vector_index_error = str(exc)
                logger.info("Redis Vector Set write failed: %s", exc)
                return False
        key = f"{self.prefix}:memory:{memory_id}"
        self.client.hset(
            key,
            mapping={
                "session_id": session_id or "",
                "memory_type": memory_type,
                "memory_id": memory_id,
                "archive_path": archive_path,
                "embedding": struct.pack(f"<{len(vector)}f", *(float(value) for value in vector)),
            },
        )
        self.client.expire(key, self.ttl_seconds)
        return True

    def remove_vector(self, memory_id: str) -> bool:
        memory_id = str(memory_id or "").strip()
        if not memory_id:
            return False
        if self._vector_backend == "vectorset":
            try:
                self.client.execute_command("VREM", self.vector_set_key, memory_id)
                return True
            except Exception:
                # Redis 8 Vector Set is optional; a stale member is harmless when
                # the local learning index is already archived.
                return False
        return bool(self.client.delete(f"{self.prefix}:memory:{memory_id}"))

    def search_vector(self, vector: list[float], *, session_id: str | None = None, top_k: int = 5) -> list[dict[str, Any]]:
        if not vector or not self._vector_dimension:
            return []
        safe_k = max(1, min(int(top_k), 20))
        if self._vector_backend == "vectorset":
            try:
                raw = self.client.execute_command(
                    "VSIM", self.vector_set_key, "FP32",
                    struct.pack(f"<{len(vector)}f", *(float(value) for value in vector)),
                    "WITHSCORES", "WITHATTRIBS", "COUNT", min(100, safe_k * 8),
                )
            except Exception:
                return []
            results = []
            for index in range(0, len(raw), 3):
                try:
                    attributes = json.loads(raw[index + 2].decode() if isinstance(raw[index + 2], bytes) else raw[index + 2])
                except (IndexError, TypeError, json.JSONDecodeError):
                    continue
                if session_id and attributes.get("session_id") != session_id:
                    continue
                attributes["similarity"] = float(raw[index + 1])
                results.append(attributes)
                if len(results) >= safe_k:
                    break
            return results
        query = f"@session_id:{{{_escape_tag(session_id)}}}=>[KNN {safe_k} @embedding $query_vec AS distance]" if session_id else f"*=>[KNN {safe_k} @embedding $query_vec AS distance]"
        try:
            raw = self.client.execute_command(
                "FT.SEARCH", self.vector_index_name, query, "PARAMS", "2", "query_vec",
                struct.pack(f"<{len(vector)}f", *(float(value) for value in vector)),
                "SORTBY", "distance", "RETURN", "4", "memory_id", "session_id", "memory_type", "archive_path", "DIALECT", "2",
            )
        except Exception:
            return []
        results = []
        for index in range(1, len(raw), 2):
            fields = dict(zip(raw[index + 1][::2], raw[index + 1][1::2]))
            results.append({
                key.decode() if isinstance(key, bytes) else key: value.decode() if isinstance(value, bytes) else value
                for key, value in fields.items()
            })
        return results

    def health(self) -> dict[str, Any]:
        info = self.client.info(section="server")
        config = self.client.config_get("appendonly")
        vector_status = "ready" if self._vector_dimension else "unavailable"
        vector_backend = self._vector_backend
        if not self._vector_dimension:
            try:
                self.client.execute_command("COMMAND INFO", "VADD")
                vector_status = "idle"
                vector_backend = "vectorset"
            except Exception:
                pass
        session_keys = []
        scan_iter = getattr(self.client, "scan_iter", None)
        if callable(scan_iter):
            session_keys = list(scan_iter(match=f"{self.prefix}:session:*:messages", count=100))
        message_count = sum(int(self.client.llen(key)) for key in session_keys)
        return {
            "enabled": True,
            "status": "ok",
            "url": _redact_redis_url(self.url),
            "redis_version": info.get("redis_version"),
            "aof_enabled": str(config.get("appendonly") or "no").lower() == "yes",
            "vector_index": bool(self._vector_dimension),
            "vector_index_status": vector_status,
            "vector_index_error": self._vector_index_error,
            "vector_index_backend": vector_backend,
            "session_cache_count": len(session_keys),
            "cached_message_count": message_count,
        }

    def close(self):
        self.client.close()


class ProductionSessionStore:
    """文件事实源 + PostgreSQL 控制面 + Redis 热状态，故障时逐项降级。"""

    def __init__(self, archive: ConversationStore):
        self.archive = archive
        self.redis: RedisSessionStore | None = None
        self.postgres: PostgresStateStore | None = None
        self.backend_errors: dict[str, str] = {}
        self.redis_enabled = os.environ.get("SOULDRIVE_REDIS_ENABLED", "1") != "0"
        self.postgres_enabled = os.environ.get("SOULDRIVE_POSTGRES_ENABLED", "1") != "0"
        self._connect_backends()
        try:
            self.replay_outbox()
        except ValueError as exc:
            # A legacy archive may have been created with a previous workspace
            # key. Keep the SQLite/file session store usable; new messages are
            # still written with the current key and the backend error remains
            # observable for diagnostics.
            self.backend_errors["outbox"] = str(exc)
            logger.warning("conversation outbox could not be replayed; continuing with archive: %s", exc)

    def _connect_backends(self) -> None:
        if self.redis is None and self.redis_enabled:
            try:
                self.redis = RedisSessionStore(
                    os.environ.get("SOULDRIVE_REDIS_URL") or DEFAULT_REDIS_URL,
                    self.archive.key,
                    max_messages=int(os.environ.get("SOULDRIVE_REDIS_MAX_MESSAGES", "32")),
                    ttl_seconds=int(os.environ.get("SOULDRIVE_REDIS_TTL_SECONDS", "604800")),
                )
                self.backend_errors.pop("redis", None)
            except Exception as exc:
                self.backend_errors["redis"] = str(exc)
                logger.warning("Redis unavailable; using file archive: %s", exc)
        if self.postgres is None and self.postgres_enabled:
            try:
                self.postgres = PostgresStateStore(
                    os.environ.get("SOULDRIVE_POSTGRES_URL") or DEFAULT_POSTGRES_URL
                )
                self.backend_errors.pop("postgres", None)
            except Exception as exc:
                self.backend_errors["postgres"] = str(exc)
                logger.warning("PostgreSQL unavailable; using local outbox: %s", exc)

    def _reset_backend(self, name: str, error: Exception | None = None) -> None:
        backend = getattr(self, name, None)
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass
        setattr(self, name, None)
        if error is not None:
            self.backend_errors[name] = str(error)

    def append_message(self, session_id: str, role: str, content: str, *, metadata: dict[str, Any] | None = None, request_id: str | None = None, project_id: str | None = None) -> dict[str, Any]:
        metadata = dict(metadata or {})
        message_id = None
        if request_id:
            status = str(metadata.get("status") or "user")
            message_id = "msg_" + hashlib.sha256(
                f"{request_id}:{role}:{status}".encode("utf-8")
            ).hexdigest()[:40]
        record = self.archive.append(
            session_id,
            role,
            content,
            metadata,
            message_id=message_id,
            request_id=request_id,
        )
        if self.redis_enabled:
            self._try_backend(
                "redis",
                lambda: self._require_redis().append(record),
                event={"event_type": "redis.session_message", "aggregate_id": record["message_id"], "payload": {"record": record}},
            )
        if self.postgres_enabled:
            archive_path = str(self.archive._session_messages_path(session_id))
            self._try_backend(
                "postgres",
                lambda: (
                    self._require_postgres().upsert_session(session_id, project_id=project_id, archive_path=archive_path, latest_sequence=int(record["sequence"]), created_at=float(record["created_at"])),
                    self._require_postgres().record_message(record, archive_path),
                ),
                event={"event_type": "postgres.session_message", "aggregate_id": record["message_id"], "payload": {"record": record, "archive_path": archive_path}},
            )
        return record

    def create_session(
        self,
        session_id: str,
        *,
        project_id: str | None = None,
        title: str,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        manifest = self.archive.create_session(session_id, project_id=project_id, title=title, workflow_id=workflow_id)
        if self.redis_enabled:
            self._try_backend(
                "redis",
                lambda: self._require_redis().set_session_metadata(manifest),
                event={
                    "event_type": "redis.session_created",
                    "aggregate_id": session_id,
                    "payload": {"metadata": manifest},
                },
            )
        if self.postgres_enabled:
            archive_path = str(self.archive._session_messages_path(session_id))
            self._try_backend(
                "postgres",
                lambda: self._require_postgres().upsert_session(
                    session_id,
                    project_id=project_id,
                    archive_path=archive_path,
                    latest_sequence=0,
                    created_at=float(manifest["created_at"]),
                    title=str(manifest.get("title") or ""),
                ),
                event={
                    "event_type": "postgres.session_created",
                    "aggregate_id": session_id,
                    "payload": {
                        "session_id": session_id,
                        "project_id": project_id,
                        "title": manifest.get("title"),
                        "archive_path": archive_path,
                        "created_at": manifest["created_at"],
                    },
                },
            )
        return manifest

    def delete_session(self, session_id: str, *, project_id: str) -> dict[str, Any] | None:
        metadata = self.archive.delete_session(session_id, project_id=project_id)
        if metadata is None:
            return None
        if self.redis_enabled:
            self._try_backend(
                "redis",
                lambda: self._require_redis().delete_session(session_id),
                event={
                    "event_type": "redis.session_deleted",
                    "aggregate_id": session_id,
                    "payload": {"session_id": session_id},
                },
            )
        if self.postgres_enabled:
            self._try_backend(
                "postgres",
                lambda: self._require_postgres().delete_session(session_id),
                event={
                    "event_type": "postgres.session_deleted",
                    "aggregate_id": session_id,
                    "payload": {"session_id": session_id},
                },
            )
        return metadata

    def session_metadata(self, session_id: str) -> dict[str, Any] | None:
        return self.archive.session_metadata(session_id)

    def list_sessions(self, project_id: str | None = None) -> list[dict[str, Any]]:
        return self.archive.list_sessions(project_id)

    def load_context(self, session_id: str, *, limit: int = 8) -> dict[str, Any]:
        self.replay_outbox(limit=20)
        archive_records = self.archive.recent(session_id, limit=max(limit, 32))
        redis_records: list[dict[str, Any]] = []
        if self.redis:
            try:
                redis_records = self.redis.recent(session_id, limit=max(limit, 32))
            except Exception as exc:
                self.backend_errors["redis"] = str(exc)
        merged: dict[str, dict[str, Any]] = {}
        for record in [*archive_records, *redis_records]:
            merged[str(record.get("message_id") or f"{record.get('session_id')}:{record.get('sequence')}")] = record
        records = sorted(merged.values(), key=lambda item: int(item.get("sequence") or 0))[-max(1, min(int(limit), 12)) :]
        summary = None
        if self.redis:
            try:
                summary = self.redis.get_summary(session_id)
            except Exception as exc:
                self.backend_errors["redis"] = str(exc)
        summary = summary or self.archive.latest_summary(session_id)
        if self.redis:
            for record in archive_records:
                try:
                    self.redis.append(record)
                except Exception as exc:
                    self.backend_errors["redis"] = str(exc)
        return {
            "history": [
                {"role": record["role"], "content": record["content"], "timestamp_ms": int(float(record["created_at"]) * 1000), "sequence": int(record.get("sequence") or 0)}
                for record in records if record.get("role") in {"user", "assistant"}
            ],
            "records": records,
            "summary": summary,
            "source": "redis+file" if redis_records else "file",
            "redis_count": len(redis_records),
            "archive_count": len(archive_records),
        }

    def load_before(self, session_id: str, before_sequence: int, *, limit: int = 8) -> dict[str, Any]:
        self.replay_outbox(limit=20)
        return {"records": self.archive.load_before(session_id, before_sequence, limit=limit), "source": "file"}

    def compact_session(
        self,
        session_id: str,
        *,
        project_id: str | None = None,
        threshold: int = 16,
        summary_builder: Callable[[list[dict[str, Any]], int], str] | None = None,
    ) -> dict[str, Any] | None:
        records = self.archive.recent(session_id, limit=200)
        if len(records) < max(4, int(threshold)):
            return None
        current = self.archive.latest_summary(session_id)
        result = build_recoverable_summary(records, summary_builder=summary_builder)
        if result is None:
            return None
        result["version"] = next_summary_version(current)
        result["project_id"] = project_id
        return self.persist_summary(result, project_id=project_id)

    def persist_summary(self, summary: dict[str, Any], *, project_id: str | None = None):
        session_id = str(summary["session_id"])
        payload = self.archive.write_summary(session_id, str(summary.get("summary") or ""), version=int(summary["version"]), covered_sequence_start=int(summary["covered_sequence_start"]), covered_sequence_end=int(summary["covered_sequence_end"]), source_message_ids=list(summary.get("source_message_ids") or []))
        if self.redis_enabled:
            self._try_backend("redis", lambda: self._require_redis().set_summary(session_id, payload), event={"event_type": "redis.session_summary", "aggregate_id": session_id, "payload": {"summary": payload}})
        if self.postgres_enabled:
            archive_path = str(self.archive._summary_path(session_id, int(payload["version"])))
            self._try_backend("postgres", lambda: self._require_postgres().record_summary(payload, archive_path), event={"event_type": "postgres.session_summary", "aggregate_id": session_id, "payload": {"summary": payload, "archive_path": archive_path}})
        return payload

    def persist_memory(self, memory: dict[str, Any], *, project_id: str | None = None, session_id: str | None = None, vector: list[float] | None = None):
        archive_path = self.archive.archive_memory(memory)
        memory_id = str(memory.get("memory_id") or memory.get("skill_id"))
        if self.postgres_enabled:
            self._try_backend(
                "postgres",
                lambda: self._require_postgres().upsert_memory(memory, archive_path, project_id, session_id),
                event={
                    "event_type": "postgres.memory",
                    "aggregate_id": memory_id,
                    "payload": {
                        "memory": memory,
                        "archive_path": archive_path,
                        "project_id": project_id,
                        "session_id": session_id,
                    },
                },
            )
        if self.redis_enabled and vector:
            self._try_backend(
                "redis",
                lambda: self._require_redis().put_vector(
                    memory_id=memory_id,
                    session_id=session_id,
                    memory_type="memory" if memory.get("memory_id") else "skill",
                    vector=vector,
                    archive_path=archive_path,
                ),
                event={
                    "event_type": "redis.memory_vector",
                    "aggregate_id": memory_id,
                    "payload": {
                        "archive_path": archive_path,
                        "session_id": session_id,
                        "memory_type": "memory" if memory.get("memory_id") else "skill",
                        "vector": list(vector),
                    },
                },
            )
        return archive_path

    def revoke_memory(self, memory: dict[str, Any], *, project_id: str | None = None, session_id: str | None = None):
        archive_path = self.archive.archive_memory(memory)
        memory_id = str(memory.get("memory_id") or "")
        if not memory_id:
            raise ValueError("memory_id is required")
        if self.postgres_enabled:
            self._try_backend(
                "postgres",
                lambda: self._require_postgres().upsert_memory(memory, archive_path, project_id, session_id),
                event={
                    "event_type": "postgres.memory",
                    "aggregate_id": memory_id,
                    "payload": {
                        "memory": memory,
                        "archive_path": archive_path,
                        "project_id": project_id,
                        "session_id": session_id,
                    },
                },
            )
        if self.redis_enabled:
            self._try_backend(
                "redis",
                lambda: self._require_redis().remove_vector(memory_id),
                event={
                    "event_type": "redis.memory_vector_delete",
                    "aggregate_id": memory_id,
                    "payload": {"memory_id": memory_id},
                },
            )
        return archive_path

    def health(self) -> dict[str, Any]:
        self._connect_backends()
        result: dict[str, Any] = {
            "schema_version": "storage_health_v1",
            "file_archive": {
                "enabled": True,
                "status": "degraded" if (self.archive.last_index_error or self.archive.archive_read_error) else "ok",
                "root": str(self.archive.archive_root),
                "sqlite_index_error": self.archive.last_index_error,
                "archive_read_error": self.archive.archive_read_error,
            },
            "redis": {
                "enabled": self.redis_enabled,
                "status": "disabled" if not self.redis_enabled else "unavailable",
                "error": self.backend_errors.get("redis"),
            },
            "postgres": {
                "enabled": self.postgres_enabled,
                "status": "disabled" if not self.postgres_enabled else "unavailable",
                "error": self.backend_errors.get("postgres"),
            },
        }
        if self.redis:
            try:
                result["redis"] = self.redis.health()
            except Exception as exc:
                self._reset_backend("redis", exc)
                self._connect_backends()
                if self.redis:
                    try:
                        result["redis"] = self.redis.health()
                        self.backend_errors.pop("redis", None)
                    except Exception as retry_exc:
                        self._reset_backend("redis", retry_exc)
                        result["redis"] = {"enabled": True, "status": "error", "error": str(retry_exc)}
                else:
                    result["redis"] = {"enabled": True, "status": "error", "error": str(exc)}
        if self.postgres:
            try:
                result["postgres"] = self.postgres.health()
            except Exception as exc:
                self._reset_backend("postgres", exc)
                self._connect_backends()
                if self.postgres:
                    try:
                        result["postgres"] = self.postgres.health()
                        self.backend_errors.pop("postgres", None)
                    except Exception as retry_exc:
                        self._reset_backend("postgres", retry_exc)
                        result["postgres"] = {"enabled": True, "status": "error", "error": str(retry_exc)}
                else:
                    result["postgres"] = {"enabled": True, "status": "error", "error": str(exc)}
        try:
            pending_outbox = len(self.archive.pending_outbox())
        except ValueError as exc:
            self.backend_errors["outbox"] = str(exc)
            pending_outbox = 0
        result["outbox"] = {
            "pending": pending_outbox,
            "path": str(self.archive.outbox_path),
        }
        result["degraded"] = bool(self.backend_errors) or bool(
            self.archive.last_index_error or self.archive.archive_read_error
        ) or any(
            item.get("enabled") and item.get("status") != "ok"
            for item in (result["redis"], result["postgres"])
        )
        return result

    def replay_outbox(self, limit: int = 100) -> dict[str, int]:
        """重放本地加密 outbox，并在 PostgreSQL 可用时重放其控制面 outbox。"""
        self._connect_backends()
        completed = 0
        failed = 0
        for event in self.archive.pending_outbox(limit=limit):
            try:
                self._dispatch_event(event)
                self.archive.mark_outbox(str(event["event_id"]), "completed")
                completed += 1
            except Exception as exc:
                failed += 1
                self.archive.mark_outbox(str(event["event_id"]), "pending", str(exc))

        if self.postgres:
            for event in self.postgres.pending_outbox(limit=limit):
                try:
                    payload = event.get("payload_json") or {}
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    self._dispatch_event(
                        {
                            **event,
                            "payload": payload,
                        }
                    )
                    self.postgres.mark_outbox(str(event["event_id"]), "completed")
                    completed += 1
                except Exception as exc:
                    failed += 1
                    self.postgres.mark_outbox(str(event["event_id"]), "pending", str(exc))
        return {"completed": completed, "failed": failed}

    def _dispatch_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("event_type") or "")
        payload = dict(event.get("payload") or {})
        if event_type == "redis.session_message":
            if not self.redis:
                raise RuntimeError("Redis is unavailable")
            record = payload.get("record") or self.archive.find_message(str(event.get("aggregate_id") or ""))
            if record and self.archive.session_metadata(str(record.get("session_id") or "")) is None:
                return
            if not record:
                session_id = str((payload.get("record") or {}).get("session_id") or payload.get("session_id") or "")
                if session_id and self.archive.session_metadata(session_id) is None:
                    return
                raise RuntimeError("conversation message is missing from file archive")
            self.redis.append(record)
            return
        if event_type == "postgres.session_message":
            if not self.postgres:
                raise RuntimeError("PostgreSQL is unavailable")
            record = payload.get("record") or self.archive.find_message(str(event.get("aggregate_id") or ""))
            if record and self.archive.session_metadata(str(record.get("session_id") or "")) is None:
                return
            if not record:
                session_id = str((payload.get("record") or {}).get("session_id") or payload.get("session_id") or "")
                if session_id and self.archive.session_metadata(session_id) is None:
                    return
                raise RuntimeError("conversation message is missing from file archive")
            archive_path = str(payload.get("archive_path") or self.archive._session_messages_path(record["session_id"]))
            metadata = record.get("metadata") or {}
            self.postgres.upsert_session(
                record["session_id"],
                project_id=metadata.get("project_id"),
                archive_path=archive_path,
                latest_sequence=int(record["sequence"]),
                created_at=float(record["created_at"]),
            )
            self.postgres.record_message(record, archive_path)
            return
        if event_type == "postgres.session_created":
            if not self.postgres:
                raise RuntimeError("PostgreSQL is unavailable")
            session_id = str(payload.get("session_id") or event.get("aggregate_id") or "")
            if self.archive.session_metadata(session_id) is None:
                return
            self.postgres.upsert_session(
                session_id,
                project_id=payload.get("project_id"),
                archive_path=str(payload["archive_path"]),
                latest_sequence=0,
                created_at=float(payload["created_at"]),
                title=payload.get("title"),
            )
            return
        if event_type == "redis.session_created":
            if not self.redis:
                raise RuntimeError("Redis is unavailable")
            session_id = str(event.get("aggregate_id") or (payload.get("metadata") or {}).get("session_id") or "")
            if self.archive.session_metadata(session_id) is None:
                return
            metadata = payload.get("metadata") or self.archive.session_metadata(session_id)
            if not metadata:
                raise RuntimeError("conversation session is missing from file archive")
            self.redis.set_session_metadata(metadata)
            return
        if event_type == "redis.session_deleted":
            if not self.redis:
                raise RuntimeError("Redis is unavailable")
            self.redis.delete_session(str(event.get("aggregate_id") or payload.get("session_id") or ""))
            return
        if event_type == "postgres.session_deleted":
            if not self.postgres:
                raise RuntimeError("PostgreSQL is unavailable")
            self.postgres.delete_session(str(event.get("aggregate_id") or payload.get("session_id") or ""))
            return
        if event_type == "redis.session_summary":
            if not self.redis:
                raise RuntimeError("Redis is unavailable")
            summary = payload.get("summary") or self.archive.latest_summary(str(event.get("aggregate_id") or ""))
            if not summary:
                raise RuntimeError("conversation summary is missing from file archive")
            self.redis.set_summary(str(summary["session_id"]), summary)
            return
        if event_type == "postgres.session_summary":
            if not self.postgres:
                raise RuntimeError("PostgreSQL is unavailable")
            summary = payload.get("summary") or self.archive.latest_summary(str(event.get("aggregate_id") or ""))
            if not summary:
                raise RuntimeError("conversation summary is missing from file archive")
            archive_path = str(payload.get("archive_path") or self.archive._summary_path(summary["session_id"], int(summary["version"])))
            self.postgres.record_summary(summary, archive_path)
            return
        if event_type == "postgres.memory":
            if not self.postgres:
                raise RuntimeError("PostgreSQL is unavailable")
            memory_id = str(event.get("aggregate_id") or "")
            memory = payload.get("memory") or self.archive.read_memory(memory_id)
            if not memory:
                raise RuntimeError("durable memory is missing from file archive")
            archive_path = str(payload.get("archive_path") or self.archive.memory_root / f"{memory_id}.json.enc")
            self.postgres.upsert_memory(memory, archive_path, payload.get("project_id"), payload.get("session_id"))
            return
        if event_type == "redis.memory_vector":
            if not self.redis:
                raise RuntimeError("Redis is unavailable")
            vector = payload.get("vector") or []
            if not vector:
                raise RuntimeError("memory vector is missing")
            self.redis.put_vector(
                memory_id=str(event.get("aggregate_id") or payload.get("memory_id") or ""),
                session_id=payload.get("session_id"),
                memory_type=str(payload.get("memory_type") or "memory"),
                vector=[float(value) for value in vector],
                archive_path=str(payload.get("archive_path") or ""),
            )
            return
        if event_type == "redis.memory_vector_delete":
            if not self.redis:
                raise RuntimeError("Redis is unavailable")
            self.redis.remove_vector(str(event.get("aggregate_id") or payload.get("memory_id") or ""))
            return
        raise RuntimeError(f"unsupported outbox event: {event_type}")

    def _try_backend(self, name: str, operation: Callable[[], Any], *, event: dict[str, Any]):
        try:
            operation()
            self.backend_errors.pop(name, None)
        except Exception as exc:
            self._reset_backend(name, exc)
            self.backend_errors[name] = str(exc)
            logger.warning("storage backend %s operation failed: %s", name, exc)
            event_id = f"{name}_{event['aggregate_id']}"
            local_event = {"event_id": event_id, "status": "pending", **event}
            self.archive.append_outbox(local_event)
            if self.postgres and name != "postgres":
                try:
                    self.postgres.enqueue(
                        {
                            **event,
                            "event_id": event_id,
                            "payload": self._control_payload(event),
                        }
                    )
                except Exception as outbox_exc:
                    logger.warning("PostgreSQL outbox unavailable: %s", outbox_exc)

    def _require_redis(self) -> RedisSessionStore:
        self._connect_backends()
        if self.redis is None:
            raise RuntimeError("Redis is unavailable")
        return self.redis

    def _require_postgres(self) -> PostgresStateStore:
        self._connect_backends()
        if self.postgres is None:
            raise RuntimeError("PostgreSQL is unavailable")
        return self.postgres

    def _control_payload(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = dict(event.get("payload") or {})
        event_type = str(event.get("event_type") or "")
        record = payload.get("record") or {}
        control = {
            "archive_path": payload.get("archive_path"),
            "session_id": record.get("session_id") or payload.get("session_id"),
            "message_id": record.get("message_id") or payload.get("message_id"),
            "memory_id": payload.get("memory_id"),
            "project_id": payload.get("project_id") or (record.get("metadata") or {}).get("project_id"),
        }
        if event_type == "redis.session_created":
            metadata = dict(payload.get("metadata") or {})
            control.update(
                {
                    "session_id": metadata.get("session_id") or control.get("session_id"),
                    "project_id": metadata.get("project_id") or control.get("project_id"),
                    "metadata": metadata,
                }
            )
        if event_type == "redis.memory_vector":
            control.update(
                {
                    "memory_type": payload.get("memory_type"),
                    "vector": payload.get("vector") or [],
                }
            )
        return control

    def close(self):
        self.archive.close()
        if self.redis:
            self.redis.close()
            self.redis = None
        if self.postgres:
            self.postgres.close()
            self.postgres = None


def _seal(value: dict[str, Any], key: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), aad)
    return base64.b64encode(nonce + ciphertext)


def _unseal(value: bytes, key: bytes, aad: bytes) -> dict[str, Any]:
    raw = base64.b64decode(value)
    return json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], aad).decode("utf-8"))


def _content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _redact_dsn(value: str) -> str:
    return value.rsplit("@", 1)[-1] if "@" in value else value


def _redact_redis_url(value: str) -> str:
    return value.rsplit("@", 1)[-1] if "@" in value else value


def _escape_tag(value: str) -> str:
    return str(value or "").replace("-", "\\-").replace(" ", "\\ ").replace("|", "\\|")
