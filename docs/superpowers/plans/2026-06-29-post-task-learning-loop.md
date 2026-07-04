# Post-Task Learning Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an audited post-task learning loop where approved memory/skill assets are persisted in the unlocked workspace, indexed, retrieved during future chat, injected into RAG context, and measured with a repeatable learning-loop evaluation.

**Architecture:** Add a focused learning subsystem beside the existing workspace, audit, RAG, and UI code. The backend owns encrypted persistence, candidate review, learning search, RAG injection, and eval; the frontend owns manual/close-time review UX. Stage 2 scheduler/self-improvement fields are reserved but not executed in this branch.

**Tech Stack:** Python `unittest`, FastAPI/Pydantic, SQLite, AESGCM via existing workspace keys, existing SoulDrive workspace/security/audit modules, React + Tauri v2, TypeScript, Vite.

**Commit Policy:** Do not create git commits in this plan. Project instructions explicitly prohibit commits unless requested. Each task ends with a scope/status check instead of a commit.

---

## File Structure

Create:

- `core/learning_store.py`: encrypted SQLite persistence for review queue, approved memories, approved skills, learning chunks, and search.
- `core/reflection.py`: transcript-to-candidate prompt/validation helpers, deterministic fallback extractor, and JSON parsing.
- `scripts/eval_learning_loop.py`: repeatable learning-loop evaluation runner.
- `eval/learning_loop_v0.1.jsonl`: baseline dataset for memory/skill recall and answer behavior.
- `tests/test_learning_store.py`: storage, accept/reject, search, and encryption behavior.
- `tests/test_reflection.py`: candidate extraction, validation, schema, and no-evidence rejection.
- `tests/test_learning_eval.py`: eval fixture and metric calculations.
- `souldrive-ui/scripts/learning-review.test.mjs`: frontend static/regression checks for review UI wiring.

Modify:

- `core/workspace.py`: add `learning_store_path`.
- `core/mcp_server.py`: add reflection/learning API routes and learning search before chat generation.
- `core/rag_engine.py`: accept optional learning context and inject it into prompt without treating it as document evidence.
- `core/tool_registry.py`: optionally expose `souldrive.learning_search` for internal diagnostics if needed.
- `tests/test_rag_prompting.py`: verify learning context prompt rules.
- `tests/test_mcp_server_security.py`: verify locked workspace rejects learning endpoints.
- `tests/test_mcp_server_papers.py` or a new API test file: verify reflection endpoints through TestClient.
- `souldrive-ui/src/App.tsx`: add learning review state, manual memory button, review modal, close-time draft/decision flow.
- `souldrive-ui/src/App.css`: add review modal and learning chips.
- `souldrive-ui/src-tauri/capabilities/default.json`: add window close permission if required by Tauri v2 API.
- `souldrive-ui/package.json`: add a learning review test script only if needed.

---

## Task 1: Workspace Path and Learning Store Persistence

**Files:**

- Modify: `core/workspace.py`
- Create: `core/learning_store.py`
- Create: `tests/test_learning_store.py`

- [ ] **Step 1: Write failing tests for workspace path and empty store**

Add `tests/test_learning_store.py`:

```python
import tempfile
import unittest

from core.learning_store import LearningStore
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


class LearningStoreTests(unittest.TestCase):
    def _store(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        workspace = SoulDriveWorkspace.from_drive(temp_dir.name).ensure()
        initialize_keystore(workspace, "correct horse battery staple")
        keys = unlock_keystore(workspace, "correct horse battery staple")
        return workspace, LearningStore(workspace, keys)

    def test_workspace_exposes_learning_store_path(self):
        workspace, _store = self._store()
        self.assertTrue(workspace.learning_store_path.endswith("data/learning.sqlite"))

    def test_new_store_lists_no_pending_reviews_or_assets(self):
        _workspace, store = self._store()
        self.assertEqual(store.list_pending_reviews(), [])
        self.assertEqual(store.list_memories(), [])
        self.assertEqual(store.list_skills(), [])
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_learning_store -v
```

Expected: fail because `core.learning_store` and `workspace.learning_store_path` do not exist.

- [ ] **Step 3: Add workspace path**

Modify `core/workspace.py` inside `SoulDriveWorkspace`:

```python
    @property
    def learning_store_path(self):
        return str(Path(self.root_path) / "data" / "learning.sqlite")
```

No new directory is needed here because `documents_path` and `papers_path` already ensure `data/` exists through `ensure()`.

- [ ] **Step 4: Implement minimal LearningStore schema**

Create `core/learning_store.py`:

```python
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


LEARNING_AAD = b"SoulDrive learning store v1"


class LearningStore:
    def __init__(self, workspace, keys):
        self.workspace = workspace
        self.keys = keys
        Path(workspace.learning_store_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(workspace.learning_store_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self):
        self.conn.close()

    def _init_schema(self):
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reflection_reviews (
                review_id TEXT PRIMARY KEY,
                trigger TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                candidate_count INTEGER NOT NULL,
                accepted_count INTEGER NOT NULL,
                rejected_count INTEGER NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reflection_candidates (
                candidate_id TEXT PRIMARY KEY,
                review_id TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                subtype TEXT NOT NULL,
                status TEXT NOT NULL,
                confidence REAL NOT NULL,
                selected_by_default INTEGER NOT NULL,
                duplicate_of TEXT,
                source_message_indexes TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                payload_nonce TEXT NOT NULL,
                payload_ciphertext TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                memory_id TEXT PRIMARY KEY,
                subtype TEXT NOT NULL,
                status TEXT NOT NULL,
                source_candidate_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                payload_nonce TEXT NOT NULL,
                payload_ciphertext TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS skills (
                skill_id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                status TEXT NOT NULL,
                version INTEGER NOT NULL,
                source_candidate_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                payload_nonce TEXT NOT NULL,
                payload_ciphertext TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS learning_chunks (
                chunk_id TEXT PRIMARY KEY,
                asset_id TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                subtype TEXT NOT NULL,
                status TEXT NOT NULL,
                text_digest TEXT NOT NULL,
                embedding_json TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                payload_nonce TEXT NOT NULL,
                payload_ciphertext TEXT NOT NULL
            )
        """)
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

    def _encrypt_payload(self, payload: dict[str, Any]) -> tuple[str, str]:
        nonce = os.urandom(12)
        plaintext = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ciphertext = AESGCM(self.keys.metadata_key).encrypt(nonce, plaintext, LEARNING_AAD)
        return _b64(nonce), _b64(ciphertext)

    def _decrypt_payload(self, nonce: str, ciphertext: str) -> dict[str, Any]:
        try:
            plaintext = AESGCM(self.keys.metadata_key).decrypt(_unb64(nonce), _unb64(ciphertext), LEARNING_AAD)
        except (InvalidTag, ValueError, KeyError, TypeError) as exc:
            raise ValueError("learning payload authentication failed") from exc
        return json.loads(plaintext.decode("utf-8"))

    def _memory_from_row(self, row) -> dict[str, Any]:
        payload = self._decrypt_payload(row["payload_nonce"], row["payload_ciphertext"])
        return {
            "memory_id": row["memory_id"],
            "subtype": row["subtype"],
            "status": row["status"],
            "source_candidate_id": row["source_candidate_id"],
            **payload,
        }

    def _skill_from_row(self, row) -> dict[str, Any]:
        payload = self._decrypt_payload(row["payload_nonce"], row["payload_ciphertext"])
        return {
            "skill_id": row["skill_id"],
            "category": row["category"],
            "status": row["status"],
            "version": row["version"],
            "source_candidate_id": row["source_candidate_id"],
            **payload,
        }


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))
```

