import base64
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.skill_runtime import normalize_skill_payload


LEARNING_AAD_MAGIC = "SoulDrive learning store"
LEARNING_AAD_VERSION = 1


class LearningStore:
    def __init__(self, workspace, keys):
        self.workspace = workspace
        self.keys = keys
        Path(workspace.learning_store_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(workspace.learning_store_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def close(self):
        self.conn.close()

    def _init_schema(self):
        cursor = self.conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS reflection_reviews (
                review_id TEXT PRIMARY KEY,
                trigger TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'discarded')),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                candidate_count INTEGER NOT NULL CHECK (candidate_count >= 0),
                accepted_count INTEGER NOT NULL CHECK (accepted_count >= 0),
                rejected_count INTEGER NOT NULL CHECK (rejected_count >= 0),
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
            CREATE TABLE IF NOT EXISTS skills (
                skill_id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
                version INTEGER NOT NULL CHECK (version >= 1),
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
                text_digest TEXT NOT NULL,
                embedding_json TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                payload_nonce TEXT NOT NULL,
                payload_ciphertext TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS skill_runs (
                run_id TEXT PRIMARY KEY,
                skill_id TEXT NOT NULL REFERENCES skills(skill_id),
                status TEXT NOT NULL CHECK (status IN ('planned', 'completed', 'failed')),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                payload_nonce TEXT NOT NULL,
                payload_ciphertext TEXT NOT NULL
            )
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
        self.conn.commit()

    def list_pending_reviews(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM reflection_reviews WHERE status = 'pending' ORDER BY created_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def list_memories(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE status = 'active' ORDER BY updated_at DESC"
        ).fetchall()
        return [self._memory_from_row(row) for row in rows]

    def list_skills(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM skills WHERE status = 'active' ORDER BY updated_at DESC"
        ).fetchall()
        return [self._skill_from_row(row) for row in rows]

    def list_skill_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        rows = self.conn.execute(
            """
            SELECT *
            FROM skill_runs
            ORDER BY created_at DESC, run_id ASC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
        return [self._skill_run_from_row(row) for row in rows]

    def create_review(self, trigger: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        if trigger not in {"manual", "close"}:
            raise ValueError("unsupported reflection trigger")
        now = _now()
        review_id = f"rev_{os.urandom(8).hex()}"
        stored_candidates: list[dict[str, Any]] = []
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO reflection_reviews (
                    review_id, trigger, status, created_at, updated_at,
                    candidate_count, accepted_count, rejected_count
                )
                VALUES (?, ?, 'pending', ?, ?, 0, 0, 0)
                """,
                (review_id, trigger, now, now),
            )
            for candidate in candidates:
                stored_candidates.append(self._store_candidate(review_id, candidate, now))
        review = self._review_record(review_id)
        review["candidates"] = stored_candidates
        return review

    def decide_review(self, review_id: str, decisions: list[dict[str, Any]]) -> dict[str, Any]:
        review = self._review_record(review_id)
        if review is None:
            raise KeyError(f"unknown review: {review_id}")

        accepted = 0
        rejected = 0
        with self.conn:
            for decision in decisions:
                candidate_id = str(decision.get("candidate_id") or "").strip()
                action = str(decision.get("action") or "").strip()
                if not candidate_id:
                    raise ValueError("candidate_id is required")
                candidate = self._candidate_record(candidate_id, review_id)
                if candidate is None:
                    raise KeyError(f"unknown candidate: {candidate_id}")

                if action == "accept":
                    self._set_candidate_status(candidate_id, "accepted")
                    final_payload = self._candidate_final_payload(candidate, decision)
                    if candidate["asset_type"] == "memory":
                        asset_id = self._insert_memory_asset(candidate, final_payload)
                    elif candidate["asset_type"] == "skill":
                        asset_id = self._insert_skill_asset(candidate, final_payload)
                    else:
                        raise ValueError("unsupported candidate asset type")
                    self._insert_learning_chunk_asset(asset_id, candidate["asset_type"], final_payload)
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
                SET status = ?, updated_at = ?
                WHERE review_id = ?
                """,
                (review_status, _now(), review_id),
            )

        result = self._review_record(review_id)
        if result is None:
            raise KeyError(f"unknown review: {review_id}")
        result["accepted_count"] = accepted if decisions else result["accepted_count"]
        result["rejected_count"] = rejected if decisions else result["rejected_count"]
        return result

    def search(self, query: str, top_k_memory: int = 3, top_k_skill: int = 2) -> dict[str, list[dict[str, Any]]]:
        terms = _search_terms(query)
        rows = self.conn.execute(
            """
            SELECT chunk_id, asset_id, asset_type, subtype, status, created_at, updated_at, payload_nonce, payload_ciphertext
            FROM learning_chunks
            WHERE status = 'active'
            ORDER BY updated_at DESC, chunk_id ASC
            """
        ).fetchall()

        scored: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
        for row in rows:
            payload = self._decrypt_payload(
                row["payload_nonce"],
                row["payload_ciphertext"],
                table="learning_chunks",
                pk_name="chunk_id",
                pk_value=row["chunk_id"],
                asset_type=row["asset_type"],
            )
            searchable_text = _chunk_search_text(payload)
            score = _keyword_score(terms, searchable_text)
            if score <= 0:
                continue
            scored.append((score, dict(row), payload))

        scored.sort(key=lambda item: (-item[0], item[1]["updated_at"], item[1]["chunk_id"]))
        memories: list[dict[str, Any]] = []
        skills: list[dict[str, Any]] = []
        for score, row, payload in scored:
            record = {
                "asset_id": row["asset_id"],
                "asset_type": row["asset_type"],
                "subtype": row["subtype"],
                "status": row["status"],
                "title": payload.get("title") or "",
                "content": payload.get("content") or payload.get("text") or "",
                "score": score,
                "updated_at": row["updated_at"],
            }
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
                    }
                )
                skills.append(record)

        return {"memories": memories, "skills": skills}

    def record_skill_run(
        self,
        skill_id: str,
        *,
        query: str,
        plan: dict[str, Any],
        matched_score: float = 0.0,
        inputs: dict[str, Any] | None = None,
        status: str = "planned",
    ) -> dict[str, Any]:
        if status not in {"planned", "completed", "failed"}:
            raise ValueError("unsupported skill run status")
        skill = self.conn.execute(
            "SELECT skill_id FROM skills WHERE skill_id = ? AND status = 'active'",
            (skill_id,),
        ).fetchone()
        if skill is None:
            raise KeyError(f"unknown active skill: {skill_id}")

        now = _now()
        run_id = f"run_{os.urandom(8).hex()}"
        payload = {
            "query": str(query or ""),
            "matched_score": float(matched_score),
            "plan": dict(plan or {}),
            "inputs": dict(inputs or {}),
            "outcome": "",
            "failure_reason": "",
        }
        nonce, ciphertext = self._encrypt_payload(
            payload,
            table="skill_runs",
            pk_name="run_id",
            pk_value=run_id,
            asset_type="skill",
        )
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO skill_runs (
                    run_id, skill_id, status, created_at, updated_at,
                    payload_nonce, payload_ciphertext
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, skill_id, status, now, now, nonce, ciphertext),
            )
        return {
            "run_id": run_id,
            "skill_id": skill_id,
            "status": status,
            "created_at": now,
            "updated_at": now,
            **payload,
        }

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

    def _memory_from_row(self, row) -> dict[str, Any]:
        payload = self._decrypt_payload(
            row["payload_nonce"],
            row["payload_ciphertext"],
            table="memories",
            pk_name="memory_id",
            pk_value=row["memory_id"],
            asset_type="memory",
        )
        return {
            **payload,
            "memory_id": row["memory_id"],
            "subtype": row["subtype"],
            "status": row["status"],
            "source_candidate_id": row["source_candidate_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

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
            "category": row["category"],
            "status": row["status"],
            "version": row["version"],
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
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _review_record(self, review_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT review_id, trigger, status, created_at, updated_at,
                   candidate_count, accepted_count, rejected_count
            FROM reflection_reviews
            WHERE review_id = ?
            """,
            (review_id,),
        ).fetchone()
        return dict(row) if row is not None else None

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
        if candidate.get("write_gate"):
            payload["write_gate"] = dict(candidate["write_gate"])
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
            ):
                if key in candidate:
                    payload[key] = candidate[key]
        return payload

    def _candidate_final_payload(self, candidate_row: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
        payload = self._candidate_payload_from_row(candidate_row)
        payload["subtype"] = candidate_row["subtype"]
        edited_title = decision.get("edited_title")
        edited_content = decision.get("edited_content")
        if edited_title is not None:
            payload["title"] = str(edited_title).strip()
        if edited_content is not None:
            payload["content"] = str(edited_content).strip()
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
        nonce, ciphertext = self._encrypt_payload(
            payload,
            table="memories",
            pk_name="memory_id",
            pk_value=asset_id,
            asset_type="memory",
        )
        self.conn.execute(
            """
            INSERT INTO memories (
                memory_id, subtype, status, source_candidate_id,
                created_at, updated_at, payload_nonce, payload_ciphertext
            )
            VALUES (?, ?, 'active', ?, ?, ?, ?, ?)
            """,
            (
                asset_id,
                candidate_row["subtype"],
                candidate_row["candidate_id"],
                now,
                now,
                nonce,
                ciphertext,
            ),
        )
        return asset_id

    def _insert_skill_asset(self, candidate_row: dict[str, Any], payload: dict[str, Any]) -> str:
        asset_id = f"skill_{candidate_row['candidate_id']}"
        now = _now()
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
                skill_id, category, status, version, source_candidate_id,
                created_at, updated_at, payload_nonce, payload_ciphertext
            )
            VALUES (?, ?, 'active', 1, ?, ?, ?, ?, ?)
            """,
            (
                asset_id,
                candidate_row["subtype"],
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
                text_digest, embedding_json, created_at, updated_at,
                payload_nonce, payload_ciphertext
            )
            VALUES (?, ?, ?, ?, 'active', ?, NULL, ?, ?, ?, ?)
            """,
            (
                chunk_id,
                asset_id,
                asset_type,
                payload.get("subtype") or "unknown",
                _text_digest(_chunk_search_text(payload)),
                now,
                now,
                nonce,
                ciphertext,
            ),
        )
        return chunk_id


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))


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
