import base64
import hashlib
import json
import os
import struct
import threading
import time
from typing import Any, Callable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.conversation_store import (
    ConversationStore,
    memory_identity_digest,
    memory_write_is_stale,
    memory_version_conflict,
    outbox_identity_digest,
    summary_identity_digest,
)
from core.conversation_compaction import build_recoverable_summary, next_summary_version
from core.logging_config import get_logger
from core.skill_runtime import SUPPORTED_WORKFLOW_IDS

logger = get_logger(__name__)

DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"
DEFAULT_POSTGRES_URL = "postgresql://souldrive:souldrive@127.0.0.1:5432/souldrive"

# Redis Vector Set filters treat missing attributes as non-matches.  Persist
# explicit wide bounds so memories without a temporal boundary remain eligible
# for pre-filtered searches while legacy projections are still checked by the
# authoritative post-filter.
_VECTOR_VALID_FROM_FLOOR = -1.0e18
_VECTOR_INVALID_AT_CEILING = 1.0e18

_REDIS_APPEND_MESSAGE_SCRIPT = """
local message_id = ARGV[1]
local identity_digest = ARGV[2]
local existing_digest = redis.call('HGET', KEYS[3], message_id)
if existing_digest then
    if existing_digest ~= identity_digest then
        return -1
    end
    return 0
end
if redis.call('SISMEMBER', KEYS[2], message_id) == 1 then
    return 0
end
redis.call('RPUSH', KEYS[1], ARGV[3])
redis.call('LTRIM', KEYS[1], -tonumber(ARGV[4]), -1)
redis.call('SADD', KEYS[2], message_id)
redis.call('HSET', KEYS[3], message_id, identity_digest)
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[5]))
redis.call('EXPIRE', KEYS[2], tonumber(ARGV[5]))
redis.call('EXPIRE', KEYS[3], tonumber(ARGV[5]))
return 1
"""

_REDIS_SET_SESSION_METADATA_SCRIPT = """
local incoming_digest = ARGV[1]
local sealed = ARGV[2]
local ttl = tonumber(ARGV[3])
local expected_digest = ARGV[4]
local expected_value = ARGV[5]
local existing_digest = redis.call('GET', KEYS[2])
if existing_digest then
    if existing_digest ~= incoming_digest then
        return -1
    end
    redis.call('SETEX', KEYS[1], ttl, sealed)
    redis.call('EXPIRE', KEYS[2], ttl)
    return 0
end
local existing_value = redis.call('GET', KEYS[1])
if existing_value then
    if expected_value == '' or existing_value ~= expected_value then
        return -2
    end
    if expected_digest ~= incoming_digest then
        return -1
    end
end
redis.call('SETEX', KEYS[1], ttl, sealed)
redis.call('SETEX', KEYS[2], ttl, incoming_digest)
return existing_value and 0 or 1
"""

_REDIS_SET_SUMMARY_SCRIPT = """
local incoming_version = tonumber(ARGV[1])
local incoming_digest = ARGV[2]
local sealed = ARGV[3]
local ttl = tonumber(ARGV[4])
local expected_version = tonumber(ARGV[5])
local expected_digest = ARGV[6]
local expected_value = ARGV[7]
local current_value = redis.call('GET', KEYS[1])
if current_value then
    if expected_value == '' or current_value ~= expected_value then
        return -2
    end
    local stored_digest = redis.call('GET', KEYS[2])
    if stored_digest and stored_digest ~= '' and stored_digest ~= expected_digest then
        return -4
    end
    local stored_version = redis.call('GET', KEYS[3])
    local current_version = tonumber(stored_version or expected_version or 0)
    if current_version > incoming_version then
        return -3
    end
    if current_version == incoming_version then
        if expected_digest ~= incoming_digest then
            return -1
        end
        redis.call('SETEX', KEYS[2], ttl, incoming_digest)
        redis.call('SETEX', KEYS[3], ttl, tostring(current_version))
        return 0
    end
end
redis.call('SETEX', KEYS[1], ttl, sealed)
redis.call('SETEX', KEYS[2], ttl, incoming_digest)
redis.call('SETEX', KEYS[3], ttl, tostring(incoming_version))
return current_value and 2 or 1
"""


def _supported_workflow_id(value: Any) -> str | None:
    normalized = str(value or "").strip()
    if normalized and normalized not in SUPPORTED_WORKFLOW_IDS:
        raise ValueError("unsupported workflow_id")
    return normalized or None


def _session_metadata_identity_conflict(
    current: dict[str, Any],
    incoming: dict[str, Any],
) -> bool:
    current_project_id = str(current.get("project_id") or "").strip() or None
    incoming_project_id = str(incoming.get("project_id") or "").strip() or None
    current_title = str(current.get("title") or "").strip() or "新对话"
    incoming_title = str(incoming.get("title") or "").strip() or "新对话"
    current_workflow_id = str(current.get("workflow_id") or "").strip() or None
    incoming_workflow_id = str(incoming.get("workflow_id") or "").strip() or None
    return (
        current_project_id != incoming_project_id
        or current_title != incoming_title
        or current_workflow_id != incoming_workflow_id
    )