- [ ] **Step 5: Run tests to verify pass**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_learning_store -v
```

Expected: pass.

- [ ] **Step 6: Check change scope**

Run:

```powershell
git status --short
```

Expected: only `core/workspace.py`, `core/learning_store.py`, and `tests/test_learning_store.py` are changed/created, plus previously approved docs.

---

## Task 2: Review Queue, Accept/Reject, and Encrypted Asset Creation

**Files:**

- Modify: `core/learning_store.py`
- Modify: `tests/test_learning_store.py`

- [ ] **Step 1: Add failing tests for review lifecycle**

Append to `LearningStoreTests`:

```python
    def test_create_review_stores_pending_candidates_without_assets(self):
        _workspace, store = self._store()
        review = store.create_review(
            trigger="manual",
            candidates=[
                {
                    "asset_type": "memory",
                    "subtype": "user_preference",
                    "title": "关闭前审核",
                    "content": "用户希望关闭前集中审核候选记忆。",
                    "evidence": ["用户明确说明不要每轮弹窗。"],
                    "rationale": "这是长期交互偏好。",
                    "confidence": 0.91,
                    "selected_by_default": True,
                    "source_message_indexes": [0],
                    "tags": ["memory"],
                }
            ],
        )

        self.assertEqual(review["status"], "pending")
        self.assertEqual(len(review["candidates"]), 1)
        self.assertEqual(store.list_memories(), [])

    def test_accept_candidate_creates_memory_and_learning_chunk(self):
        _workspace, store = self._store()
        review = store.create_review(
            trigger="manual",
            candidates=[
                {
                    "asset_type": "memory",
                    "subtype": "user_preference",
                    "title": "关闭前审核",
                    "content": "用户希望关闭前集中审核候选记忆。",
                    "evidence": ["用户明确说明不要每轮弹窗。"],
                    "rationale": "这是长期交互偏好。",
                    "confidence": 0.91,
                    "selected_by_default": True,
                    "source_message_indexes": [0],
                    "tags": ["memory"],
                }
            ],
        )
        candidate_id = review["candidates"][0]["candidate_id"]

        result = store.decide_review(
            review["review_id"],
            [{"candidate_id": candidate_id, "action": "accept", "edited_title": "关闭前集中审核", "edited_content": "关闭前集中审核候选记忆。"}],
        )

        self.assertEqual(result["status"], "completed")
        memories = store.list_memories()
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["title"], "关闭前集中审核")
        self.assertEqual(memories[0]["content"], "关闭前集中审核候选记忆。")
        self.assertEqual(len(store.search("什么时候保存记忆", top_k_memory=3, top_k_skill=2)["memories"]), 1)

    def test_reject_candidate_does_not_create_asset(self):
        _workspace, store = self._store()
        review = store.create_review(
            trigger="manual",
            candidates=[
                {
                    "asset_type": "skill",
                    "subtype": "sop",
                    "title": "实现前先写测试",
                    "content": "先写失败测试，再实现。",
                    "evidence": ["任务中反复强调 TDD。"],
                    "rationale": "这是可复用流程。",
                    "confidence": 0.8,
                    "selected_by_default": True,
                    "source_message_indexes": [1],
                    "tags": ["skill"],
                }
            ],
        )
        candidate_id = review["candidates"][0]["candidate_id"]

        store.decide_review(review["review_id"], [{"candidate_id": candidate_id, "action": "reject"}])

        self.assertEqual(store.list_skills(), [])
        self.assertEqual(store.search("实现前怎么做", top_k_memory=3, top_k_skill=2)["skills"], [])
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_learning_store -v
```

Expected: fail because `create_review`, `decide_review`, and `search` are missing.

- [ ] **Step 3: Implement review creation**

Add to `LearningStore`:

```python
    def create_review(self, trigger: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        if trigger not in ("manual", "close"):
            raise ValueError("unsupported reflection trigger")
        now = time.time()
        review_id = f"rev_{uuid.uuid4().hex}"
        self.conn.execute(
            """
            INSERT INTO reflection_reviews (
                review_id, trigger, status, created_at, updated_at,
                candidate_count, accepted_count, rejected_count
            )
            VALUES (?, ?, 'pending', ?, ?, ?, 0, 0)
            """,
            (review_id, trigger, now, now, len(candidates)),
        )
        stored_candidates = []
        for candidate in candidates:
            stored_candidates.append(self._insert_candidate(review_id, candidate, now))
        self.conn.commit()
        return {
            "review_id": review_id,
            "trigger": trigger,
            "status": "pending",
            "candidates": stored_candidates,
        }

    def _insert_candidate(self, review_id: str, candidate: dict[str, Any], now: float) -> dict[str, Any]:
        candidate_id = f"cand_{uuid.uuid4().hex}"
        source_indexes = candidate.get("source_message_indexes") or []
        payload = {
            "title": str(candidate.get("title") or "").strip(),
            "content": str(candidate.get("content") or "").strip(),
            "evidence": list(candidate.get("evidence") or []),
            "rationale": str(candidate.get("rationale") or "").strip(),
            "tags": list(candidate.get("tags") or []),
            "source_excerpt": str(candidate.get("source_excerpt") or "").strip(),
        }
        nonce, ciphertext = self._encrypt_payload(payload)
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
                candidate["asset_type"],
                candidate["subtype"],
                float(candidate.get("confidence") or 0),
                1 if candidate.get("selected_by_default") else 0,
                candidate.get("duplicate_of"),
                json.dumps(source_indexes),
                now,
                now,
                nonce,
                ciphertext,
            ),
        )
        return {
            "candidate_id": candidate_id,
            "asset_type": candidate["asset_type"],
            "subtype": candidate["subtype"],
            "confidence": float(candidate.get("confidence") or 0),
            "selected_by_default": bool(candidate.get("selected_by_default")),
            **payload,
        }
