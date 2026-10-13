import base64
import hashlib
import hmac
import json
import math
import os
import sqlite3
import threading
import time
from functools import wraps
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.memory_gate import contains_sensitive_memory, contains_untrusted_memory_instruction
from core.memory_roles import classify_memory_family
from core.skill_runtime import (
    SUPPORTED_WORKFLOW_IDS,
    is_skill_visible_in_workflow,
    normalize_skill_payload,
)


LEARNING_AAD_MAGIC = "SoulDrive learning store"
LEARNING_AAD_VERSION = 1
MEMORY_SYNC_MAX_ATTEMPTS = 5
MEMORY_MUTATION_RECEIPT_SCHEMA_VERSION = "memory_mutation_receipt_v1"
SKILL_DELETION_RECEIPT_SCHEMA_VERSION = "skill_deletion_receipt_v1"
_UNSET = object()


def _synchronized(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


def memory_sync_error_observation(error: Any) -> dict[str, Any]:
    """Return a stable, non-sensitive observation for an outbox failure."""
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
            return memory_sync_error_observation(decoded)

    text = str(error or "")
    return {
        "error_type": "UnknownError",
        "error_digest": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "error_chars": len(text),
    }


def _serialize_memory_sync_error(error: Any) -> str | None:
    if error is None or str(error or "") == "":
        return None
    return json.dumps(
        memory_sync_error_observation(error),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _workflow_memory_visible(
    subtype: Any,
    payload: dict[str, Any],
    workflow_id: str | None,
    *,
    context_only: bool,
) -> bool:
    """Keep workflow facts inside their bound conversation playbook."""
    if str(subtype or "").strip() != "workflow_fact":
        return True
    requested = str(workflow_id or "").strip()
    stored = str(payload.get("workflow_id") or "").strip()
    if requested:
        return bool(stored) and stored == requested

    # Management reads may inspect legacy/unbound facts, but a context read
    # must never inject a workflow fact whose owner is unknown.
    return not context_only


def _memory_sync_scope_matches(
    payload: dict[str, Any],
    *,
    project_id: str | None,
    include_global: bool,
) -> bool:
    """Match an outbox payload before claiming its downstream side effect."""
    event_memory = payload.get("memory") if isinstance(payload, dict) else None
    event_scope = event_memory if isinstance(event_memory, dict) else payload
    scope = str(event_scope.get("scope") or "global").strip().lower()
    if scope == "global":
        return bool(include_global)
    if scope != "project" or project_id is None:
        return False
    stored_project_id = str(event_scope.get("project_id") or "").strip() or None
    return stored_project_id == project_id


def _skill_scope_sql(project_id: str | None, *, include_global: bool = True) -> tuple[str, list[Any]]:
    """Build the fail-closed Skill namespace predicate used by every consumer."""
    normalized_project_id = str(project_id or "").strip() or None
    if normalized_project_id:
        if include_global:
            return (
                "(COALESCE(scope, 'global') = 'global' OR "
                "(COALESCE(scope, 'global') = 'project' AND project_id = ?))",
                [normalized_project_id],
            )
        return (
            "COALESCE(scope, 'global') = 'project' AND project_id = ?",
            [normalized_project_id],
        )
    return "COALESCE(scope, 'global') = 'global'", []


class LearningStore:
    def __init__(self, workspace, keys):
        self.workspace = workspace
        self.keys = keys
        Path(workspace.learning_store_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(workspace.learning_store_path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    @_synchronized
    def close(self):
        self.conn.close()

    def _init_schema(self):
        cursor = self.conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS reflection_reviews (
                review_id TEXT PRIMARY KEY,
                trigger TEXT NOT NULL,
                project_id TEXT,
                status TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'discarded')),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                candidate_count INTEGER NOT NULL CHECK (candidate_count >= 0),
                accepted_count INTEGER NOT NULL CHECK (accepted_count >= 0),
                rejected_count INTEGER NOT NULL CHECK (rejected_count >= 0),
                decision_digest TEXT,
                decision_project_id TEXT,
                idempotency_key TEXT,
                idempotency_digest TEXT,
                CHECK (accepted_count + rejected_count <= candidate_count)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS reflection_candidates (
                candidate_id TEXT PRIMARY KEY,
                review_id TEXT NOT NULL REFERENCES reflection_reviews(review_id),
                asset_type TEXT NOT NULL CHECK (asset_type IN ('memory', 'skill')),
                subtype TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'rejected')),
                confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
                selected_by_default INTEGER NOT NULL CHECK (selected_by_default IN (0, 1)),
                duplicate_of TEXT,
                source_message_indexes TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                claim_token TEXT,
                claim_until REAL,
                next_attempt_at REAL,
                payload_nonce TEXT NOT NULL,
                payload_ciphertext TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                memory_id TEXT PRIMARY KEY,
                subtype TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
                scope TEXT NOT NULL DEFAULT 'global' CHECK (scope IN ('global', 'project')),
                project_id TEXT,
                source_candidate_id TEXT NOT NULL REFERENCES reflection_candidates(candidate_id),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_used_at REAL,
                valid_from REAL,
                invalid_at REAL,
                payload_nonce TEXT NOT NULL,
                payload_ciphertext TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_versions (
                memory_version_id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL,
                record_version INTEGER NOT NULL CHECK (record_version >= 1),
                subtype TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
                scope TEXT NOT NULL DEFAULT 'global' CHECK (scope IN ('global', 'project')),
                project_id TEXT,
                source_candidate_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_used_at REAL,
                valid_from REAL,
                invalid_at REAL,
                payload_nonce TEXT NOT NULL,
                payload_ciphertext TEXT NOT NULL,
                UNIQUE(memory_id, record_version)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS skills (
                skill_id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
                version INTEGER NOT NULL CHECK (version >= 1),
                scope TEXT NOT NULL DEFAULT 'global' CHECK (scope IN ('global', 'project')),
                project_id TEXT,
                source_candidate_id TEXT NOT NULL REFERENCES reflection_candidates(candidate_id),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                payload_nonce TEXT NOT NULL,
                payload_ciphertext TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS learning_chunks (
                chunk_id TEXT PRIMARY KEY,
                asset_id TEXT NOT NULL,
                asset_type TEXT NOT NULL CHECK (asset_type IN ('memory', 'skill')),
                subtype TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
                scope TEXT NOT NULL DEFAULT 'global' CHECK (scope IN ('global', 'project')),
                project_id TEXT,
                text_digest TEXT NOT NULL,
                embedding_json TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_used_at REAL,
                valid_from REAL,
                invalid_at REAL,
                payload_nonce TEXT NOT NULL,
                payload_ciphertext TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS retrieval_feedback (
                feedback_id TEXT PRIMARY KEY,
                scope TEXT NOT NULL CHECK (scope IN ('global', 'project')),
                project_id TEXT,
                query_digest TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                payload_nonce TEXT NOT NULL,
                payload_ciphertext TEXT NOT NULL,
                CHECK (
                    (scope = 'global' AND project_id IS NULL)
                    OR (scope = 'project' AND project_id IS NOT NULL)
                ),
                UNIQUE(scope, project_id, query_digest)
            )
            """
        )
        feedback_columns = {
            str(row[1])
            for row in cursor.execute("PRAGMA table_info(retrieval_feedback)").fetchall()
        }
        if "workflow_id" not in feedback_columns:
            cursor.execute("ALTER TABLE retrieval_feedback ADD COLUMN workflow_id TEXT")
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_retrieval_feedback_scope_workflow
            ON retrieval_feedback(status, scope, project_id, workflow_id, query_digest, updated_at)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_retrieval_feedback_lookup
            ON retrieval_feedback(status, scope, project_id, query_digest, updated_at)
            """
        )
        # SQLite treats NULLs as distinct in UNIQUE constraints.  Keep the
        # legacy table constraint for compatibility, but add scope-specific
        # partial unique indexes for active feedback when the existing store
        # has no conflicting active rows.  A legacy conflict must not make the
        # workspace fail to open; retrieval_feedback_health() reports it for
        # explicit review instead.
        active_feedback_duplicate = cursor.execute(
            """
            SELECT 1
            FROM retrieval_feedback
            WHERE status = 'active'
            GROUP BY scope, project_id, query_digest
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        ).fetchone()
        if active_feedback_duplicate is None:
            cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_retrieval_feedback_global_active_unique
                ON retrieval_feedback(query_digest)
                WHERE scope = 'global' AND status = 'active'
                """
            )
            cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_retrieval_feedback_project_active_unique
                ON retrieval_feedback(project_id, query_digest)
                WHERE scope = 'project' AND status = 'active'
                """
            )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_mutation_receipts (
                idempotency_key TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                input_digest TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('completed')),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                response_nonce TEXT NOT NULL,
                response_ciphertext TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS skill_deletion_receipts (
                idempotency_key TEXT PRIMARY KEY,
                requested_identifier TEXT NOT NULL,
                skill_id TEXT NOT NULL,
                project_id TEXT,
                requested_project_id TEXT,
                source_path TEXT NOT NULL,
                source_digest TEXT NOT NULL DEFAULT '',
                input_digest TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('in_progress', 'source_removed', 'completed', 'failed')
                ),
                owner_id TEXT,
                lease_expires_at REAL,
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                response_nonce TEXT,
                response_ciphertext TEXT
            )
            """
        )
        deletion_columns = {
            str(row[1])
            for row in cursor.execute("PRAGMA table_info(skill_deletion_receipts)").fetchall()
        }
        if "requested_project_id" not in deletion_columns:
            cursor.execute("ALTER TABLE skill_deletion_receipts ADD COLUMN requested_project_id TEXT")
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS learning_sync_outbox (
                event_id TEXT PRIMARY KEY,
                operation TEXT NOT NULL CHECK (
                    operation IN ('memory_update', 'memory_revoke', 'memory_delete')
                ),
                memory_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'completed')),
                attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                last_error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                claim_token TEXT,
                claim_until REAL,
                retry_paused_at REAL,
                payload_nonce TEXT NOT NULL,
                payload_ciphertext TEXT NOT NULL
            )
            """
        )
        outbox_columns = {
            str(row[1])
            for row in cursor.execute("PRAGMA table_info(learning_sync_outbox)").fetchall()
        }
        if "claim_token" not in outbox_columns:
            cursor.execute("ALTER TABLE learning_sync_outbox ADD COLUMN claim_token TEXT")
        if "claim_until" not in outbox_columns:
            cursor.execute("ALTER TABLE learning_sync_outbox ADD COLUMN claim_until REAL")
        if "next_attempt_at" not in outbox_columns:
            cursor.execute("ALTER TABLE learning_sync_outbox ADD COLUMN next_attempt_at REAL")
        if "retry_paused_at" not in outbox_columns:
            cursor.execute("ALTER TABLE learning_sync_outbox ADD COLUMN retry_paused_at REAL")
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_learning_sync_outbox_pending
            ON learning_sync_outbox(status, claim_until, created_at, event_id)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_learning_sync_outbox_ready
            ON learning_sync_outbox(status, next_attempt_at, claim_until, created_at, event_id)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_learning_sync_outbox_ready_v2
            ON learning_sync_outbox(status, retry_paused_at, next_attempt_at, claim_until, created_at, event_id)
            """
        )
        review_columns = {
            str(row[1])
            for row in cursor.execute("PRAGMA table_info(reflection_reviews)").fetchall()
        }
        if "decision_digest" not in review_columns:
            cursor.execute("ALTER TABLE reflection_reviews ADD COLUMN decision_digest TEXT")
        if "decision_project_id" not in review_columns:
            cursor.execute("ALTER TABLE reflection_reviews ADD COLUMN decision_project_id TEXT")
        if "idempotency_key" not in review_columns:
            cursor.execute("ALTER TABLE reflection_reviews ADD COLUMN idempotency_key TEXT")
        if "idempotency_digest" not in review_columns:
            cursor.execute("ALTER TABLE reflection_reviews ADD COLUMN idempotency_digest TEXT")
        if "project_id" not in review_columns:
            cursor.execute("ALTER TABLE reflection_reviews ADD COLUMN project_id TEXT")
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_reflection_reviews_idempotency "
            "ON reflection_reviews(idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
        skill_columns = {
            str(row[1])
            for row in cursor.execute("PRAGMA table_info(skills)").fetchall()
        }
        if "scope" not in skill_columns:
            cursor.execute(
                "ALTER TABLE skills ADD COLUMN scope TEXT NOT NULL DEFAULT 'global'"
            )
        if "project_id" not in skill_columns:
            cursor.execute("ALTER TABLE skills ADD COLUMN project_id TEXT")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_skills_scope_project "
            "ON skills(status, scope, project_id, updated_at, skill_id)"
        )
        for table in ("memories", "learning_chunks"):
            columns = {
                str(row[1])
                for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if "scope" not in columns:
                cursor.execute(
                    f"ALTER TABLE {table} ADD COLUMN scope TEXT NOT NULL DEFAULT 'global'"
                )
            if "project_id" not in columns:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN project_id TEXT")
            if "last_used_at" not in columns:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN last_used_at REAL")
            if "valid_from" not in columns:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN valid_from REAL")
            if "invalid_at" not in columns:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN invalid_at REAL")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_scope_project "
            "ON memories(status, scope, project_id, updated_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_validity "
            "ON memories(status, valid_from, invalid_at, updated_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_versions_validity "
            "ON memory_versions(status, memory_id, valid_from, invalid_at, updated_at)"
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS skill_runs (
                run_id TEXT PRIMARY KEY,
                skill_id TEXT NOT NULL REFERENCES skills(skill_id),
                status TEXT NOT NULL CHECK (status IN ('planned', 'completed', 'failed')),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                idempotency_key TEXT,
                idempotency_digest TEXT,
                payload_nonce TEXT NOT NULL,
                payload_ciphertext TEXT NOT NULL
            )
            """
        )
        skill_run_columns = {
            str(row[1])
            for row in cursor.execute("PRAGMA table_info(skill_runs)").fetchall()
        }
        if "idempotency_key" not in skill_run_columns:
            cursor.execute("ALTER TABLE skill_runs ADD COLUMN idempotency_key TEXT")
        if "idempotency_digest" not in skill_run_columns:
            cursor.execute("ALTER TABLE skill_runs ADD COLUMN idempotency_digest TEXT")
        cursor.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_skill_runs_idempotency_key
            ON skill_runs(idempotency_key)
            WHERE idempotency_key IS NOT NULL
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS reflection_candidates_insert_counts
            AFTER INSERT ON reflection_candidates
            FOR EACH ROW
            BEGIN
                UPDATE reflection_reviews
                SET candidate_count = candidate_count + 1,
                    accepted_count = accepted_count + CASE WHEN NEW.status = 'accepted' THEN 1 ELSE 0 END,
                    rejected_count = rejected_count + CASE WHEN NEW.status = 'rejected' THEN 1 ELSE 0 END,
                    updated_at = strftime('%s','now')
                WHERE review_id = NEW.review_id;
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS reflection_candidates_delete_counts
            AFTER DELETE ON reflection_candidates
            FOR EACH ROW
            BEGIN
                UPDATE reflection_reviews
                SET candidate_count = candidate_count - 1,
                    accepted_count = accepted_count - CASE WHEN OLD.status = 'accepted' THEN 1 ELSE 0 END,
                    rejected_count = rejected_count - CASE WHEN OLD.status = 'rejected' THEN 1 ELSE 0 END,
                    updated_at = strftime('%s','now')
                WHERE review_id = OLD.review_id;
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS reflection_candidates_update_counts
            AFTER UPDATE OF status ON reflection_candidates
            FOR EACH ROW
            BEGIN
                UPDATE reflection_reviews
                SET accepted_count = accepted_count
                    + CASE WHEN OLD.status = 'accepted' THEN -1 ELSE 0 END
                    + CASE WHEN NEW.status = 'accepted' THEN 1 ELSE 0 END,
                    rejected_count = rejected_count
                    + CASE WHEN OLD.status = 'rejected' THEN -1 ELSE 0 END
                    + CASE WHEN NEW.status = 'rejected' THEN 1 ELSE 0 END,
                    updated_at = strftime('%s','now')
                WHERE review_id = NEW.review_id;
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS reflection_candidates_guard_memory_update
            BEFORE UPDATE OF asset_type, status ON reflection_candidates
            FOR EACH ROW
            WHEN EXISTS (
                SELECT 1 FROM memories
                WHERE source_candidate_id = OLD.candidate_id
            )
            AND NOT (
                NEW.asset_type = 'memory'
                AND NEW.status = 'accepted'
            )
            BEGIN
                SELECT RAISE(ABORT, 'memory source candidate must remain accepted memory');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS reflection_candidates_guard_skill_update
            BEFORE UPDATE OF asset_type, status ON reflection_candidates
            FOR EACH ROW
            WHEN EXISTS (
                SELECT 1 FROM skills
                WHERE source_candidate_id = OLD.candidate_id
            )
            AND NOT (
                NEW.asset_type = 'skill'
                AND NEW.status = 'accepted'
            )
            BEGIN
                SELECT RAISE(ABORT, 'skill source candidate must remain accepted skill');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS memories_guard_active_chunk_update
            BEFORE UPDATE OF status ON memories
            FOR EACH ROW
            WHEN NEW.status <> 'active'
             AND EXISTS (
                SELECT 1 FROM learning_chunks
                WHERE asset_type = 'memory'
                  AND asset_id = OLD.memory_id
                  AND status = 'active'
             )
            BEGIN
                SELECT RAISE(ABORT, 'active memory cannot change while active learning chunks exist');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS memories_guard_active_chunk_delete
            BEFORE DELETE ON memories
            FOR EACH ROW
            WHEN EXISTS (
                SELECT 1 FROM learning_chunks
                WHERE asset_type = 'memory'
                  AND asset_id = OLD.memory_id
                  AND status = 'active'
            )
            BEGIN
                SELECT RAISE(ABORT, 'active memory cannot be deleted while active learning chunks exist');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS skills_guard_active_chunk_update
            BEFORE UPDATE OF status ON skills
            FOR EACH ROW
            WHEN NEW.status <> 'active'
             AND EXISTS (
                SELECT 1 FROM learning_chunks
                WHERE asset_type = 'skill'
                  AND asset_id = OLD.skill_id
                  AND status = 'active'
             )
            BEGIN
                SELECT RAISE(ABORT, 'active skill cannot change while active learning chunks exist');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS skills_guard_active_chunk_delete
            BEFORE DELETE ON skills
            FOR EACH ROW
            WHEN EXISTS (
                SELECT 1 FROM learning_chunks
                WHERE asset_type = 'skill'
                  AND asset_id = OLD.skill_id
                  AND status = 'active'
            )
            BEGIN
                SELECT RAISE(ABORT, 'active skill cannot be deleted while active learning chunks exist');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS memories_source_candidate_insert
            BEFORE INSERT ON memories
            FOR EACH ROW
            WHEN NOT EXISTS (
                SELECT 1 FROM reflection_candidates
                WHERE candidate_id = NEW.source_candidate_id
                  AND asset_type = 'memory'
                  AND status = 'accepted'
            )
            BEGIN
                SELECT RAISE(ABORT, 'memory source candidate must be accepted memory');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS memories_source_candidate_update
            BEFORE UPDATE ON memories
            FOR EACH ROW
            WHEN NOT EXISTS (
                SELECT 1 FROM reflection_candidates
                WHERE candidate_id = NEW.source_candidate_id
                  AND asset_type = 'memory'
                  AND status = 'accepted'
            )
            BEGIN
                SELECT RAISE(ABORT, 'memory source candidate must be accepted memory');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS skills_source_candidate_insert
            BEFORE INSERT ON skills
            FOR EACH ROW
            WHEN NOT EXISTS (
                SELECT 1 FROM reflection_candidates
                WHERE candidate_id = NEW.source_candidate_id
                  AND asset_type = 'skill'
                  AND status = 'accepted'
            )
            BEGIN
                SELECT RAISE(ABORT, 'skill source candidate must be accepted skill');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS skills_source_candidate_update
            BEFORE UPDATE ON skills
            FOR EACH ROW
            WHEN NOT EXISTS (
                SELECT 1 FROM reflection_candidates
                WHERE candidate_id = NEW.source_candidate_id
                  AND asset_type = 'skill'
                  AND status = 'accepted'
            )
            BEGIN
                SELECT RAISE(ABORT, 'skill source candidate must be accepted skill');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS learning_chunks_memory_asset_insert
            BEFORE INSERT ON learning_chunks
            FOR EACH ROW
            WHEN NEW.asset_type = 'memory'
             AND NOT EXISTS (
                SELECT 1 FROM memories
                WHERE memory_id = NEW.asset_id
                  AND status = 'active'
             )
            BEGIN
                SELECT RAISE(ABORT, 'learning chunk memory asset must be active memory');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS learning_chunks_memory_asset_update
            BEFORE UPDATE ON learning_chunks
            FOR EACH ROW
            WHEN NEW.asset_type = 'memory'
             AND NOT EXISTS (
                SELECT 1 FROM memories
                WHERE memory_id = NEW.asset_id
                  AND status = 'active'
             )
            BEGIN
                SELECT RAISE(ABORT, 'learning chunk memory asset must be active memory');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS learning_chunks_skill_asset_insert
            BEFORE INSERT ON learning_chunks
            FOR EACH ROW
            WHEN NEW.asset_type = 'skill'
             AND NOT EXISTS (
                SELECT 1 FROM skills
                WHERE skill_id = NEW.asset_id
                  AND status = 'active'
             )
            BEGIN
                SELECT RAISE(ABORT, 'learning chunk skill asset must be active skill');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS learning_chunks_skill_asset_update
            BEFORE UPDATE ON learning_chunks
            FOR EACH ROW
            WHEN NEW.asset_type = 'skill'
             AND NOT EXISTS (
                SELECT 1 FROM skills
                WHERE skill_id = NEW.asset_id
                  AND status = 'active'
             )
            BEGIN
                SELECT RAISE(ABORT, 'learning chunk skill asset must be active skill');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS learning_chunks_memory_asset_update_type_guard
            BEFORE UPDATE OF asset_type ON learning_chunks
            FOR EACH ROW
            WHEN NEW.asset_type = 'memory'
             AND NOT EXISTS (
                SELECT 1 FROM memories
                WHERE memory_id = NEW.asset_id
                  AND status = 'active'
             )
            BEGIN
                SELECT RAISE(ABORT, 'learning chunk memory asset must be active memory');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS learning_chunks_skill_asset_update_type_guard
            BEFORE UPDATE OF asset_type ON learning_chunks
            FOR EACH ROW
            WHEN NEW.asset_type = 'skill'
             AND NOT EXISTS (
                SELECT 1 FROM skills
                WHERE skill_id = NEW.asset_id
                  AND status = 'active'
             )
            BEGIN
                SELECT RAISE(ABORT, 'learning chunk skill asset must be active skill');
            END
            """
        )
        self._sanitize_legacy_memory_sync_errors()
        self.conn.commit()

    def _sanitize_legacy_memory_sync_errors(self) -> None:
        rows = self.conn.execute(
            "SELECT event_id, last_error FROM learning_sync_outbox "
            "WHERE last_error IS NOT NULL AND last_error <> ''"
        ).fetchall()
        updates = [
            (_serialize_memory_sync_error(row["last_error"]), row["event_id"])
            for row in rows
        ]
        if updates:
            self.conn.executemany(
                "UPDATE learning_sync_outbox SET last_error = ? WHERE event_id = ?",
                updates,
            )

    @_synchronized
    def list_pending_reviews(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        normalized_project_id = str(project_id or "").strip() or None
        if normalized_project_id is None:
            rows = self.conn.execute(
                """
                SELECT * FROM reflection_reviews
                WHERE status = 'pending' AND project_id IS NULL
                ORDER BY created_at DESC, review_id ASC
                """
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT * FROM reflection_reviews
                WHERE status = 'pending' AND project_id = ?
                ORDER BY created_at DESC, review_id ASC
                """,
                (normalized_project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @_synchronized
    def project_scope_snapshot(self, project_id: str) -> dict[str, Any]:
        """Return opaque IDs/counts needed to clean one project namespace."""
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            raise ValueError("project_id is required")
        memory_ids = [
            str(row[0])
            for row in self.conn.execute(
                "SELECT memory_id FROM memories WHERE scope = 'project' AND project_id = ? "
                "ORDER BY memory_id ASC",
                (normalized_project_id,),
            ).fetchall()
        ]
        skill_ids = [
            str(row[0])
            for row in self.conn.execute(
                "SELECT skill_id FROM skills WHERE scope = 'project' AND project_id = ? "
                "ORDER BY skill_id ASC",
                (normalized_project_id,),
            ).fetchall()
        ]
        return {
            "project_id": normalized_project_id,
            "memory_ids": memory_ids,
            "skill_ids": skill_ids,
            "memory_count": len(memory_ids),
            "skill_count": len(skill_ids),
            "skill_run_count": int(self.conn.execute(
                "SELECT COUNT(*) FROM skill_runs r JOIN skills s ON s.skill_id = r.skill_id "
                "WHERE s.scope = 'project' AND s.project_id = ?",
                (normalized_project_id,),
            ).fetchone()[0]),
            "learning_chunk_count": int(self.conn.execute(
                "SELECT COUNT(*) FROM learning_chunks WHERE scope = 'project' AND project_id = ?",
                (normalized_project_id,),
            ).fetchone()[0]),
            "pending_review_count": int(self.conn.execute(
                "SELECT COUNT(*) FROM reflection_reviews WHERE status = 'pending' AND project_id = ?",
                (normalized_project_id,),
            ).fetchone()[0]),
        }

    @_synchronized
    def delete_project_scope(self, project_id: str) -> dict[str, Any]:
        """Atomically purge one project namespace while preserving global assets."""
        snapshot = self.project_scope_snapshot(project_id)
        normalized_project_id = snapshot["project_id"]
        memory_ids = list(snapshot["memory_ids"])
        skill_ids = list(snapshot["skill_ids"])
        source_candidate_ids = [
            str(row[0])
            for row in self.conn.execute(
                "SELECT source_candidate_id FROM memories WHERE scope = 'project' AND project_id = ? "
                "UNION SELECT source_candidate_id FROM skills WHERE scope = 'project' AND project_id = ?",
                (normalized_project_id, normalized_project_id),
            ).fetchall()
        ]

        def placeholders(values: list[str]) -> str:
            return ", ".join("?" for _ in values)

        with self.conn:
            skill_run_count = 0
            memory_version_count = 0
            mutation_receipt_count = 0
            sync_outbox_count = 0
            learning_chunk_count = 0
            if skill_ids:
                cursor = self.conn.execute(
                    f"DELETE FROM skill_runs WHERE skill_id IN ({placeholders(skill_ids)})",
                    tuple(skill_ids),
                )
                skill_run_count = int(cursor.rowcount or 0)

            chunk_predicates = ["(scope = 'project' AND project_id = ?)"]
            chunk_params: list[Any] = [normalized_project_id]
            if memory_ids:
                chunk_predicates.append(
                    f"(asset_type = 'memory' AND asset_id IN ({placeholders(memory_ids)}))"
                )
                chunk_params.extend(memory_ids)
            if skill_ids:
                chunk_predicates.append(
                    f"(asset_type = 'skill' AND asset_id IN ({placeholders(skill_ids)}))"
                )
                chunk_params.extend(skill_ids)
            cursor = self.conn.execute(
                "DELETE FROM learning_chunks WHERE " + " OR ".join(chunk_predicates),
                tuple(chunk_params),
            )
            learning_chunk_count = int(cursor.rowcount or 0)

            version_predicates = ["(scope = 'project' AND project_id = ?)"]
            version_params: list[Any] = [normalized_project_id]
            if memory_ids:
                version_predicates.append(
                    f"memory_id IN ({placeholders(memory_ids)})"
                )
                version_params.extend(memory_ids)
            cursor = self.conn.execute(
                "DELETE FROM memory_versions WHERE " + " OR ".join(version_predicates),
                tuple(version_params),
            )
            memory_version_count = int(cursor.rowcount or 0)

            if memory_ids:
                memory_params = tuple(memory_ids)
                cursor = self.conn.execute(
                    f"DELETE FROM memory_mutation_receipts WHERE memory_id IN ({placeholders(memory_ids)})",
                    memory_params,
                )
                mutation_receipt_count = int(cursor.rowcount or 0)
                cursor = self.conn.execute(
                    f"DELETE FROM learning_sync_outbox WHERE memory_id IN ({placeholders(memory_ids)})",
                    memory_params,
                )
                sync_outbox_count = int(cursor.rowcount or 0)

            cursor = self.conn.execute(
                "DELETE FROM memories WHERE scope = 'project' AND project_id = ?",
                (normalized_project_id,),
            )
            memory_count = int(cursor.rowcount or 0)
            cursor = self.conn.execute(
                "DELETE FROM skills WHERE scope = 'project' AND project_id = ?",
                (normalized_project_id,),
            )
            skill_count = int(cursor.rowcount or 0)

            candidate_count = 0
            candidate_predicates = [
                "review_id IN (SELECT review_id FROM reflection_reviews WHERE project_id = ?)",
            ]
            candidate_params: list[Any] = [normalized_project_id]
            if source_candidate_ids:
                candidate_predicates.append(
                    f"candidate_id IN ({placeholders(source_candidate_ids)})"
                )
                candidate_params.extend(source_candidate_ids)
            cursor = self.conn.execute(
                "DELETE FROM reflection_candidates WHERE ("
                + " OR ".join(candidate_predicates)
                + ") AND NOT EXISTS (SELECT 1 FROM memories WHERE source_candidate_id = candidate_id)"
                " AND NOT EXISTS (SELECT 1 FROM skills WHERE source_candidate_id = candidate_id)",
                tuple(candidate_params),
            )
            candidate_count = int(cursor.rowcount or 0)
            cursor = self.conn.execute(
                "DELETE FROM reflection_reviews WHERE project_id = ? "
                "AND NOT EXISTS (SELECT 1 FROM reflection_candidates WHERE review_id = reflection_reviews.review_id)",
                (normalized_project_id,),
            )
            review_count = int(cursor.rowcount or 0)
            cursor = self.conn.execute(
                "DELETE FROM retrieval_feedback WHERE scope = 'project' AND project_id = ?",
                (normalized_project_id,),
            )
            feedback_count = int(cursor.rowcount or 0)

        return {
            "status": "deleted",
            "project_id": normalized_project_id,
            "memory_count": memory_count,
            "skill_count": skill_count,
            "skill_run_count": skill_run_count,
            "learning_chunk_count": learning_chunk_count,
            "memory_version_count": memory_version_count,
            "mutation_receipt_count": mutation_receipt_count,
            "sync_outbox_count": sync_outbox_count,
            "candidate_count": candidate_count,
            "review_count": review_count,
            "feedback_count": feedback_count,
        }

    @_synchronized
    def list_memories(
        self,
        project_id: str | None = None,
        *,
        context_only: bool = False,
        include_metadata: bool = False,
        include_global: bool = True,
        as_of: float | None = None,
        workflow_id: str | None = None,
    ) -> list[dict[str, Any]]:
        project_id = str(project_id or "").strip()
        workflow_id = str(workflow_id or "").strip() or None
        validity_clause = ""
        validity_params: tuple[float, float] = ()
        if context_only or as_of is not None:
            reference_time = _normalise_timestamp(
                _now() if as_of is None else as_of,
                field="as_of",
            )
            validity_clause = (
                " AND (valid_from IS NULL OR valid_from <= ?)"
                " AND (invalid_at IS NULL OR invalid_at > ?)"
            )
            validity_params = (reference_time, reference_time)
        if project_id:
            if include_global:
                rows = self.conn.execute(
                    "SELECT * FROM memories WHERE status = 'active' "
                    f"{validity_clause} "
                    "AND (scope = 'global' OR (scope = 'project' AND project_id = ?)) "
                    "ORDER BY updated_at DESC",
                    (*validity_params, project_id),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM memories WHERE status = 'active' "
                    f"{validity_clause} "
                    "AND scope = 'project' AND project_id = ? "
                    "ORDER BY updated_at DESC",
                    (*validity_params, project_id),
                ).fetchall()
        elif context_only:
            rows = self.conn.execute(
                "SELECT * FROM memories WHERE status = 'active' "
                f"{validity_clause} AND scope = 'global' ORDER BY updated_at DESC",
                validity_params,
            ).fetchall()
        elif as_of is not None:
            rows = self.conn.execute(
                "SELECT * FROM memories WHERE status = 'active' "
                f"{validity_clause} AND scope = 'global' ORDER BY updated_at DESC",
                validity_params,
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM memories WHERE status = 'active' ORDER BY updated_at DESC"
            ).fetchall()
        records = []
        workflow_context_only = context_only or as_of is not None
        for row in rows:
            record = self._memory_from_row(row, include_metadata=include_metadata)
            if _workflow_memory_visible(
                record.get("subtype"),
                record,
                workflow_id,
                context_only=workflow_context_only,
            ):
                records.append(record)
        if context_only or as_of is not None:
            history_sql = (
                "SELECT * FROM memory_versions WHERE status = 'active' "
                f"{validity_clause}"
            )
            history_params: list[Any] = list(validity_params)
            if project_id:
                if include_global:
                    history_sql += " AND (scope = 'global' OR (scope = 'project' AND project_id = ?))"
                    history_params.append(project_id)
                else:
                    history_sql += " AND scope = 'project' AND project_id = ?"
                    history_params.append(project_id)
            elif context_only or not project_id:
                history_sql += " AND scope = 'global'"
            history_rows = self.conn.execute(
                history_sql + " ORDER BY updated_at DESC, memory_version_id ASC",
                history_params,
            ).fetchall()
            for row in history_rows:
                record = self._memory_version_from_row(row, include_metadata=include_metadata)
                if _workflow_memory_visible(
                    record.get("subtype"),
                    record,
                    workflow_id,
                    context_only=True,
                ):
                    records.append(record)
        return sorted(
            records,
            key=lambda item: (
                -float(item.get("updated_at") or 0.0),
                str(item.get("memory_id") or item.get("asset_id") or ""),
            ),
        )

    @_synchronized
    def touch_memories(self, memory_ids: list[str], *, used_at: float | None = None) -> int:
        """Record actual context injection without changing preference freshness."""
        ids = list(dict.fromkeys(str(memory_id or "").strip() for memory_id in memory_ids))
        ids = [memory_id for memory_id in ids if memory_id]
        if not ids:
            return 0
        timestamp = float(used_at if used_at is not None else _now())
        placeholders = ", ".join("?" for _ in ids)
        with self.conn:
            cursor = self.conn.execute(
                f"UPDATE memories SET last_used_at = ? "
                f"WHERE status = 'active' AND memory_id IN ({placeholders})",
                (timestamp, *ids),
            )
            self.conn.execute(
                f"UPDATE learning_chunks SET last_used_at = ? "
                f"WHERE asset_type = 'memory' AND status = 'active' AND asset_id IN ({placeholders})",
                (timestamp, *ids),
            )
        return int(cursor.rowcount or 0)

    @_synchronized
    def memory_health(
        self,
        project_id: str | None = None,
        *,
        include_global: bool = True,
        max_issues: int = 50,
    ) -> dict[str, Any]:
        """Audit memory/index invariants without changing the learning store.

        The audit deliberately reports identifiers and counts, not decrypted
        memory content. It is a doctor-style diagnostic surface: callers can
        decide whether to edit/revoke/rebuild, while this method never repairs
        or merges facts automatically.
        """
        normalized_project_id = str(project_id or "").strip() or None
        max_issues = max(1, min(int(max_issues), 200))
        now = _now()

        def scope_clause(alias: str) -> tuple[str, list[Any]]:
            if normalized_project_id:
                if include_global:
                    return (
                        f" AND ({alias}.scope = 'global' OR "
                        f"({alias}.scope = 'project' AND {alias}.project_id = ?))",
                        [normalized_project_id],
                    )
                return (
                    f" AND {alias}.scope = 'project' AND {alias}.project_id = ?",
                    [normalized_project_id],
                )
            return f" AND {alias}.scope = 'global'", []

        def in_scope(row: Any) -> bool:
            scope = str(row["scope"] or "global")
            row_project_id = str(row["project_id"] or "").strip() or None
            if normalized_project_id:
                return (include_global and scope == "global") or (
                    scope == "project" and row_project_id == normalized_project_id
                )
            return scope == "global"

        issues: list[dict[str, Any]] = []
        total_issue_count = 0
        truncated = False

        def add_issue(kind: str, severity: str, **fields: Any) -> None:
            nonlocal total_issue_count, truncated
            total_issue_count += 1
            if len(issues) >= max_issues:
                truncated = True
                return
            issues.append({"kind": kind, "severity": severity, **fields})

        memory_scope_sql, memory_scope_params = scope_clause("m")
        memory_rows = self.conn.execute(
            "SELECT m.memory_id, m.subtype, m.status, m.scope, m.project_id, "
            "m.created_at, m.updated_at, m.payload_nonce, m.payload_ciphertext "
            "FROM memories m WHERE m.status = 'active'" + memory_scope_sql +
            " ORDER BY m.updated_at DESC, m.memory_id ASC",
            memory_scope_params,
        ).fetchall()
        memory_by_id = {str(row["memory_id"]): row for row in memory_rows}

        duplicate_groups: dict[tuple[str, str, str | None, str, str, str], list[str]] = {}
        for row in memory_rows:
            memory_id = str(row["memory_id"])
            try:
                payload = self._decrypt_payload(
                    row["payload_nonce"],
                    row["payload_ciphertext"],
                    table="memories",
                    pk_name="memory_id",
                    pk_value=memory_id,
                    asset_type="memory",
                )
            except ValueError:
                add_issue(
                    "memory_payload_unreadable",
                    "error",
                    memory_ids=[memory_id],
                    scope=row["scope"] or "global",
                    project_id=row["project_id"],
                )
                continue
            memory_text = f"{payload.get('title') or ''}\n{payload.get('content') or ''}"
            if contains_untrusted_memory_instruction(memory_text):
                add_issue(
                    "memory_untrusted_instruction",
                    "error",
                    memory_ids=[memory_id],
                    scope=row["scope"] or "global",
                    project_id=row["project_id"],
                )
            if contains_sensitive_memory(memory_text):
                add_issue(
                    "memory_sensitive_content",
                    "error",
                    memory_ids=[memory_id],
                    scope=row["scope"] or "global",
                    project_id=row["project_id"],
                )
            subtype = str(row["subtype"] or "")
            if classify_memory_family("memory", subtype) == "unknown":
                add_issue(
                    "unclassified_memory_family",
                    "error",
                    memory_ids=[memory_id],
                    subtype=subtype,
                    scope=row["scope"] or "global",
                    project_id=row["project_id"],
                )
            workflow_id = str(payload.get("workflow_id") or "").strip()
            if subtype == "workflow_fact":
                if not workflow_id:
                    add_issue(
                        "workflow_memory_unbound",
                        "error",
                        memory_ids=[memory_id],
                        subtype=subtype,
                        scope=row["scope"] or "global",
                        project_id=row["project_id"],
                    )
                elif workflow_id not in SUPPORTED_WORKFLOW_IDS:
                    add_issue(
                        "workflow_memory_unknown_namespace",
                        "error",
                        memory_ids=[memory_id],
                        subtype=subtype,
                        workflow_id=workflow_id,
                        scope=row["scope"] or "global",
                        project_id=row["project_id"],
                    )
            if subtype == "project_constraint" and (
                str(row["scope"] or "global") != "project"
                or not str(row["project_id"] or "").strip()
            ):
                add_issue(
                    "project_constraint_unbound",
                    "error",
                    memory_ids=[memory_id],
                    subtype=subtype,
                    scope=row["scope"] or "global",
                    project_id=row["project_id"],
                )
            key = (
                subtype,
                str(row["scope"] or "global"),
                str(row["project_id"] or "").strip() or None,
                _normalise_memory_text(payload.get("title")),
                _normalise_memory_text(payload.get("content")),
                workflow_id
                if subtype == "workflow_fact"
                else "",
            )
            duplicate_groups.setdefault(key, []).append(memory_id)

        for (subtype, scope, group_project_id, _title, _content, _workflow_id), memory_ids in duplicate_groups.items():
            if len(memory_ids) > 1:
                add_issue(
                    "duplicate_active_memory",
                    "error",
                    memory_ids=memory_ids,
                    count=len(memory_ids),
                    subtype=subtype,
                    scope=scope,
                    project_id=group_project_id,
                )

        chunk_scope_sql, chunk_scope_params = scope_clause("c")
        chunk_rows = self.conn.execute(
            "SELECT c.chunk_id, c.asset_id, c.asset_type, c.subtype, c.status, "
            "c.scope, c.project_id, c.updated_at, c.payload_nonce, c.payload_ciphertext "
            "FROM learning_chunks c WHERE c.status = 'active' "
            "AND c.asset_type = 'memory'" + chunk_scope_sql +
            " ORDER BY c.updated_at DESC, c.chunk_id ASC",
            chunk_scope_params,
        ).fetchall()
        chunks_by_asset: dict[str, list[Any]] = {}
        for chunk in chunk_rows:
            chunks_by_asset.setdefault(str(chunk["asset_id"]), []).append(chunk)

            try:
                self._decrypt_payload(
                    chunk["payload_nonce"],
                    chunk["payload_ciphertext"],
                    table="learning_chunks",
                    pk_name="chunk_id",
                    pk_value=chunk["chunk_id"],
                    asset_type=chunk["asset_type"],
                )
            except ValueError:
                add_issue(
                    "memory_chunk_payload_unreadable",
                    "error",
                    chunk_ids=[chunk["chunk_id"]],
                    memory_ids=[chunk["asset_id"]],
                    scope=chunk["scope"] or "global",
                    project_id=chunk["project_id"],
                )

            parent = memory_by_id.get(str(chunk["asset_id"]))
            if parent is None:
                parent_row = self.conn.execute(
                    "SELECT memory_id, status, scope, project_id FROM memories WHERE memory_id = ?",
                    (chunk["asset_id"],),
                ).fetchone()
                if parent_row is not None and not in_scope(parent_row):
                    continue
                add_issue(
                    "orphan_active_chunk",
                    "error",
                    chunk_ids=[chunk["chunk_id"]],
                    memory_ids=[chunk["asset_id"]],
                    scope=chunk["scope"] or "global",
                    project_id=chunk["project_id"],
                )
                continue
            if str(parent["status"] or "") != "active":
                add_issue(
                    "archived_memory_active_chunk",
                    "error",
                    chunk_ids=[chunk["chunk_id"]],
                    memory_ids=[chunk["asset_id"]],
                    scope=parent["scope"] or "global",
                    project_id=parent["project_id"],
                )
                continue
            if (
                str(chunk["scope"] or "global") != str(parent["scope"] or "global")
                or (str(chunk["project_id"] or "").strip() or None)
                != (str(parent["project_id"] or "").strip() or None)
            ):
                add_issue(
                    "memory_chunk_scope_mismatch",
                    "error",
                    chunk_ids=[chunk["chunk_id"]],
                    memory_ids=[chunk["asset_id"]],
                    scope=parent["scope"] or "global",
                    project_id=parent["project_id"],
                )

        for memory_id, row in memory_by_id.items():
            if not chunks_by_asset.get(memory_id):
                add_issue(
                    "memory_missing_chunk",
                    "error",
                    memory_ids=[memory_id],
                    scope=row["scope"] or "global",
                    project_id=row["project_id"],
                )

        version_scope_sql, version_scope_params = scope_clause("v")
        version_rows = self.conn.execute(
            "SELECT v.memory_version_id, v.memory_id, v.record_version, v.subtype, v.status, "
            "v.scope, v.project_id, v.valid_from, v.invalid_at, "
            "v.payload_nonce, v.payload_ciphertext "
            "FROM memory_versions v WHERE v.status = 'active'" + version_scope_sql +
            " ORDER BY v.updated_at DESC, v.memory_version_id ASC",
            version_scope_params,
        ).fetchall()
        active_version_count = len(version_rows)
        version_parent_cache: dict[str, Any | None] = {}
        version_intervals: dict[str, list[tuple[float, float, str]]] = {}
        for version in version_rows:
            memory_id = str(version["memory_id"] or "")
            version_id = str(version["memory_version_id"] or "")
            try:
                version_payload = self._decrypt_payload(
                    version["payload_nonce"],
                    version["payload_ciphertext"],
                    table="memory_versions",
                    pk_name="memory_version_id",
                    pk_value=version_id,
                    asset_type="memory",
                )
            except ValueError:
                version_payload = None
                add_issue(
                    "memory_version_payload_unreadable",
                    "error",
                    memory_version_ids=[version_id],
                    memory_ids=[memory_id],
                    scope=version["scope"] or "global",
                    project_id=version["project_id"],
                )
            if version_payload is not None:
                if classify_memory_family("memory", version["subtype"]) == "unknown":
                    add_issue(
                        "unclassified_memory_family",
                        "error",
                        memory_version_ids=[version_id],
                        memory_ids=[memory_id],
                        subtype=version["subtype"],
                        scope=version["scope"] or "global",
                        project_id=version["project_id"],
                    )
                version_text = (
                    f"{version_payload.get('title') or ''}\n"
                    f"{version_payload.get('content') or ''}"
                )
                if contains_untrusted_memory_instruction(version_text):
                    add_issue(
                        "memory_version_untrusted_instruction",
                        "error",
                        memory_version_ids=[version_id],
                        memory_ids=[memory_id],
                        scope=version["scope"] or "global",
                        project_id=version["project_id"],
                    )
                if contains_sensitive_memory(version_text):
                    add_issue(
                        "memory_version_sensitive_content",
                        "error",
                        memory_version_ids=[version_id],
                        memory_ids=[memory_id],
                        scope=version["scope"] or "global",
                        project_id=version["project_id"],
                    )
                if str(version["subtype"] or "") == "workflow_fact":
                    version_workflow_id = str(version_payload.get("workflow_id") or "").strip()
                    if not version_workflow_id:
                        add_issue(
                            "workflow_memory_version_unbound",
                            "error",
                            memory_version_ids=[version_id],
                            memory_ids=[memory_id],
                            subtype="workflow_fact",
                            scope=version["scope"] or "global",
                            project_id=version["project_id"],
                        )
                    elif version_workflow_id not in SUPPORTED_WORKFLOW_IDS:
                        add_issue(
                            "workflow_memory_version_unknown_namespace",
                            "error",
                            memory_version_ids=[version_id],
                            memory_ids=[memory_id],
                            subtype="workflow_fact",
                            workflow_id=version_workflow_id,
                            scope=version["scope"] or "global",
                            project_id=version["project_id"],
                        )
                if str(version["subtype"] or "") == "project_constraint" and (
                    str(version["scope"] or "global") != "project"
                    or not str(version["project_id"] or "").strip()
                ):
                    add_issue(
                        "project_constraint_version_unbound",
                        "error",
                        memory_version_ids=[version_id],
                        memory_ids=[memory_id],
                        subtype="project_constraint",
                        scope=version["scope"] or "global",
                        project_id=version["project_id"],
                    )
            if version["valid_from"] is not None and version["invalid_at"] is not None:
                if float(version["invalid_at"]) <= float(version["valid_from"]):
                    add_issue(
                        "memory_version_invalid_interval",
                        "error",
                        memory_version_ids=[version["memory_version_id"]],
                        memory_ids=[memory_id],
                        scope=version["scope"] or "global",
                        project_id=version["project_id"],
                    )
                else:
                    version_intervals.setdefault(memory_id, []).append((
                        float(version["valid_from"]),
                        float(version["invalid_at"]),
                        str(version["memory_version_id"]),
                    ))
            if memory_id not in version_parent_cache:
                version_parent_cache[memory_id] = memory_by_id.get(memory_id)
                if version_parent_cache[memory_id] is None:
                    version_parent_cache[memory_id] = self.conn.execute(
                        "SELECT memory_id, status, scope, project_id FROM memories WHERE memory_id = ?",
                        (memory_id,),
                    ).fetchone()
            parent = version_parent_cache[memory_id]
            if parent is None:
                add_issue(
                    "orphan_memory_version",
                    "error",
                    memory_version_ids=[version["memory_version_id"]],
                    memory_ids=[memory_id],
                    scope=version["scope"] or "global",
                    project_id=version["project_id"],
                )
                continue
            if not in_scope(parent):
                continue
            if (
                str(version["scope"] or "global") != str(parent["scope"] or "global")
                or (str(version["project_id"] or "").strip() or None)
                != (str(parent["project_id"] or "").strip() or None)
            ):
                add_issue(
                    "memory_version_scope_mismatch",
                    "error",
                    memory_version_ids=[version["memory_version_id"]],
                    memory_ids=[memory_id],
                    scope=parent["scope"] or "global",
                    project_id=parent["project_id"],
                )

        for memory_id, intervals in version_intervals.items():
            ordered_intervals = sorted(intervals, key=lambda item: (item[0], item[1], item[2]))
            previous = None
            for current in ordered_intervals:
                if previous is not None and current[0] < previous[1]:
                    add_issue(
                        "memory_version_overlap",
                        "error",
                        memory_version_ids=[previous[2], current[2]],
                        memory_ids=[memory_id],
                        overlap_range={
                            "valid_from": current[0],
                            "invalid_at": min(previous[1], current[1]),
                        },
                    )
                    previous = max(previous, current, key=lambda item: (item[1], item[0], item[2]))
                else:
                    previous = current

        if normalized_project_id:
            if include_global:
                pending_review_scope_sql = " AND (r.project_id IS NULL OR r.project_id = ?)"
            else:
                pending_review_scope_sql = " AND r.project_id = ?"
            pending_review_scope_params = [normalized_project_id]
        else:
            pending_review_scope_sql = " AND r.project_id IS NULL"
            pending_review_scope_params = []
        pending_review_count = int(self.conn.execute(
            "SELECT COUNT(*) FROM reflection_reviews r WHERE r.status = 'pending'" + pending_review_scope_sql,
            pending_review_scope_params,
        ).fetchone()[0])

        pending_sync_rows = self.conn.execute(
            "SELECT event_id, attempts, next_attempt_at, retry_paused_at, payload_nonce, payload_ciphertext "
            "FROM learning_sync_outbox WHERE status = 'pending' ORDER BY created_at ASC, event_id ASC"
        ).fetchall()
        pending_sync_count = 0
        retrying_sync_count = 0
        retry_blocked_sync_count = 0
        paused_sync_count = 0
        for row in pending_sync_rows:
            try:
                payload = self._decrypt_payload(
                    row["payload_nonce"],
                    row["payload_ciphertext"],
                    table="learning_sync_outbox",
                    pk_name="event_id",
                    pk_value=row["event_id"],
                    asset_type="memory",
                )
            except ValueError:
                add_issue("sync_payload_unreadable", "error", event_ids=[row["event_id"]])
                continue
            event_memory = payload.get("memory") if isinstance(payload, dict) else None
            event_scope = event_memory if isinstance(event_memory, dict) else payload
            if normalized_project_id:
                event_scope_name = str(event_scope.get("scope") or "global")
                event_project_id = str(event_scope.get("project_id") or "").strip() or None
                allowed = (include_global and event_scope_name == "global") or (
                    event_scope_name == "project" and event_project_id == normalized_project_id
                )
                if not allowed:
                    continue
            elif str(event_scope.get("scope") or "global") != "global":
                continue
            event_memory_id = str(
                (event_memory or {}).get("memory_id") or row["event_id"]
            ) if isinstance(event_memory, dict) else str(row["event_id"])
            if isinstance(event_memory, dict):
                event_memory_text = (
                    f"{event_memory.get('title') or ''}\n"
                    f"{event_memory.get('content') or ''}"
                )
                event_issue_fields = {
                    "event_ids": [row["event_id"]],
                    "memory_ids": [event_memory_id] if event_memory_id else [],
                    "scope": event_scope.get("scope") or "global",
                    "project_id": event_scope.get("project_id"),
                }
                if contains_untrusted_memory_instruction(event_memory_text):
                    add_issue(
                        "pending_sync_untrusted_instruction",
                        "error",
                        **event_issue_fields,
                    )
                if contains_sensitive_memory(event_memory_text):
                    add_issue(
                        "pending_sync_sensitive_content",
                        "error",
                        **event_issue_fields,
                    )
            pending_sync_count += 1
            attempts = int(row["attempts"] or 0)
            if attempts > 0:
                retrying_sync_count += 1
            if row["next_attempt_at"] is not None and float(row["next_attempt_at"]) > now:
                retry_blocked_sync_count += 1
            if row["retry_paused_at"] is not None:
                paused_sync_count += 1
                add_issue(
                    "paused_memory_sync",
                    "error",
                    event_ids=[row["event_id"]],
                    memory_ids=[event_memory_id] if event_memory_id else [],
                    attempts=int(row["attempts"] or 0),
                    max_attempts=MEMORY_SYNC_MAX_ATTEMPTS,
                )

        if pending_sync_count:
            add_issue(
                "pending_memory_sync",
                "warning",
                count=pending_sync_count,
                retrying_count=retrying_sync_count,
                retry_blocked_count=retry_blocked_sync_count,
                paused_count=paused_sync_count,
            )

        hard_issue_count = sum(1 for issue in issues if issue["severity"] == "error")
        status = "error" if hard_issue_count else "degraded" if pending_sync_count else "ok"
        return {
            "status": status,
            "scope": "project" if normalized_project_id else "global",
            "project_id": normalized_project_id,
            "include_global": bool(include_global),
            "counts": {
                "active_memories": len(memory_rows),
                "active_memory_chunks": len(chunk_rows),
                "active_memory_versions": active_version_count,
                "pending_reviews": pending_review_count,
                "pending_syncs": pending_sync_count,
                "retrying_syncs": retrying_sync_count,
                "retry_blocked_syncs": retry_blocked_sync_count,
                "paused_syncs": paused_sync_count,
            },
            "issues": issues,
            "issue_count": total_issue_count,
            "truncated": truncated,
        }

    @_synchronized
    def retrieval_feedback_health(
        self,
        project_id: str | None = None,
        *,
        include_global: bool = True,
        max_issues: int = 50,
    ) -> dict[str, Any]:
        """Audit confirmed retrieval corrections without changing them.

        Older workspaces may contain duplicate global rows because SQLite
        considers NULL values distinct in a UNIQUE constraint.  This surface
        reports those rows by opaque IDs and digests only; it never chooses a
        correction to delete or merge automatically.
        """
        normalized_project_id = str(project_id or "").strip() or None
        max_issues = max(1, min(int(max_issues), 200))
        if normalized_project_id:
            if include_global:
                where = "scope = 'global' OR (scope = 'project' AND project_id = ?)"
                params: tuple[Any, ...] = (normalized_project_id,)
            else:
                where = "scope = 'project' AND project_id = ?"
                params = (normalized_project_id,)
        else:
            where = "scope = 'global'"
            params = ()

        rows = self.conn.execute(
            """
            SELECT feedback_id, scope, project_id, workflow_id, query_digest, status, updated_at
            FROM retrieval_feedback
            WHERE """ + where + """
            ORDER BY updated_at DESC, feedback_id ASC
            """,
            params,
        ).fetchall()
        groups: dict[tuple[str, str | None, str | None, str], list[Any]] = {}
        active_count = 0
        revoked_count = 0
        for row in rows:
            if str(row["status"] or "") == "active":
                active_count += 1
            else:
                revoked_count += 1
            key = (
                str(row["scope"] or "global"),
                str(row["project_id"] or "").strip() or None,
                str(row["workflow_id"] or "").strip() or None,
                str(row["query_digest"] or ""),
            )
            groups.setdefault(key, []).append(row)

        issues: list[dict[str, Any]] = []
        total_issue_count = 0
        truncated = False

        def add_issue(kind: str, severity: str, **fields: Any) -> None:
            nonlocal total_issue_count, truncated
            total_issue_count += 1
            if len(issues) >= max_issues:
                truncated = True
                return
            issues.append({"kind": kind, "severity": severity, **fields})

        duplicate_active_groups = 0
        for (scope, group_project_id, group_workflow_id, query_digest), group_rows in groups.items():
            active_rows = [row for row in group_rows if str(row["status"] or "") == "active"]
            if len(active_rows) <= 1:
                continue
            duplicate_active_groups += 1
            add_issue(
                "duplicate_active_retrieval_feedback",
                "error",
                feedback_ids=sorted(str(row["feedback_id"]) for row in active_rows),
                count=len(active_rows),
                scope=scope,
                project_id=group_project_id,
                workflow_id=group_workflow_id,
                query_digest=query_digest,
            )

        index_names = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA index_list(retrieval_feedback)").fetchall()
        }
        unique_index_enforced = {
            "global_active": "idx_retrieval_feedback_global_active_unique" in index_names,
            "project_active": "idx_retrieval_feedback_project_active_unique" in index_names,
        }
        if not all(unique_index_enforced.values()):
            add_issue(
                "retrieval_feedback_unique_index_deferred",
                "warning",
                enforced=unique_index_enforced,
                reason="active duplicate rows must be reviewed before the database guard can be created",
            )

        hard_issue_count = sum(1 for issue in issues if issue["severity"] == "error")
        return {
            "status": "error" if hard_issue_count else "degraded" if issues else "ok",
            "scope": "project" if normalized_project_id else "global",
            "project_id": normalized_project_id,
            "include_global": bool(include_global),
            "counts": {
                "active_feedback": active_count,
                "revoked_feedback": revoked_count,
                "feedback_groups": len(groups),
                "duplicate_active_groups": duplicate_active_groups,
            },
            "unique_index_enforced": unique_index_enforced,
            "issues": issues,
            "issue_count": total_issue_count,
            "truncated": truncated,
        }

    @staticmethod
    def _require_memory_project_scope(row: sqlite3.Row, project_id: str | None) -> None:
        if str(row["scope"] or "global").strip().lower() != "project":
            return
        normalized_project_id = str(project_id or "").strip() or None
        stored_project_id = str(row["project_id"] or "").strip() or None
        if normalized_project_id is None:
            raise ValueError("project_id is required for project-scoped memory")
        if stored_project_id != normalized_project_id:
            raise ValueError("memory belongs to another project")

    @staticmethod
    def _normalise_memory_idempotency_key(idempotency_key: str | None) -> str | None:
        key = str(idempotency_key or "").strip()
        if not key:
            return None
        if len(key) > 128 or any(ord(char) < 33 or ord(char) > 126 for char in key):
            raise ValueError("idempotency_key must be printable and at most 128 characters")
        return key

    @staticmethod
    def _normalise_skill_idempotency_key(idempotency_key: str | None) -> str | None:
        return LearningStore._normalise_memory_idempotency_key(idempotency_key)

    def _begin_memory_mutation_locked(
        self,
        *,
        idempotency_key: str,
        operation: str,
        memory_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        canonical = json.dumps(
            {
                "operation": operation,
                "memory_id": memory_id,
                "payload": payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        input_digest = hmac.new(self.keys.metadata_key, canonical, hashlib.sha256).hexdigest()
        row = self.conn.execute(
            "SELECT input_digest, status, response_nonce, response_ciphertext "
            "FROM memory_mutation_receipts WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return {"status": "claimed", "input_digest": input_digest}
        if not hmac.compare_digest(str(row["input_digest"] or ""), input_digest):
            raise ValueError("idempotency_key is already bound to a different memory mutation")
        response = self._decrypt_payload(
            row["response_nonce"],
            row["response_ciphertext"],
            table="memory_mutation_receipts",
            pk_name="idempotency_key",
            pk_value=idempotency_key,
            asset_type="memory",
        )
        if not isinstance(response, dict):
            raise ValueError("invalid memory mutation receipt envelope")
        schema_version = response.get("schema_version")
        if schema_version not in (None, MEMORY_MUTATION_RECEIPT_SCHEMA_VERSION):
            raise ValueError("unsupported memory mutation receipt schema")
        memory = response.get("memory")
        if not isinstance(memory, dict):
            raise ValueError("invalid memory mutation receipt response")
        return {
            "status": "replayed",
            "input_digest": input_digest,
            "memory": memory,
        }

    def _complete_memory_mutation_locked(
        self,
        *,
        idempotency_key: str,
        operation: str,
        memory_id: str,
        input_digest: str,
        memory: dict[str, Any],
    ) -> None:
        nonce, ciphertext = self._encrypt_payload(
            {
                "schema_version": MEMORY_MUTATION_RECEIPT_SCHEMA_VERSION,
                "memory": memory,
            },
            table="memory_mutation_receipts",
            pk_name="idempotency_key",
            pk_value=idempotency_key,
            asset_type="memory",
        )
        now = _now()
        self.conn.execute(
            """
            INSERT INTO memory_mutation_receipts (
                idempotency_key, operation, memory_id, input_digest, status,
                created_at, updated_at, response_nonce, response_ciphertext
            ) VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?)
            """,
            (
                idempotency_key,
                operation,
                memory_id,
                input_digest,
                now,
                now,
                nonce,
                ciphertext,
            ),
        )

    @staticmethod
    def memory_sync_event_id(operation: str, memory: dict[str, Any]) -> str:
        operation = str(operation or "").strip()
        if operation not in {"memory_update", "memory_revoke", "memory_delete"}:
            raise ValueError("unsupported memory sync operation")
        memory_id = str(memory.get("memory_id") or "").strip()
        if not memory_id:
            raise ValueError("memory_id is required for memory sync")
        record_version = int(memory.get("record_version") or 1)
        return f"{operation}:{memory_id}:v{record_version}"

    @staticmethod
    def _memory_sync_retry_delay(attempts: int) -> float:
        """Back off failed derived syncs without hiding them from later recovery."""
        attempt = max(int(attempts or 1), 1)
        # Preserve the existing fast one-retry recovery contract. Repeated
        # failures then back off exponentially instead of hot-looping on each
        # query or memory-manager refresh.
        return min(300.0, float(2 ** min(max(attempt - 2, 0), 8))) if attempt > 1 else 0.0

    def _queue_memory_sync_locked(self, operation: str, memory: dict[str, Any]) -> str:
        event_id = self.memory_sync_event_id(operation, memory)
        nonce, ciphertext = self._encrypt_payload(
            {
                "operation": operation,
                "memory": memory,
                "project_id": memory.get("project_id"),
            },
            table="learning_sync_outbox",
            pk_name="event_id",
            pk_value=event_id,
            asset_type="memory",
        )
        now = _now()
        insert_cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO learning_sync_outbox (
                event_id, operation, memory_id, status, attempts, last_error,
                created_at, updated_at, claim_token, claim_until, next_attempt_at,
                payload_nonce, payload_ciphertext
            ) VALUES (?, ?, ?, 'pending', 0, NULL, ?, ?, NULL, NULL, NULL, ?, ?)
            """,
            (
                event_id,
                operation,
                str(memory["memory_id"]),
                now,
                now,
                nonce,
                ciphertext,
            ),
        )
        if insert_cursor.rowcount == 0:
            existing = self.conn.execute(
                "SELECT operation, payload_nonce, payload_ciphertext "
                "FROM learning_sync_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if existing is None:
                raise ValueError("memory sync event disappeared")
            existing_payload = self._decrypt_payload(
                existing["payload_nonce"],
                existing["payload_ciphertext"],
                table="learning_sync_outbox",
                pk_name="event_id",
                pk_value=event_id,
                asset_type="memory",
            )
            expected_payload = {
                "operation": operation,
                "memory": memory,
                "project_id": memory.get("project_id"),
            }
            if existing["operation"] != operation or existing_payload != expected_payload:
                raise ValueError("memory sync event identity conflict")
        return event_id

    @_synchronized
    def pending_memory_syncs(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        rows = self.conn.execute(
            """
            SELECT * FROM learning_sync_outbox
            WHERE status = 'pending'
            ORDER BY created_at ASC, event_id ASC
            """,
        ).fetchall()
        pending: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = self._decrypt_payload(
                    row["payload_nonce"],
                    row["payload_ciphertext"],
                    table="learning_sync_outbox",
                    pk_name="event_id",
                    pk_value=row["event_id"],
                    asset_type="memory",
                )
            except ValueError:
                continue
            pending.append(
                {
                    "event_id": row["event_id"],
                    "operation": row["operation"],
                    "memory_id": row["memory_id"],
                    "status": "paused" if row["retry_paused_at"] is not None else row["status"],
                    "attempts": int(row["attempts"] or 0),
                    "last_error": row["last_error"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "claim_token": row["claim_token"],
                    "claim_until": row["claim_until"],
                    "next_attempt_at": row["next_attempt_at"],
                    "retry_paused_at": row["retry_paused_at"],
                    "payload": payload,
                }
            )
            if len(pending) >= limit:
                break
        return pending

    @_synchronized
    def claim_pending_memory_syncs(
        self,
        owner_id: str,
        *,
        limit: int = 20,
        lease_seconds: float = 30.0,
        scope_project_id: str | None | object = _UNSET,
        include_global: bool = True,
    ) -> list[dict[str, Any]]:
        owner_id = str(owner_id or "").strip()
        if not owner_id:
            raise ValueError("owner_id is required for memory sync claim")
        limit = max(1, min(int(limit), 100))
        lease_seconds = max(1.0, min(float(lease_seconds), 300.0))
        scoped_claim = scope_project_id is not _UNSET
        normalized_scope_project_id = (
            str(scope_project_id or "").strip() or None
            if scoped_claim
            else None
        )
        now = _now()
        claimed: list[dict[str, Any]] = []
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            if not scoped_claim:
                self.conn.execute(
                    """
                    UPDATE learning_sync_outbox
                    SET retry_paused_at = ?, next_attempt_at = NULL,
                        claim_token = NULL, claim_until = NULL, updated_at = ?
                    WHERE status = 'pending' AND retry_paused_at IS NULL
                      AND attempts >= ?
                      AND (claim_until IS NULL OR claim_until <= ?)
                    """,
                    (now, now, MEMORY_SYNC_MAX_ATTEMPTS, now),
                )
                rows = self.conn.execute(
                    """
                    SELECT * FROM learning_sync_outbox
                    WHERE status = 'pending'
                      AND retry_paused_at IS NULL
                      AND attempts < ?
                      AND (claim_until IS NULL OR claim_until <= ?)
                      AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                    ORDER BY created_at ASC, event_id ASC
                    """,
                    (MEMORY_SYNC_MAX_ATTEMPTS, now, now),
                ).fetchall()
                ready_rows = []
                for row in rows:
                    try:
                        self._decrypt_payload(
                            row["payload_nonce"],
                            row["payload_ciphertext"],
                            table="learning_sync_outbox",
                            pk_name="event_id",
                            pk_value=row["event_id"],
                            asset_type="memory",
                        )
                    except ValueError:
                        continue
                    ready_rows.append(row)
                    if len(ready_rows) >= limit:
                        break
                rows = ready_rows
            else:
                # Payloads are encrypted, so the scope predicate cannot be
                # expressed in SQL. Scan ready rows while holding the same
                # write lock used by the unscoped claim path. An out-of-scope
                # event is never claimed or paused as a side effect of a
                # project-scoped query, and an unrelated backlog cannot starve
                # a matching event behind an arbitrary scan limit.
                candidates = self.conn.execute(
                    """
                    SELECT * FROM learning_sync_outbox
                    WHERE status = 'pending'
                      AND (claim_until IS NULL OR claim_until <= ?)
                    ORDER BY created_at ASC, event_id ASC
                    """,
                    (now,),
                ).fetchall()
                rows = []
                for row in candidates:
                    if row["retry_paused_at"] is not None:
                        continue
                    try:
                        payload = self._decrypt_payload(
                            row["payload_nonce"],
                            row["payload_ciphertext"],
                            table="learning_sync_outbox",
                            pk_name="event_id",
                            pk_value=row["event_id"],
                            asset_type="memory",
                        )
                    except ValueError:
                        continue
                    if not _memory_sync_scope_matches(
                        payload,
                        project_id=normalized_scope_project_id,
                        include_global=include_global,
                    ):
                        continue
                    if int(row["attempts"] or 0) >= MEMORY_SYNC_MAX_ATTEMPTS:
                        self.conn.execute(
                            """
                            UPDATE learning_sync_outbox
                            SET retry_paused_at = ?, next_attempt_at = NULL,
                                claim_token = NULL, claim_until = NULL, updated_at = ?
                            WHERE event_id = ? AND status = 'pending'
                            """,
                            (now, now, row["event_id"]),
                        )
                        continue
                    if row["next_attempt_at"] is not None and float(row["next_attempt_at"]) > now:
                        continue
                    rows.append(row)
                    if len(rows) >= limit:
                        break
            for row in rows:
                try:
                    payload = self._decrypt_payload(
                        row["payload_nonce"],
                        row["payload_ciphertext"],
                        table="learning_sync_outbox",
                        pk_name="event_id",
                        pk_value=row["event_id"],
                        asset_type="memory",
                    )
                except ValueError:
                    continue
                claim_token = f"{owner_id}:{os.urandom(8).hex()}"
                self.conn.execute(
                    """
                    UPDATE learning_sync_outbox
                    SET attempts = attempts + 1, updated_at = ?,
                        claim_token = ?, claim_until = ?
                    WHERE event_id = ? AND status = 'pending'
                    """,
                    (now, claim_token, now + lease_seconds, row["event_id"]),
                )
                claimed.append(
                    {
                        "event_id": row["event_id"],
                        "operation": row["operation"],
                        "memory_id": row["memory_id"],
                        "status": row["status"],
                        "attempts": int(row["attempts"] or 0) + 1,
                        "last_error": row["last_error"],
                        "created_at": row["created_at"],
                        "updated_at": now,
                        "claim_token": claim_token,
                        "claim_until": now + lease_seconds,
                        "next_attempt_at": row["next_attempt_at"],
                        "retry_paused_at": row["retry_paused_at"],
                        "payload": payload,
                    }
                )
        return claimed

    @_synchronized
    def complete_memory_sync(self, event_id: str, *, claim_token: str | None = None) -> bool:
        event_id = str(event_id or "").strip()
        if not event_id:
            return False
        claim_token = str(claim_token or "").strip() or None
        now = _now()
        claim_clause = " AND claim_token = ? AND claim_until > ?" if claim_token else " AND claim_token IS NULL"
        params: tuple[Any, ...] = (now, event_id, claim_token, now) if claim_token else (now, event_id)
        with self.conn:
            cursor = self.conn.execute(
                f"""
                UPDATE learning_sync_outbox
                SET status = 'completed', last_error = NULL, updated_at = ?,
                    claim_token = NULL, claim_until = NULL, next_attempt_at = NULL
                WHERE event_id = ? AND status = 'pending' AND retry_paused_at IS NULL{claim_clause}
                """,
                params,
            )
        return bool(cursor.rowcount)

    @_synchronized
    def fail_memory_sync(self, event_id: str, error: Any, *, claim_token: str | None = None) -> bool:
        event_id = str(event_id or "").strip()
        if not event_id:
            return False
        claim_token = str(claim_token or "").strip() or None
        claim_clause = " AND claim_token = ?" if claim_token else " AND claim_token IS NULL"
        now = _now()
        lease_clause = " AND claim_until > ?" if claim_token else ""
        with self.conn:
            lookup_params: tuple[Any, ...] = (event_id, claim_token, now) if claim_token else (event_id,)
            row = self.conn.execute(
                f"""
                SELECT attempts, retry_paused_at FROM learning_sync_outbox
                WHERE event_id = ? AND status = 'pending' AND retry_paused_at IS NULL{claim_clause}{lease_clause}
                """,
                lookup_params,
            ).fetchone()
            if row is None:
                return False
            attempts = int(row["attempts"] or 0)
            should_pause = attempts >= MEMORY_SYNC_MAX_ATTEMPTS
            next_attempt_at = None if should_pause else now + self._memory_sync_retry_delay(attempts)
            retry_paused_at = now if should_pause else None
            params: tuple[Any, ...] = (
                _serialize_memory_sync_error(error),
                now,
                next_attempt_at,
                retry_paused_at,
                event_id,
                claim_token,
                now,
            ) if claim_token else (
                _serialize_memory_sync_error(error),
                now,
                next_attempt_at,
                retry_paused_at,
                event_id,
            )
            cursor = self.conn.execute(
                f"""
                UPDATE learning_sync_outbox
                SET last_error = ?, updated_at = ?, next_attempt_at = ?, retry_paused_at = ?,
                    claim_token = NULL, claim_until = NULL
                WHERE event_id = ? AND status = 'pending' AND retry_paused_at IS NULL{claim_clause}{lease_clause}
                """,
                params,
            )
        return bool(cursor.rowcount)

    @_synchronized
    def resume_memory_sync(
        self,
        event_id: str,
        *,
        project_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> bool | dict[str, Any]:
        """Resume one paused derived-memory sync after an explicit local repair."""
        event_id = str(event_id or "").strip()
        if not event_id or len(event_id) > 256:
            return False
        normalized_project_id = str(project_id or "").strip() or None
        idempotency_key = self._normalise_memory_idempotency_key(idempotency_key)
        receipt_payload = {
            "event_id": event_id,
            "project_id": normalized_project_id,
        } if idempotency_key else None
        with self.conn:
            receipt = None
            if idempotency_key and receipt_payload is not None:
                self.conn.execute("BEGIN IMMEDIATE")
                receipt = self._begin_memory_mutation_locked(
                    idempotency_key=idempotency_key,
                    operation="memory_sync_resume",
                    memory_id=event_id,
                    payload=receipt_payload,
                )
                if receipt["status"] == "replayed":
                    stored = receipt.get("memory") or {}
                    return {
                        **stored,
                        "event_id": event_id,
                        "project_id": normalized_project_id,
                        "replayed": True,
                    }

            row = self.conn.execute(
                """
                SELECT * FROM learning_sync_outbox
                WHERE event_id = ? AND status = 'pending'
                """,
                (event_id,),
            ).fetchone()
            if row is None:
                return (
                    {"event_id": event_id, "project_id": normalized_project_id, "resumed": False, "replayed": False}
                    if idempotency_key
                    else False
                )
            payload = self._decrypt_payload(
                row["payload_nonce"],
                row["payload_ciphertext"],
                table="learning_sync_outbox",
                pk_name="event_id",
                pk_value=event_id,
                asset_type="memory",
            )
            event_memory = payload.get("memory") if isinstance(payload, dict) else None
            event_scope = event_memory if isinstance(event_memory, dict) else payload
            scope = str(event_scope.get("scope") or "global").strip().lower()
            stored_project_id = str(event_scope.get("project_id") or "").strip() or None
            if scope == "project":
                if normalized_project_id is None:
                    raise ValueError("project_id is required for project-scoped memory sync")
                if stored_project_id != normalized_project_id:
                    raise ValueError("memory sync belongs to another project")
            elif normalized_project_id is not None:
                raise ValueError("global memory sync does not accept project_id")
            if row["retry_paused_at"] is None:
                response = {
                    "event_id": event_id,
                    "project_id": normalized_project_id,
                    "resumed": False,
                    "replayed": False,
                }
                if idempotency_key and receipt is not None:
                    self._complete_memory_mutation_locked(
                        idempotency_key=idempotency_key,
                        operation="memory_sync_resume",
                        memory_id=event_id,
                        input_digest=receipt["input_digest"],
                        memory=response,
                    )
                    return response
                return False
            now = _now()
            cursor = self.conn.execute(
                """
                UPDATE learning_sync_outbox
                SET attempts = 0, last_error = NULL, updated_at = ?,
                    claim_token = NULL, claim_until = NULL, next_attempt_at = NULL,
                    retry_paused_at = NULL
                WHERE event_id = ? AND status = 'pending' AND retry_paused_at IS NOT NULL
                """,
                (now, event_id),
            )
            resumed = bool(cursor.rowcount)
            if resumed and idempotency_key and receipt is not None:
                response = {
                    "event_id": event_id,
                    "project_id": normalized_project_id,
                    "resumed": True,
                    "replayed": False,
                }
                self._complete_memory_mutation_locked(
                    idempotency_key=idempotency_key,
                    operation="memory_sync_resume",
                    memory_id=event_id,
                    input_digest=receipt["input_digest"],
                    memory=response,
                )
                return response
            return resumed

    @_synchronized
    def update_memory(
        self,
        memory_id: str,
        *,
        title: str,
        content: str,
        expected_updated_at: float | None = None,
        project_id: str | None = None,
        valid_from: float | None | object = _UNSET,
        invalid_at: float | None | object = _UNSET,
        idempotency_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Edit an active memory and its searchable encrypted chunk atomically."""
        memory_id = str(memory_id or "").strip()
        title = str(title or "").strip()
        content = str(content or "").strip()
        if not memory_id:
            return None
        if not title or not content:
            raise ValueError("memory title and content are required")
        if contains_sensitive_memory(f"{title}\n{content}") or contains_untrusted_memory_instruction(f"{title}\n{content}"):
            raise ValueError("memory content contains sensitive or privileged data")

        idempotency_key = self._normalise_memory_idempotency_key(idempotency_key)
        receipt_payload = None
        if idempotency_key:
            receipt_payload = {
                "title": title,
                "content": content,
                "expected_updated_at": expected_updated_at,
                "project_id": str(project_id or "").strip() or None,
                "valid_from_provided": valid_from is not _UNSET,
                "valid_from": None if valid_from is _UNSET else _normalise_optional_timestamp(valid_from, field="valid_from"),
                "invalid_at_provided": invalid_at is not _UNSET,
                "invalid_at": None if invalid_at is _UNSET else _normalise_optional_timestamp(invalid_at, field="invalid_at"),
            }

        now = _now()
        with self.conn:
            receipt = None
            if idempotency_key and receipt_payload is not None:
                self.conn.execute("BEGIN IMMEDIATE")
                receipt = self._begin_memory_mutation_locked(
                    idempotency_key=idempotency_key,
                    operation="update",
                    memory_id=memory_id,
                    payload=receipt_payload,
                )
                if receipt["status"] == "replayed":
                    return {"memory": receipt["memory"], "replayed": True}
            row = self.conn.execute(
                "SELECT * FROM memories WHERE memory_id = ? AND status = 'active'",
                (memory_id,),
            ).fetchone()
            if row is None:
                return None
            latest_updated_at = self.conn.execute(
                """
                SELECT MAX(updated_at) FROM (
                    SELECT updated_at
                    FROM memories
                    WHERE status = 'active'
                    UNION ALL
                    SELECT updated_at
                    FROM learning_chunks
                    WHERE status = 'active' AND asset_type = 'memory'
                )
                """
            ).fetchone()[0]
            if latest_updated_at is not None and now <= float(latest_updated_at):
                # Windows clocks can resolve immediate writes to the same
                # value. Keep the canonical memory and its derived search
                # chunk newer than every active memory timestamp.
                now = math.nextafter(float(latest_updated_at), math.inf)
            self._require_memory_project_scope(row, project_id)
            if expected_updated_at is not None and abs(float(row["updated_at"]) - float(expected_updated_at)) > 1e-6:
                raise ValueError("memory has changed; reload before editing")

            payload = self._decrypt_payload(
                row["payload_nonce"],
                row["payload_ciphertext"],
                table="memories",
                pk_name="memory_id",
                pk_value=memory_id,
                asset_type="memory",
            )
            old_title = str(payload.get("title") or "").strip()
            old_content = str(payload.get("content") or "").strip()
            content_changed = title != old_title or content != old_content

            next_valid_from = (
                now
                if content_changed and valid_from is _UNSET and invalid_at is _UNSET
                else row["valid_from"]
                if valid_from is _UNSET
                else _normalise_optional_timestamp(valid_from, field="valid_from")
            )
            next_invalid_at = (
                row["invalid_at"]
                if invalid_at is _UNSET
                else _normalise_optional_timestamp(invalid_at, field="invalid_at")
            )
            _validate_memory_validity(next_valid_from, next_invalid_at)

            duplicate_probe = {
                **payload,
                "subtype": row["subtype"],
                "scope": row["scope"] or "global",
                "project_id": row["project_id"],
                "title": title,
                "content": content,
                "valid_from": next_valid_from,
                "invalid_at": next_invalid_at,
            }
            duplicate_row = self._find_exact_active_memory_locked(duplicate_probe)
            if duplicate_row is not None and str(duplicate_row["memory_id"]) != memory_id:
                raise ValueError("memory content duplicates an active memory")

            validity_changed = (
                next_valid_from != row["valid_from"]
                or next_invalid_at != row["invalid_at"]
            )
            if content_changed or validity_changed:
                previous_record_version = int(payload.get("record_version") or 1)
                history_valid_from = row["valid_from"]
                if content_changed:
                    history_boundary = float(next_valid_from if next_valid_from is not None else now)
                    history_invalid_at: float | None = history_boundary
                    if row["invalid_at"] is not None:
                        history_invalid_at = min(float(row["invalid_at"]), history_boundary)
                else:
                    # A validity-only edit also changes the point-in-time
                    # projection. Preserve the old active interval before
                    # replacing the canonical bounds, otherwise a memory
                    # moved into the future disappears from earlier lookups.
                    boundaries = [
                        float(value)
                        for value in (next_valid_from, next_invalid_at)
                        if value is not None
                        and value != row["valid_from"]
                        and value != row["invalid_at"]
                    ]
                    history_invalid_at = min(boundaries) if boundaries else None
                    if history_valid_from is None:
                        history_valid_from = float(row["created_at"])
                    if history_invalid_at is not None and row["invalid_at"] is not None:
                        history_invalid_at = min(float(row["invalid_at"]), history_invalid_at)
                    if (
                        history_invalid_at is not None
                        and history_valid_from is not None
                        and history_invalid_at <= float(history_valid_from)
                    ):
                        history_invalid_at = None

                if history_invalid_at is not None:
                    _validate_memory_validity(history_valid_from, history_invalid_at)
                    memory_version_id = f"{memory_id}:v{previous_record_version}"
                    history_nonce, history_ciphertext = self._encrypt_payload(
                        payload,
                        table="memory_versions",
                        pk_name="memory_version_id",
                        pk_value=memory_version_id,
                        asset_type="memory",
                    )
                    self.conn.execute(
                        """
                        INSERT OR IGNORE INTO memory_versions (
                            memory_version_id, memory_id, record_version, subtype, status,
                            scope, project_id, source_candidate_id, created_at, updated_at,
                            last_used_at, valid_from, invalid_at, payload_nonce, payload_ciphertext
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            memory_version_id,
                            memory_id,
                            previous_record_version,
                            row["subtype"],
                            row["status"],
                            row["scope"] or "global",
                            row["project_id"],
                            row["source_candidate_id"],
                            row["created_at"],
                            row["updated_at"],
                            row["last_used_at"],
                            history_valid_from,
                            history_invalid_at,
                            history_nonce,
                            history_ciphertext,
                        ),
                    )
            payload["title"] = title
            payload["content"] = content
            payload["text"] = ""
            payload["text"] = _chunk_search_text(payload)
            payload["record_version"] = int(payload.get("record_version") or 1) + 1
            payload["edited_at"] = now
            nonce, ciphertext = self._encrypt_payload(
                payload,
                table="memories",
                pk_name="memory_id",
                pk_value=memory_id,
                asset_type="memory",
            )

            chunk = self.conn.execute(
                "SELECT * FROM learning_chunks "
                "WHERE asset_type = 'memory' AND asset_id = ? AND status = 'active' "
                "ORDER BY chunk_id ASC LIMIT 1",
                (memory_id,),
            ).fetchone()
            if chunk is None:
                raise ValueError("memory learning chunk is missing")
            chunk_payload = {
                **payload,
                "asset_id": memory_id,
                "asset_type": "memory",
                "text": _chunk_search_text(payload),
            }
            chunk_nonce, chunk_ciphertext = self._encrypt_payload(
                chunk_payload,
                table="learning_chunks",
                pk_name="chunk_id",
                pk_value=chunk["chunk_id"],
                asset_type="memory",
            )
            self.conn.execute(
                "UPDATE memories SET updated_at = ?, valid_from = ?, invalid_at = ?, "
                "payload_nonce = ?, payload_ciphertext = ? "
                "WHERE memory_id = ?",
                (now, next_valid_from, next_invalid_at, nonce, ciphertext, memory_id),
            )
            self.conn.execute(
                "UPDATE learning_chunks SET text_digest = ?, updated_at = ?, "
                "valid_from = ?, invalid_at = ?, payload_nonce = ?, payload_ciphertext = ? "
                "WHERE chunk_id = ?",
                (
                    _text_digest(chunk_payload["text"]),
                    now,
                    next_valid_from,
                    next_invalid_at,
                    chunk_nonce,
                    chunk_ciphertext,
                    chunk["chunk_id"],
                ),
            )
            updated = self.conn.execute(
                "SELECT * FROM memories WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            updated_memory = self._memory_from_row(updated, include_metadata=True) if updated is not None else None
            if updated_memory is not None:
                self._queue_memory_sync_locked("memory_update", updated_memory)
            if idempotency_key and receipt is not None and updated_memory is not None:
                self._complete_memory_mutation_locked(
                    idempotency_key=idempotency_key,
                    operation="update",
                    memory_id=memory_id,
                    input_digest=receipt["input_digest"],
                    memory=updated_memory,
                )
        if idempotency_key:
            return {"memory": updated_memory, "replayed": False} if updated_memory is not None else None
        return updated_memory

    @_synchronized
    def delete_memory(
        self,
        memory_id: str,
        *,
        project_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Remove an active memory from the local index while returning an audit record."""
        memory_id = str(memory_id or "").strip()
        if not memory_id:
            return None
        idempotency_key = self._normalise_memory_idempotency_key(idempotency_key)
        receipt_payload = {
            "project_id": str(project_id or "").strip() or None,
        } if idempotency_key else None
        deleted_at = _now()
        with self.conn:
            receipt = None
            if idempotency_key and receipt_payload is not None:
                self.conn.execute("BEGIN IMMEDIATE")
                receipt = self._begin_memory_mutation_locked(
                    idempotency_key=idempotency_key,
                    operation="delete",
                    memory_id=memory_id,
                    payload=receipt_payload,
                )
                if receipt["status"] == "replayed":
                    return {"memory": receipt["memory"], "replayed": True}
            row = self.conn.execute(
                "SELECT * FROM memories WHERE memory_id = ? AND status = 'active'",
                (memory_id,),
            ).fetchone()
            if row is None:
                return None
            self._require_memory_project_scope(row, project_id)
            deleted = self._memory_from_row(row, include_metadata=True)
            self.conn.execute(
                "DELETE FROM learning_chunks WHERE asset_type = 'memory' AND asset_id = ?",
                (memory_id,),
            )
            self.conn.execute(
                "UPDATE memory_versions SET status = 'archived' WHERE memory_id = ?",
                (memory_id,),
            )
            self.conn.execute("DELETE FROM memories WHERE memory_id = ?", (memory_id,))
            deleted["status"] = "deleted"
            deleted["deleted_at"] = deleted_at
            self._queue_memory_sync_locked("memory_delete", deleted)
            if idempotency_key and receipt is not None:
                self._complete_memory_mutation_locked(
                    idempotency_key=idempotency_key,
                    operation="delete",
                    memory_id=memory_id,
                    input_digest=receipt["input_digest"],
                    memory=deleted,
                )
        if idempotency_key:
            return {"memory": deleted, "replayed": False}
        return deleted

    @_synchronized
    def archive_memory(
        self,
        memory_id: str,
        *,
        project_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Soft-revoke one memory while keeping its encrypted source record."""
        memory_id = str(memory_id or "").strip()
        if not memory_id:
            return None
        idempotency_key = self._normalise_memory_idempotency_key(idempotency_key)
        receipt_payload = {
            "project_id": str(project_id or "").strip() or None,
        } if idempotency_key else None
        now = _now()
        with self.conn:
            receipt = None
            if idempotency_key and receipt_payload is not None:
                self.conn.execute("BEGIN IMMEDIATE")
                receipt = self._begin_memory_mutation_locked(
                    idempotency_key=idempotency_key,
                    operation="revoke",
                    memory_id=memory_id,
                    payload=receipt_payload,
                )
                if receipt["status"] == "replayed":
                    return {"memory": receipt["memory"], "replayed": True}
            row = self.conn.execute(
                "SELECT * FROM memories WHERE memory_id = ? AND status = 'active'",
                (memory_id,),
            ).fetchone()
            if row is None:
                return None
            self._require_memory_project_scope(row, project_id)
            current_payload = self._decrypt_payload(
                row["payload_nonce"],
                row["payload_ciphertext"],
                table="memories",
                pk_name="memory_id",
                pk_value=memory_id,
                asset_type="memory",
            )
            current_record_version = int(current_payload.get("record_version") or 1)
            history_valid_from = (
                float(row["valid_from"])
                if row["valid_from"] is not None
                else float(row["created_at"])
            )
            history_invalid_at = (
                min(float(row["invalid_at"]), now)
                if row["invalid_at"] is not None
                else now
            )
            # Revoke is a temporal invalidation, not a history purge. Preserve
            # the last active version so an as_of query can still answer what
            # was true before the revoke. A scheduled-but-never-valid memory
            # has no historical interval to preserve.
            if history_valid_from < history_invalid_at:
                memory_version_id = f"{memory_id}:v{current_record_version}"
                history_payload = dict(current_payload)
                history_payload.pop("valid_from", None)
                history_payload.pop("invalid_at", None)
                history_nonce, history_ciphertext = self._encrypt_payload(
                    history_payload,
                    table="memory_versions",
                    pk_name="memory_version_id",
                    pk_value=memory_version_id,
                    asset_type="memory",
                )
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO memory_versions (
                        memory_version_id, memory_id, record_version, subtype, status,
                        scope, project_id, source_candidate_id, created_at, updated_at,
                        last_used_at, valid_from, invalid_at, payload_nonce, payload_ciphertext
                    )
                    VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_version_id,
                        memory_id,
                        current_record_version,
                        row["subtype"],
                        row["scope"] or "global",
                        row["project_id"],
                        row["source_candidate_id"],
                        row["created_at"],
                        row["updated_at"],
                        row["last_used_at"],
                        history_valid_from,
                        history_invalid_at,
                        history_nonce,
                        history_ciphertext,
                    ),
                )
            self.conn.execute(
                "UPDATE learning_chunks SET status = 'archived', updated_at = ? "
                "WHERE asset_type = 'memory' AND asset_id = ? AND status = 'active'",
                (now, memory_id),
            )
            self.conn.execute(
                "UPDATE memories SET status = 'archived', updated_at = ? WHERE memory_id = ?",
                (now, memory_id),
            )
            archived = self.conn.execute(
                "SELECT * FROM memories WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            archived_memory = self._memory_from_row(archived, include_metadata=True) if archived is not None else None
            if archived_memory is not None:
                self._queue_memory_sync_locked("memory_revoke", archived_memory)
            if idempotency_key and receipt is not None and archived_memory is not None:
                self._complete_memory_mutation_locked(
                    idempotency_key=idempotency_key,
                    operation="revoke",
                    memory_id=memory_id,
                    input_digest=receipt["input_digest"],
                    memory=archived_memory,
                )
        if idempotency_key:
            return {"memory": archived_memory, "replayed": False} if archived_memory is not None else None
        return archived_memory

    @_synchronized
    def list_skills(
        self,
        *,
        project_id: str | None = None,
        include_global: bool = True,
    ) -> list[dict[str, Any]]:
        scope_sql, scope_params = _skill_scope_sql(project_id, include_global=include_global)
        rows = self.conn.execute(
            "SELECT * FROM skills WHERE status = 'active' AND " + scope_sql +
            " ORDER BY updated_at DESC, skill_id ASC",
            tuple(scope_params),
        ).fetchall()
        return [self._skill_from_row(row) for row in rows]

    @_synchronized
    def get_skill(
        self,
        skill_id: str,
        *,
        project_id: str | None = None,
        include_global: bool = True,
    ) -> dict[str, Any] | None:
        """Load one active Skill without decrypting unrelated legacy records."""
        scope_sql, scope_params = _skill_scope_sql(project_id, include_global=include_global)
        row = self.conn.execute(
            "SELECT * FROM skills WHERE skill_id = ? AND status = 'active' AND " + scope_sql,
            (skill_id, *scope_params),
        ).fetchone()
        return self._skill_from_row(row) if row is not None else None

    def _skill_deletion_record(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        status = str(row["status"] or "")
        response = None
        if status == "completed" and row["response_nonce"] and row["response_ciphertext"]:
            payload = self._decrypt_payload(
                row["response_nonce"],
                row["response_ciphertext"],
                table="skill_deletion_receipts",
                pk_name="idempotency_key",
                pk_value=row["idempotency_key"],
                asset_type="skill",
            )
            if not isinstance(payload, dict):
                raise ValueError("invalid Skill deletion receipt envelope")
            schema_version = payload.get("schema_version")
            if schema_version not in (None, SKILL_DELETION_RECEIPT_SCHEMA_VERSION):
                raise ValueError("unsupported Skill deletion receipt schema")
            response = payload.get("response")
            if not isinstance(response, dict):
                raise ValueError("invalid Skill deletion receipt response")
        raw_error = str(row["error"] or "")
        return {
            "idempotency_key": row["idempotency_key"],
            "requested_identifier": row["requested_identifier"],
            "skill_id": row["skill_id"],
            "project_id": row["project_id"],
            # NULL denotes an old receipt whose request scope was not recorded;
            # an empty string denotes a known unscoped request.
            "requested_project_id": row["requested_project_id"],
            "source_path": row["source_path"],
            "source_digest": row["source_digest"],
            "status": status,
            "owner_id": row["owner_id"],
            "lease_expires_at": (
                float(row["lease_expires_at"])
                if row["lease_expires_at"] is not None
                else None
            ),
            "response": response,
            "error_observation": memory_sync_error_observation(raw_error) if raw_error else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @_synchronized
    def skill_deletion_receipt(self, idempotency_key: str) -> dict[str, Any] | None:
        key = self._normalise_skill_idempotency_key(idempotency_key)
        if not key:
            return None
        row = self.conn.execute(
            "SELECT * FROM skill_deletion_receipts WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        return self._skill_deletion_record(row)

    @_synchronized
    def claim_skill_deletion(
        self,
        requested_identifier: str,
        skill_id: str,
        source_path: str,
        idempotency_key: str,
        *,
        project_id: str | None = None,
        requested_project_id: str | None = None,
        source_digest: str | None = None,
        owner_id: str,
        lease_seconds: float = 300.0,
    ) -> dict[str, Any]:
        key = self._normalise_skill_idempotency_key(idempotency_key)
        if not key:
            raise ValueError("idempotency_key is required")
        requested = str(requested_identifier or "").strip()
        normalized_skill_id = str(skill_id or "").strip()
        normalized_project_id = str(project_id or "").strip() or None
        normalized_requested_project_id = str(requested_project_id or "").strip() or None
        normalized_source_path = str(source_path or "").replace("\\", "/").strip()
        normalized_source_digest = str(source_digest or "").strip()
        owner = str(owner_id or "").strip()
        if not requested or not normalized_skill_id or not normalized_source_path or not owner:
            raise ValueError("skill deletion identity is incomplete")
        inputs = {
            "requested_identifier": requested,
            "skill_id": normalized_skill_id,
            "project_id": normalized_project_id,
            "source_path": normalized_source_path,
            "source_digest": normalized_source_digest,
        }
        canonical = json.dumps(
            inputs,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        legacy_input_digest = hmac.new(self.keys.metadata_key, canonical, hashlib.sha256).hexdigest()
        inputs["requested_project_id"] = normalized_requested_project_id
        canonical = json.dumps(
            inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        input_digest = hmac.new(self.keys.metadata_key, canonical, hashlib.sha256).hexdigest()
        now = _now()
        lease_until = time.time() + max(30.0, min(float(lease_seconds), 3600.0))
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                "SELECT * FROM skill_deletion_receipts WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            existed = row is not None
            if row is None:
                self.conn.execute(
                    """
                    INSERT INTO skill_deletion_receipts(
                        idempotency_key, requested_identifier, skill_id, project_id, requested_project_id,
                        source_path, source_digest, input_digest, status, owner_id,
                        lease_expires_at, error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'in_progress', ?, ?, '', ?, ?)
                    """,
                    (
                        key,
                        requested,
                        normalized_skill_id,
                        normalized_project_id,
                        normalized_requested_project_id or "",
                        normalized_source_path,
                        normalized_source_digest,
                        input_digest,
                        owner,
                        lease_until,
                        now,
                        now,
                    ),
                )
                row = self.conn.execute(
                    "SELECT * FROM skill_deletion_receipts WHERE idempotency_key = ?",
                    (key,),
                ).fetchone()
            else:
                expected_digest = input_digest
                if row["requested_project_id"] is None:
                    # A project Skill could only be resolved from its own project.
                    # An old global Skill receipt cannot establish the caller scope.
                    if not normalized_project_id or normalized_requested_project_id != normalized_project_id:
                        raise ValueError("legacy Skill deletion receipt has unknown request scope")
                    expected_digest = legacy_input_digest
                if not hmac.compare_digest(str(row["input_digest"] or ""), expected_digest):
                    raise ValueError("idempotency_key is already bound to different Skill deletion input")
                status = str(row["status"] or "")
                if status == "completed":
                    return self._skill_deletion_record(row) | {
                        "claimed": False,
                        "replayed": True,
                    }
                lease_active = float(row["lease_expires_at"] or 0) > time.time()
                # Once the source has been removed, replaying the same key is
                # safe: the remaining phase is the database deletion.  Do not
                # make a client wait for the old worker lease in that phase.
                # An in-progress source deletion still keeps its lease fence,
                # because its filesystem side effect may not be knowable yet.
                if status == "in_progress" and lease_active and str(row["owner_id"] or "") != owner:
                    return self._skill_deletion_record(row) | {
                        "claimed": False,
                        "in_flight": True,
                        "replayed": True,
                    }
                next_status = "source_removed" if status == "source_removed" else "in_progress"
                self.conn.execute(
                    """
                    UPDATE skill_deletion_receipts
                    SET status = ?, owner_id = ?, lease_expires_at = ?,
                        requested_project_id = ?, input_digest = ?,
                        error = '', updated_at = ?
                    WHERE idempotency_key = ?
                    """,
                    (next_status, owner, lease_until, normalized_requested_project_id or "", input_digest, now, key),
                )
                row = self.conn.execute(
                    "SELECT * FROM skill_deletion_receipts WHERE idempotency_key = ?",
                    (key,),
                ).fetchone()
        return self._skill_deletion_record(row) | {
            "claimed": True,
            "replayed": bool(existed),
        }

    @_synchronized
    def mark_skill_deletion_source_removed(
        self,
        idempotency_key: str,
        owner_id: str,
    ) -> None:
        key = self._normalise_skill_idempotency_key(idempotency_key)
        owner = str(owner_id or "").strip()
        if not key or not owner:
            raise ValueError("skill deletion receipt identity is incomplete")
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE skill_deletion_receipts
                SET status = 'source_removed', updated_at = ?
                WHERE idempotency_key = ? AND owner_id = ? AND status = 'in_progress'
                """,
                (_now(), key, owner),
            )
        if cursor.rowcount:
            return
        receipt = self.skill_deletion_receipt(key)
        if receipt and receipt.get("status") == "source_removed":
            return
        if receipt and receipt.get("status") == "completed":
            return
        raise ValueError("skill deletion receipt lease lost")

    @_synchronized
    def mark_skill_deletion_failed(
        self,
        idempotency_key: str,
        owner_id: str,
        error: Any,
    ) -> None:
        key = self._normalise_skill_idempotency_key(idempotency_key)
        owner = str(owner_id or "").strip()
        if not key or not owner:
            return
        with self.conn:
            self.conn.execute(
                """
                UPDATE skill_deletion_receipts
                SET status = 'failed', owner_id = NULL, lease_expires_at = NULL,
                    error = ?, updated_at = ?
                WHERE idempotency_key = ? AND owner_id = ? AND status = 'in_progress'
                """,
                (_serialize_memory_sync_error(error) or '', _now(), key, owner),
            )

    @_synchronized
    def complete_skill_deletion(
        self,
        idempotency_key: str,
        owner_id: str,
        response: dict[str, Any],
    ) -> None:
        key = self._normalise_skill_idempotency_key(idempotency_key)
        owner = str(owner_id or "").strip()
        if not key or not owner:
            raise ValueError("skill deletion receipt identity is incomplete")
        if not isinstance(response, dict):
            raise ValueError("invalid Skill deletion receipt response")
        nonce, ciphertext = self._encrypt_payload(
            {
                "schema_version": SKILL_DELETION_RECEIPT_SCHEMA_VERSION,
                "response": response,
            },
            table="skill_deletion_receipts",
            pk_name="idempotency_key",
            pk_value=key,
            asset_type="skill",
        )
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE skill_deletion_receipts
                SET status = 'completed', owner_id = NULL, lease_expires_at = NULL,
                    error = '', updated_at = ?, response_nonce = ?, response_ciphertext = ?
                WHERE idempotency_key = ? AND owner_id = ?
                  AND status IN ('in_progress', 'source_removed')
                """,
                (_now(), nonce, ciphertext, key, owner),
            )
        if cursor.rowcount:
            return
        receipt = self.skill_deletion_receipt(key)
        if receipt and receipt.get("status") == "completed":
            return
        raise ValueError("skill deletion receipt lease lost")

    @_synchronized
    def delete_skill(
        self,
        skill_id: str,
        *,
        project_id: str | None = None,
        include_global: bool = True,
    ) -> bool:
        scope_sql, scope_params = _skill_scope_sql(project_id, include_global=include_global)
        row = self.conn.execute(
            "SELECT skill_id FROM skills WHERE skill_id = ? AND status = 'active' AND " + scope_sql,
            (skill_id, *scope_params),
        ).fetchone()
        if row is None:
            return False
        with self.conn:
            # skill_runs keeps a foreign-key reference to the active Skill.
            # Remove its local execution history in the same transaction so a
            # previously used Skill can be deleted just like a never-run one.
            self.conn.execute("DELETE FROM skill_runs WHERE skill_id = ?", (skill_id,))
            self.conn.execute(
                "DELETE FROM learning_chunks WHERE asset_type = 'skill' AND asset_id = ?",
                (skill_id,),
            )
            self.conn.execute("DELETE FROM skills WHERE skill_id = ?", (skill_id,))
        return True

    @_synchronized
    def archive_skill(
        self,
        skill_id: str,
        *,
        project_id: str | None = None,
        include_global: bool = True,
    ) -> bool:
        """Hide a superseded local Skill while preserving its execution history."""
        scope_sql, scope_params = _skill_scope_sql(project_id, include_global=include_global)
        row = self.conn.execute(
            "SELECT skill_id FROM skills WHERE skill_id = ? AND status = 'active' AND " + scope_sql,
            (skill_id, *scope_params),
        ).fetchone()
        if row is None:
            return False
        now = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE learning_chunks SET status = 'archived', updated_at = ? "
                "WHERE asset_type = 'skill' AND asset_id = ? AND status = 'active'",
                (now, skill_id),
            )
            self.conn.execute(
                "UPDATE skills SET status = 'archived', updated_at = ? WHERE skill_id = ?",
                (now, skill_id),
            )
        return True

    @_synchronized
    def list_skill_runs(
        self,
        limit: int = 50,
        *,
        project_id: str | None = None,
        include_global: bool = True,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        scope_sql, scope_params = _skill_scope_sql(project_id, include_global=include_global)
        rows = self.conn.execute(
            """
            SELECT r.*, s.scope AS skill_scope, s.project_id AS skill_project_id
            FROM skill_runs AS r
            JOIN skills AS s ON s.skill_id = r.skill_id
            WHERE s.status = 'active' AND """ + scope_sql + """
            ORDER BY created_at DESC, run_id ASC
            LIMIT ?
            """,
            (*scope_params, safe_limit),
        ).fetchall()
        return [self._skill_run_from_row(row) for row in rows]

    @_synchronized
    def sync_project_skill(self, candidate: dict[str, Any]) -> str:
        source_path = str(candidate.get("source_path") or "").strip()
        if not source_path:
            raise ValueError("project skill source_path is required")
        project_id = str(candidate.get("project_id") or "").strip() or None
        payload = normalize_skill_payload({**candidate, "subtype": "workflow"})
        if payload.get("workflow_scope_invalid"):
            raise ValueError("skill has unsupported workflow scope")
        payload.update({
            "source_path": source_path,
            "source_digest": str(candidate.get("source_digest") or ""),
            "scope": "project" if project_id else "global",
            "project_id": project_id,
        })
        for existing in self.list_skills(project_id=project_id, include_global=False):
            if existing.get("source_path") != source_path:
                continue
            if existing.get("source_digest") == payload["source_digest"]:
                return "unchanged"
            skill_id = existing["skill_id"]
            payload["skill_id"] = skill_id
            now = _now()
            nonce, ciphertext = self._encrypt_payload(payload, table="skills", pk_name="skill_id", pk_value=skill_id, asset_type="skill")
            chunk_payload = {**payload, "asset_id": skill_id, "asset_type": "skill", "text": _chunk_search_text(payload)}
            chunk_nonce, chunk_ciphertext = self._encrypt_payload(chunk_payload, table="learning_chunks", pk_name="chunk_id", pk_value=f"chunk_{skill_id}", asset_type="skill")
            with self.conn:
                self.conn.execute(
                    "UPDATE skills SET version = version + 1, scope = ?, project_id = ?, "
                    "updated_at = ?, payload_nonce = ?, payload_ciphertext = ? WHERE skill_id = ?",
                    (payload["scope"], project_id, now, nonce, ciphertext, skill_id),
                )
                self.conn.execute(
                    "UPDATE learning_chunks SET subtype = ?, scope = ?, project_id = ?, "
                    "text_digest = ?, updated_at = ?, payload_nonce = ?, payload_ciphertext = ? "
                    "WHERE chunk_id = ?",
                    (
                        payload["subtype"],
                        payload["scope"],
                        project_id,
                        _text_digest(chunk_payload["text"]),
                        now,
                        chunk_nonce,
                        chunk_ciphertext,
                        f"chunk_{skill_id}",
                    ),
                )
            return "updated"
        review = self.create_review("manual", [{
            **payload, "asset_type": "skill", "subtype": "workflow", "evidence": [source_path],
            "rationale": "User-provided project Skill file.", "confidence": 1.0,
            "selected_by_default": True, "source_message_indexes": [],
        }], project_id=project_id)
        self.decide_review(
            review["review_id"],
            [{"candidate_id": review["candidates"][0]["candidate_id"], "action": "accept"}],
            project_id=project_id,
        )
        return "imported"

    @_synchronized
    def create_review(
        self,
        trigger: str,
        candidates: list[dict[str, Any]],
        *,
        project_id: str | None = None,
        idempotency_key: str | None = None,
        idempotency_digest: str | None = None,
    ) -> dict[str, Any]:
        if trigger not in {"manual", "close", "runtime"}:
            raise ValueError("unsupported reflection trigger")
        normalized_project_id = str(project_id or "").strip() or None
        idempotency_key = self._normalise_memory_idempotency_key(idempotency_key)
        idempotency_digest = str(idempotency_digest or "").strip() or None
        if idempotency_key and not idempotency_digest:
            raise ValueError("idempotency_digest is required with idempotency_key")
        now = _now()
        review_id = f"rev_{os.urandom(8).hex()}"
        stored_candidates: list[dict[str, Any]] = []
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                existing = self.conn.execute(
                    "SELECT review_id, idempotency_digest FROM reflection_reviews "
                    "WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    if not hmac.compare_digest(
                        str(existing["idempotency_digest"] or ""),
                        idempotency_digest or "",
                    ):
                        raise ValueError("idempotency_key is already bound to a different reflection draft")
                    replayed = self._review_with_candidates(existing["review_id"])
                    if replayed is None:
                        raise ValueError("reflection draft receipt points to a missing review")
                    replayed["idempotency_key"] = idempotency_key
                    replayed["replayed"] = True
                    return replayed
            self.conn.execute(
                """
                INSERT INTO reflection_reviews (
                    review_id, trigger, project_id, status, created_at, updated_at,
                    candidate_count, accepted_count, rejected_count,
                    idempotency_key, idempotency_digest
                )
                VALUES (?, ?, ?, 'pending', ?, ?, 0, 0, 0, ?, ?)
                """,
                (
                    review_id,
                    trigger,
                    normalized_project_id,
                    now,
                    now,
                    idempotency_key,
                    idempotency_digest,
                ),
            )
            for candidate in candidates:
                stored_candidates.append(self._store_candidate(review_id, candidate, now))
        review = self._review_record(review_id)
        review["candidates"] = stored_candidates
        if idempotency_key:
            review["idempotency_key"] = idempotency_key
            review["replayed"] = False
        return review

    @_synchronized
    def replay_reflection_draft(
        self,
        idempotency_key: str,
        idempotency_digest: str,
    ) -> dict[str, Any] | None:
        """Return a durable reflection draft when its key matches the input."""
        normalized_key = self._normalise_memory_idempotency_key(idempotency_key)
        normalized_digest = str(idempotency_digest or "").strip()
        if not normalized_key or not normalized_digest:
            return None
        row = self.conn.execute(
            "SELECT review_id, idempotency_digest FROM reflection_reviews "
            "WHERE idempotency_key = ?",
            (normalized_key,),
        ).fetchone()
        if row is None:
            return None
        if not hmac.compare_digest(str(row["idempotency_digest"] or ""), normalized_digest):
            raise ValueError("idempotency_key is already bound to a different reflection draft")
        review = self._review_with_candidates(row["review_id"])
        if review is None:
            raise ValueError("reflection draft receipt points to a missing review")
        review["idempotency_key"] = normalized_key
        review["replayed"] = True
        return review

    @_synchronized
    def decide_review(
        self,
        review_id: str,
        decisions: list[dict[str, Any]],
        *,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        review = self._review_record(review_id)
        if review is None:
            raise KeyError(f"unknown review: {review_id}")

        normalized_project_id = str(project_id or "").strip() or None
        bound_project_id = str(review.get("project_id") or "").strip() or None
        if bound_project_id != normalized_project_id:
            raise ValueError(
                f"review is bound to project scope {bound_project_id or 'global'}; decision project must match"
            )
        decision_digest = self._decision_digest(decisions, normalized_project_id)

        seen_candidate_ids: set[str] = set()
        for decision in decisions:
            candidate_id = str(decision.get("candidate_id") or "").strip()
            if candidate_id in seen_candidate_ids:
                raise ValueError("candidate_id must appear only once per review decision")
            seen_candidate_ids.add(candidate_id)

        accepted = 0
        rejected = 0
        accepted_assets: list[dict[str, Any]] = []
        with self.conn:
            # Serialize the pending -> finalized transition.  Without an
            # immediate write lock, two UI windows can both observe pending
            # and create the same accepted memory before either decision is
            # recorded.
            self.conn.execute("BEGIN IMMEDIATE")
            review = self._review_record(review_id)
            if review is None:
                raise KeyError(f"unknown review: {review_id}")
            bound_project_id = str(review.get("project_id") or "").strip() or None
            if bound_project_id != normalized_project_id:
                raise ValueError(
                    f"review is bound to project scope {bound_project_id or 'global'}; decision project must match"
                )
            if review["status"] != "pending":
                if (
                    review.get("decision_digest") != decision_digest
                    or review.get("decision_project_id") != normalized_project_id
                ):
                    raise ValueError("review has already been finalized; retry must reuse the same decisions")
                return self._replay_review_decision(review, decisions, decision_digest)
            for decision in decisions:
                candidate_id = str(decision.get("candidate_id") or "").strip()
                action = str(decision.get("action") or "").strip()
                if not candidate_id:
                    raise ValueError("candidate_id is required")
                candidate = self._candidate_record(candidate_id, review_id)
                if candidate is None:
                    raise KeyError(f"unknown candidate: {candidate_id}")

                if action == "accept":
                    if (
                        candidate["asset_type"] == "memory"
                        and str(candidate.get("subtype") or "") == "project_constraint"
                        and normalized_project_id is None
                    ):
                        raise ValueError("project_constraint requires project scope")
                    self._set_candidate_status(candidate_id, "accepted")
                    final_payload = self._candidate_final_payload(candidate, decision)
                    if candidate["asset_type"] == "memory":
                        subtype = str(candidate.get("subtype") or final_payload.get("subtype") or "")
                        final_payload["scope"] = "global" if subtype == "user_preference" or not normalized_project_id else "project"
                        final_payload["project_id"] = (
                            None
                            if final_payload["scope"] == "global"
                            else normalized_project_id
                        )
                        final_payload["record_version"] = 1
                        if review["trigger"] in {"manual", "close"}:
                            final_payload["confirmation_status"] = "human_reviewed"
                            final_payload["confirmed_at"] = _now()
                        else:
                            final_payload["confirmation_status"] = "explicit_signal"
                            final_payload["confirmed_at"] = None
                        superseded_at = _now()
                        final_payload["superseded_memory_ids"] = self._supersede_conflicting_memories_locked(
                            candidate,
                            final_payload,
                            superseded_at=superseded_at,
                            validity_boundary=final_payload.get("valid_from"),
                        )
                        if (
                            final_payload["superseded_memory_ids"]
                            and final_payload.get("valid_from") is None
                        ):
                            # A replacement fact becomes true at the same
                            # boundary at which the previous fact stops being
                            # current, so point-in-time reads do not overlap.
                            final_payload["valid_from"] = superseded_at
                        existing_memory = self._find_exact_active_memory_locked(final_payload)
                        if existing_memory is not None:
                            existing_memory_id = str(existing_memory["memory_id"])
                            self._record_candidate_dedup_locked(candidate, existing_memory_id)
                            existing_asset = self._memory_from_row(existing_memory, include_metadata=True)
                            existing_asset.update(
                                {
                                    "asset_type": "memory",
                                    "deduplicated": True,
                                    "deduplicated_to": existing_memory_id,
                                }
                            )
                            accepted_assets.append(existing_asset)
                            accepted += 1
                            continue
                        asset_id = self._insert_memory_asset(candidate, final_payload)
                    elif candidate["asset_type"] == "skill":
                        final_payload["scope"] = "project" if normalized_project_id else "global"
                        final_payload["project_id"] = normalized_project_id
                        asset_id = self._insert_skill_asset(candidate, final_payload)
                    else:
                        raise ValueError("unsupported candidate asset type")
                    self._insert_learning_chunk_asset(asset_id, candidate["asset_type"], final_payload)
                    accepted_assets.append(
                        {
                            **final_payload,
                            "memory_id": asset_id if candidate["asset_type"] == "memory" else None,
                            "skill_id": asset_id if candidate["asset_type"] == "skill" else None,
                            "asset_type": candidate["asset_type"],
                            "memory_family": classify_memory_family(
                                candidate["asset_type"],
                                final_payload.get("subtype"),
                            ),
                            "status": "active",
                            "source_candidate_id": candidate_id,
                            "created_at": _now(),
                            "updated_at": _now(),
                        }
                    )
                    if candidate["asset_type"] == "memory":
                        self._queue_memory_sync_locked("memory_update", accepted_assets[-1])
                    accepted += 1
                elif action == "reject":
                    self._set_candidate_status(candidate_id, "rejected")
                    rejected += 1
                else:
                    raise ValueError("unsupported review decision")

            review_status = "completed" if decisions else "discarded"
            self.conn.execute(
                """
                UPDATE reflection_reviews
                SET status = ?, updated_at = ?, decision_digest = ?, decision_project_id = ?
                WHERE review_id = ?
                """,
                (review_status, _now(), decision_digest, normalized_project_id, review_id),
            )

        result = self._review_record(review_id)
        if result is None:
            raise KeyError(f"unknown review: {review_id}")
        result["accepted_count"] = accepted if decisions else result["accepted_count"]
        result["rejected_count"] = rejected if decisions else result["rejected_count"]
        result["accepted_assets"] = accepted_assets
        result["idempotency_key"] = f"{review_id}:{decision_digest}"
        result["replayed"] = False
        return result

    def _decision_digest(self, decisions: list[dict[str, Any]], project_id: str | None) -> str:
        canonical: list[dict[str, Any]] = []
        for decision in decisions or []:
            canonical.append(
                {
                    "candidate_id": str(decision.get("candidate_id") or "").strip(),
                    "action": str(decision.get("action") or "").strip(),
                    "edited_title": decision.get("edited_title"),
                    "edited_content": decision.get("edited_content"),
                    "valid_from": decision.get("valid_from"),
                    "invalid_at": decision.get("invalid_at"),
                }
            )
        canonical.sort(key=lambda item: item["candidate_id"])
        payload = json.dumps(
            {"project_id": project_id, "decisions": canonical},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self.keys.metadata_key, payload, hashlib.sha256).hexdigest()

    def _replay_review_decision(
        self,
        review: dict[str, Any],
        decisions: list[dict[str, Any]],
        decision_digest: str,
    ) -> dict[str, Any]:
        accepted_assets: list[dict[str, Any]] = []
        for decision in decisions or []:
            if str(decision.get("action") or "").strip() != "accept":
                continue
            candidate_id = str(decision.get("candidate_id") or "").strip()
            asset = self._asset_for_candidate(candidate_id)
            if asset is None:
                raise ValueError("review is finalized but its accepted asset is missing")
            accepted_assets.append(asset)
        result = dict(review)
        result["accepted_assets"] = accepted_assets
        result["idempotency_key"] = f"{review['review_id']}:{decision_digest}"
        result["replayed"] = True
        return result

    def _asset_for_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        memory_row = self.conn.execute(
            "SELECT * FROM memories WHERE source_candidate_id = ? ORDER BY created_at ASC, memory_id ASC LIMIT 1",
            (candidate_id,),
        ).fetchone()
        if memory_row is not None:
            asset = self._memory_from_row(memory_row, include_metadata=True)
            asset["asset_type"] = "memory"
            return asset
        candidate_row = self.conn.execute(
            "SELECT * FROM reflection_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if candidate_row is not None and candidate_row["asset_type"] == "memory":
            candidate_payload = self._candidate_payload_from_row(dict(candidate_row))
            deduplicated_memory_id = str(candidate_payload.get("deduplicated_memory_id") or "").strip()
            if deduplicated_memory_id:
                deduplicated_row = self.conn.execute(
                    "SELECT * FROM memories WHERE memory_id = ?",
                    (deduplicated_memory_id,),
                ).fetchone()
                if deduplicated_row is not None:
                    asset = self._memory_from_row(deduplicated_row, include_metadata=True)
                    asset.update(
                        {
                            "asset_type": "memory",
                            "deduplicated": True,
                            "deduplicated_to": deduplicated_memory_id,
                        }
                    )
                    return asset
        skill_row = self.conn.execute(
            "SELECT * FROM skills WHERE source_candidate_id = ? ORDER BY created_at ASC, skill_id ASC LIMIT 1",
            (candidate_id,),
        ).fetchone()
        if skill_row is not None:
            asset = self._skill_from_row(skill_row)
            asset["asset_type"] = "skill"
            return asset
        return None

    @_synchronized
    def search(
        self,
        query: str,
        top_k_memory: int = 3,
        top_k_skill: int = 2,
        *,
        project_id: str | None = None,
        context_only: bool = False,
        include_global: bool = True,
        as_of: float | None = None,
        workflow_id: str | None = None,
        ) -> dict[str, list[dict[str, Any]]]:
        terms = _search_terms(query)
        project_id = str(project_id or "").strip()
        workflow_id = str(workflow_id or "").strip() or None
        reference_time: float | None = None
        query_sql = (
            "SELECT chunk_id, asset_id, asset_type, subtype, status, scope, project_id, "
            "created_at, updated_at, last_used_at, valid_from, invalid_at, "
            "payload_nonce, payload_ciphertext "
            "FROM learning_chunks WHERE status = 'active'"
        )
        params: list[Any] = []
        if context_only or as_of is not None:
            reference_time = _normalise_timestamp(
                _now() if as_of is None else as_of,
                field="as_of",
            )
            query_sql += (
                " AND (asset_type <> 'memory' OR ("
                "valid_from IS NULL OR valid_from <= ?) AND "
                "(invalid_at IS NULL OR invalid_at > ?))"
            )
            params.extend((reference_time, reference_time))
        if project_id and include_global:
            query_sql += " AND (scope = 'global' OR (scope = 'project' AND project_id = ?))"
            params.append(project_id)
        elif project_id:
            query_sql += " AND scope = 'project' AND project_id = ?"
            params.append(project_id)
        elif context_only or not project_id:
            query_sql += " AND scope = 'global'"
        rows = self.conn.execute(query_sql + " ORDER BY updated_at DESC, chunk_id ASC", params).fetchall()

        history_rows = []
        if reference_time is not None:
            history_sql = (
                "SELECT memory_version_id, memory_id AS asset_id, record_version, subtype, "
                "status, scope, project_id, source_candidate_id, created_at, updated_at, "
                "last_used_at, valid_from, invalid_at, payload_nonce, payload_ciphertext "
                "FROM memory_versions WHERE status = 'active' "
                "AND (valid_from IS NULL OR valid_from <= ?) "
                "AND (invalid_at IS NULL OR invalid_at > ?)"
            )
            history_params: list[Any] = [reference_time, reference_time]
            if project_id and include_global:
                history_sql += " AND (scope = 'global' OR (scope = 'project' AND project_id = ?))"
                history_params.append(project_id)
            elif project_id:
                history_sql += " AND scope = 'project' AND project_id = ?"
                history_params.append(project_id)
            elif context_only or not project_id:
                history_sql += " AND scope = 'global'"
            history_rows = self.conn.execute(
                history_sql + " ORDER BY updated_at DESC, memory_version_id ASC",
                history_params,
            ).fetchall()

        scored: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
        for row in rows:
            try:
                payload = self._decrypt_payload(
                    row["payload_nonce"],
                    row["payload_ciphertext"],
                    table="learning_chunks",
                    pk_name="chunk_id",
                    pk_value=row["chunk_id"],
                    asset_type=row["asset_type"],
                )
            except ValueError:
                continue
            if row["asset_type"] == "skill" and context_only and not is_skill_visible_in_workflow(
                payload,
                workflow_id,
            ):
                continue
            if row["asset_type"] == "memory" and not _workflow_memory_visible(
                row["subtype"],
                payload,
                workflow_id,
                context_only=context_only or reference_time is not None,
            ):
                continue
            searchable_text = _chunk_search_text(payload)
            score = _keyword_score(terms, searchable_text)
            if score <= 0:
                continue
            scored.append((score, dict(row), payload))
        for row in history_rows:
            payload = self._decrypt_payload(
                row["payload_nonce"],
                row["payload_ciphertext"],
                table="memory_versions",
                pk_name="memory_version_id",
                pk_value=row["memory_version_id"],
                asset_type="memory",
            )
            if not _workflow_memory_visible(
                row["subtype"],
                payload,
                workflow_id,
                context_only=True,
            ):
                continue
            searchable_text = _chunk_search_text(payload)
            score = _keyword_score(terms, searchable_text)
            if score <= 0:
                continue
            history_row = dict(row)
            history_row["chunk_id"] = row["memory_version_id"]
            history_row["asset_type"] = "memory"
            scored.append((score, history_row, payload))

        # Keep the relevance score primary, then prefer the newest version so
        # an old tie cannot consume the bounded top-k memory window first.
        scored.sort(
            key=lambda item: (
                -item[0],
                -float(item[1].get("updated_at") or 0.0),
                item[1]["chunk_id"],
            )
        )
        memories: list[dict[str, Any]] = []
        skills: list[dict[str, Any]] = []
        seen_memory_ids: set[str] = set()
        for score, row, payload in scored:
            if row["asset_type"] == "memory":
                memory_asset_id = str(row["asset_id"] or "")
                if memory_asset_id in seen_memory_ids:
                    continue
                seen_memory_ids.add(memory_asset_id)
            record = {
                "asset_id": row["asset_id"],
                "asset_type": row["asset_type"],
                "subtype": row["subtype"],
                "memory_family": classify_memory_family(row["asset_type"], row["subtype"]),
                "status": row["status"],
                "scope": row["scope"] or "global",
                "project_id": row["project_id"],
                "source_candidate_id": payload.get("source_candidate_id") or "",
                "source_message_indexes": list(payload.get("source_message_indexes") or []),
                "source_excerpt": str(payload.get("source_excerpt") or ""),
                "source_trace": {
                    "candidate_id": payload.get("source_candidate_id") or "",
                    "message_indexes": list(payload.get("source_message_indexes") or []),
                    "excerpt": str(payload.get("source_excerpt") or ""),
                },
                "title": payload.get("title") or "",
                "content": payload.get("content") or payload.get("text") or "",
                "score": score,
                "updated_at": row["updated_at"],
            }
            source_session_id = str(payload.get("source_session_id") or "").strip()
            if source_session_id:
                record["source_session_id"] = source_session_id
                record["source_trace"]["session_id"] = source_session_id
            source_workflow_id = str(payload.get("source_workflow_id") or "").strip()
            if source_workflow_id:
                record["source_workflow_id"] = source_workflow_id
                record["source_trace"]["workflow_id"] = source_workflow_id
            source_message_ids = list(payload.get("source_message_ids") or [])
            source_message_sequences = list(payload.get("source_message_sequences") or [])
            if source_message_ids:
                record["source_message_ids"] = source_message_ids
                record["source_trace"]["message_ids"] = source_message_ids
            if source_message_sequences:
                record["source_message_sequences"] = source_message_sequences
                record["source_trace"]["message_sequences"] = source_message_sequences
            workflow_memory_id = str(payload.get("workflow_id") or "").strip()
            if row["asset_type"] == "memory" and workflow_memory_id:
                record["workflow_id"] = workflow_memory_id
            if row["asset_type"] == "memory":
                record.update(
                    {
                        "confirmation_status": payload.get("confirmation_status") or "legacy",
                        "confirmed_at": payload["confirmed_at"] if "confirmed_at" in payload else row["created_at"],
                        "last_used_at": row["last_used_at"],
                        "valid_from": row["valid_from"],
                        "invalid_at": row["invalid_at"],
                        "record_version": int(payload.get("record_version") or 1),
                    }
                )
            if row["asset_type"] == "memory" and len(memories) < top_k_memory:
                memories.append(record)
            elif row["asset_type"] == "skill" and len(skills) < top_k_skill:
                record.update(
                    {
                        "skill_id": payload.get("skill_id") or row["asset_id"],
                        "intent": payload.get("intent") or payload.get("title") or "",
                        "triggers": list(payload.get("triggers") or []),
                        "required_inputs": list(payload.get("required_inputs") or []),
                        "applicability": payload.get("applicability") or "",
                        "steps": list(payload.get("steps") or []),
                        "tool_permissions": list(payload.get("tool_permissions") or []),
                        "output_contract": payload.get("output_contract") or "",
                        "success_criteria": list(payload.get("success_criteria") or []),
                        "failure_modes": list(payload.get("failure_modes") or []),
                        "target": payload.get("target") or "conversation",
                        "compatibility_status": payload.get("compatibility_status") or "pending",
                        "adapter_path": payload.get("adapter_path") or "",
                        "adapter_schema": payload.get("adapter_schema") or "",
                    }
                )
                skills.append(record)

        return {"memories": memories, "skills": skills}

    @_synchronized
    def save_retrieval_feedback(
        self,
        query: str,
        corrected_query: str,
        *,
        scope: str = "global",
        project_id: str | None = None,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist an explicitly confirmed query correction without plaintext indexing."""
        query = _normalize_retrieval_feedback_query(query)
        corrected_query = _normalize_retrieval_feedback_query(corrected_query)
        if not query:
            raise ValueError("query is required")
        if not corrected_query:
            raise ValueError("corrected_query is required")
        if query == corrected_query:
            raise ValueError("corrected_query must differ from query")
        scope = str(scope or "global").strip().lower()
        if scope not in {"global", "project"}:
            raise ValueError("unsupported retrieval feedback scope")
        project_id = str(project_id or "").strip() or None
        if scope == "project" and not project_id:
            raise ValueError("project_id is required for project feedback")
        if scope == "global":
            project_id = None
        workflow_id = _normalise_retrieval_feedback_workflow(workflow_id)

        query_digest = _retrieval_feedback_digest(
            self.keys.metadata_key,
            query,
            workflow_id=workflow_id,
        )
        now = _now()
        with self.conn:
            # The UNIQUE constraint cannot distinguish NULL project_id values.
            # Serialize the lookup and write so concurrent global feedback
            # requests replay one row instead of creating duplicate IDs.
            self.conn.execute("BEGIN IMMEDIATE")
            existing = self.conn.execute(
                """
                SELECT feedback_id, created_at
                FROM retrieval_feedback
                WHERE scope = ? AND project_id IS ? AND workflow_id IS ? AND query_digest = ?
                ORDER BY updated_at DESC, feedback_id ASC
                LIMIT 1
                """,
                (scope, project_id, workflow_id, query_digest),
            ).fetchone()
            feedback_id = str(existing["feedback_id"]) if existing else f"rfb_{os.urandom(8).hex()}"
            created_at = float(existing["created_at"]) if existing else now
            payload = {
                "schema_version": "retrieval_feedback_v1",
                "query": query,
                "corrected_query": corrected_query,
                "workflow_id": workflow_id,
                "source": "user_confirmed",
            }
            nonce, ciphertext = self._encrypt_payload(
                payload,
                table="retrieval_feedback",
                pk_name="feedback_id",
                pk_value=feedback_id,
                asset_type="retrieval_feedback",
            )
            if existing:
                self.conn.execute(
                    """
                    UPDATE retrieval_feedback
                    SET status = 'active', updated_at = ?,
                        payload_nonce = ?, payload_ciphertext = ?
                    WHERE feedback_id = ?
                    """,
                    (now, nonce, ciphertext, feedback_id),
                )
            else:
                self.conn.execute(
                    """
                    INSERT INTO retrieval_feedback (
                        feedback_id, scope, project_id, workflow_id, query_digest, status,
                        created_at, updated_at, payload_nonce, payload_ciphertext
                    )
                    VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                    """,
                    (
                        feedback_id,
                        scope,
                        project_id,
                        workflow_id,
                        query_digest,
                        created_at,
                        now,
                        nonce,
                        ciphertext,
                    ),
                )
        return {
            "feedback_id": feedback_id,
            "scope": scope,
            "project_id": project_id,
            "workflow_id": workflow_id,
            "status": "active",
            "query": query,
            "corrected_query": corrected_query,
            "source": "user_confirmed",
            "created_at": created_at,
            "updated_at": now,
        }

    @_synchronized
    def list_retrieval_feedback(
        self,
        query: str | None = None,
        *,
        project_id: str | None = None,
        include_revoked: bool = False,
        workflow_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return feedback for the requested project and workflow namespace."""
        project_id = str(project_id or "").strip() or None
        workflow_id = _normalise_retrieval_feedback_workflow(workflow_id)
        clauses = []
        params: list[Any] = []
        if not include_revoked:
            clauses.append("status = 'active'")
        if query is not None:
            normalized_query = _normalize_retrieval_feedback_query(query)
            if not normalized_query:
                return []
            generic_digest = _retrieval_feedback_digest(self.keys.metadata_key, normalized_query)
            if workflow_id:
                scoped_digest = _retrieval_feedback_digest(
                    self.keys.metadata_key,
                    normalized_query,
                    workflow_id=workflow_id,
                )
                clauses.append("query_digest IN (?, ?)")
                params.extend([generic_digest, scoped_digest])
            else:
                clauses.append("query_digest = ?")
                params.append(generic_digest)
        if workflow_id:
            clauses.append("(workflow_id IS NULL OR workflow_id = ?)")
            params.append(workflow_id)
        else:
            # A workflow-less caller requests only generic corrections.  This
            # keeps a correction learned in one research window from leaking
            # into an unbound request or another window's fallback path.
            clauses.append("workflow_id IS NULL")
        if project_id:
            clauses.append("(scope = 'global' OR (scope = 'project' AND project_id = ?))")
            params.append(project_id)
        else:
            clauses.append("scope = 'global'")
        where = " AND ".join(clauses) if clauses else "1 = 1"
        rows = self.conn.execute(
            """
            SELECT feedback_id, scope, project_id, workflow_id, query_digest, status,
                   created_at, updated_at, payload_nonce, payload_ciphertext
            FROM retrieval_feedback
            WHERE """ + where + """
            ORDER BY CASE WHEN workflow_id IS NOT NULL THEN 0 ELSE 1 END,
                     CASE WHEN scope = 'project' THEN 0 ELSE 1 END,
                     updated_at DESC, feedback_id ASC""",
            tuple(params),
        ).fetchall()
        records = []
        for row in rows:
            payload = self._decrypt_payload(
                row["payload_nonce"],
                row["payload_ciphertext"],
                table="retrieval_feedback",
                pk_name="feedback_id",
                pk_value=row["feedback_id"],
                asset_type="retrieval_feedback",
            )
            records.append(
                {
                    "feedback_id": row["feedback_id"],
                    "scope": row["scope"],
                    "project_id": row["project_id"],
                    "workflow_id": row["workflow_id"],
                    "status": row["status"],
                    "query": str(payload.get("query") or ""),
                    "corrected_query": str(payload.get("corrected_query") or ""),
                    "source": str(payload.get("source") or "legacy"),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
        return records

    @_synchronized
    def revoke_retrieval_feedback(
        self,
        feedback_id: str,
        *,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        feedback_id = str(feedback_id or "").strip()
        if not feedback_id:
            return None
        requested_project_id = str(project_id or "").strip() or None
        row = self.conn.execute(
            """
            SELECT feedback_id, scope, project_id, workflow_id, status, created_at, updated_at
            FROM retrieval_feedback
            WHERE feedback_id = ?
            """,
            (feedback_id,),
        ).fetchone()
        if row is None or row["status"] == "revoked":
            return None
        stored_project_id = str(row["project_id"] or "").strip() or None
        if stored_project_id is None:
            if requested_project_id is not None:
                raise ValueError("global retrieval feedback is workspace-scoped; omit project_id")
        elif requested_project_id is None:
            raise ValueError("project_id is required for project retrieval feedback")
        elif requested_project_id != stored_project_id:
            raise ValueError("retrieval feedback belongs to another project")
        now = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE retrieval_feedback SET status = 'revoked', updated_at = ? WHERE feedback_id = ?",
                (now, feedback_id),
            )
        return {
            "feedback_id": row["feedback_id"],
            "scope": row["scope"],
            "project_id": row["project_id"],
            "workflow_id": row["workflow_id"],
            "status": "revoked",
            "created_at": row["created_at"],
            "updated_at": now,
        }

    @_synchronized
    def record_skill_run(
        self,
        skill_id: str,
        *,
        query: str,
        plan: dict[str, Any],
        matched_score: float = 0.0,
        inputs: dict[str, Any] | None = None,
        status: str = "planned",
        idempotency_key: str | None = None,
        project_id: str | None = None,
        include_global: bool = True,
    ) -> dict[str, Any]:
        if status not in {"planned", "completed", "failed"}:
            raise ValueError("unsupported skill run status")
        skill_id = str(skill_id or "").strip()
        project_id = str(project_id or "").strip() or None
        idempotency_key = self._normalise_skill_run_idempotency_key(idempotency_key)
        canonical = json.dumps(
            {
                "skill_id": skill_id,
                "project_id": project_id,
                "query": str(query or ""),
                "plan": dict(plan or {}),
                "matched_score": float(matched_score),
                "inputs": dict(inputs or {}),
                "status": status,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        idempotency_digest = (
            hmac.new(self.keys.metadata_key, canonical, hashlib.sha256).hexdigest()
            if idempotency_key
            else None
        )
        payload = {
            "query": str(query or ""),
            "matched_score": float(matched_score),
            "plan": dict(plan or {}),
            "inputs": dict(inputs or {}),
            "outcome": "",
            "failure_reason": "",
        }
        with self.conn:
            if idempotency_key:
                self.conn.execute("BEGIN IMMEDIATE")
                existing = self.conn.execute(
                    "SELECT * FROM skill_runs WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    if not hmac.compare_digest(
                        str(existing["idempotency_digest"] or ""),
                        str(idempotency_digest or ""),
                    ):
                        raise ValueError("idempotency_key is already bound to a different Skill run")
                    replay = self._skill_run_from_row(existing)
                    replay["replayed"] = True
                    return replay

            scope_sql, scope_params = _skill_scope_sql(project_id, include_global=include_global)
            skill = self.conn.execute(
                "SELECT skill_id, project_id FROM skills WHERE skill_id = ? AND status = 'active' AND " + scope_sql,
                (skill_id, *scope_params),
            ).fetchone()
            if skill is None:
                raise KeyError(f"unknown active skill: {skill_id}")

            now = _now()
            run_id = f"run_{os.urandom(8).hex()}"
            nonce, ciphertext = self._encrypt_payload(
                payload,
                table="skill_runs",
                pk_name="run_id",
                pk_value=run_id,
                asset_type="skill",
            )
            self.conn.execute(
                """
                INSERT INTO skill_runs (
                    run_id, skill_id, status, created_at, updated_at,
                    idempotency_key, idempotency_digest,
                    payload_nonce, payload_ciphertext
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    skill_id,
                    status,
                    now,
                    now,
                    idempotency_key,
                    idempotency_digest,
                    nonce,
                    ciphertext,
                ),
            )
        result = {
            "run_id": run_id,
            "skill_id": skill_id,
            "project_id": project_id,
            "status": status,
            "created_at": now,
            "updated_at": now,
            **payload,
        }
        if idempotency_key:
            result["replayed"] = False
        return result

    @staticmethod
    def _normalise_skill_run_idempotency_key(idempotency_key: str | None) -> str | None:
        key = str(idempotency_key or "").strip()
        if not key:
            return None
        if len(key) > 128 or any(ord(char) < 33 or ord(char) > 126 for char in key):
            raise ValueError("idempotency_key must be printable and at most 128 characters")
        return key

    @_synchronized
    def update_skill_run(
        self,
        run_id: str,
        *,
        status: str,
        outcome: str = "",
        failure_reason: str = "",
    ) -> dict[str, Any] | None:
        """Persist the terminal state of a Skill execution without exposing its payload."""
        if status not in {"planned", "completed", "failed"}:
            raise ValueError("unsupported skill run status")
        row = self.conn.execute(
            "SELECT * FROM skill_runs WHERE run_id = ?",
            (str(run_id or ""),),
        ).fetchone()
        if row is None:
            return None
        if str(row["status"] or "") == "completed":
            # A retry or a late callback must not change a durable terminal
            # success.  The conditional update below also protects the race
            # where another connection completes the run after this read but
            # before this writer commits.
            return self._skill_run_from_row(row)
        payload = self._decrypt_payload(
            row["payload_nonce"],
            row["payload_ciphertext"],
            table="skill_runs",
            pk_name="run_id",
            pk_value=row["run_id"],
            asset_type="skill",
        )
        payload["outcome"] = str(outcome or "")[:2000]
        payload["failure_reason"] = str(failure_reason or "")[:500]
        nonce, ciphertext = self._encrypt_payload(
            payload,
            table="skill_runs",
            pk_name="run_id",
            pk_value=row["run_id"],
            asset_type="skill",
        )
        now = _now()
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE skill_runs
                SET status = ?, updated_at = ?, payload_nonce = ?, payload_ciphertext = ?
                WHERE run_id = ? AND (? = 'completed' OR status <> 'completed')
                """,
                (status, now, nonce, ciphertext, row["run_id"], status),
            )
            if not cursor.rowcount:
                current = self.conn.execute(
                    "SELECT * FROM skill_runs WHERE run_id = ?",
                    (row["run_id"],),
                ).fetchone()
                if current is not None:
                    return self._skill_run_from_row(current)
        return self._skill_run_from_row(
            self.conn.execute(
                "SELECT * FROM skill_runs WHERE run_id = ?",
                (row["run_id"],),
            ).fetchone()
        )

    def _encrypt_payload(
        self,
        payload: dict[str, Any],
        *,
        table: str,
        pk_name: str,
        pk_value: str,
        asset_type: str | None = None,
    ) -> tuple[str, str]:
        nonce = os.urandom(12)
        plaintext = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ciphertext = AESGCM(self.keys.metadata_key).encrypt(
            nonce,
            plaintext,
            _payload_aad(table, pk_name, pk_value, asset_type=asset_type),
        )
        return _b64(nonce), _b64(ciphertext)

    def _decrypt_payload(
        self,
        nonce: str,
        ciphertext: str,
        *,
        table: str,
        pk_name: str,
        pk_value: str,
        asset_type: str | None = None,
    ) -> dict[str, Any]:
        try:
            plaintext = AESGCM(self.keys.metadata_key).decrypt(
                _unb64(nonce),
                _unb64(ciphertext),
                _payload_aad(table, pk_name, pk_value, asset_type=asset_type),
            )
        except (InvalidTag, ValueError, TypeError) as exc:
            raise ValueError("learning payload authentication failed") from exc
        return json.loads(plaintext.decode("utf-8"))

    def _memory_from_row(self, row, *, include_metadata: bool = False) -> dict[str, Any]:
        payload = self._decrypt_payload(
            row["payload_nonce"],
            row["payload_ciphertext"],
            table="memories",
            pk_name="memory_id",
            pk_value=row["memory_id"],
            asset_type="memory",
        )
        # Validity is canonical in the row so legacy encrypted payloads cannot
        # override the query-time temporal filter.
        payload.pop("valid_from", None)
        payload.pop("invalid_at", None)
        memory = {
            **payload,
            "memory_id": row["memory_id"],
            "subtype": row["subtype"],
            "memory_family": classify_memory_family("memory", row["subtype"]),
            "status": row["status"],
            "scope": row["scope"] or "global",
            "project_id": row["project_id"],
            "source_candidate_id": row["source_candidate_id"],
            "source_message_indexes": list(payload.get("source_message_indexes") or []),
            "source_excerpt": str(payload.get("source_excerpt") or ""),
            "source_trace": {
                "candidate_id": row["source_candidate_id"],
                "message_indexes": list(payload.get("source_message_indexes") or []),
                "excerpt": str(payload.get("source_excerpt") or ""),
            },
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        source_session_id = str(payload.get("source_session_id") or "").strip()
        if source_session_id:
            memory["source_session_id"] = source_session_id
            memory["source_trace"]["session_id"] = source_session_id
        source_workflow_id = str(payload.get("source_workflow_id") or "").strip()
        if source_workflow_id:
            memory["source_workflow_id"] = source_workflow_id
            memory["source_trace"]["workflow_id"] = source_workflow_id
        source_message_ids = list(payload.get("source_message_ids") or [])
        source_message_sequences = list(payload.get("source_message_sequences") or [])
        if source_message_ids:
            memory["source_message_ids"] = source_message_ids
            memory["source_trace"]["message_ids"] = source_message_ids
        if source_message_sequences:
            memory["source_message_sequences"] = source_message_sequences
            memory["source_trace"]["message_sequences"] = source_message_sequences
        if include_metadata:
            memory.update(
                {
                    "confirmation_status": payload.get("confirmation_status") or "legacy",
                    "confirmed_at": payload["confirmed_at"] if "confirmed_at" in payload else row["created_at"],
                    "last_used_at": row["last_used_at"],
                    "valid_from": row["valid_from"],
                    "invalid_at": row["invalid_at"],
                    "record_version": int(payload.get("record_version") or 1),
                }
            )
        return memory

    def _memory_version_from_row(self, row, *, include_metadata: bool = False) -> dict[str, Any]:
        payload = self._decrypt_payload(
            row["payload_nonce"],
            row["payload_ciphertext"],
            table="memory_versions",
            pk_name="memory_version_id",
            pk_value=row["memory_version_id"],
            asset_type="memory",
        )
        payload.pop("valid_from", None)
        payload.pop("invalid_at", None)
        memory = {
            **payload,
            "memory_id": row["memory_id"],
            "subtype": row["subtype"],
            "memory_family": classify_memory_family("memory", row["subtype"]),
            "status": row["status"],
            "scope": row["scope"] or "global",
            "project_id": row["project_id"],
            "source_candidate_id": row["source_candidate_id"],
            "source_message_indexes": list(payload.get("source_message_indexes") or []),
            "source_excerpt": str(payload.get("source_excerpt") or ""),
            "source_trace": {
                "candidate_id": row["source_candidate_id"],
                "message_indexes": list(payload.get("source_message_indexes") or []),
                "excerpt": str(payload.get("source_excerpt") or ""),
            },
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        source_session_id = str(payload.get("source_session_id") or "").strip()
        if source_session_id:
            memory["source_session_id"] = source_session_id
            memory["source_trace"]["session_id"] = source_session_id
        source_workflow_id = str(payload.get("source_workflow_id") or "").strip()
        if source_workflow_id:
            memory["source_workflow_id"] = source_workflow_id
            memory["source_trace"]["workflow_id"] = source_workflow_id
        source_message_ids = list(payload.get("source_message_ids") or [])
        source_message_sequences = list(payload.get("source_message_sequences") or [])
        if source_message_ids:
            memory["source_message_ids"] = source_message_ids
            memory["source_trace"]["message_ids"] = source_message_ids
        if source_message_sequences:
            memory["source_message_sequences"] = source_message_sequences
            memory["source_trace"]["message_sequences"] = source_message_sequences
        if include_metadata:
            memory.update(
                {
                    "confirmation_status": payload.get("confirmation_status") or "legacy",
                    "confirmed_at": payload["confirmed_at"] if "confirmed_at" in payload else row["created_at"],
                    "last_used_at": row["last_used_at"],
                    "valid_from": row["valid_from"],
                    "invalid_at": row["invalid_at"],
                    "record_version": int(row["record_version"] or payload.get("record_version") or 1),
                }
            )
        return memory

    def _skill_from_row(self, row) -> dict[str, Any]:
        payload = self._decrypt_payload(
            row["payload_nonce"],
            row["payload_ciphertext"],
            table="skills",
            pk_name="skill_id",
            pk_value=row["skill_id"],
            asset_type="skill",
        )
        return {
            **payload,
            "skill_id": row["skill_id"],
            "memory_family": classify_memory_family("skill", payload.get("subtype") or "skill"),
            "category": row["category"],
            "status": row["status"],
            "version": row["version"],
            "scope": row["scope"] or "global",
            "project_id": row["project_id"],
            "source_candidate_id": row["source_candidate_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _skill_run_from_row(self, row) -> dict[str, Any]:
        payload = self._decrypt_payload(
            row["payload_nonce"],
            row["payload_ciphertext"],
            table="skill_runs",
            pk_name="run_id",
            pk_value=row["run_id"],
            asset_type="skill",
        )
        return {
            **payload,
            "run_id": row["run_id"],
            "skill_id": row["skill_id"],
            "scope": row["skill_scope"] if "skill_scope" in row.keys() else None,
            "project_id": row["skill_project_id"] if "skill_project_id" in row.keys() else None,
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _review_record(self, review_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT review_id, trigger, status, created_at, updated_at,
                   project_id,
                   candidate_count, accepted_count, rejected_count,
                   decision_digest, decision_project_id
            FROM reflection_reviews
            WHERE review_id = ?
            """,
            (review_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def _review_with_candidates(self, review_id: str) -> dict[str, Any] | None:
        review = self._review_record(review_id)
        if review is None:
            return None
        rows = self.conn.execute(
            "SELECT * FROM reflection_candidates "
            "WHERE review_id = ? ORDER BY created_at ASC, candidate_id ASC",
            (review_id,),
        ).fetchall()
        review["candidates"] = [self._candidate_from_row(dict(row)) for row in rows]
        return review

    def _candidate_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = self._candidate_payload_from_row(row)
        try:
            source_indexes = json.loads(row.get("source_message_indexes") or "[]")
        except (TypeError, ValueError):
            source_indexes = []
        return {
            **payload,
            "candidate_id": row["candidate_id"],
            "review_id": row["review_id"],
            "asset_type": row["asset_type"],
            "subtype": row["subtype"],
            "status": row["status"],
            "confidence": float(row["confidence"] or 0.0),
            "selected_by_default": bool(row["selected_by_default"]),
            "duplicate_of": row["duplicate_of"],
            "source_message_indexes": [
                int(index) for index in source_indexes
                if isinstance(index, int) and index >= 0
            ],
        }

    def _candidate_record(self, candidate_id: str, review_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM reflection_candidates
            WHERE candidate_id = ? AND review_id = ?
            """,
            (candidate_id, review_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def _store_candidate(self, review_id: str, candidate: dict[str, Any], now: float) -> dict[str, Any]:
        asset_type = str(candidate.get("asset_type") or "").strip()
        subtype = str(candidate.get("subtype") or "").strip()
        if asset_type not in {"memory", "skill"}:
            raise ValueError("candidate asset_type is required")
        if not subtype:
            raise ValueError("candidate subtype is required")
        if asset_type == "memory" and subtype == "workflow_fact":
            workflow_id = str(candidate.get("workflow_id") or "").strip()
            if not workflow_id:
                raise ValueError("workflow_fact requires workflow_id")
            if workflow_id not in SUPPORTED_WORKFLOW_IDS:
                raise ValueError("workflow_fact requires a supported workflow_id")
        if asset_type == "skill" and normalize_skill_payload(candidate).get("workflow_scope_invalid"):
            raise ValueError("skill has unsupported workflow scope")

        candidate_id = f"cand_{os.urandom(8).hex()}"
        payload = self._candidate_payload(candidate)
        nonce, ciphertext = self._encrypt_payload(
            payload,
            table="reflection_candidates",
            pk_name="candidate_id",
            pk_value=candidate_id,
            asset_type=asset_type,
        )
        self.conn.execute(
            """
            INSERT INTO reflection_candidates (
                candidate_id, review_id, asset_type, subtype, status,
                confidence, selected_by_default, duplicate_of,
                source_message_indexes, created_at, updated_at,
                payload_nonce, payload_ciphertext
            )
            VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                review_id,
                asset_type,
                subtype,
                float(candidate.get("confidence") or 0),
                1 if candidate.get("selected_by_default") else 0,
                candidate.get("duplicate_of"),
                json.dumps(candidate.get("source_message_indexes") or [], ensure_ascii=False),
                now,
                now,
                nonce,
                ciphertext,
            ),
        )
        return {
            "candidate_id": candidate_id,
            "review_id": review_id,
            "asset_type": asset_type,
            "subtype": subtype,
            "status": "pending",
            "confidence": float(candidate.get("confidence") or 0),
            "selected_by_default": bool(candidate.get("selected_by_default")),
            "duplicate_of": candidate.get("duplicate_of"),
            "source_message_indexes": list(candidate.get("source_message_indexes") or []),
            **payload,
        }

    def _candidate_payload(self, candidate: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "title": str(candidate.get("title") or "").strip(),
            "content": str(candidate.get("content") or "").strip(),
            "evidence": list(candidate.get("evidence") or []),
            "rationale": str(candidate.get("rationale") or "").strip(),
            "tags": list(candidate.get("tags") or []),
            "source_excerpt": str(candidate.get("source_excerpt") or "").strip(),
        }
        source_session_id = str(candidate.get("source_session_id") or "").strip()
        if source_session_id:
            payload["source_session_id"] = source_session_id[:128]
        source_workflow_id = str(candidate.get("source_workflow_id") or "").strip()
        if source_workflow_id:
            payload["source_workflow_id"] = source_workflow_id[:64]
        source_message_ids = [
            str(message_id).strip()[:128]
            for message_id in candidate.get("source_message_ids") or []
            if str(message_id).strip()
        ][:32]
        if source_message_ids:
            payload["source_message_ids"] = source_message_ids
        source_message_sequences = [
            int(sequence)
            for sequence in candidate.get("source_message_sequences") or []
            if isinstance(sequence, int) and not isinstance(sequence, bool) and sequence > 0
        ][:32]
        if source_message_sequences:
            payload["source_message_sequences"] = source_message_sequences
        if candidate.get("conflicts_with"):
            payload["conflicts_with"] = [
                str(memory_id).strip()
                for memory_id in candidate.get("conflicts_with") or []
                if str(memory_id).strip()
            ]
        if candidate.get("write_gate"):
            payload["write_gate"] = dict(candidate["write_gate"])
        if (
            candidate.get("asset_type") == "memory"
            and candidate.get("subtype") == "workflow_fact"
        ):
            workflow_id = str(candidate.get("workflow_id") or "").strip()
            if workflow_id:
                payload["workflow_id"] = workflow_id[:64]
        if candidate.get("asset_type") == "skill":
            for key in (
                "intent",
                "triggers",
                "required_inputs",
                "applicability",
                "steps",
                "tool_permissions",
                "output_contract",
                "success_criteria",
                "failure_modes",
                "eval_cases",
                "source_path",
                "source_digest",
                "presentation_style_source",
                "target",
                "compatibility_status",
                "adapter_path",
                "adapter_schema",
                "raw_source_digest",
                "presentation_contract",
                "workflow_ids",
                "scope",
                "project_id",
            ):
                if key in candidate:
                    payload[key] = candidate[key]
            if "presentation_style" in candidate:
                payload["presentation_style"] = dict(candidate["presentation_style"] or {})
            if "resource_manifest" in candidate:
                payload["resource_manifest"] = list(candidate["resource_manifest"] or [])[:48]
        return payload

    def _candidate_final_payload(self, candidate_row: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
        payload = self._candidate_payload_from_row(candidate_row)
        payload["subtype"] = candidate_row["subtype"]
        try:
            source_indexes = json.loads(candidate_row.get("source_message_indexes") or "[]")
        except (TypeError, ValueError):
            source_indexes = []
        payload["source_message_indexes"] = [
            int(index) for index in source_indexes if isinstance(index, int) and index >= 0
        ]
        payload["source_candidate_id"] = candidate_row["candidate_id"]
        if candidate_row["asset_type"] == "memory":
            payload["source_trace"] = {
                "candidate_id": candidate_row["candidate_id"],
                "message_indexes": list(payload["source_message_indexes"]),
                "excerpt": str(payload.get("source_excerpt") or ""),
            }
            source_message_ids = list(payload.get("source_message_ids") or [])
            source_message_sequences = list(payload.get("source_message_sequences") or [])
            if source_message_ids:
                payload["source_trace"]["message_ids"] = source_message_ids
            if source_message_sequences:
                payload["source_trace"]["message_sequences"] = source_message_sequences
            source_session_id = str(payload.get("source_session_id") or "").strip()
            if source_session_id:
                payload["source_trace"]["session_id"] = source_session_id
            source_workflow_id = str(payload.get("source_workflow_id") or "").strip()
            if source_workflow_id:
                payload["source_trace"]["workflow_id"] = source_workflow_id
        edited_title = decision.get("edited_title")
        edited_content = decision.get("edited_content")
        if edited_title is not None:
            payload["title"] = str(edited_title).strip()
        if edited_content is not None:
            payload["content"] = str(edited_content).strip()
        validity_fields = {"valid_from", "invalid_at"}
        if candidate_row["asset_type"] != "memory" and validity_fields.intersection(decision):
            raise ValueError("memory validity fields are only valid for memory candidates")
        if candidate_row["asset_type"] == "memory":
            if "valid_from" in decision:
                payload["valid_from"] = _normalise_optional_timestamp(
                    decision.get("valid_from"),
                    field="valid_from",
                )
            if "invalid_at" in decision:
                payload["invalid_at"] = _normalise_optional_timestamp(
                    decision.get("invalid_at"),
                    field="invalid_at",
                )
            _validate_memory_validity(payload.get("valid_from"), payload.get("invalid_at"))
        if candidate_row["asset_type"] == "skill":
            payload = normalize_skill_payload(payload)
            payload["subtype"] = candidate_row["subtype"]
        payload["text"] = _chunk_search_text(payload)
        return payload

    def _candidate_payload_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return self._decrypt_payload(
            row["payload_nonce"],
            row["payload_ciphertext"],
            table="reflection_candidates",
            pk_name="candidate_id",
            pk_value=row["candidate_id"],
            asset_type=row["asset_type"],
        )

    def _record_candidate_dedup_locked(self, candidate_row: dict[str, Any], memory_id: str) -> None:
        payload = self._candidate_payload_from_row(candidate_row)
        payload["deduplicated_memory_id"] = memory_id
        nonce, ciphertext = self._encrypt_payload(
            payload,
            table="reflection_candidates",
            pk_name="candidate_id",
            pk_value=candidate_row["candidate_id"],
            asset_type=candidate_row["asset_type"],
        )
        self.conn.execute(
            "UPDATE reflection_candidates SET updated_at = ?, payload_nonce = ?, payload_ciphertext = ? "
            "WHERE candidate_id = ?",
            (_now(), nonce, ciphertext, candidate_row["candidate_id"]),
        )

    def _find_exact_active_memory_locked(self, final_payload: dict[str, Any]):
        expected_scope = str(final_payload.get("scope") or "global")
        expected_project_id = str(final_payload.get("project_id") or "").strip() or None
        expected_workflow_id = str(final_payload.get("workflow_id") or "").strip()
        expected_title = _normalise_memory_text(final_payload.get("title"))
        expected_content = _normalise_memory_text(final_payload.get("content"))
        if not expected_title or not expected_content:
            return None
        reference_time = _normalise_timestamp(
            _now() if final_payload.get("valid_from") is None else final_payload.get("valid_from"),
            field="valid_from",
        )
        # ponytail: bounded local scan keeps the encrypted schema simple; add a keyed
        # fingerprint only if memory volume makes this measurable.
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE status = 'active' AND subtype = ? AND scope = ? "
            "AND (valid_from IS NULL OR valid_from <= ?) "
            "AND (invalid_at IS NULL OR invalid_at > ?) "
            "ORDER BY created_at ASC, memory_id ASC",
            (final_payload.get("subtype") or "", expected_scope, reference_time, reference_time),
        ).fetchall()
        for row in rows:
            if (str(row["project_id"] or "").strip() or None) != expected_project_id:
                continue
            payload = self._decrypt_payload(
                row["payload_nonce"],
                row["payload_ciphertext"],
                table="memories",
                pk_name="memory_id",
                pk_value=row["memory_id"],
                asset_type="memory",
            )
            if (
                _normalise_memory_text(payload.get("title")) == expected_title
                and _normalise_memory_text(payload.get("content")) == expected_content
                and (
                    str(final_payload.get("subtype") or "") != "workflow_fact"
                    or str(payload.get("workflow_id") or "").strip() == expected_workflow_id
                )
            ):
                return row
        return None

    def _preserve_memory_version_locked(
        self,
        row: sqlite3.Row,
        payload: dict[str, Any],
        *,
        invalid_at: float,
    ) -> None:
        """Keep an archived memory's last valid interval queryable."""
        memory_id = str(row["memory_id"] or "").strip()
        if not memory_id:
            return
        history_valid_from = (
            float(row["valid_from"])
            if row["valid_from"] is not None
            else float(row["created_at"])
        )
        history_invalid_at = min(
            float(row["invalid_at"]),
            float(invalid_at),
        ) if row["invalid_at"] is not None else float(invalid_at)
        if history_valid_from >= history_invalid_at:
            return
        _validate_memory_validity(history_valid_from, history_invalid_at)

        current_record_version = int(payload.get("record_version") or 1)
        memory_version_id = f"{memory_id}:v{current_record_version}"
        history_payload = dict(payload)
        history_payload.pop("valid_from", None)
        history_payload.pop("invalid_at", None)
        history_nonce, history_ciphertext = self._encrypt_payload(
            history_payload,
            table="memory_versions",
            pk_name="memory_version_id",
            pk_value=memory_version_id,
            asset_type="memory",
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO memory_versions (
                memory_version_id, memory_id, record_version, subtype, status,
                scope, project_id, source_candidate_id, created_at, updated_at,
                last_used_at, valid_from, invalid_at, payload_nonce, payload_ciphertext
            )
            VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory_version_id,
                memory_id,
                current_record_version,
                row["subtype"],
                row["scope"] or "global",
                row["project_id"],
                row["source_candidate_id"],
                row["created_at"],
                row["updated_at"],
                row["last_used_at"],
                history_valid_from,
                history_invalid_at,
                history_nonce,
                history_ciphertext,
            ),
        )

    def _supersede_conflicting_memories_locked(
        self,
        candidate_row: dict[str, Any],
        final_payload: dict[str, Any],
        *,
        superseded_at: float | None = None,
        validity_boundary: float | None = None,
    ) -> list[str]:
        candidate_payload = self._candidate_payload_from_row(candidate_row)
        if str(candidate_payload.get("title") or "").strip() != str(final_payload.get("title") or "").strip():
            return []

        expected_scope = str(final_payload.get("scope") or "global")
        expected_project_id = str(final_payload.get("project_id") or "").strip() or None
        expected_workflow_id = str(final_payload.get("workflow_id") or "").strip()
        superseded: list[str] = []
        seen: set[str] = set()
        for raw_memory_id in candidate_payload.get("conflicts_with") or []:
            memory_id = str(raw_memory_id or "").strip()
            if not memory_id or memory_id in seen:
                continue
            seen.add(memory_id)
            row = self.conn.execute(
                "SELECT * FROM memories WHERE memory_id = ? AND status = 'active'",
                (memory_id,),
            ).fetchone()
            if row is None:
                continue
            if (
                str(row["subtype"] or "") != str(candidate_row["subtype"] or "")
                or str(row["scope"] or "global") != expected_scope
                or (str(row["project_id"] or "").strip() or None) != expected_project_id
            ):
                continue
            old_payload = self._decrypt_payload(
                row["payload_nonce"],
                row["payload_ciphertext"],
                table="memories",
                pk_name="memory_id",
                pk_value=memory_id,
                asset_type="memory",
            )
            if str(old_payload.get("title") or "").strip() != str(final_payload.get("title") or "").strip():
                continue
            if (
                str(candidate_row.get("subtype") or "") == "workflow_fact"
                and str(old_payload.get("workflow_id") or "").strip() != expected_workflow_id
            ):
                continue
            now = float(superseded_at if superseded_at is not None else _now())
            history_boundary = float(
                validity_boundary if validity_boundary is not None else now
            )
            self._preserve_memory_version_locked(
                row,
                old_payload,
                invalid_at=history_boundary,
            )
            self.conn.execute(
                "UPDATE learning_chunks SET status = 'archived', updated_at = ? "
                "WHERE asset_type = 'memory' AND asset_id = ? AND status = 'active'",
                (now, memory_id),
            )
            self.conn.execute(
                "UPDATE memories SET status = 'archived', updated_at = ? WHERE memory_id = ?",
                (now, memory_id),
            )
            archived = self.conn.execute(
                "SELECT * FROM memories WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            if archived is not None:
                archived_memory = self._memory_from_row(archived, include_metadata=True)
                self._queue_memory_sync_locked("memory_revoke", archived_memory)
            superseded.append(memory_id)
        return superseded

    def _set_candidate_status(self, candidate_id: str, status: str) -> None:
        self.conn.execute(
            """
            UPDATE reflection_candidates
            SET status = ?, updated_at = ?
            WHERE candidate_id = ?
            """,
            (status, _now(), candidate_id),
        )

    def _insert_memory_asset(self, candidate_row: dict[str, Any], payload: dict[str, Any]) -> str:
        asset_id = f"mem_{candidate_row['candidate_id']}"
        now = _now()
        memory_text = f"{payload.get('title') or ''}\n{payload.get('content') or ''}"
        if contains_sensitive_memory(memory_text) or contains_untrusted_memory_instruction(memory_text):
            raise ValueError("memory content contains sensitive or privileged data")
        scope = str(payload.get("scope") or "global")
        project_id = str(payload.get("project_id") or "") or None
        valid_from = _normalise_optional_timestamp(payload.get("valid_from"), field="valid_from")
        invalid_at = _normalise_optional_timestamp(payload.get("invalid_at"), field="invalid_at")
        _validate_memory_validity(valid_from, invalid_at)
        stored_payload = dict(payload)
        stored_payload.pop("valid_from", None)
        stored_payload.pop("invalid_at", None)
        nonce, ciphertext = self._encrypt_payload(
            stored_payload,
            table="memories",
            pk_name="memory_id",
            pk_value=asset_id,
            asset_type="memory",
        )
        self.conn.execute(
            """
            INSERT INTO memories (
                memory_id, subtype, status, scope, project_id, source_candidate_id,
                created_at, updated_at, valid_from, invalid_at,
                payload_nonce, payload_ciphertext
            )
            VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                asset_id,
                candidate_row["subtype"],
                scope,
                project_id,
                candidate_row["candidate_id"],
                now,
                now,
                valid_from,
                invalid_at,
                nonce,
                ciphertext,
            ),
        )
        return asset_id

    def _insert_skill_asset(self, candidate_row: dict[str, Any], payload: dict[str, Any]) -> str:
        asset_id = f"skill_{candidate_row['candidate_id']}"
        now = _now()
        project_id = str(payload.get("project_id") or "").strip() or None
        scope = "project" if project_id else "global"
        payload["scope"] = scope
        payload["project_id"] = project_id
        payload["skill_id"] = asset_id
        nonce, ciphertext = self._encrypt_payload(
            payload,
            table="skills",
            pk_name="skill_id",
            pk_value=asset_id,
            asset_type="skill",
        )
        self.conn.execute(
            """
            INSERT INTO skills (
                skill_id, category, status, version, scope, project_id, source_candidate_id,
                created_at, updated_at, payload_nonce, payload_ciphertext
            )
            VALUES (?, ?, 'active', 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                asset_id,
                candidate_row["subtype"],
                scope,
                project_id,
                candidate_row["candidate_id"],
                now,
                now,
                nonce,
                ciphertext,
            ),
        )
        return asset_id

    def _insert_learning_chunk_asset(self, asset_id: str, asset_type: str, payload: dict[str, Any]) -> str:
        chunk_id = f"chunk_{asset_id}"
        now = _now()
        valid_from = (
            _normalise_optional_timestamp(payload.get("valid_from"), field="valid_from")
            if asset_type == "memory"
            else None
        )
        invalid_at = (
            _normalise_optional_timestamp(payload.get("invalid_at"), field="invalid_at")
            if asset_type == "memory"
            else None
        )
        _validate_memory_validity(valid_from, invalid_at)
        chunk_payload = {
            **payload,
            "asset_id": asset_id,
            "asset_type": asset_type,
            "text": _chunk_search_text(payload),
        }
        nonce, ciphertext = self._encrypt_payload(
            chunk_payload,
            table="learning_chunks",
            pk_name="chunk_id",
            pk_value=chunk_id,
            asset_type=asset_type,
        )
        self.conn.execute(
            """
            INSERT INTO learning_chunks (
                chunk_id, asset_id, asset_type, subtype, status,
                scope, project_id, text_digest, embedding_json, created_at, updated_at,
                valid_from, invalid_at, payload_nonce, payload_ciphertext
            )
            VALUES (?, ?, ?, ?, 'active', ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                chunk_id,
                asset_id,
                asset_type,
                payload.get("subtype") or "unknown",
                payload.get("scope") or "global",
                payload.get("project_id"),
                _text_digest(_chunk_search_text(payload)),
                now,
                now,
                valid_from,
                invalid_at,
                nonce,
                ciphertext,
            ),
        )
        return chunk_id


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))


def _normalize_retrieval_feedback_query(value: str | None) -> str:
    return " ".join(str(value or "").strip().split())[:6000]


def _normalise_retrieval_feedback_workflow(value: str | None) -> str | None:
    workflow_id = str(value or "").strip() or None
    if workflow_id and workflow_id not in SUPPORTED_WORKFLOW_IDS:
        raise ValueError("unsupported retrieval feedback workflow")
    return workflow_id


def _retrieval_feedback_digest(
    key: bytes,
    query: str,
    *,
    workflow_id: str | None = None,
) -> str:
    if workflow_id:
        # Keep generic feedback digests backward-compatible while giving each
        # supported workflow its own durable identity in the legacy UNIQUE key.
        query = f"{query}\nworkflow_id={workflow_id}"
    return hmac.new(key, query.casefold().encode("utf-8"), hashlib.sha256).hexdigest()


def _payload_aad(table: str, pk_name: str, pk_value: str, *, asset_type: str | None = None) -> bytes:
    if not table or not pk_name or not pk_value:
        raise ValueError("learning payload table, pk_name, and pk_value are required")
    payload = {
        "magic": LEARNING_AAD_MAGIC,
        "version": LEARNING_AAD_VERSION,
        "table": table,
        "pk_name": pk_name,
        "pk_value": pk_value,
        "asset_type": asset_type,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _now() -> float:
    return time.time()


def _normalise_optional_timestamp(value: Any, *, field: str) -> float | None:
    if value is None:
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite Unix timestamp") from exc
    if not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError(f"{field} must be a finite Unix timestamp")
    return timestamp


def _normalise_timestamp(value: Any, *, field: str) -> float:
    timestamp = _normalise_optional_timestamp(value, field=field)
    if timestamp is None:
        raise ValueError(f"{field} is required")
    return timestamp


def _validate_memory_validity(valid_from: float | None, invalid_at: float | None) -> None:
    if valid_from is not None and invalid_at is not None and invalid_at <= valid_from:
        raise ValueError("invalid_at must be greater than valid_from")


def _normalise_memory_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _chunk_search_text(payload: dict[str, Any]) -> str:
    parts = [
        str(payload.get("title") or ""),
        str(payload.get("content") or ""),
        str(payload.get("text") or ""),
        str(payload.get("rationale") or ""),
        str(payload.get("intent") or ""),
        str(payload.get("applicability") or ""),
        str(payload.get("output_contract") or ""),
    ]
    parts.extend(str(item) for item in payload.get("tags") or [])
    parts.extend(str(item) for item in payload.get("triggers") or [])
    parts.extend(str(item) for item in payload.get("steps") or [])
    parts.extend(str(item) for item in payload.get("success_criteria") or [])
    parts.extend(str(item) for item in payload.get("failure_modes") or [])
    return " ".join(part for part in parts if part).strip()


def _text_digest(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _search_terms(query: str) -> set[str]:
    normalized = " ".join(str(query or "").lower().split())
    terms = {token for token in normalized.split() if len(token) >= 2}
    for size in (2, 3, 4):
        for index in range(0, max(0, len(normalized) - size + 1)):
            chunk = normalized[index:index + size].strip()
            if len(chunk) == size and any("\u4e00" <= char <= "\u9fff" for char in chunk):
                terms.add(chunk)
    return terms


def _keyword_score(terms: set[str], text: str) -> float:
    if not terms:
        return 0.0
    normalized = str(text or "").lower()
    matches = sum(1 for term in terms if term in normalized)
    return matches / len(terms)