def _session_metadata_identity_digest(metadata: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "project_id": str(metadata.get("project_id") or "").strip() or None,
                "title": str(metadata.get("title") or "").strip() or "新对话",
                "workflow_id": str(metadata.get("workflow_id") or "").strip() or None,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _vector_filter_literal(value: Any) -> str:
    return json.dumps(str(value or ""), ensure_ascii=False)


def _redisearch_vector_metadata_filter(
    *,
    session_id: str | None,
    project_id: str | None,
    include_global: bool,
    as_of: float | None,
    embedding_model: str | None,
    workflow_id: str | None,
) -> str:
    parts = ["@projection_version:{memory_vector_v2}", "@status:{active}"]
    if project_id:
        project_tag = _escape_tag(project_id)
        scope_filter = (
            f"(@scope:{{global}} | (@scope:{{project}} @project_id:{{{project_tag}}}))"
            if include_global
            else f"(@scope:{{project}} @project_id:{{{project_tag}}})"
        )
    else:
        scope_filter = "@scope:{global}"
    parts.append(scope_filter)
    if session_id:
        parts.append(f"@session_id:{{{_escape_tag(session_id)}}}")
    if embedding_model:
        parts.append(f"@embedding_model:{{{_escape_tag(embedding_model)}}}")
    if workflow_id:
        workflow_tag = _escape_tag(workflow_id)
        # Workflow-bound memories and unbound shared memories are both valid
        # for a workflow query.  The negative wildcard keeps the latter in the
        # pre-filter instead of leaving all workflow filtering to post-KNN code.
        parts.append(f"(@workflow_id:{{{workflow_tag}}}|-@workflow_id:{{*}})")
    reference_time = float(as_of if as_of is not None else time.time())
    reference_text = format(reference_time, ".15g")
    # Missing numeric fields remain eligible; these negative ranges only
    # remove vectors that are provably not valid at the requested time.
    parts.append(f"-@valid_from:[({reference_text} +inf]")
    parts.append(f"-@invalid_at:[-inf {reference_text}]")
    return " ".join(parts)


def _vector_set_metadata_filter(
    *,
    session_id: str | None,
    project_id: str | None,
    include_global: bool,
    as_of: float | None,
    embedding_model: str | None,
    workflow_id: str | None,
) -> str:
    parts = [
        f".projection_version == {_vector_filter_literal('memory_vector_v2')}",
        f".status == {_vector_filter_literal('active')}",
    ]
    if project_id:
        project_literal = _vector_filter_literal(project_id)
        scope_expression = (
            f"(.scope == {_vector_filter_literal('global')} or "
            f"(.scope == {_vector_filter_literal('project')} and .project_id == {project_literal}))"
            if include_global
            else f"(.scope == {_vector_filter_literal('project')} and .project_id == {project_literal})"
        )
    else:
        scope_expression = f".scope == {_vector_filter_literal('global')}"
    parts.append(scope_expression)
    if session_id:
        parts.append(f".session_id == {_vector_filter_literal(session_id)}")
    if embedding_model:
        parts.append(f".embedding_model == {_vector_filter_literal(embedding_model)}")
    if workflow_id:
        parts.append(
            f"(.workflow_id == {_vector_filter_literal(workflow_id)} or "
            f".workflow_id == {_vector_filter_literal('')})"
        )
    reference_time = float(as_of if as_of is not None else time.time())
    reference_text = format(reference_time, ".15g")
    parts.append(f".valid_from <= {reference_text}")
    parts.append(f".invalid_at > {reference_text}")
    return " and ".join(parts)


def _redisearch_vector_dimension(info: Any) -> int | None:
    """Read an explicit VECTOR DIM from either RESP2 or RESP3 FT.INFO data."""
    def text(value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value or "")

    def mapping(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return {text(key).lower(): item for key, item in value.items()}
        if not isinstance(value, (list, tuple)):
            return {}
        result: dict[str, Any] = {}
        for index in range(0, len(value) - 1, 2):
            result[text(value[index]).lower()] = value[index + 1]
        return result

    top_level = mapping(info)
    attributes = top_level.get("attributes") or []
    if isinstance(attributes, dict):
        attributes = [attributes]
    if not isinstance(attributes, (list, tuple)):
        return None
    for attribute in attributes:
        fields = mapping(attribute)
        if text(fields.get("type")).upper() != "VECTOR":
            continue
        algorithm_attributes = fields.get("algorithm_attribute") or fields.get("algorithm_attributes")
        algorithm_fields = mapping(algorithm_attributes)
        try:
            dimension = int(text(algorithm_fields.get("dim")))
        except (TypeError, ValueError):
            continue
        if dimension > 0:
            return dimension
    return None


def _message_projection_identity_conflict(
    current: dict[str, Any] | tuple[Any, ...],
    record: dict[str, Any],
    content_hash: str,
) -> bool:
    if isinstance(current, dict):
        current_session_id = current.get("session_id")
        current_sequence = current.get("sequence")
        current_role = current.get("role")
        current_content_hash = current.get("content_hash")
        current_metadata = current.get("metadata_json")
    else:
        current_session_id, current_sequence, current_role, current_content_hash, current_metadata = current
    if isinstance(current_metadata, str):
        try:
            current_metadata = json.loads(current_metadata)
        except (TypeError, ValueError, json.JSONDecodeError):
            current_metadata = {}
    return (
        str(current_session_id or "") != str(record.get("session_id") or "")
        or int(current_sequence or 0) != int(record.get("sequence") or 0)
        or str(current_role or "") != str(record.get("role") or "")
        or str(current_content_hash or "") != content_hash
        or (current_metadata or {}) != (record.get("metadata") or {})
    )


def _memory_projection_identity_conflict(
    current: dict[str, Any] | tuple[Any, ...],
    memory: dict[str, Any],
    content_hash: str,
    project_id: str | None,
    session_id: str | None,
    metadata: dict[str, Any],
) -> bool:
    if isinstance(current, dict):
        current_project_id = current.get("project_id")
        current_session_id = current.get("session_id")
        current_status = current.get("status")
        current_content_hash = current.get("content_hash")
        current_metadata = current.get("metadata_json")
    else:
        current_project_id, current_session_id, current_status, current_content_hash, current_metadata = current
    if isinstance(current_metadata, str):
        try:
            current_metadata = json.loads(current_metadata)
        except (TypeError, ValueError, json.JSONDecodeError):
            current_metadata = {}
    current_metadata = current_metadata if isinstance(current_metadata, dict) else {}
    current_version = int(current_metadata.get("record_version") or 1)
    incoming_version = int(memory.get("record_version") or 1)
    if current_version != incoming_version:
        return False
    if str(current_status or "active").strip().lower() != str(memory.get("status") or "active").strip().lower():
        return False
    if str(current_content_hash or "") != content_hash:
        return True
    if str(current_project_id or "").strip() != str(project_id or "").strip():
        return True
    if str(current_session_id or "").strip() != str(session_id or "").strip():
        return True
    return any(
        key != "record_version"
        and key in metadata
        and current_metadata.get(key) != metadata.get(key)
        for key in current_metadata
    )


def _validated_memory_workflow(memory: dict[str, Any]) -> str | None:
    workflow_id = _supported_workflow_id(memory.get("workflow_id"))
    if str(memory.get("subtype") or "").strip() == "workflow_fact" and not workflow_id:
        raise ValueError("workflow_fact requires workflow_id")
    return workflow_id


def _configured_memory_embedding_model_id() -> str | None:
    provider = str(os.environ.get("SOULDRIVE_EMBEDDING_PROVIDER") or "").strip().lower()
    if provider != "ollama":
        return None
    model = str(
        os.environ.get("SOULDRIVE_OLLAMA_EMBEDDING_MODEL") or "qwen3-embedding:4b"
    ).strip()
    return f"{provider}:{model}" if model else None


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
                workflow_id TEXT,
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
                metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
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
                payload_hash TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                claim_token TEXT,
                claim_until DOUBLE PRECISION,
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
            )
            """,
            "ALTER TABLE souldrive_sessions ADD COLUMN IF NOT EXISTS title TEXT NOT NULL DEFAULT '新对话'",
            "ALTER TABLE souldrive_sessions ADD COLUMN IF NOT EXISTS workflow_id TEXT",
            "ALTER TABLE souldrive_session_summaries ADD COLUMN IF NOT EXISTS metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE souldrive_outbox_events ADD COLUMN IF NOT EXISTS payload_hash TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE souldrive_outbox_events ADD COLUMN IF NOT EXISTS claim_token TEXT",
            "ALTER TABLE souldrive_outbox_events ADD COLUMN IF NOT EXISTS claim_until DOUBLE PRECISION",
            "CREATE INDEX IF NOT EXISTS idx_souldrive_messages_session ON souldrive_session_messages(session_id, sequence)",
            "CREATE INDEX IF NOT EXISTS idx_souldrive_outbox_status ON souldrive_outbox_events(status, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_souldrive_outbox_claim ON souldrive_outbox_events(status, claim_until, created_at, event_id)",
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
        workflow_id: str | None = None,
    ):
        now = time.time()
        normalized_project_id = str(project_id or "").strip() or None
        normalized_title = str(title or "").strip() or "新对话"
        normalized_workflow_id = _supported_workflow_id(workflow_id)
        incoming_identity = {
            "project_id": normalized_project_id,
            "title": normalized_title,
            "workflow_id": normalized_workflow_id,
        }
        with self.lock, self.connection.cursor() as cursor:
            cursor.execute(
                """SELECT project_id, title, workflow_id
                   FROM souldrive_sessions
                   WHERE session_id = %s
                   FOR UPDATE""",
                (session_id,),
            )
            fetchone = getattr(cursor, "fetchone", None)
            current = fetchone() if callable(fetchone) else None
            if current:
                current_identity = (
                    dict(current)
                    if isinstance(current, dict)
                    else {
                        "project_id": current[0],
                        "title": current[1],
                        "workflow_id": current[2],
                    }
                )
                if _session_metadata_identity_conflict(current_identity, incoming_identity):
                    raise ValueError("session metadata conflict")
            cursor.execute(
                """INSERT INTO souldrive_sessions
                   (session_id, project_id, title, workflow_id, archive_path, latest_sequence, created_at, updated_at)
                   VALUES (%s, %s, COALESCE(NULLIF(%s, ''), '新对话'), %s, %s, %s, %s, %s)
                   ON CONFLICT (session_id) DO UPDATE SET
                     project_id = EXCLUDED.project_id,
                     title = EXCLUDED.title,
                     workflow_id = EXCLUDED.workflow_id,
                     archive_path = EXCLUDED.archive_path,
                     latest_sequence = GREATEST(souldrive_sessions.latest_sequence, EXCLUDED.latest_sequence),
                     updated_at = EXCLUDED.updated_at
                   WHERE souldrive_sessions.project_id IS NOT DISTINCT FROM EXCLUDED.project_id
                     AND souldrive_sessions.title = EXCLUDED.title
                     AND souldrive_sessions.workflow_id IS NOT DISTINCT FROM EXCLUDED.workflow_id""",
                (
                    session_id,
                    normalized_project_id,
                    normalized_title,
                    normalized_workflow_id,
                    archive_path,
                    latest_sequence,
                    created_at,
                    now,
                ),
            )
            if getattr(cursor, "rowcount", 1) == 0:
                cursor.execute(
                    """SELECT project_id, title, workflow_id
                       FROM souldrive_sessions
                       WHERE session_id = %s
                       FOR UPDATE""",
                    (session_id,),
                )
                fetchone = getattr(cursor, "fetchone", None)
                current = fetchone() if callable(fetchone) else None
                if not current:
                    raise RuntimeError("session projection conflict could not be resolved")
                current_identity = (
                    dict(current)
                    if isinstance(current, dict)
                    else {
                        "project_id": current[0],
                        "title": current[1],
                        "workflow_id": current[2],
                    }
                )
                if _session_metadata_identity_conflict(current_identity, incoming_identity):
                    raise ValueError("session metadata conflict")

    def delete_session(self, session_id: str) -> None:
        with self.lock, self.connection.cursor() as cursor:
            cursor.execute("DELETE FROM souldrive_sessions WHERE session_id = %s", (str(session_id),))

    def record_message(self, record: dict[str, Any], archive_path: str):
        content_hash = _content_hash(str(record.get("content") or ""))
        serialized_metadata = json.dumps(record.get("metadata") or {}, ensure_ascii=False)
        with self.lock, self.connection.cursor() as cursor:
            cursor.execute(
                """SELECT session_id, sequence, role, content_hash, metadata_json
                   FROM souldrive_session_messages
                   WHERE message_id = %s
                   FOR UPDATE""",
                (record["message_id"],),
            )
            fetchone = getattr(cursor, "fetchone", None)
            current = fetchone() if callable(fetchone) else None
            if current and _message_projection_identity_conflict(current, record, content_hash):
                raise ValueError("message projection conflict")
            if current:
                return
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
                    content_hash,
                    archive_path,
                    serialized_metadata,
                    float(record["created_at"]),
                ),
            )
            if getattr(cursor, "rowcount", 1) == 0:
                cursor.execute(
                    """SELECT session_id, sequence, role, content_hash, metadata_json
                       FROM souldrive_session_messages
                       WHERE message_id = %s
                       FOR UPDATE""",
                    (record["message_id"],),
                )
                fetchone = getattr(cursor, "fetchone", None)
                current = fetchone() if callable(fetchone) else None
                if not current:
                    raise RuntimeError("message projection conflict could not be resolved")
                if _message_projection_identity_conflict(current, record, content_hash):
                    raise ValueError("message projection conflict")

    def record_summary(self, summary: dict[str, Any], archive_path: str):
        summary_hash = _content_hash(str(summary.get("summary") or ""))
        identity_hash = summary_identity_digest(summary)
        with self.lock, self.connection.cursor() as cursor:
            cursor.execute(
                """SELECT summary_hash, metadata_json
                   FROM souldrive_session_summaries
                   WHERE session_id = %s AND version = %s
                   FOR UPDATE""",
                (summary["session_id"], int(summary["version"])),
            )
            current = cursor.fetchone()
            if current:
                current_summary_hash = (
                    current.get("summary_hash")
                    if isinstance(current, dict)
                    else current[0]
                )
                current_metadata = (
                    current.get("metadata_json")
                    if isinstance(current, dict)
                    else current[1]
                ) or {}
                current_identity_hash = current_metadata.get("identity_hash")
                if (
                    current_summary_hash != summary_hash
                    or (
                        current_identity_hash
                        and current_identity_hash != identity_hash
                    )
                ):
                    raise ValueError("summary version conflict")
            else:
                cursor.execute(
                    """INSERT INTO souldrive_session_summaries
                       (session_id, version, covered_sequence_start, covered_sequence_end, summary_hash, archive_path, metadata_json, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                       ON CONFLICT (session_id, version) DO NOTHING""",
                    (
                        summary["session_id"],
                        int(summary["version"]),
                        int(summary["covered_sequence_start"]),
                        int(summary["covered_sequence_end"]),
                        summary_hash,
                        archive_path,
                        json.dumps({
                            "identity_hash": identity_hash,
                            "input_sequence_start": summary.get("input_sequence_start"),
                            "input_sequence_end": summary.get("input_sequence_end"),
                            "input_message_count": summary.get("input_message_count"),
                            "excerpt_message_ids": summary.get("excerpt_message_ids"),
                            "excerpt_truncated": summary.get("excerpt_truncated"),
                            "excerpt_status": summary.get("excerpt_status") or "unknown",
                        }, ensure_ascii=False),
                        float(summary["created_at"]),
                    ),
                )
                if getattr(cursor, "rowcount", 1) == 0:
                    cursor.execute(
                        """SELECT summary_hash, metadata_json
                           FROM souldrive_session_summaries
                           WHERE session_id = %s AND version = %s
                           FOR UPDATE""",
                        (summary["session_id"], int(summary["version"])),
                    )
                    current = cursor.fetchone()
                    if not current:
                        raise RuntimeError("summary projection conflict could not be resolved")
                    current_summary_hash = (
                        current.get("summary_hash")
                        if isinstance(current, dict)
                        else current[0]
                    )
                    current_metadata = (
                        current.get("metadata_json")
                        if isinstance(current, dict)
                        else current[1]
                    ) or {}
                    current_identity_hash = current_metadata.get("identity_hash")
                    if (
                        current_summary_hash != summary_hash
                        or (
                            current_identity_hash
                            and current_identity_hash != identity_hash
                        )
                    ):
                        raise ValueError("summary version conflict")
            cursor.execute(
                "UPDATE souldrive_sessions SET summary_version = GREATEST(summary_version, %s), updated_at = %s WHERE session_id = %s",
                (int(summary["version"]), time.time(), summary["session_id"]),
            )

    def upsert_memory(self, memory: dict[str, Any], archive_path: str, project_id: str | None = None, session_id: str | None = None):
        memory_id = str(memory.get("memory_id") or memory.get("skill_id") or "")
        record_version = int(memory.get("record_version") or 1)
        now = time.time()
        metadata = {
            "title": memory.get("title"),
            "subtype": memory.get("subtype"),
            "source_candidate_id": memory.get("source_candidate_id"),
            "tags": memory.get("tags") or [],
            "record_version": record_version,
        }
        workflow_id = _validated_memory_workflow(memory)
        if workflow_id:
            metadata["workflow_id"] = workflow_id
        with self.lock, self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT project_id, session_id, status, content_hash, metadata_json "
                "FROM souldrive_memory_records WHERE memory_id = %s",
                (memory_id,),
            )
            current = cursor.fetchone()
            if current:
                current_metadata = current.get("metadata_json") if isinstance(current, dict) else current[4]
                if isinstance(current_metadata, str):
                    try:
                        current_metadata = json.loads(current_metadata)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        current_metadata = {}
                current_version = int(current_metadata.get("record_version") or 1)
                if current_version > record_version:
                    return
                content_hash = _content_hash(str(memory.get("content") or memory.get("text") or ""))
                if _memory_projection_identity_conflict(
                    current,
                    memory,
                    content_hash,
                    project_id,
                    session_id,
                    metadata,
                ):
                    raise ValueError("memory version conflict")
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

    def delete_memory(self, memory_id: str) -> None:
        with self.lock, self.connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM souldrive_memory_records WHERE memory_id = %s",
                (str(memory_id or ""),),
            )

    def cancel_memory_outbox(self, memory_id: str) -> None:
        with self.lock, self.connection.cursor() as cursor:
            cursor.execute(
                "UPDATE souldrive_outbox_events SET status = 'cancelled', last_error = %s, "
                "updated_at = %s WHERE status = 'pending' AND aggregate_id = %s",
                ("project_deleted", time.time(), str(memory_id or "")),
            )

    def enqueue(self, event: dict[str, Any]):
        event_id = str(event.get("event_id") or "").strip()
        if not event_id:
            raise ValueError("outbox event_id is required")
        identity_hash = outbox_identity_digest(event)
        with self.lock, self.connection.cursor() as cursor:
            cursor.execute(
                """SELECT event_type, aggregate_id, payload_hash, payload_json
                   FROM souldrive_outbox_events
                   WHERE event_id = %s
                   FOR UPDATE""",
                (event_id,),
            )
            fetchone = getattr(cursor, "fetchone", None)
            current = fetchone() if callable(fetchone) else None
            if current:
                if isinstance(current, dict):
                    current_event_type = current.get("event_type")
                    current_aggregate_id = current.get("aggregate_id")
                    current_hash = str(current.get("payload_hash") or "")
                    current_payload = current.get("payload_json") or {}
                else:
                    current_event_type, current_aggregate_id, current_hash, current_payload = current
                    current_hash = str(current_hash or "")
                if not current_hash:
                    if isinstance(current_payload, str):
                        try:
                            current_payload = json.loads(current_payload)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            current_payload = {}
                    current_hash = outbox_identity_digest({
                        "event_type": current_event_type,
                        "aggregate_id": current_aggregate_id,
                        "payload": current_payload,
                    })
                if current_hash != identity_hash:
                    raise ValueError("outbox event conflict")
                return
            cursor.execute(
                """INSERT INTO souldrive_outbox_events
                   (event_id, event_type, aggregate_id, payload_json, payload_hash, status, created_at, updated_at)
                   VALUES (%s, %s, %s, %s::jsonb, %s, 'pending', %s, %s)
                   ON CONFLICT (event_id) DO NOTHING""",
                (
                    event_id,
                    event["event_type"],
                    event["aggregate_id"],
                    json.dumps(event.get("payload") or {}, ensure_ascii=False),
                    identity_hash,
                    float(event.get("created_at") or time.time()),
                    time.time(),
                ),
            )
            if getattr(cursor, "rowcount", 1) == 0:
                cursor.execute(
                    """SELECT event_type, aggregate_id, payload_hash, payload_json
                       FROM souldrive_outbox_events
                       WHERE event_id = %s
                       FOR UPDATE""",
                    (event_id,),
                )
                current = fetchone() if callable(fetchone) else None
                if not current:
                    raise RuntimeError("outbox event conflict could not be resolved")
                if isinstance(current, dict):
                    current_hash = str(current.get("payload_hash") or "")
                else:
                    current_hash = str(current[2] or "")
                if current_hash != identity_hash:
                    raise ValueError("outbox event conflict")

    def mark_outbox(
        self,
        event_id: str,
        status: str,
        error: str = "",
        *,
        claim_token: str | None = None,
    ) -> bool:
        with self.lock, self.connection.cursor() as cursor:
            if claim_token:
                cursor.execute(
                    """
                    UPDATE souldrive_outbox_events
                    SET status = %s, last_error = %s, claim_token = NULL,
                        claim_until = NULL, updated_at = %s
                    WHERE event_id = %s AND status = 'in_flight' AND claim_token = %s
                    """,
                    (status, error[:1000], time.time(), event_id, str(claim_token)),
                )
            else:
                cursor.execute(
                    "UPDATE souldrive_outbox_events SET status = %s, attempts = attempts + 1, last_error = %s, updated_at = %s WHERE event_id = %s",
                    (status, error[:1000], time.time(), event_id),
                )
            return bool(cursor.rowcount)

    def pending_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 1000))
        with self.lock, self.connection.cursor() as cursor:
            cursor.execute(
                """SELECT event_id, event_type, aggregate_id, payload_json,
                          status, attempts, last_error, created_at, updated_at
                     FROM souldrive_outbox_events
                     WHERE status = 'pending'
                     ORDER BY created_at ASC, event_id ASC
                     LIMIT %s""",
                (safe_limit,),
            )
            return list(cursor.fetchall())

    def claim_pending_outbox(
        self,
        limit: int = 100,
        *,
        owner_id: str | None = None,
        lease_seconds: float = 300.0,
    ) -> list[dict[str, Any]]:
        """Atomically lease PostgreSQL outbox rows for one replay worker.

        ``SKIP LOCKED`` prevents concurrent workers from taking the same
        pending row. Expired leases are reclaimable after a worker crash; the
        returned claim token fences the later completion/failure update. The
        external dispatch remains at-least-once if a worker dies after
        dispatch and before acknowledging the row.
        """
        safe_limit = max(1, min(int(limit), 1000))
        safe_lease_seconds = max(1.0, min(float(lease_seconds), 3600.0))
        now = time.time()
        claim_until = now + safe_lease_seconds
        owner = str(owner_id or "postgres-outbox-worker").strip()[:128] or "postgres-outbox-worker"
        claim_token = f"{owner}:{os.urandom(8).hex()}"
        with self.lock, self.connection.cursor() as cursor:
            cursor.execute(
                """
                WITH claimable AS (
                    SELECT event_id
                    FROM souldrive_outbox_events
                    WHERE status = 'pending'
                       OR (status = 'in_flight' AND (claim_until IS NULL OR claim_until <= %s))
                    ORDER BY created_at ASC, event_id ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE souldrive_outbox_events AS events
                SET status = 'in_flight', attempts = events.attempts + 1,
                    claim_token = %s, claim_until = %s, updated_at = %s
                FROM claimable
                WHERE events.event_id = claimable.event_id
                RETURNING events.event_id, events.event_type, events.aggregate_id,
                          events.payload_json, events.status, events.attempts,
                          events.last_error, events.created_at, events.updated_at,
                          events.claim_token, events.claim_until
                """,
                (now, safe_limit, claim_token, claim_until, now),
            )
            rows = list(cursor.fetchall())
        normalized_rows = [dict(row) if not isinstance(row, dict) else row for row in rows]
        normalized_rows.sort(
            key=lambda row: (float(row.get("created_at") or 0.0), str(row.get("event_id") or ""))
        )
        return normalized_rows

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
    def __init__(
        self,
        url: str,
        key: bytes,
        *,
        prefix: str = "souldrive",
        max_messages: int = 32,
        ttl_seconds: int = 604800,
        memory_vector_ttl_seconds: int | None = None,
    ):
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
        raw_memory_vector_ttl = (
            None if memory_vector_ttl_seconds is None else int(memory_vector_ttl_seconds)
        )
        self.memory_vector_ttl_seconds = (
            None
            if raw_memory_vector_ttl is None or raw_memory_vector_ttl <= 0
            else max(60, min(raw_memory_vector_ttl, 2_592_000))
        )
        self.vector_index_name = f"{prefix}:memory_idx"
        self.vector_set_key = f"{prefix}:memory_vectors"
        self._vector_dimension: int | None = None
        self._vector_index_error: str | None = None
        self._vector_backend: str | None = None
        self._summary_lock = threading.RLock()
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

    def _message_digest_key(self, session_id: str) -> str:
        return f"{self.prefix}:session:{session_id}:message_digests"

    def _summary_key(self, session_id: str) -> str:
        return f"{self.prefix}:session:{session_id}:summary"

    def _summary_identity_key(self, session_id: str) -> str:
        return f"{self.prefix}:session:{session_id}:summary_identity"

    def _summary_version_key(self, session_id: str) -> str:
        return f"{self.prefix}:session:{session_id}:summary_version"

    def _metadata_key(self, session_id: str) -> str:
        return f"{self.prefix}:session:{session_id}:metadata"

    def _metadata_digest_key(self, session_id: str) -> str:
        return f"{self.prefix}:session:{session_id}:metadata_identity"

    def set_session_metadata(self, metadata: dict[str, Any]) -> None:
        session_id = str(metadata.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        _supported_workflow_id(metadata.get("workflow_id"))
        metadata_key = self._metadata_key(session_id)
        metadata_digest_key = self._metadata_digest_key(session_id)
        existing_value = self.client.get(metadata_key)
        expected_digest = ""
        if existing_value:
            existing = _unseal(
                existing_value,
                self.key,
                f"metadata:{session_id}".encode(),
            )
            if _session_metadata_identity_conflict(existing, metadata):
                raise ValueError("session metadata conflict")
            expected_digest = _session_metadata_identity_digest(existing)
        result = int(
            self.client.eval(
                _REDIS_SET_SESSION_METADATA_SCRIPT,
                2,
                metadata_key,
                metadata_digest_key,
                _session_metadata_identity_digest(metadata),
                _seal(metadata, self.key, f"metadata:{session_id}".encode()),
                str(self.ttl_seconds),
                expected_digest,
                existing_value or b"",
            )
            or 0
        )
        if result == -1:
            raise ValueError("session metadata conflict")
        if result == -2:
            raise ValueError("session metadata concurrent conflict")

    def delete_session(self, session_id: str) -> None:
        session_id = str(session_id or "").strip()
        if not session_id:
            return
        self.client.delete(
            self._session_key(session_id),
            self._seen_key(session_id),
            self._message_digest_key(session_id),
            self._summary_key(session_id),
            self._summary_identity_key(session_id),
            self._summary_version_key(session_id),
            self._metadata_key(session_id),
            self._metadata_digest_key(session_id),
        )

    def append(self, record: dict[str, Any]):
        session_id = str(record["session_id"])
        message_id = str(record["message_id"])
        identity_digest = _message_identity_digest(record)
        token = _seal(record, self.key, f"session:{session_id}".encode())
        result = int(
            self.client.eval(
                _REDIS_APPEND_MESSAGE_SCRIPT,
                3,
                self._session_key(session_id),
                self._seen_key(session_id),
                self._message_digest_key(session_id),
                message_id,
                identity_digest,
                token,
                str(self.max_messages),
                str(self.ttl_seconds),
            )
            or 0
        )
        if result == -1:
            raise ValueError("message projection conflict")
        return result == 1

    def recent(self, session_id: str, limit: int = 8) -> list[dict[str, Any]]:
        values = self.client.lrange(self._session_key(session_id), -max(1, min(int(limit), self.max_messages)), -1)
        return [_unseal(value, self.key, f"session:{session_id}".encode()) for value in values]

    def set_summary(self, session_id: str, summary: dict[str, Any]) -> bool:
        """Apply only the newest recoverable summary to the hot projection."""
        session_id = str(session_id or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        incoming_version = int(summary.get("version") or 0)
        with self._summary_lock:
            summary_key = self._summary_key(session_id)
            current_value = self.client.get(summary_key)
            current = (
                _unseal(current_value, self.key, f"summary:{session_id}".encode())
                if current_value
                else None
            )
            current_version = int((current or {}).get("version") or 0)
            incoming_digest = summary_identity_digest(summary)
            expected_digest = summary_identity_digest(current) if current else ""
            result = int(
                self.client.eval(
                    _REDIS_SET_SUMMARY_SCRIPT,
                    3,
                    summary_key,
                    self._summary_identity_key(session_id),
                    self._summary_version_key(session_id),
                    str(incoming_version),
                    incoming_digest,
                    _seal(summary, self.key, f"summary:{session_id}".encode()),
                    str(self.ttl_seconds),
                    str(current_version),
                    expected_digest,
                    current_value or b"",
                )
                or 0
            )
            if result in {1, 2, 0}:
                return True
            if result == -3:
                return False
            if result == -1:
                raise ValueError("summary version conflict")
            if result == -4:
                raise ValueError("summary projection identity conflict")
            if result == -2:
                raise ValueError("summary concurrent conflict")
            raise RuntimeError("summary projection update failed")

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
            info = self.client.execute_command("FT.INFO", self.vector_index_name)
            existing_dimension = _redisearch_vector_dimension(info)
            if existing_dimension is not None and existing_dimension != dimension:
                self._vector_dimension = existing_dimension
                self._vector_index_error = (
                    f"vector dimension mismatch: expected {existing_dimension}, got {dimension}"
                )
                self._vector_backend = "redisearch"
                return False
            self._vector_dimension = existing_dimension or dimension
            self._vector_index_error = None
            self._vector_backend = "redisearch"
            return True
        except Exception:
            pass
        try:
            self.client.execute_command(
                "FT.CREATE", self.vector_index_name, "ON", "HASH", "PREFIX", "1", f"{self.prefix}:memory:",
                "SCHEMA", "session_id", "TAG", "workflow_id", "TAG", "memory_type", "TAG", "memory_id", "TAG",
                "projection_version", "TAG", "scope", "TAG", "project_id", "TAG", "status", "TAG", "embedding_model", "TAG",
                "valid_from", "NUMERIC", "invalid_at", "NUMERIC",
                "embedding", "VECTOR", "HNSW", "6", "TYPE", "FLOAT32", "DIM", dimension,
                "DISTANCE_METRIC", "COSINE",
            )
            self._vector_dimension = dimension
            self._vector_index_error = None
            self._vector_backend = "redisearch"
            return True
        except Exception as exc:
            redisearch_error = exc
        # Another process may have created the RediSearch index between our
        # FT.INFO probe and FT.CREATE.  Recheck before falling back to the
        # Redis 8 Vector Set path; Redis 7 deployments may not expose VADD.
        try:
            info = self.client.execute_command("FT.INFO", self.vector_index_name)
            existing_dimension = _redisearch_vector_dimension(info)
            if existing_dimension is not None and existing_dimension != dimension:
                self._vector_dimension = existing_dimension
                self._vector_index_error = (
                    f"vector dimension mismatch: expected {existing_dimension}, got {dimension}"
                )
                self._vector_backend = "redisearch"
                return False
            self._vector_dimension = existing_dimension or dimension
            self._vector_index_error = None
            self._vector_backend = "redisearch"
            return True
        except Exception:
            pass
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

    def put_vector(
        self,
        *,
        memory_id: str,
        session_id: str | None,
        memory_type: str,
        vector: list[float],
        archive_path: str,
        scope: str | None = None,
        project_id: str | None = None,
        status: str = "active",
        valid_from: float | None = None,
        invalid_at: float | None = None,
        record_version: int = 1,
        embedding_model: str | None = None,
        workflow_id: str | None = None,
        memory_subtype: str | None = None,
    ) -> bool:
        normalized_workflow_id = _supported_workflow_id(workflow_id)
        if str(memory_subtype or "").strip() == "workflow_fact" and not normalized_workflow_id:
            raise ValueError("workflow_fact requires workflow_id")
        if not self.ensure_vector_index(len(vector)):
            return False
        normalized_scope = str(scope or "").strip().lower()
        if normalized_scope not in {"global", "project"}:
            normalized_scope = "project" if str(project_id or "").strip() else "global"
        normalized_project_id = str(project_id or "").strip() if normalized_scope == "project" else ""
        if normalized_scope == "project" and not normalized_project_id:
            raise ValueError("project_id is required for project-scoped vector memory")
        metadata = {
            "projection_version": "memory_vector_v2",
            "memory_id": memory_id,
            "session_id": session_id or "",
            "memory_type": memory_type,
            "scope": normalized_scope,
            "project_id": normalized_project_id,
            "status": str(status or "active").strip().lower() or "active",
            "archive_path": archive_path,
            "record_version": max(1, int(record_version or 1)),
            "valid_from": (
                float(valid_from)
                if valid_from is not None
                else _VECTOR_VALID_FROM_FLOOR
            ),
            "invalid_at": (
                float(invalid_at)
                if invalid_at is not None
                else _VECTOR_INVALID_AT_CEILING
            ),
        }
        if normalized_workflow_id:
            metadata["workflow_id"] = normalized_workflow_id
        else:
            # Keep shared vectors filterable by workflow-aware Vector Set
            # queries. Older projections may omit this field and are still
            # handled by the unfiltered compatibility retry below.
            metadata["workflow_id"] = ""
        normalized_embedding_model = str(embedding_model or "").strip()
        if normalized_embedding_model:
            metadata["embedding_model"] = normalized_embedding_model
        if self._vector_backend == "vectorset":
            try:
                self.client.execute_command(
                    "VADD", self.vector_set_key, "FP32",
                    struct.pack(f"<{len(vector)}f", *(float(value) for value in vector)), memory_id,
                    "SETATTR", json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                )
                if self.memory_vector_ttl_seconds is None:
                    self.client.persist(self.vector_set_key)
                else:
                    self.client.expire(self.vector_set_key, self.memory_vector_ttl_seconds)
                return True
            except Exception as exc:
                self._vector_index_error = str(exc)
                logger.info("Redis Vector Set write failed: %s", exc)
                return False
        key = f"{self.prefix}:memory:{memory_id}"
        mapping = {
            field: value
            for field, value in metadata.items()
            if field not in {"valid_from", "invalid_at"}
        }
        mapping["embedding"] = struct.pack(f"<{len(vector)}f", *(float(value) for value in vector))
        if "valid_from" in metadata:
            mapping["valid_from"] = metadata["valid_from"]
        if "invalid_at" in metadata:
            mapping["invalid_at"] = metadata["invalid_at"]
        self.client.hset(
            key,
            mapping=mapping,
        )
        if self.memory_vector_ttl_seconds is None:
            self.client.persist(key)
        else:
            self.client.expire(key, self.memory_vector_ttl_seconds)
        return True

    def remove_vector(self, memory_id: str) -> bool:
        memory_id = str(memory_id or "").strip()
        if not memory_id:
            return False
        if self._vector_backend == "vectorset":
            try:
                self.client.execute_command("VREM", self.vector_set_key, memory_id)
                return True
            except Exception as exc:
                self._vector_index_error = str(exc)
                logger.info("Redis Vector Set delete failed: %s", exc)
                raise
        return bool(self.client.delete(f"{self.prefix}:memory:{memory_id}"))

    @staticmethod
    def _vector_result_matches_scope(
        attributes: dict[str, Any],
        *,
        session_id: str | None,
        project_id: str | None,
        include_global: bool,
        as_of: float | None,
        embedding_model: str | None,
        workflow_id: str | None = None,
    ) -> bool:
        if str(attributes.get("projection_version") or "") != "memory_vector_v2":
            return False
        if session_id and str(attributes.get("session_id") or "") != session_id:
            return False
        if str(attributes.get("status") or "").strip().lower() != "active":
            return False
        expected_embedding_model = str(embedding_model or "").strip()
        if expected_embedding_model and str(attributes.get("embedding_model") or "").strip() != expected_embedding_model:
            return False
        requested_workflow_id = str(workflow_id or "").strip()
        if requested_workflow_id and requested_workflow_id not in SUPPORTED_WORKFLOW_IDS:
            return False
        stored_workflow_id = str(attributes.get("workflow_id") or "").strip()
        if stored_workflow_id and stored_workflow_id not in SUPPORTED_WORKFLOW_IDS:
            return False
        if stored_workflow_id and stored_workflow_id != requested_workflow_id:
            return False
        scope = str(attributes.get("scope") or "").strip().lower()
        stored_project_id = str(attributes.get("project_id") or "").strip()
        if scope not in {"global", "project"}:
            return False
        if project_id:
            if scope == "project" and stored_project_id != project_id:
                return False
            if scope == "global" and not include_global:
                return False
        elif scope != "global":
            return False
        reference_time = float(as_of if as_of is not None else time.time())
        try:
            valid_from = attributes.get("valid_from")
            if valid_from not in (None, "") and float(valid_from) > reference_time:
                return False
            invalid_at = attributes.get("invalid_at")
            if invalid_at not in (None, "") and float(invalid_at) <= reference_time:
                return False
        except (TypeError, ValueError):
            return False
        return True

    def search_vector(
        self,
        vector: list[float],
        *,
        session_id: str | None = None,
        project_id: str | None = None,
        include_global: bool = True,
        as_of: float | None = None,
        embedding_model: str | None = None,
        workflow_id: str | None = None,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        if not vector or not self._vector_dimension:
            return []
        safe_k = max(1, min(int(top_k), 20))
        candidate_k = min(100, max(safe_k * 8, safe_k))
        if self._vector_backend == "vectorset":
            vector_set_filter = _vector_set_metadata_filter(
                session_id=session_id,
                project_id=project_id,
                include_global=include_global,
                as_of=as_of,
                embedding_model=embedding_model,
                workflow_id=workflow_id,
            )
            vector_values = struct.pack(f"<{len(vector)}f", *(float(value) for value in vector))
            filtered_query = True
            try:
                raw = self.client.execute_command(
                    "VSIM", self.vector_set_key, "FP32",
                    vector_values,
                    "WITHSCORES", "WITHATTRIBS", "COUNT", candidate_k,
                    "FILTER", vector_set_filter, "FILTER-EF", 0,
                )
            except Exception:
                # Redis 8 vector-set filtering is optional across supported
                # deployments.  Preserve the pre-existing unfiltered query as
                # a compatibility fallback; the scope gate below remains
                # authoritative for returned attributes.
                try:
                    raw = self.client.execute_command(
                        "VSIM", self.vector_set_key, "FP32", vector_values,
                        "WITHSCORES", "WITHATTRIBS", "COUNT", candidate_k,
                    )
                    filtered_query = False
                except Exception:
                    return []
            results = []
            seen_memory_ids: set[str] = set()

            def append_filtered_results(raw_results: Any) -> None:
                for index in range(0, len(raw_results), 3):
                    try:
                        attributes = json.loads(
                            raw_results[index + 2].decode()
                            if isinstance(raw_results[index + 2], bytes)
                            else raw_results[index + 2]
                        )
                    except (IndexError, TypeError, json.JSONDecodeError):
                        continue
                    if not self._vector_result_matches_scope(
                        attributes,
                        session_id=session_id,
                        project_id=project_id,
                        include_global=include_global,
                        as_of=as_of,
                        embedding_model=embedding_model,
                        workflow_id=workflow_id,
                    ):
                        continue
                    memory_id = str(attributes.get("memory_id") or raw_results[index] or "").strip()
                    if memory_id and memory_id in seen_memory_ids:
                        continue
                    if memory_id:
                        seen_memory_ids.add(memory_id)
                    attributes["similarity"] = float(raw_results[index + 1])
                    results.append(attributes)
                    if len(results) >= safe_k:
                        break

            append_filtered_results(raw)
            if filtered_query and len(results) < safe_k:
                # Older Vector Set projections may not have the explicit
                # temporal sentinel fields.  Retry without the server-side
                # filter so the authoritative scope/time gate can still keep
                # those legacy records instead of silently losing recall.
                try:
                    compatibility_raw = self.client.execute_command(
                        "VSIM", self.vector_set_key, "FP32", vector_values,
                        "WITHSCORES", "WITHATTRIBS", "COUNT", candidate_k,
                    )
                except Exception:
                    compatibility_raw = []
                append_filtered_results(compatibility_raw)
            return results
        metadata_filter = _redisearch_vector_metadata_filter(
            session_id=session_id,
            project_id=project_id,
            include_global=include_global,
            as_of=as_of,
            embedding_model=embedding_model,
            workflow_id=workflow_id,
        )
        query = f"({metadata_filter})=>[KNN {candidate_k} @embedding $query_vec AS distance]"
        legacy_query = (
            f"@session_id:{{{_escape_tag(session_id)}}}=>[KNN {candidate_k} @embedding $query_vec AS distance]"
            if session_id
            else f"*=>[KNN {candidate_k} @embedding $query_vec AS distance]"
        )
        try:
            raw = self.client.execute_command(
                "FT.SEARCH", self.vector_index_name, query, "PARAMS", "2", "query_vec",
                struct.pack(f"<{len(vector)}f", *(float(value) for value in vector)),
                "SORTBY", "distance", "RETURN", "13", "memory_id", "session_id", "memory_type", "archive_path",
                "projection_version", "scope", "project_id", "status", "valid_from", "invalid_at", "embedding_model", "workflow_id", "distance", "DIALECT", "2",
            )
        except Exception:
            try:
                raw = self.client.execute_command(
                    "FT.SEARCH", self.vector_index_name, legacy_query, "PARAMS", "2", "query_vec",
                    struct.pack(f"<{len(vector)}f", *(float(value) for value in vector)),
                    "SORTBY", "distance", "RETURN", "5", "memory_id", "session_id", "memory_type", "archive_path", "distance", "DIALECT", "2",
                )
            except Exception:
                return []
        results = []
        for index in range(1, len(raw), 2):
            fields = dict(zip(raw[index + 1][::2], raw[index + 1][1::2]))
            attributes = {
                key.decode() if isinstance(key, bytes) else key: value.decode() if isinstance(value, bytes) else value
                for key, value in fields.items()
            }
            if self._vector_result_matches_scope(
                attributes,
                session_id=session_id,
                project_id=project_id,
                include_global=include_global,
                as_of=as_of,
                embedding_model=embedding_model,
                workflow_id=workflow_id,
            ):
                memory_id = str(attributes.get("memory_id") or "").strip()
                if memory_id and "record_version" not in attributes:
                    # RediSearch v2 projections store record_version in the
                    # hash but older schemas did not load it into FT.SEARCH.
                    # Read it from the derived record so semantic ranking can
                    # still be fenced against the SQLite authority.
                    read_field = getattr(self.client, "hget", None)
                    if callable(read_field):
                        try:
                            raw_version = read_field(
                                f"{self.prefix}:memory:{memory_id}",
                                "record_version",
                            )
                            if isinstance(raw_version, bytes):
                                raw_version = raw_version.decode("utf-8", errors="replace")
                            if raw_version not in (None, ""):
                                attributes["record_version"] = int(raw_version)
                        except Exception:
                            pass
                results.append(attributes)
            if len(results) >= safe_k:
                break
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
            "memory_vector_ttl_seconds": self.memory_vector_ttl_seconds,
            "memory_vector_expiration_policy": (
                "no_expiry" if self.memory_vector_ttl_seconds is None else "ttl"
            ),
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
        self.memory_vector_rehydrate: dict[str, Any] = {
            "schema_version": "memory_vector_rehydrate_v1",
            "status": "not_attempted",
            "scanned": 0,
            "attempted": 0,
            "restored": 0,
            "skipped": 0,
            "failed": 0,
            "cursor": None,
            "has_more": False,
            "cycle_completed": False,
        }
        self._outbox_replay_lock = threading.Lock()
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
        except Exception as exc:
            self.backend_errors["outbox"] = str(exc)
            logger.warning("conversation outbox replay failed; continuing with archive: %s", exc)
        try:
            self.memory_vector_rehydrate = self.rehydrate_memory_vectors()
        except Exception as exc:
            self.memory_vector_rehydrate = {
                **self.memory_vector_rehydrate,
                "status": "error",
                "error": str(exc),
            }
            logger.warning("memory vector rehydration was skipped: %s", exc)

    def _connect_backends(self) -> None:
        if self.redis is None and self.redis_enabled:
            try:
                self.redis = RedisSessionStore(
                    os.environ.get("SOULDRIVE_REDIS_URL") or DEFAULT_REDIS_URL,
                    self.archive.key,
                    max_messages=int(os.environ.get("SOULDRIVE_REDIS_MAX_MESSAGES", "32")),
                    ttl_seconds=int(os.environ.get("SOULDRIVE_REDIS_TTL_SECONDS", "604800")),
                    memory_vector_ttl_seconds=(
                        int(os.environ.get("SOULDRIVE_REDIS_MEMORY_VECTOR_TTL_SECONDS", "0"))
                    ),
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
        session_metadata = self.archive.session_metadata(session_id)
        bound_project_id = (
            str((session_metadata or {}).get("project_id") or "").strip() or None
        )
        requested_project_id = str(project_id or "").strip() or None
        if session_metadata is not None and requested_project_id is not None:
            if bound_project_id is None:
                raise ValueError("global session cannot be assigned to a project")
            if requested_project_id != bound_project_id:
                raise ValueError("session belongs to another project")
        effective_project_id = bound_project_id or requested_project_id
        message_id = None
        if request_id:
            status = str(metadata.get("status") or "user")
            normalized_role = str(role or "").strip().lower()
            if normalized_role == "ai":
                normalized_role = "assistant"
            # Reuse a legacy message identity when it is already present in
            # this session. New identities include the session so a request
            # key may be safely reused by another durable conversation.
            existing = self.archive.find_message_by_request_id(
                session_id,
                request_id,
                role=normalized_role,
            )
            if existing is not None:
                message_id = str(existing.get("message_id") or "") or None
        if request_id and message_id is None:
            message_id = "msg_" + hashlib.sha256(
                f"{session_id}:{request_id}:{role}:{status}".encode("utf-8")
            ).hexdigest()[:40]
        record = self.archive.append(
            session_id,
            role,
            content,
            metadata,
            message_id=message_id,
            request_id=request_id,
        )
        session_metadata = session_metadata or {}
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
                    self._require_postgres().upsert_session(
                        session_id,
                        project_id=effective_project_id,
                        archive_path=archive_path,
                        latest_sequence=int(record["sequence"]),
                        created_at=float(record["created_at"]),
                        title=session_metadata.get("title"),
                        workflow_id=session_metadata.get("workflow_id"),
                    ),
                    self._require_postgres().record_message(record, archive_path),
                ),
                event={
                    "event_type": "postgres.session_message",
                    "aggregate_id": record["message_id"],
                    "payload": {
                        "record": record,
                        "archive_path": archive_path,
                        "session_metadata": session_metadata,
                    },
                },
            )
        return record

    def find_message_by_request_id(
        self,
        session_id: str,
        request_id: str,
        *,
        role: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any] | None:
        return self.archive.find_message_by_request_id(
            session_id,
            request_id,
            role=role,
            status=status,
        )

    def find_message(self, message_id: str) -> dict[str, Any] | None:
        return self.archive.find_message(message_id)

    def claim_request(
        self,
        session_id: str,
        request_id: str,
        query: str,
        *,
        owner_id: str,
        input_digest: str | None = None,
        lease_seconds: float | None = None,
    ) -> dict[str, Any]:
        kwargs = {"owner_id": owner_id, "input_digest": input_digest}
        if lease_seconds is not None:
            kwargs["lease_seconds"] = lease_seconds
        return self.archive.claim_request(session_id, request_id, query, **kwargs)

    def complete_request(
        self,
        session_id: str,
        request_id: str,
        *,
        owner_id: str,
        lease_token: str,
        response_message_id: str | None = None,
    ) -> bool:
        return self.archive.complete_request(
            session_id,
            request_id,
            owner_id=owner_id,
            lease_token=lease_token,
            response_message_id=response_message_id,
        )

    def renew_request(
        self,
        session_id: str,
        request_id: str,
        *,
        owner_id: str,
        lease_token: str,
        lease_seconds: float | None = None,
    ) -> bool:
        kwargs = {
            "owner_id": owner_id,
            "lease_token": lease_token,
        }
        if lease_seconds is not None:
            kwargs["lease_seconds"] = lease_seconds
        return self.archive.renew_request(session_id, request_id, **kwargs)

    def fail_request(
        self,
        session_id: str,
        request_id: str,
        *,
        owner_id: str,
        lease_token: str,
        error: str,
    ) -> bool:
        return self.archive.fail_request(
            session_id,
            request_id,
            owner_id=owner_id,
            lease_token=lease_token,
            error=error,
        )

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
                    project_id=manifest.get("project_id"),
                    archive_path=archive_path,
                    latest_sequence=0,
                    created_at=float(manifest["created_at"]),
                    title=str(manifest.get("title") or ""),
                    workflow_id=manifest.get("workflow_id"),
                ),
                event={
                    "event_type": "postgres.session_created",
                    "aggregate_id": session_id,
                    "payload": {
                        "session_id": session_id,
                        "project_id": manifest.get("project_id"),
                        "title": manifest.get("title"),
                        "workflow_id": manifest.get("workflow_id"),
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

    def delete_memory(self, memory_id: str, *, project_id: str | None = None) -> dict[str, Any]:
        """Remove a durable memory archive and fence/replay its projections."""
        normalized_memory_id = str(memory_id or "").strip()
        if not normalized_memory_id:
            return {"memory_id": "", "archive_deleted": False}
        record = self.archive.delete_memory(normalized_memory_id, project_id=project_id)
        try:
            self.archive.cancel_memory_outbox(normalized_memory_id)
        except Exception:
            logger.warning("failed to fence local memory outbox for %s", normalized_memory_id, exc_info=True)
        if self.postgres is not None:
            try:
                self.postgres.cancel_memory_outbox(normalized_memory_id)
            except Exception:
                logger.warning("failed to fence PostgreSQL memory outbox for %s", normalized_memory_id, exc_info=True)
        if self.redis_enabled:
            self._try_backend(
                "redis",
                lambda: self._require_redis().remove_vector(normalized_memory_id),
                event={
                    "event_type": "redis.memory_vector_delete",
                    "aggregate_id": normalized_memory_id,
                    "payload": {"memory_id": normalized_memory_id, "status": "deleted", "record_version": 1},
                },
            )
        if self.postgres_enabled:
            self._try_backend(
                "postgres",
                lambda: self._require_postgres().delete_memory(normalized_memory_id),
                event={
                    "event_type": "postgres.memory_deleted",
                    "aggregate_id": normalized_memory_id,
                    "payload": {"memory_id": normalized_memory_id, "project_id": project_id},
                },
            )
        return {
            "memory_id": normalized_memory_id,
            "archive_deleted": record is not None,
        }

    def delete_project_memories(
        self,
        project_id: str,
        *,
        memory_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Delete project memory archives and all configured memory projections."""
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            raise ValueError("project_id is required")
        candidates = {
            str(memory_id or "").strip()
            for memory_id in (memory_ids or [])
            if str(memory_id or "").strip()
        }
        candidates.update(self.archive.memory_ids_for_project(normalized_project_id))
        deleted = 0
        for memory_id in sorted(candidates):
            result = self.delete_memory(memory_id, project_id=normalized_project_id)
            if result.get("archive_deleted"):
                deleted += 1
        return {
            "status": "deleted",
            "project_id": normalized_project_id,
            "memory_count": len(candidates),
            "archive_deleted_count": deleted,
        }

    def session_metadata(self, session_id: str) -> dict[str, Any] | None:
        return self.archive.session_metadata(session_id)

    def list_sessions(self, project_id: str | None = None) -> list[dict[str, Any]]:
        return self.archive.list_sessions(project_id)

    def load_context(self, session_id: str, *, limit: int = 8) -> dict[str, Any]:
        try:
            self.replay_outbox(limit=20)
        except Exception:
            logger.warning(
                "conversation outbox replay failed during context load",
                exc_info=True,
            )
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
        try:
            self.replay_outbox(limit=20)
        except Exception:
            logger.warning(
                "conversation outbox replay failed during history pagination",
                exc_info=True,
            )
        return {"records": self.archive.load_before(session_id, before_sequence, limit=limit), "source": "file"}

    def compact_session(
        self,
        session_id: str,
        *,
        project_id: str | None = None,
        threshold: int = 16,
        summary_builder: Callable[[list[dict[str, Any]], int], str] | None = None,
    ) -> dict[str, Any] | None:
        session_metadata = self.archive.session_metadata(session_id)
        if session_metadata is not None and project_id is not None:
            session_project_id = str(session_metadata.get("project_id") or "").strip() or None
            requested_project_id = str(project_id or "").strip() or None
            if session_project_id != requested_project_id:
                raise ValueError("session belongs to another project")
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
        payload = self.archive.write_summary(
            session_id,
            str(summary.get("summary") or ""),
            version=int(summary["version"]),
            covered_sequence_start=int(summary["covered_sequence_start"]),
            covered_sequence_end=int(summary["covered_sequence_end"]),
            source_message_ids=list(summary.get("source_message_ids") or []),
            input_sequence_start=summary.get("input_sequence_start"),
            input_sequence_end=summary.get("input_sequence_end"),
            input_message_count=summary.get("input_message_count"),
            excerpt_message_ids=summary.get("excerpt_message_ids"),
            excerpt_truncated=summary.get("excerpt_truncated"),
            excerpt_status=summary.get("excerpt_status"),
        )
        if self.redis_enabled:
            self._try_backend("redis", lambda: self._require_redis().set_summary(session_id, payload), event={"event_type": "redis.session_summary", "aggregate_id": session_id, "payload": {"summary": payload}})
        if self.postgres_enabled:
            archive_path = str(self.archive._summary_path(session_id, int(payload["version"])))
            self._try_backend("postgres", lambda: self._require_postgres().record_summary(payload, archive_path), event={"event_type": "postgres.session_summary", "aggregate_id": session_id, "payload": {"summary": payload, "archive_path": archive_path}})
        return payload

    def persist_memory(
        self,
        memory: dict[str, Any],
        *,
        project_id: str | None = None,
        session_id: str | None = None,
        vector: list[float] | None = None,
        vector_model_id: str | None = None,
    ):
        workflow_id = _validated_memory_workflow(memory)
        memory_id = str(memory.get("memory_id") or memory.get("skill_id"))
        effective_session_id = str(session_id or memory.get("source_session_id") or "").strip() or None
        incoming_version = int(memory.get("record_version") or 1)
        scope = str(memory.get("scope") or "").strip().lower()
        normalized_project_id = str(memory.get("project_id") or project_id or "").strip() or None
        if scope not in {"global", "project"}:
            scope = "project" if normalized_project_id else "global"
        if scope == "global":
            normalized_project_id = None
        archival_memory = dict(memory)
        # The encrypted local archive is the authority used to recover old or
        # partial outbox events. Persist the same canonical scope identity that
        # the projections receive, including a project supplied separately by
        # the caller.
        archival_memory["scope"] = scope
        archival_memory["record_version"] = incoming_version
        if normalized_project_id:
            archival_memory["project_id"] = normalized_project_id
        else:
            archival_memory.pop("project_id", None)
        if effective_session_id:
            archival_memory["session_id"] = effective_session_id
        if vector:
            archival_memory["semantic_embedding"] = [float(value) for value in vector]
            normalized_vector_model_id = str(vector_model_id or "").strip()
            if normalized_vector_model_id:
                archival_memory["semantic_embedding_model"] = normalized_vector_model_id
        archive_path = self.archive.archive_memory(archival_memory)
        current = self.archive.read_memory(memory_id)
        if current is not None and memory_write_is_stale(current, memory):
            return archive_path
        vector_metadata = {
            "scope": scope,
            "project_id": normalized_project_id,
            "status": memory.get("status") or "active",
            "valid_from": memory.get("valid_from"),
            "invalid_at": memory.get("invalid_at"),
            "record_version": incoming_version,
        }
        if workflow_id:
            vector_metadata["workflow_id"] = workflow_id
        normalized_vector_model_id = str(vector_model_id or "").strip() or None
        if self.postgres_enabled:
            self._try_backend(
                "postgres",
                lambda: self._require_postgres().upsert_memory(
                    memory,
                    archive_path,
                    normalized_project_id,
                    effective_session_id,
                ),
                event={
                    "event_type": "postgres.memory",
                    "aggregate_id": memory_id,
                    "payload": {
                        "memory": archival_memory,
                        "archive_path": archive_path,
                        "project_id": normalized_project_id,
                        "session_id": effective_session_id,
                    },
                },
            )
        if self.redis_enabled and vector:
            self._try_backend(
                "redis",
                lambda: self._put_redis_vector_or_raise(
                    memory_id=memory_id,
                    session_id=effective_session_id,
                    memory_type="memory" if memory.get("memory_id") else "skill",
                    vector=vector,
                    archive_path=archive_path,
                    embedding_model=normalized_vector_model_id,
                    memory_subtype=memory.get("subtype"),
                    **vector_metadata,
                ),
                event={
                    "event_type": "redis.memory_vector",
                    "aggregate_id": memory_id,
                    "payload": {
                        "archive_path": archive_path,
                        "session_id": effective_session_id,
                        "memory_type": "memory" if memory.get("memory_id") else "skill",
                        **vector_metadata,
                        "record_version": incoming_version,
                        "memory_identity_digest": memory_identity_digest(archival_memory),
                        "embedding_model": normalized_vector_model_id,
                        "memory_subtype": memory.get("subtype"),
                        "vector": list(vector),
                    },
                },
            )
        elif self.redis_enabled:
            # A newer active record without an embedding must not leave an
            # older semantic projection active. Keyword retrieval remains the
            # safe fallback until a fresh vector is available.
            self._try_backend(
                "redis",
                lambda: self._require_redis().remove_vector(memory_id),
                event={
                    "event_type": "redis.memory_vector_delete",
                    "aggregate_id": memory_id,
                    "payload": {
                        "memory_id": memory_id,
                        "record_version": incoming_version,
                    },
                },
            )
        return archive_path

    def rehydrate_memory_vectors(self, *, limit: int = 100) -> dict[str, Any]:
        """Restore bounded, model-matched vector projections from the archive."""
        result: dict[str, Any] = {
            "schema_version": "memory_vector_rehydrate_v1",
            "status": "not_attempted",
            "scanned": 0,
            "attempted": 0,
            "restored": 0,
            "skipped": 0,
            "failed": 0,
            "cursor": None,
            "has_more": False,
            "cycle_completed": False,
        }
        if not self.redis_enabled:
            result["status"] = "disabled"
            return result
        if self.redis is None:
            result["status"] = "unavailable"
            return result
        expected_model = _configured_memory_embedding_model_id()
        if not expected_model:
            result["status"] = "embedding_model_unconfigured"
            return result
        checkpoint_name = "memory_vector_rehydrate_v1"
        checkpoint = self.archive.read_projection_checkpoint(checkpoint_name) or {}
        cursor = str(checkpoint.get("cursor") or "").strip() or None
        if str(checkpoint.get("embedding_model") or "").strip() != expected_model:
            cursor = None
        page = self.archive.archived_memories_page(
            cursor=cursor,
            limit=max(1, min(int(limit), 1000)),
        )
        records = list(page.get("records") or [])
        result["scanned"] = len(records)
        for memory in records:
            if str(memory.get("status") or "active").strip().lower() != "active":
                result["skipped"] += 1
                continue
            stored_model = str(memory.get("semantic_embedding_model") or "").strip()
            vector = memory.get("semantic_embedding")
            if not stored_model or stored_model != expected_model or not isinstance(vector, list) or not vector:
                result["skipped"] += 1
                continue
            memory_id = str(memory.get("memory_id") or memory.get("skill_id") or "").strip()
            if not memory_id:
                result["skipped"] += 1
                continue
            result["attempted"] += 1
            try:
                restored = self.redis.put_vector(
                    memory_id=memory_id,
                    session_id=memory.get("session_id"),
                    memory_type="memory" if memory.get("memory_id") else "skill",
                    vector=[float(value) for value in vector],
                    archive_path=str(self.archive.memory_root / f"{memory_id}.json.enc"),
                    scope=memory.get("scope"),
                    project_id=memory.get("project_id"),
                    status=str(memory.get("status") or "active"),
                    valid_from=memory.get("valid_from"),
                    invalid_at=memory.get("invalid_at"),
                    record_version=int(memory.get("record_version") or 1),
                    embedding_model=stored_model,
                    workflow_id=memory.get("workflow_id"),
                    memory_subtype=memory.get("subtype"),
                )
                if restored:
                    result["restored"] += 1
                else:
                    result["failed"] += 1
            except Exception as exc:
                result["failed"] += 1
                result.setdefault("errors", []).append(str(exc)[:300])
        can_advance = result["failed"] == 0
        result["cursor"] = page.get("next_cursor") if can_advance else cursor
        result["has_more"] = bool(page.get("has_more")) if can_advance else True
        result["cycle_completed"] = not result["has_more"] and can_advance
        result["status"] = "ok" if result["failed"] == 0 else "degraded"
        self.archive.write_projection_checkpoint(
            checkpoint_name,
            {
                "schema_version": "memory_vector_rehydrate_v1",
                "embedding_model": expected_model,
                "cursor": result["cursor"],
                "has_more": result["has_more"],
            },
        )
        return result

    def revoke_memory(self, memory: dict[str, Any], *, project_id: str | None = None, session_id: str | None = None):
        _validated_memory_workflow(memory)
        memory_id = str(memory.get("memory_id") or "")
        if not memory_id:
            raise ValueError("memory_id is required")
        effective_session_id = str(session_id or memory.get("source_session_id") or "").strip() or None
        incoming_version = int(memory.get("record_version") or 1)
        revoke_scope = str(memory.get("scope") or "").strip().lower()
        normalized_project_id = str(memory.get("project_id") or project_id or "").strip() or None
        if revoke_scope not in {"global", "project"}:
            revoke_scope = "project" if normalized_project_id else "global"
        if revoke_scope == "global":
            normalized_project_id = None
        archival_memory = dict(memory)
        archival_memory["scope"] = revoke_scope
        archival_memory["record_version"] = incoming_version
        if normalized_project_id:
            archival_memory["project_id"] = normalized_project_id
        else:
            archival_memory.pop("project_id", None)
        if effective_session_id:
            archival_memory["session_id"] = effective_session_id
        archive_path = self.archive.archive_memory(archival_memory)
        current = self.archive.read_memory(memory_id)
        if current is not None and memory_write_is_stale(current, memory):
            return archive_path
        if self.postgres_enabled:
            self._try_backend(
                "postgres",
                lambda: self._require_postgres().upsert_memory(
                    memory,
                    archive_path,
                    normalized_project_id,
                    effective_session_id,
                ),
                event={
                    "event_type": "postgres.memory",
                    "aggregate_id": memory_id,
                    "payload": {
                        "memory": archival_memory,
                        "archive_path": archive_path,
                        "project_id": normalized_project_id,
                        "session_id": effective_session_id,
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
                    "payload": {"memory_id": memory_id, "record_version": incoming_version},
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
            "memory_vector_rehydrate": dict(self.memory_vector_rehydrate),
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
        if self.redis and (
            str(self.memory_vector_rehydrate.get("status") or "").strip().lower() in {
                "not_attempted",
                "unavailable",
                "error",
            }
            or bool(self.memory_vector_rehydrate.get("has_more"))
        ):
            try:
                self.memory_vector_rehydrate = self.rehydrate_memory_vectors()
            except Exception as exc:
                self.memory_vector_rehydrate = {
                    **self.memory_vector_rehydrate,
                    "status": "error",
                    "error": str(exc),
                }
                logger.warning("memory vector rehydration retry failed: %s", exc)
        result["memory_vector_rehydrate"] = dict(self.memory_vector_rehydrate)
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
        ) or str(self.memory_vector_rehydrate.get("status") or "").strip().lower() in {
            "degraded",
            "error",
        }
        return result

    def replay_outbox(self, limit: int = 100) -> dict[str, int]:
        """重放本地加密 outbox，并在 PostgreSQL 可用时重放其控制面 outbox。"""
        with self._outbox_replay_lock:
            return self._replay_outbox_locked(limit)

    def _replay_outbox_locked(self, limit: int = 100) -> dict[str, int]:
        self._connect_backends()
        completed = 0
        failed = 0
        local_owner = f"local-replay:{os.getpid()}:{id(self)}"
        for event in self.archive.pending_outbox(limit=limit):
            claim = self.archive.claim_outbox(
                str(event.get("event_id") or ""),
                owner_id=local_owner,
            )
            if claim is None:
                continue
            try:
                self._dispatch_event(event)
                if not self.archive.mark_outbox(
                    str(event["event_id"]),
                    "completed",
                    claim_token=claim["claim_token"],
                ):
                    raise RuntimeError("local outbox claim was lost before completion")
                completed += 1
            except Exception as exc:
                failed += 1
                try:
                    self.archive.mark_outbox(
                        str(event["event_id"]),
                        "pending",
                        str(exc),
                        claim_token=claim["claim_token"],
                    )
                except Exception:
                    logger.warning("failed to persist local conversation outbox failure receipt")

        if self.postgres:
            postgres_owner = f"replay:{os.getpid()}:{id(self)}"
            for event in self.postgres.claim_pending_outbox(
                limit=limit,
                owner_id=postgres_owner,
            ):
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
                    marked = self.postgres.mark_outbox(
                        str(event["event_id"]),
                        "completed",
                        claim_token=event.get("claim_token"),
                    )
                    if marked is False:
                        raise RuntimeError("PostgreSQL outbox claim was lost before completion")
                    completed += 1
                except Exception as exc:
                    failed += 1
                    try:
                        self.postgres.mark_outbox(
                            str(event["event_id"]),
                            "pending",
                            str(exc),
                            claim_token=event.get("claim_token"),
                        )
                    except Exception:
                        logger.warning("failed to persist PostgreSQL conversation outbox failure receipt")
        return {"completed": completed, "failed": failed}

    def _memory_event_is_stale(self, event: dict[str, Any], payload: dict[str, Any]) -> bool:
        memory = payload.get("memory") or {}
        memory_id = str(
            event.get("aggregate_id")
            or payload.get("memory_id")
            or memory.get("memory_id")
            or ""
        ).strip()
        if not memory_id:
            return False
        current = self.archive.read_memory(memory_id)
        if current is None:
            # A project deletion can remove the local archive before a stale
            # memory upsert reaches Redis/PostgreSQL. Do not recreate data
            # that has already been authoritatively deleted.
            return str(event.get("event_type") or "") in {
                "postgres.memory",
                "redis.memory_vector",
            }
        incoming_version = int(payload.get("record_version") or memory.get("record_version") or 1)
        if event.get("event_type") == "redis.memory_vector_delete":
            incoming_status = "deleted"
        else:
            incoming_status = str(payload.get("status") or memory.get("status") or "active")
        if memory:
            incoming = dict(memory)
            incoming.setdefault("record_version", incoming_version)
            incoming.setdefault("status", incoming_status)
            if memory_version_conflict(current, incoming):
                raise ValueError("memory version conflict")
        else:
            identity_digest = str(payload.get("memory_identity_digest") or "").strip()
            current_status = str(current.get("status") or "active").strip().lower()
            if (
                identity_digest
                and int(current.get("record_version") or 1) == incoming_version
                and current_status == incoming_status.strip().lower()
                and identity_digest != memory_identity_digest(current)
            ):
                raise ValueError("memory version conflict")
            incoming = {
                "record_version": incoming_version,
                "status": incoming_status,
            }
        return memory_write_is_stale(current, incoming)

    def _dispatch_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("event_type") or "")
        payload = dict(event.get("payload") or {})
        if event_type in {
            "postgres.memory",
            "redis.memory_vector",
            "redis.memory_vector_delete",
        } and self._memory_event_is_stale(event, payload):
            return
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
            session_metadata = payload.get("session_metadata") or self.archive.session_metadata(record["session_id"]) or {}
            self.postgres.upsert_session(
                record["session_id"],
                project_id=metadata.get("project_id") or session_metadata.get("project_id"),
                archive_path=archive_path,
                latest_sequence=int(record["sequence"]),
                created_at=float(record["created_at"]),
                title=session_metadata.get("title"),
                workflow_id=session_metadata.get("workflow_id"),
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
                workflow_id=payload.get("workflow_id"),
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
            session_id = str(event.get("aggregate_id") or payload.get("session_id") or "").strip()
            if self.archive.session_metadata(session_id) is None:
                return
            summary = payload.get("summary") or self.archive.latest_summary(session_id)
            if not summary:
                raise RuntimeError("conversation summary is missing from file archive")
            self.redis.set_summary(session_id, summary)
            return
        if event_type == "postgres.session_summary":
            if not self.postgres:
                raise RuntimeError("PostgreSQL is unavailable")
            session_id = str(event.get("aggregate_id") or payload.get("session_id") or "").strip()
            if self.archive.session_metadata(session_id) is None:
                return
            summary = payload.get("summary") or self.archive.latest_summary(session_id)
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
            replay_scope = str(memory.get("scope") or payload.get("scope") or "").strip().lower()
            replay_project_id = str(
                memory.get("project_id") or payload.get("project_id") or ""
            ).strip() or None
            if replay_scope == "global":
                replay_project_id = None
            self.postgres.upsert_memory(
                memory,
                archive_path,
                replay_project_id,
                memory.get("session_id") or payload.get("session_id"),
            )
            return
        if event_type == "postgres.memory_deleted":
            if not self.postgres:
                raise RuntimeError("PostgreSQL is unavailable")
            self.postgres.delete_memory(str(event.get("aggregate_id") or payload.get("memory_id") or ""))
            return
        if event_type == "redis.memory_vector":
            if not self.redis:
                raise RuntimeError("Redis is unavailable")
            memory_id = str(event.get("aggregate_id") or payload.get("memory_id") or "")
            memory = payload.get("memory") or self.archive.read_memory(memory_id) or {}
            vector = payload.get("vector") or []
            if not vector:
                raise RuntimeError("memory vector is missing")
            replay_scope = str(memory.get("scope") or payload.get("scope") or "").strip().lower()
            replay_project_id = str(
                memory.get("project_id") or payload.get("project_id") or ""
            ).strip() or None
            if replay_scope == "global":
                replay_project_id = None
            self._put_redis_vector_or_raise(
                memory_id=memory_id,
                session_id=memory.get("session_id") or payload.get("session_id"),
                memory_type=str(
                    payload.get("memory_type")
                    or ("memory" if memory.get("memory_id") else "skill")
                ),
                vector=[float(value) for value in vector],
                archive_path=str(
                    payload.get("archive_path")
                    or self.archive.memory_root / f"{memory_id}.json.enc"
                ),
                scope=memory.get("scope") or payload.get("scope"),
                project_id=replay_project_id,
                status=str(payload.get("status") or memory.get("status") or "active"),
                valid_from=(
                    payload.get("valid_from")
                    if "valid_from" in payload
                    else memory.get("valid_from")
                ),
                invalid_at=(
                    payload.get("invalid_at")
                    if "invalid_at" in payload
                    else memory.get("invalid_at")
                ),
                record_version=int(
                    payload.get("record_version") or memory.get("record_version") or 1
                ),
                embedding_model=payload.get("embedding_model") or memory.get("semantic_embedding_model"),
                workflow_id=payload.get("workflow_id") or memory.get("workflow_id"),
                memory_subtype=payload.get("memory_subtype") or memory.get("subtype"),
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
            event_type = str(event.get("event_type") or "").strip().lower()
            event_key = event_type.replace(".", "_") or "backend_event"
            # The aggregate alone is not an idempotency identity: a session
            # emits create, message, summary and delete events with the same
            # aggregate ID.  Include the event kind so a later lifecycle
            # event cannot overwrite an earlier pending outbox event.
            event_id = f"{name}_{event.get('aggregate_id')}_{event_key}"
            if event_type in {
                "postgres.memory",
                "postgres.memory_deleted",
                "redis.memory_vector",
                "redis.memory_vector_delete",
            }:
                payload = event.get("payload") or {}
                memory = payload.get("memory") or {}
                version = payload.get("record_version") or memory.get("record_version") or 1
                state = str(payload.get("status") or memory.get("status") or "active").strip().lower() or "active"
                if event_type == "redis.memory_vector_delete" and not payload.get("status"):
                    state = "deleted"
                state_key = state.replace(" ", "_")
                event_id = f"{name}_{event.get('aggregate_id')}_v{int(version or 1)}_{event_key}_{state_key}"
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

    def _put_redis_vector_or_raise(self, **kwargs: Any) -> None:
        """Keep a false vector-write result retryable for the durable outbox."""
        if not self._require_redis().put_vector(**kwargs):
            raise RuntimeError("Redis vector projection write failed")

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
            "memory_id": payload.get("memory_id") or event.get("aggregate_id"),
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
                    "scope": payload.get("scope"),
                    "status": payload.get("status"),
                    "valid_from": payload.get("valid_from"),
                    "invalid_at": payload.get("invalid_at"),
                    "vector": payload.get("vector") or [],
                    "record_version": payload.get("record_version") or 1,
                    "memory_identity_digest": payload.get("memory_identity_digest"),
                    "embedding_model": payload.get("embedding_model"),
                    "workflow_id": payload.get("workflow_id"),
                    "memory_subtype": payload.get("memory_subtype"),
                }
            )
        if event_type == "redis.memory_vector_delete":
            control["record_version"] = payload.get("record_version") or 1
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


def _message_identity_digest(record: dict[str, Any]) -> str:
    return _content_hash(
        json.dumps(
            {
                "session_id": str(record.get("session_id") or ""),
                "sequence": int(record.get("sequence") or 0),
                "role": str(record.get("role") or ""),
                "content": str(record.get("content") or ""),
                "metadata": record.get("metadata") or {},
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _redact_dsn(value: str) -> str:
    return value.rsplit("@", 1)[-1] if "@" in value else value


def _redact_redis_url(value: str) -> str:
    return value.rsplit("@", 1)[-1] if "@" in value else value


def _escape_tag(value: str) -> str:
    return str(value or "").replace("-", "\\-").replace(" ", "\\ ").replace("|", "\\|")