```

- [ ] **Step 4: Implement decision handling and chunk creation**

Add to `LearningStore`:

```python
    def decide_review(self, review_id: str, decisions: list[dict[str, Any]]) -> dict[str, Any]:
        review = self.conn.execute(
            "SELECT * FROM reflection_reviews WHERE review_id = ?",
            (review_id,),
        ).fetchone()
        if review is None:
            raise KeyError(f"unknown review: {review_id}")
        accepted = 0
        rejected = 0
        for decision in decisions:
            action = decision.get("action")
            candidate_id = str(decision.get("candidate_id") or "")
            candidate = self._candidate_row(candidate_id, review_id)
            if action == "accept":
                self._accept_candidate(candidate, decision)
                accepted += 1
            elif action == "reject":
                self._set_candidate_status(candidate_id, "rejected")
                rejected += 1
            else:
                raise ValueError("unsupported review decision")
        now = time.time()
        status = "completed" if accepted or rejected else "discarded"
        self.conn.execute(
            """
            UPDATE reflection_reviews
            SET status = ?, updated_at = ?, accepted_count = ?, rejected_count = ?
            WHERE review_id = ?
            """,
            (status, now, accepted, rejected, review_id),
        )
        self.conn.commit()
        return {
            "review_id": review_id,
            "status": status,
            "accepted_count": accepted,
            "rejected_count": rejected,
        }

    def _candidate_row(self, candidate_id: str, review_id: str):
        row = self.conn.execute(
            "SELECT * FROM reflection_candidates WHERE candidate_id = ? AND review_id = ?",
            (candidate_id, review_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown candidate: {candidate_id}")
        return row

    def _set_candidate_status(self, candidate_id: str, status: str):
        self.conn.execute(
            "UPDATE reflection_candidates SET status = ?, updated_at = ? WHERE candidate_id = ?",
            (status, time.time(), candidate_id),
        )

    def _accept_candidate(self, candidate, decision: dict[str, Any]):
        payload = self._decrypt_payload(candidate["payload_nonce"], candidate["payload_ciphertext"])
        title = str(decision.get("edited_title") or payload["title"]).strip()
        content = str(decision.get("edited_content") or payload["content"]).strip()
        final_payload = {
            **payload,
            "title": title,
            "content": content,
        }
        now = time.time()
        if candidate["asset_type"] == "memory":
            asset_id = f"mem_{uuid.uuid4().hex}"
            nonce, ciphertext = self._encrypt_payload(final_payload)
            self.conn.execute(
                """
                INSERT INTO memories (
                    memory_id, subtype, status, source_candidate_id,
                    created_at, updated_at, payload_nonce, payload_ciphertext
                )
                VALUES (?, ?, 'active', ?, ?, ?, ?, ?)
                """,
                (asset_id, candidate["subtype"], candidate["candidate_id"], now, now, nonce, ciphertext),
            )
        elif candidate["asset_type"] == "skill":
            asset_id = f"skill_{uuid.uuid4().hex}"
            nonce, ciphertext = self._encrypt_payload(final_payload)
            self.conn.execute(
                """
                INSERT INTO skills (
                    skill_id, category, status, version, source_candidate_id,
                    created_at, updated_at, payload_nonce, payload_ciphertext
                )
                VALUES (?, ?, 'active', 1, ?, ?, ?, ?, ?)
                """,
                (asset_id, candidate["subtype"], candidate["candidate_id"], now, now, nonce, ciphertext),
            )
        else:
            raise ValueError("unsupported candidate asset type")
        self._insert_learning_chunk(asset_id, candidate["asset_type"], candidate["subtype"], final_payload, now)
        self._set_candidate_status(candidate["candidate_id"], "accepted")

    def _insert_learning_chunk(self, asset_id: str, asset_type: str, subtype: str, payload: dict[str, Any], now: float):
        text = self._chunk_text(payload)
        nonce, ciphertext = self._encrypt_payload({"text": text, **payload})
        self.conn.execute(
            """
            INSERT INTO learning_chunks (
                chunk_id, asset_id, asset_type, subtype, status, text_digest,
                embedding_json, created_at, updated_at, payload_nonce, payload_ciphertext
            )
            VALUES (?, ?, ?, ?, 'active', ?, NULL, ?, ?, ?, ?)
            """,
            (
                f"chunk_{uuid.uuid4().hex}",
                asset_id,
                asset_type,
                subtype,
                self._text_digest(text),
                now,
                now,
                nonce,
                ciphertext,
            ),
        )

    def _chunk_text(self, payload: dict[str, Any]) -> str:
        parts = [payload.get("title") or "", payload.get("content") or ""]
        parts.extend(str(item) for item in payload.get("tags") or [])
        return " ".join(part for part in parts if part).strip()

    def _text_digest(self, text: str) -> str:
        return hmac.new(self.keys.metadata_key, text.encode("utf-8"), hashlib.sha256).hexdigest()
```

- [ ] **Step 5: Implement keyword search**

Add to `LearningStore`:

```python
    def search(self, query: str, top_k_memory: int = 3, top_k_skill: int = 2) -> dict[str, list[dict[str, Any]]]:
        terms = _search_terms(query)
        rows = self.conn.execute(
            "SELECT * FROM learning_chunks WHERE status = 'active'"
        ).fetchall()
        scored = []
        for row in rows:
            payload = self._decrypt_payload(row["payload_nonce"], row["payload_ciphertext"])
            text = payload.get("text") or ""
            score = _keyword_score(terms, text)
            if score <= 0:
                continue
            scored.append((score, row, payload))
        scored.sort(key=lambda item: item[0], reverse=True)
        memories = []
        skills = []
        for score, row, payload in scored:
            item = {
                "asset_id": row["asset_id"],
                "asset_type": row["asset_type"],
                "subtype": row["subtype"],
                "title": payload.get("title") or "",
                "content": payload.get("content") or "",
                "score": score,
            }
            if row["asset_type"] == "memory" and len(memories) < top_k_memory:
                memories.append(item)
            if row["asset_type"] == "skill" and len(skills) < top_k_skill:
                skills.append(item)
        return {"memories": memories, "skills": skills}


def _search_terms(query: str) -> set[str]:
    normalized = str(query or "").lower()
    terms = {token for token in normalized.replace("？", " ").replace("。", " ").split() if len(token) >= 2}
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
    return matches / max(1, len(terms))
```

- [ ] **Step 6: Run lifecycle tests**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_learning_store -v
```

Expected: pass.

- [ ] **Step 7: Check encrypted storage does not expose accepted text**

Append test:

```python
    def test_plaintext_asset_content_is_not_visible_in_sqlite_file(self):
        workspace, store = self._store()
        review = store.create_review(
            trigger="manual",
            candidates=[
                {
                    "asset_type": "memory",
                    "subtype": "user_preference",
                    "title": "敏感偏好",
                    "content": "VERY_PRIVATE_LEARNING_CONTENT",
                    "evidence": ["用户提供。"],
                    "rationale": "长期偏好。",
                    "confidence": 0.9,
                    "selected_by_default": True,
                    "source_message_indexes": [0],
                    "tags": [],
                }
            ],
        )
        store.decide_review(review["review_id"], [{"candidate_id": review["candidates"][0]["candidate_id"], "action": "accept"}])
        store.close()

        with open(workspace.learning_store_path, "rb") as file:
            raw = file.read()
        self.assertNotIn(b"VERY_PRIVATE_LEARNING_CONTENT", raw)
```

Run same test command. Expected: pass.

- [ ] **Step 8: Check change scope**

Run:

```powershell
git status --short
```

Expected: only learning store related files plus docs are changed.

---

## Task 3: Reflection Candidate Extraction and Validation

**Files:**

- Create: `core/reflection.py`
- Create: `tests/test_reflection.py`

- [ ] **Step 1: Write failing reflection tests**

Create `tests/test_reflection.py`:

```python
import unittest

from core.reflection import build_reflection_candidates, parse_reflection_response


class ReflectionTests(unittest.TestCase):
    def test_user_preference_candidate_requires_user_evidence(self):
        messages = [
            {"role": "user", "content": "我不希望每轮问答都弹窗保存记忆，希望关闭程序前集中审核。"},
            {"role": "ai", "content": "明白。"},
        ]

        candidates = build_reflection_candidates(messages)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["asset_type"], "memory")
        self.assertEqual(candidates[0]["subtype"], "user_preference")
        self.assertIn(0, candidates[0]["source_message_indexes"])

    def test_assistant_only_claim_does_not_create_memory(self):
        messages = [
            {"role": "ai", "content": "用户喜欢自动保存所有信息。"},
        ]

        self.assertEqual(build_reflection_candidates(messages), [])

    def test_parse_model_json_rejects_candidate_without_evidence(self):
        raw = '{"candidates":[{"asset_type":"memory","subtype":"user_preference","title":"偏好","content":"内容","evidence":[],"rationale":"理由","confidence":0.9,"source_message_indexes":[0]}]}'

        self.assertEqual(parse_reflection_response(raw, [{"role": "user", "content": "hello"}]), [])
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_reflection -v
```

Expected: fail because `core.reflection` does not exist.

- [ ] **Step 3: Implement reflection helpers**

Create `core/reflection.py`:

```python
import json
import re
from typing import Any


VALID_ASSET_TYPES = {"memory", "skill"}
VALID_MEMORY_SUBTYPES = {"user_preference", "project_fact", "workflow_fact"}
VALID_SKILL_SUBTYPES = {"sop", "troubleshooting", "template", "pitfall"}


def build_reflection_candidates(messages: list[dict[str, str]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for index, message in enumerate(messages or []):
        if message.get("role") != "user":
            continue
        content = str(message.get("content") or "").strip()
        if _looks_like_user_preference(content):
            candidates.append({
                "asset_type": "memory",
                "subtype": "user_preference",
                "title": _preference_title(content),
                "content": _compact(content),
                "evidence": [_compact(content, 160)],
                "rationale": "用户明确表达了长期交互偏好，后续回答应检索并遵循。",
                "confidence": 0.86,
                "selected_by_default": True,
                "source_message_indexes": [index],
                "tags": ["preference"],
            })
    return candidates


def parse_reflection_response(raw: str, messages: list[dict[str, str]]) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    items = parsed.get("candidates") if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        return []
    return [candidate for candidate in (_normalize_candidate(item, messages) for item in items) if candidate is not None]


def build_reflection_prompt(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    transcript = "\n".join(
        f"[{index}] {message.get('role')}: {message.get('content')}"
        for index, message in enumerate(messages or [])
    )
    system = (
        "你是 SoulDrive 的任务后反思器。只能从给定 transcript 中提取可长期复用的候选 memory 或 skill。"
        "不要推断 transcript 没有明确支持的信息。"
        "用户偏好和项目事实必须来自 user 消息。"
        "输出 JSON，格式为 {\"candidates\": [...]}。"
    )
    user = (
        "请提取候选长期资产。字段包括 asset_type, subtype, title, content, evidence, rationale, "
        "confidence, selected_by_default, source_message_indexes, tags。\n\n"
        f"<transcript>\n{transcript}\n</transcript>"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _normalize_candidate(item: Any, messages: list[dict[str, str]]) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    asset_type = item.get("asset_type")
    subtype = item.get("subtype")
    if asset_type not in VALID_ASSET_TYPES:
        return None
    if asset_type == "memory" and subtype not in VALID_MEMORY_SUBTYPES:
        return None
    if asset_type == "skill" and subtype not in VALID_SKILL_SUBTYPES:
        return None
    evidence = [str(value).strip() for value in item.get("evidence") or [] if str(value).strip()]
    source_indexes = [int(value) for value in item.get("source_message_indexes") or [] if _valid_message_index(value, messages)]
    title = str(item.get("title") or "").strip()
    content = str(item.get("content") or "").strip()
    if not title or not content or not evidence or not source_indexes:
        return None
    if asset_type == "memory" and subtype in {"user_preference", "project_fact"}:
        if not any(messages[index].get("role") == "user" for index in source_indexes):
            return None
    return {
        "asset_type": asset_type,
        "subtype": subtype,
        "title": title,
        "content": content,
        "evidence": evidence,
        "rationale": str(item.get("rationale") or "").strip(),
        "confidence": max(0.0, min(float(item.get("confidence") or 0), 1.0)),
        "selected_by_default": bool(item.get("selected_by_default")),
        "source_message_indexes": source_indexes,
        "tags": [str(value).strip() for value in item.get("tags") or [] if str(value).strip()],
    }


def _looks_like_user_preference(content: str) -> bool:
    markers = ("我希望", "我不希望", "不要", "偏好", "以后", "关闭前", "集中审核")
    return any(marker in content for marker in markers) and any(marker in content for marker in ("记忆", "保存", "审核", "弹窗"))


def _preference_title(content: str) -> str:
    if "关闭" in content and "审核" in content:
        return "关闭前集中审核记忆"
    return "用户长期偏好"


def _compact(content: str, limit: int = 220) -> str:
    normalized = " ".join(str(content or "").split())
    return normalized if len(normalized) <= limit else normalized[:limit].rstrip()


def _valid_message_index(value: Any, messages: list[dict[str, str]]) -> bool:
    try:
        index = int(value)
    except (TypeError, ValueError):
        return False
    return 0 <= index < len(messages)
```

- [ ] **Step 4: Run tests**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_reflection -v
```

Expected: pass.

- [ ] **Step 5: Check change scope**

Run:

```powershell
git status --short
```

Expected: reflection files and previous task files changed.

---

## Task 4: Reflection and Learning API Routes

**Files:**

- Modify: `core/mcp_server.py`
- Create: `tests/test_mcp_server_learning.py`

- [ ] **Step 1: Write failing API tests**

Create `tests/test_mcp_server_learning.py`:

```python
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import mcp_server
from core.security_context import clear_workspace_keys, set_workspace_keys
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


class McpServerLearningTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.workspace = SoulDriveWorkspace.from_drive(self.temp_dir.name).ensure()
        initialize_keystore(self.workspace, "correct horse battery staple")
        self.keys = unlock_keystore(self.workspace, "correct horse battery staple")
        set_workspace_keys(self.workspace.root_path, self.keys)
        self.addCleanup(clear_workspace_keys)
        self.client = TestClient(mcp_server.app)

    def _state(self):
        return {
            "locked": False,
            "software_unlocked": True,
            "workspace_path": self.workspace.root_path,
            "auth_level": "HARDWARE_PLUS_PASSWORD",
        }

    def test_reflection_draft_requires_unlocked_workspace(self):
        with patch.object(mcp_server, "effective_runtime_state", return_value={"locked": True, "software_unlocked": False}):
            response = self.client.post("/reflection/draft", json={"trigger": "manual", "messages": []}, headers={"X-SoulDrive-Token": "test"})
        self.assertEqual(response.status_code, 423)

    def test_reflection_draft_returns_pending_candidates(self):
        with patch.object(mcp_server, "effective_runtime_state", return_value=self._state()):
            response = self.client.post(
                "/reflection/draft",
                json={
                    "trigger": "manual",
                    "messages": [{"role": "user", "content": "我不希望每轮问答后弹窗保存记忆，希望关闭前集中审核。"}],
                },
                headers={"X-SoulDrive-Token": "test"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["candidates"][0]["asset_type"], "memory")

    def test_decide_accept_creates_searchable_memory(self):
        with patch.object(mcp_server, "effective_runtime_state", return_value=self._state()):
            draft = self.client.post(
                "/reflection/draft",
                json={"trigger": "manual", "messages": [{"role": "user", "content": "我不希望每轮问答后弹窗保存记忆，希望关闭前集中审核。"}]},
                headers={"X-SoulDrive-Token": "test"},
            ).json()
            candidate_id = draft["candidates"][0]["candidate_id"]
            decide = self.client.post(
                f"/reflection/reviews/{draft['review_id']}/decide",
                json={"decisions": [{"candidate_id": candidate_id, "action": "accept"}]},
                headers={"X-SoulDrive-Token": "test"},
            )
            search = self.client.post(
                "/learning/search",
                json={"query": "什么时候保存记忆", "top_k_memory": 3, "top_k_skill": 2},
                headers={"X-SoulDrive-Token": "test"},
            )
        self.assertEqual(decide.status_code, 200)
        self.assertEqual(len(search.json()["memories"]), 1)
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_mcp_server_learning -v
```

Expected: fail because routes are missing.

- [ ] **Step 3: Add request models and store helper**

Modify `core/mcp_server.py` imports:

```python
from core.learning_store import LearningStore
from core.reflection import build_reflection_candidates, build_reflection_prompt, parse_reflection_response
```

Add Pydantic models near existing request models:

```python
class ReflectionMessage(BaseModel):
    role: str = Field(max_length=16)
    content: str = Field(max_length=12000)

class ReflectionDraftRequest(BaseModel):
    trigger: str = Field(pattern="^(manual|close)$")
    messages: list[ReflectionMessage] = Field(default_factory=list, max_length=200)

class ReflectionDecision(BaseModel):
    candidate_id: str = Field(max_length=128)
    action: str = Field(pattern="^(accept|reject)$")
    edited_title: str | None = Field(default=None, max_length=240)
    edited_content: str | None = Field(default=None, max_length=4000)

class ReflectionDecideRequest(BaseModel):
    decisions: list[ReflectionDecision] = Field(default_factory=list, max_length=100)

class LearningSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=6000)
    top_k_memory: int = Field(default=3, ge=0, le=10)
    top_k_skill: int = Field(default=2, ge=0, le=10)
```

Add helper:

```python
def current_learning_store() -> LearningStore:
    workspace = current_workspace()
    keys = get_workspace_keys(workspace.root_path)
    if keys is None:
        raise RuntimeError("workspace keys are not available")
    return LearningStore(workspace, keys)
```

- [ ] **Step 4: Add reflection and learning routes**

Add routes before `/tools/list`:

```python
@app.post("/reflection/draft")
async def reflection_draft(request: ReflectionDraftRequest):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response

    messages = [message.model_dump() for message in request.messages]
    if not messages:
        return JSONResponse({"error": "messages are required for reflection draft"}, status_code=400)

    store = current_learning_store()
    try:
        candidates = _generate_reflection_candidates(messages)
        review = store.create_review(request.trigger, candidates)
        current_audit_logger().append_event("reflection.draft_created", {
            "trigger": request.trigger,
            "candidate_count": len(candidates),
        })
        return review
    finally:
        store.close()


@app.get("/reflection/reviews/pending")
async def reflection_pending_reviews():
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    store = current_learning_store()
    try:
        return {"reviews": store.list_pending_reviews()}
    finally:
        store.close()


@app.post("/reflection/reviews/{review_id}/decide")
async def reflection_decide(review_id: str, request: ReflectionDecideRequest):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    store = current_learning_store()
    try:
        result = store.decide_review(review_id, [decision.model_dump(exclude_none=True) for decision in request.decisions])
        current_audit_logger().append_event("reflection.review_completed", {
            "review_id": review_id,
            "accepted_count": result["accepted_count"],
            "rejected_count": result["rejected_count"],
        })
        return result
    finally:
        store.close()


@app.get("/learning/memories")
async def learning_memories():
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    store = current_learning_store()
    try:
        return {"memories": store.list_memories()}
    finally:
        store.close()


@app.get("/learning/skills")
async def learning_skills():
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    store = current_learning_store()
    try:
        return {"skills": store.list_skills()}
    finally:
        store.close()


@app.post("/learning/search")
async def learning_search(request: LearningSearchRequest):
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    store = current_learning_store()
    try:
        result = store.search(request.query, request.top_k_memory, request.top_k_skill)
        current_audit_logger().append_event("learning.search_performed", {
            "query_chars": len(request.query),
            "memory_count": len(result["memories"]),
            "skill_count": len(result["skills"]),
        })
        return result
    finally:
        store.close()
```

Add generation helper:

```python
def _generate_reflection_candidates(messages: list[dict[str, str]]) -> list[dict]:
    prompt = build_reflection_prompt(messages)
    try:
        ensure_runtime_loaded()
        raw = rag._generate_answer_text(prompt)
        parsed = parse_reflection_response(raw, messages)
        if parsed:
            return parsed
    except Exception as exc:
        runtime_metrics.record_error(f"reflection generation fallback: {exc}")
    return build_reflection_candidates(messages)
```

- [ ] **Step 5: Run API tests**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_mcp_server_learning -v
```

Expected: pass.

- [ ] **Step 6: Run related security tests**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_mcp_server_security tests.test_runtime_state -v
```

Expected: pass.

- [ ] **Step 7: Check change scope**

Run:

```powershell
git status --short
```

Expected: backend learning API files and tests changed.

---

## Task 5: RAG Learning Context Injection

**Files:**

- Modify: `core/rag_engine.py`
- Modify: `core/mcp_server.py`
- Modify: `tests/test_rag_prompting.py`
- Modify: `tests/test_mcp_server_learning.py`

- [ ] **Step 1: Add failing prompt tests**

Append to `tests/test_rag_prompting.py`:

```python
    def test_build_answer_messages_includes_learning_context_outside_document_evidence(self):
        messages = build_answer_messages(
            "以后什么时候保存记忆？",
            "未检索到直接相关的文献片段。",
            "",
            learning_context={
                "memories": [{"title": "关闭前集中审核", "content": "用户希望关闭前集中审核候选记忆。", "score": 0.9}],
                "skills": [{"title": "实现前测试", "content": "先写失败测试，再实现。", "score": 0.8}],
            },
        )

        prompt = messages[-1]["content"]
        self.assertIn("【已确认长期记忆】", prompt)
        self.assertIn("关闭前集中审核", prompt)
        self.assertIn("【已确认可复用技能】", prompt)
        self.assertIn("实现前测试", prompt)
        self.assertLess(prompt.index("【已确认长期记忆】"), prompt.index("【私有文献详情】"))
```

- [ ] **Step 2: Run test to verify failure**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_rag_prompting.RagPromptingTests.test_build_answer_messages_includes_learning_context_outside_document_evidence -v
```

Expected: fail because `build_answer_messages` has no `learning_context` argument.

- [ ] **Step 3: Modify RAG prompt builder**

Update `core/rag_engine.py` function signature:

```python
def build_answer_messages(
    query: str,
    vector_context: str,
    graph_context: str,
    learning_context: dict | None = None,
) -> list[dict[str, str]]:
```

In system prompt, add:

```python
        "如果提供了【已确认长期记忆】或【已确认可复用技能】，它们只能作为用户偏好、项目状态或工作方法参考。"
        "当前用户指令优先于长期记忆和技能。"
        "文档证据优先于长期记忆中的事实性内容。"
        "不要把长期记忆或技能当作文档证据引用。"
```

Add helper:

```python
def build_learning_context(learning_context: dict | None) -> str:
    if not learning_context:
        return ""
    lines = []
    memories = learning_context.get("memories") or []
    skills = learning_context.get("skills") or []
    if memories:
        lines.append("【已确认长期记忆】")
        for item in memories[:3]:
            lines.append(f"- {mindmap_text(item.get('title') or '记忆')}：{mindmap_text(item.get('content') or '')}")
    if skills:
        lines.append("【已确认可复用技能】")
        for item in skills[:2]:
            lines.append(f"- {mindmap_text(item.get('title') or '技能')}：{mindmap_text(item.get('content') or '')}")
    return "\n".join(lines)
```

Update user prompt:

```python
    learning_context_text = build_learning_context(learning_context)
    user_prompt = (
        "<context>\n"
        f"{learning_context_text}\n"
        f"{graph_context}\n"
        "【私有文献详情】\n"
        f"{vector_context}\n"
        "</context>\n\n"
        f"<user_query>\n{query}\n</user_query>"
    )
```

- [ ] **Step 4: Pass learning context through generation**

Update `RAGEngine.generate_response_stream` signature:

```python
    def generate_response_stream(
        self,
        query: str,
        top_k: int = 3,
        trace_id: str | None = None,
        learning_context: dict | None = None,
    ):
```

Update call:

```python
        messages = build_answer_messages(query, vector_context, graph_context_text, learning_context=learning_context)
```

- [ ] **Step 5: Add server-side learning search before chat generation**

In `core/mcp_server.py` `chat_stream.event_generator`, after `ensure_runtime_loaded()` and before calling `rag.generate_response_stream`, add:

```python
            learning_context = _learning_context_for_query(request.query)
            for chunk in rag.generate_response_stream(
                query=request.query,
                top_k=clamp_chat_top_k(request.top_k),
                trace_id=trace_id,
                learning_context=learning_context,
            ):
                yield chunk
```

Add helper:

```python
def _learning_context_for_query(query: str) -> dict:
    try:
        store = current_learning_store()
        try:
            result = store.search(query, top_k_memory=3, top_k_skill=2)
        finally:
            store.close()
        if result["memories"] or result["skills"]:
            current_audit_logger().append_event("learning.injected_into_rag", {
                "query_chars": len(query),
                "memory_count": len(result["memories"]),
                "skill_count": len(result["skills"]),
            })
        return result
    except Exception as exc:
        runtime_metrics.record_error(f"learning context unavailable: {exc}")
        return {"memories": [], "skills": []}
```

- [ ] **Step 6: Run prompt and API tests**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_rag_prompting tests.test_mcp_server_learning -v
```

Expected: pass.

- [ ] **Step 7: Check RAG regression tests**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_rag_context tests.test_rag_prompting tests.test_answer_quality -v
```

Expected: pass.

- [ ] **Step 8: Check change scope**

Run:

```powershell
git status --short
```

Expected: RAG, server, learning tests changed.

---

## Task 6: Learning Loop Evaluation Dataset and Script

**Files:**

- Create: `eval/learning_loop_v0.1.jsonl`
- Create: `scripts/eval_learning_loop.py`
- Create: `tests/test_learning_eval.py`

- [ ] **Step 1: Add evaluation dataset**

Create `eval/learning_loop_v0.1.jsonl`:

```jsonl
{"id":"MEM-01","setup_assets":[{"asset_type":"memory","subtype":"user_preference","title":"关闭前集中审核记忆","content":"用户偏好：关闭程序前集中审核候选记忆，不希望每轮问答都弹窗。"}],"query":"以后你应该什么时候询问我是否保存记忆？","expected_keywords":["关闭","集中审核","不希望每轮"],"forbidden_keywords":["每次提问后","立即弹窗"],"metric":"preference_following"}
{"id":"SKILL-01","setup_assets":[{"asset_type":"skill","subtype":"sop","title":"实现前先写失败测试","content":"处理代码功能时，先写失败测试，再实现最小代码，再运行测试确认通过。"}],"query":"接下来实现一个后端功能时，你应该按什么流程做？","expected_keywords":["失败测试","最小代码","运行测试"],"forbidden_keywords":["直接实现","不用测试"],"metric":"skill_application"}
{"id":"MEM-02","setup_assets":[{"asset_type":"memory","subtype":"project_fact","title":"SoulDrive 学习闭环阶段","content":"当前分支阶段 1 要完整实现人工审核、learning 检索、RAG 注入和评估；阶段 2 只预留 scheduler。"}],"query":"这个分支第一阶段和第二阶段的边界是什么？","expected_keywords":["阶段 1","RAG 注入","阶段 2","预留"],"forbidden_keywords":["自动执行周期任务"],"metric":"project_fact"}
```

- [ ] **Step 2: Write failing eval tests**

Create `tests/test_learning_eval.py`:

```python
import tempfile
import unittest
from pathlib import Path

from scripts import eval_learning_loop


class LearningEvalTests(unittest.TestCase):
    def test_keyword_metric_scores_expected_and_forbidden_terms(self):
        score = eval_learning_loop.score_answer(
            answer="应该在关闭程序前集中审核，不希望每轮问答都弹窗。",
            expected_keywords=["关闭", "集中审核"],
            forbidden_keywords=["每次提问后"],
        )

        self.assertEqual(score["expected_hits"], 2)
        self.assertEqual(score["forbidden_hits"], 0)
        self.assertTrue(score["passed"])

    def test_load_records_reads_jsonl(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "learning.jsonl"
            path.write_text('{"id":"X","setup_assets":[],"query":"q","expected_keywords":[],"forbidden_keywords":[],"metric":"m"}\n', encoding="utf-8")

            records = eval_learning_loop.load_records(path)

        self.assertEqual(records[0]["id"], "X")
```

- [ ] **Step 3: Run tests to verify failure**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_learning_eval -v
```

Expected: fail because script is missing.

- [ ] **Step 4: Implement eval script**

Create `scripts/eval_learning_loop.py`:

```python
import argparse
import json
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "eval" / "learning_loop_v0.1.jsonl"


def load_records(path: Path) -> list[dict]:
    records = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            if line.strip():
                records.append(json.loads(line))
    return records


def score_answer(answer: str, expected_keywords: list[str], forbidden_keywords: list[str]) -> dict:
    expected_hits = sum(1 for keyword in expected_keywords if keyword in answer)
    forbidden_hits = sum(1 for keyword in forbidden_keywords if keyword in answer)
    return {
        "expected_hits": expected_hits,
        "expected_total": len(expected_keywords),
        "forbidden_hits": forbidden_hits,
        "passed": expected_hits == len(expected_keywords) and forbidden_hits == 0,
    }


def summarize(results: list[dict]) -> dict:
    total = len(results)
    passed = sum(1 for result in results if result["score"]["passed"])
    average_latency = sum(result["latency_ms"] for result in results) / max(1, total)
    return {
        "total": total,
        "passed": passed,
        "pass_rate": passed / max(1, total),
        "average_latency_ms": round(average_latency, 2),
    }


def run_static_baseline(records: list[dict]) -> list[dict]:
    results = []
    for record in records:
        started = time.time()
        answer = "当前系统未接入长期学习资产。"
        results.append({
            "id": record["id"],
            "metric": record["metric"],
            "answer": answer,
            "score": score_answer(answer, record["expected_keywords"], record["forbidden_keywords"]),
            "latency_ms": int((time.time() - started) * 1000),
        })
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate SoulDrive learning-loop behavior.")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args(argv)
    records = load_records(Path(args.dataset))
    results = run_static_baseline(records)
    summary = summarize(results)
    print(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run eval tests and baseline**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_learning_eval -v
conda run -n souldrive python scripts/eval_learning_loop.py --baseline
```

Expected: tests pass; baseline prints JSON summary with low pass rate.

- [ ] **Step 6: Check change scope**

Run:

```powershell
git status --short
```

Expected: eval dataset/script/tests created.

---

## Task 7: Frontend Manual Review UI

**Files:**

- Modify: `souldrive-ui/src/App.tsx`
- Modify: `souldrive-ui/src/App.css`
- Create: `souldrive-ui/scripts/learning-review.test.mjs`
- Modify: `souldrive-ui/package.json` if adding test script

- [ ] **Step 1: Add frontend static test**

Create `souldrive-ui/scripts/learning-review.test.mjs`:

```javascript
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import assert from "node:assert/strict";

const app = readFileSync(resolve("src", "App.tsx"), "utf8");

assert.match(app, /ReflectionCandidate/);
assert.match(app, /requestReflectionDraft/);
assert.match(app, /submitReflectionDecisions/);
assert.match(app, /learning-review-dialog/);
assert.match(app, /\/reflection\/draft/);
assert.match(app, /\/reflection\/reviews\/\$\{activeReview\.review_id\}\/decide/);

console.log("learning review UI wiring checks passed");
```

If adding npm script, modify `souldrive-ui/package.json`:

```json
"test:learning": "node scripts/learning-review.test.mjs"
```

- [ ] **Step 2: Run test to verify failure**

Run:

```powershell
cd souldrive-ui
npm run test:learning
```

Expected: fail because UI types/functions are missing.

- [ ] **Step 3: Add TypeScript interfaces and state**

Modify `souldrive-ui/src/App.tsx` near existing interfaces:

```typescript
interface ReflectionCandidate {
  candidate_id: string;
  asset_type: "memory" | "skill";
  subtype: string;
  title: string;
  content: string;
  evidence: string[];
  rationale: string;
  confidence: number;
  selected_by_default: boolean;
}

interface ReflectionReview {
  review_id: string;
  status: "pending" | "completed" | "discarded";
  candidates: ReflectionCandidate[];
}

interface ReviewDraftState {
  review: ReflectionReview | null;
  selectedIds: Set<string>;
  edits: Record<string, { title: string; content: string }>;
  mode: "idle" | "drafting" | "reviewing" | "submitting" | "done" | "error";
  message: string;
  closeAfterSubmit: boolean;
}
```

Add state in `App()`:

```typescript
  const [reviewState, setReviewState] = useState<ReviewDraftState>({
    review: null,
    selectedIds: new Set(),
    edits: {},
    mode: "idle",
    message: "",
    closeAfterSubmit: false,
  });
```

- [ ] **Step 4: Add draft and decision functions**

Add inside `App()`:

```typescript
  const authHeaders = () => (apiToken ? { "X-SoulDrive-Token": apiToken } : undefined);

  const requestReflectionDraft = async (trigger: "manual" | "close", closeAfterSubmit = false) => {
    if (!messages.length || locked) {
      setReviewState((previous) => ({
        ...previous,
        mode: "done",
        message: locked ? "工作区未解锁，无法生成记忆候选。" : "当前会话没有可沉淀内容。",
        closeAfterSubmit,
      }));
      return null;
    }
    setReviewState((previous) => ({ ...previous, mode: "drafting", message: "正在生成候选记忆..." }));
    try {
      const response = await fetch(`${apiBaseUrl}/reflection/draft`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(authHeaders() || {}),
        },
        body: JSON.stringify({ trigger, messages }),
      });
      if (!response.ok) throw new Error("reflection draft failed");
      const review = await response.json() as ReflectionReview;
      const selectedIds = new Set(review.candidates.filter((item) => item.selected_by_default).map((item) => item.candidate_id));
      const edits = Object.fromEntries(review.candidates.map((item) => [item.candidate_id, { title: item.title, content: item.content }]));
      setReviewState({ review, selectedIds, edits, mode: review.candidates.length ? "reviewing" : "done", message: review.candidates.length ? "" : "本轮没有可沉淀内容。", closeAfterSubmit });
      return review;
    } catch {
      setReviewState((previous) => ({ ...previous, mode: "error", message: "候选记忆生成失败。", closeAfterSubmit }));
      return null;
    }
  };

  const submitReflectionDecisions = async () => {
    const activeReview = reviewState.review;
    if (!activeReview) return;
    setReviewState((previous) => ({ ...previous, mode: "submitting", message: "正在写入长期记忆..." }));
    const decisions = activeReview.candidates.map((candidate) => {
      const accepted = reviewState.selectedIds.has(candidate.candidate_id);
      const edit = reviewState.edits[candidate.candidate_id] || { title: candidate.title, content: candidate.content };
      return accepted
        ? { candidate_id: candidate.candidate_id, action: "accept", edited_title: edit.title, edited_content: edit.content }
        : { candidate_id: candidate.candidate_id, action: "reject" };
    });
    try {
      const response = await fetch(`${apiBaseUrl}/reflection/reviews/${activeReview.review_id}/decide`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(authHeaders() || {}),
        },
        body: JSON.stringify({ decisions }),
      });
      if (!response.ok) throw new Error("reflection decide failed");
      setReviewState((previous) => ({ ...previous, mode: "done", message: previous.closeAfterSubmit ? "记忆已完成，将自动关闭程序。" : "记忆已完成。" }));
    } catch {
      setReviewState((previous) => ({ ...previous, mode: "error", message: "记忆写入失败，请重试或放弃本次记忆。" }));
    }
  };
```

- [ ] **Step 5: Add manual memory button**

In `dialog-header` or another existing top control area, add a button:

```tsx
            <button
              type="button"
              className="memory-action"
              disabled={locked || isGenerating || reviewState.mode === "drafting" || reviewState.mode === "submitting"}
              onClick={() => void requestReflectionDraft("manual")}
              title="审核并保存本轮可复用记忆"
            >
              <BrainCircuit size={15} />
              记忆
            </button>
```

- [ ] **Step 6: Add review dialog component**

Add before final root closing `</div>`:

```tsx
      {reviewState.mode === "reviewing" && reviewState.review && (
        <section className="learning-review-backdrop" role="presentation">
          <div className="learning-review-dialog" role="dialog" aria-modal="true" aria-labelledby="learning-review-title">
            <div className="learning-review-head">
              <div>
                <span>Learning Review</span>
                <h2 id="learning-review-title">审核候选记忆</h2>
              </div>
              <button type="button" title="关闭审核" onClick={() => setReviewState((previous) => ({ ...previous, mode: "idle", review: null }))}>
                <X size={17} />
              </button>
            </div>
            <div className="learning-review-list">
              {reviewState.review.candidates.map((candidate) => {
                const edit = reviewState.edits[candidate.candidate_id] || { title: candidate.title, content: candidate.content };
                const selected = reviewState.selectedIds.has(candidate.candidate_id);
                return (
                  <article key={candidate.candidate_id} className={`learning-review-card ${selected ? "selected" : ""}`}>
                    <label>
                      <input
                        type="checkbox"
                        checked={selected}
                        onChange={(event) => {
                          setReviewState((previous) => {
                            const next = new Set(previous.selectedIds);
                            if (event.target.checked) next.add(candidate.candidate_id);
                            else next.delete(candidate.candidate_id);
                            return { ...previous, selectedIds: next };
                          });
                        }}
                      />
                      <span>{candidate.asset_type.toUpperCase()} · {candidate.subtype}</span>
                    </label>
                    <input
                      value={edit.title}
                      onChange={(event) => setReviewState((previous) => ({
                        ...previous,
                        edits: { ...previous.edits, [candidate.candidate_id]: { ...edit, title: event.target.value } },
                      }))}
                    />
                    <textarea
                      value={edit.content}
                      onChange={(event) => setReviewState((previous) => ({
                        ...previous,
                        edits: { ...previous.edits, [candidate.candidate_id]: { ...edit, content: event.target.value } },
                      }))}
                    />
                    <p>{candidate.rationale}</p>
                  </article>
                );
              })}
            </div>
            <div className="learning-review-actions">
              <button type="button" onClick={() => setReviewState((previous) => ({ ...previous, mode: "idle", review: null }))}>
                取消
              </button>
              <button type="button" onClick={() => void submitReflectionDecisions()}>
                {reviewState.closeAfterSubmit ? "确认记忆并关闭" : "确认记忆"}
              </button>
            </div>
          </div>
        </section>
      )}
```

- [ ] **Step 7: Add minimal CSS**

Append to `souldrive-ui/src/App.css`:

```css
.learning-review-backdrop {
  position: fixed;
  inset: 0;
  z-index: 40;
  display: grid;
  place-items: center;
  background: rgba(9, 12, 18, 0.62);
}

.learning-review-dialog {
  width: min(760px, calc(100vw - 32px));
  max-height: calc(100vh - 48px);
  overflow: hidden;
  display: flex;
  flex-direction: column;
  background: #101722;
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 8px;
}

.learning-review-head,
.learning-review-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 16px;
}

.learning-review-list {
  overflow: auto;
  padding: 0 16px 16px;
  display: grid;
  gap: 12px;
}

.learning-review-card {
  display: grid;
  gap: 10px;
  padding: 12px;
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 8px;
}

.learning-review-card input[type="text"],
.learning-review-card input:not([type]),
.learning-review-card textarea {
  width: 100%;
}

.learning-review-card textarea {
  min-height: 88px;
  resize: vertical;
}
```

- [ ] **Step 8: Run frontend checks**

Run:

```powershell
cd souldrive-ui
npm run test:learning
npm run build
```

Expected: pass.

- [ ] **Step 9: Check change scope**

Run:

```powershell
git status --short
```

Expected: frontend files and learning backend files changed.

---

## Task 8: Tauri Close-Time Review Flow

**Files:**

- Modify: `souldrive-ui/src/App.tsx`
- Modify: `souldrive-ui/src-tauri/capabilities/default.json`
- Modify: `souldrive-ui/scripts/learning-review.test.mjs`

- [ ] **Step 1: Extend static test for close handling**

Append to `souldrive-ui/scripts/learning-review.test.mjs`:

```javascript
assert.match(app, /onCloseRequested/);
assert.match(app, /closeAfterSubmit/);
assert.match(app, /确认记忆并关闭/);
```

- [ ] **Step 2: Run test to verify failure**

Run:

```powershell
cd souldrive-ui
npm run test:learning
```

Expected: fail until close handling is added.

- [ ] **Step 3: Import Tauri window API**

Modify imports in `souldrive-ui/src/App.tsx`:

```typescript
import { getCurrentWindow } from "@tauri-apps/api/window";
```

- [ ] **Step 4: Add close bypass ref**

Inside `App()`:

```typescript
  const closeBypassRef = useRef(false);
```

- [ ] **Step 5: Register close handler**

Add effect after runtime config effects:

```typescript
  useEffect(() => {
    let unlisten: (() => void) | null = null;
    const setupCloseHandler = async () => {
      try {
        const appWindow = getCurrentWindow();
        unlisten = await appWindow.onCloseRequested(async (event) => {
          if (closeBypassRef.current) return;
          if (!messages.length || locked || isGenerating) return;
          event.preventDefault();
          await requestReflectionDraft("close", true);
        });
      } catch {
        // Browser preview mode does not expose Tauri window APIs.
      }
    };
    void setupCloseHandler();
    return () => {
      if (unlisten) unlisten();
    };
  }, [messages, locked, isGenerating, apiBaseUrl, apiToken]);
```

- [ ] **Step 6: Close after successful submit**

In `submitReflectionDecisions`, after setting done state on success:

```typescript
      if (reviewState.closeAfterSubmit) {
        window.setTimeout(() => {
          closeBypassRef.current = true;
          void getCurrentWindow().close();
        }, 900);
      }
```

- [ ] **Step 7: Add close permission if build requires it**

If `npm run build` or `cargo check` reports missing Tauri window permission, modify `souldrive-ui/src-tauri/capabilities/default.json` permissions:

```json
"permissions": [
  "core:default",
  "core:window:allow-close"
]
```

If Tauri v2 schema rejects that permission, leave `core:default` unchanged and document that close API is covered by default capability in this app version.

- [ ] **Step 8: Run frontend and Tauri checks**

Run:

```powershell
cd souldrive-ui
npm run test:learning
npm run build
cd src-tauri
cargo check
```

Expected: pass.

- [ ] **Step 9: Check change scope**

Run:

```powershell
git status --short
```

Expected: close-time review frontend files changed.

---

## Task 9: Full Verification and Known Baseline Handling

**Files:**

- Modify only if verification reveals task-owned issues.

- [ ] **Step 1: Run focused backend test suite**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_learning_store tests.test_reflection tests.test_mcp_server_learning tests.test_rag_prompting tests.test_learning_eval -v
```

Expected: pass.

- [ ] **Step 2: Run full backend suite**

Run:

```powershell
conda run -n souldrive python -m unittest discover -s tests -v
```

Expected: all task-owned tests pass. If `test_eval_script_can_resolve_project_local_workspace` fails because the clean worktree lacks local `souldrive_db/config/workspace.json`, record it as the known pre-existing baseline/environment issue observed before code changes.

- [ ] **Step 3: Run learning eval baseline command**

Run:

```powershell
conda run -n souldrive python scripts/eval_learning_loop.py --baseline
```

Expected: JSON summary prints. Baseline pass rate should remain low because static baseline does not use learning retrieval.

- [ ] **Step 4: Run frontend checks**

Run:

```powershell
cd souldrive-ui
npm run test:learning
npm run build
cd src-tauri
cargo check
```

Expected: pass.

- [ ] **Step 5: Review diff for scope**

Run:

```powershell
git diff --stat
git status --short
```

Expected: changes are limited to learning loop backend, tests, eval, frontend review UI, and approved docs/plans.

- [ ] **Step 6: Final implementation report**

Report:

- Files changed.
- Tests run and results.
- Known baseline issue if still present.
- No commit, no merge, no push.

---

## Plan Self-Review Checklist

- Spec coverage:
  - Reflection candidate generation: Task 3.
  - Human review queue: Tasks 2, 4, 7, 8.
  - Encrypted persistence: Tasks 1, 2.
  - Learning search: Tasks 2, 4.
  - RAG injection: Task 5.
  - Eval baseline: Task 6.
  - Frontend manual and close-time review: Tasks 7, 8.
  - Stage 2 scheduler reservation: covered in design doc, no implementation in this plan.
- Placeholder scan:
  - Plan contains no unresolved placeholder steps.
  - One conditional Tauri permission step is intentional because Tauri v2 permission behavior depends on generated schema; verification command decides the exact edit.
- Type consistency:
  - Backend review IDs use `review_id`.
  - Candidate IDs use `candidate_id`.
  - Frontend `ReflectionReview` matches API response shape.
  - `LearningStore.search()` returns `{memories, skills}` used by API and RAG.
