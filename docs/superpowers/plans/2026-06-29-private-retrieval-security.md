# Private Retrieval Security Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make secure retrieval use dense, keyword, and graph candidate IDs before decrypting candidate text, while keeping locked workspaces opaque and cleanup behavior strict.

**Architecture:** Keep the existing Python/FastAPI sidecar and secure workspace model. Extend the secure vector store with in-memory dense search and encrypted HMAC keyword postings, extend the secure graph store with encrypted provenance-backed recall, then update `LocalKnowledgeBase` to merge chunk ID candidates before decrypting text for reranking and answer generation.

**Tech Stack:** Python 3.10, unittest, SQLite, cryptography AES-GCM/HMAC, FastAPI, local sentence-transformers models.

---

## Files And Responsibilities

- `core/secure_vector_store.py`: encrypted chunk, metadata, embedding persistence; in-memory dense candidate recall; encrypted HMAC keyword postings; bounded candidate text decryption.
- `core/secure_graph_store.py`: encrypted graph payloads plus entity/relationship provenance recall.
- `core/graph_extractor.py`: pass source chunk IDs into graph extraction and graph store writes.
- `core/indexer.py`: ensure graph extraction receives chunk metadata after chunk IDs are known.
- `core/knowledge_base.py`: merge dense, keyword, and graph candidate IDs before decrypting candidate text; keep legacy Chroma path behavior intact.
- `core/mcp_server.py`: prevent document/index count exposure before software unlock.
- `tests/test_secure_vector_store.py`: secure dense/keyword candidate behavior and bounded decryption.
- `tests/test_secure_graph_store.py`: graph provenance recall and encrypted payload regression.
- `tests/test_mcp_server_papers.py`: locked document list opacity and unlocked compatibility.
- `tests/test_secure_retrieval_flow.py`: end-to-end secure retrieval candidate merge without full plaintext scan.

## Task 1: Locked Document Lists Stay Opaque

**Files:**
- Modify: `tests/test_mcp_server_papers.py`
- Modify: `core/mcp_server.py`

- [ ] **Step 1: Write the failing test**

Add this test to `McpServerPapersTests`:

```python
    def test_documents_list_rejects_before_software_unlock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            source_pdf = Path(temp_dir) / "private.pdf"
            source_pdf.write_bytes(b"%PDF private")
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            set_workspace_keys(workspace.root_path, keys)
            try:
                store = SecureDocumentStore(workspace, keys)
                try:
                    store.import_document(str(source_pdf))
                finally:
                    store.close()
            finally:
                clear_workspace_keys(workspace.root_path)

            state_path = str(Path(temp_dir) / "runtime_state.json")
            with patch.object(runtime_state, "STATE_PATH", state_path), patch(
                "core.mcp_server.current_workspace",
                return_value=workspace,
            ), patch.dict(
                "os.environ",
                {"SOULDRIVE_API_TOKEN": "test-token"},
                clear=False,
            ):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                response = TestClient(mcp_server.app).get(
                    "/documents/list",
                    headers={"X-SoulDrive-Token": "test-token"},
                )

        self.assertEqual(response.status_code, 423)
        payload = response.json()
        self.assertEqual(payload["status"], "locked")
        self.assertNotIn("document_count", payload)
        self.assertNotIn("documents", payload)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_mcp_server_papers.McpServerPapersTests.test_documents_list_rejects_before_software_unlock -v
```

Expected: FAIL because `/documents/list` currently returns a payload before software unlock.

- [ ] **Step 3: Implement the minimal API guard**

In `core/mcp_server.py`, update both list endpoints:

```python
@app.get("/documents/list")
async def documents_list():
    mounted_response = require_workspace_mounted()
    if mounted_response is not None:
        return mounted_response
    locked_response = require_software_unlock()
    if locked_response is not None:
        return locked_response
    return _document_library_payload(current_workspace())
```

For `/papers/list`, add the same `locked_response` guard before calling `_document_library_payload`.

- [ ] **Step 4: Run test to verify it passes**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_mcp_server_papers -v
```

Expected: PASS.

- [ ] **Step 5: Check status without committing**

Run:

```powershell
git status --short
```

Expected: modified `core/mcp_server.py`, modified `tests/test_mcp_server_papers.py`, plus existing design/plan docs.

## Task 2: Secure Dense Recall Returns IDs Before Text Decryption

**Files:**
- Modify: `tests/test_secure_vector_store.py`
- Modify: `core/secure_vector_store.py`

- [ ] **Step 1: Write the failing tests**

Add tests to `SecureVectorStoreTests`:

```python
    def test_dense_candidates_do_not_decrypt_chunk_text_until_loaded_by_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureVectorStore(workspace.secure_vector_store_path, keys)
            try:
                store.add_chunks(
                    ids=["alpha-child", "beta-child"],
                    documents=["alpha private answer", "beta unrelated"],
                    metadatas=[
                        {"parent_id": "alpha-parent", "source_filename": "alpha.pdf"},
                        {"parent_id": "beta-parent", "source_filename": "beta.pdf"},
                    ],
                    embeddings=[[1.0, 0.0], [0.0, 1.0]],
                )
                candidates = store.dense_candidates([1.0, 0.0], limit=1)
                loaded = store.load_candidates([item.chunk_id for item in candidates])
            finally:
                store.close()

        self.assertEqual([item.chunk_id for item in candidates], ["alpha-child"])
        self.assertEqual(candidates[0].content, None)
        self.assertEqual(loaded[0].content, "alpha private answer")
        self.assertEqual(loaded[0].metadata["source_filename"], "alpha.pdf")
```

Also add:

```python
    def test_load_candidates_preserves_requested_order_and_skips_missing_ids(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureVectorStore(workspace.secure_vector_store_path, keys)
            try:
                store.add_chunks(
                    ids=["first", "second"],
                    documents=["first text", "second text"],
                    metadatas=[{"parent_id": "p1"}, {"parent_id": "p2"}],
                    embeddings=[[1.0], [0.5]],
                )
                loaded = store.load_candidates(["second", "missing", "first"])
            finally:
                store.close()

        self.assertEqual([item.chunk_id for item in loaded], ["second", "first"])
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_secure_vector_store.SecureVectorStoreTests.test_dense_candidates_do_not_decrypt_chunk_text_until_loaded_by_id tests.test_secure_vector_store.SecureVectorStoreTests.test_load_candidates_preserves_requested_order_and_skips_missing_ids -v
```

Expected: FAIL because `dense_candidates` and `load_candidates` do not exist.

- [ ] **Step 3: Implement candidate APIs**

In `core/secure_vector_store.py`, add:

```python
@dataclass(frozen=True)
class SecureCandidate:
    chunk_id: str
    score: float = 0.0
    content: str | None = None
    metadata: dict[str, Any] | None = None
```

Add methods to `SecureVectorStore`:

```python
    def dense_candidates(self, query_embedding: list[float], limit: int = 80) -> list[SecureCandidate]:
        rows = self.conn.execute(
            """
            SELECT child_id, embedding_nonce, embedding_ciphertext
            FROM child_chunks
            ORDER BY child_id ASC
            """
        ).fetchall()
        ranked = []
        for row in rows:
            child_id = row[0]
            embedding = self._decrypt_json(
                self.keys.index_key,
                row[1],
                row[2],
                CHILD_EMBEDDING_AAD + child_id.encode("utf-8"),
            )
            score = self._cosine_similarity(query_embedding, embedding)
            ranked.append(SecureCandidate(chunk_id=child_id, score=score))
        ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked[:limit]

    def load_candidates(self, chunk_ids: list[str]) -> list[SecureCandidate]:
        if not chunk_ids:
            return []
        rows = self.conn.execute(
            """
            SELECT child_id, content_nonce, content_ciphertext, metadata_nonce, metadata_ciphertext
            FROM child_chunks
            WHERE child_id IN ({})
            """.format(",".join("?" for _ in chunk_ids)),
            tuple(chunk_ids),
        ).fetchall()
        by_id = {row[0]: row for row in rows}
        loaded = []
        for chunk_id in chunk_ids:
            row = by_id.get(chunk_id)
            if row is None:
                continue
            loaded.append(
                SecureCandidate(
                    chunk_id=chunk_id,
                    content=self._decrypt_text(
                        self.keys.index_key,
                        row[1],
                        row[2],
                        CHILD_CONTENT_AAD + chunk_id.encode("utf-8"),
                    ),
                    metadata=self._decrypt_json(
                        self.keys.metadata_key,
                        row[3],
                        row[4],
                        CHILD_METADATA_AAD + chunk_id.encode("utf-8"),
                    ),
                )
            )
        return loaded
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_secure_vector_store -v
```

Expected: PASS.

## Task 3: Secure Keyword HMAC Postings Recall

**Files:**
- Modify: `tests/test_secure_vector_store.py`
- Modify: `core/secure_vector_store.py`

- [ ] **Step 1: Write the failing test**

Add this test to `SecureVectorStoreTests`:

```python
    def test_keyword_candidates_use_hmac_postings_without_plaintext_storage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureVectorStore(workspace.secure_vector_store_path, keys)
            try:
                store.add_chunks(
                    ids=["policy-child", "other-child"],
                    documents=["zero trust policy requires device verification", "lunch menu unrelated"],
                    metadatas=[
                        {"parent_id": "policy-parent", "source_filename": "policy.pdf"},
                        {"parent_id": "other-parent", "source_filename": "other.pdf"},
                    ],
                    embeddings=[[1.0], [0.0]],
                )
                candidates = store.keyword_candidates("zero trust", limit=5)
            finally:
                store.close()

            persisted = Path(workspace.secure_vector_store_path).read_bytes()

        self.assertEqual(candidates[0].chunk_id, "policy-child")
        self.assertNotIn(b"zero trust", persisted)
        self.assertNotIn(b"policy requires", persisted)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_secure_vector_store.SecureVectorStoreTests.test_keyword_candidates_use_hmac_postings_without_plaintext_storage -v
```

Expected: FAIL because `keyword_candidates` does not exist or does not use encrypted postings.

- [ ] **Step 3: Implement encrypted HMAC postings**

In `core/secure_vector_store.py`:

1. Import `hashlib`, `hmac`, and `TOKEN_PATTERN` from `core.keyword_index`.
2. Add `KEYWORD_POSTING_AAD = b"SoulDrive secure keyword posting v1"`.
3. Add schema:

```python
            CREATE TABLE IF NOT EXISTS keyword_postings (
                token_id TEXT PRIMARY KEY,
                posting_nonce TEXT NOT NULL,
                posting_ciphertext TEXT NOT NULL
            )
```

4. In `add_chunks`, after writing child chunks, call `_upsert_keyword_postings(child_id, content)`.
5. Implement:

```python
    def keyword_candidates(self, query: str, limit: int = 80) -> list[SecureCandidate]:
        scores: dict[str, float] = {}
        for token in self._tokens(query):
            token_id = self._token_id(token)
            row = self.conn.execute(
                """
                SELECT posting_nonce, posting_ciphertext
                FROM keyword_postings
                WHERE token_id = ?
                """,
                (token_id,),
            ).fetchone()
            if row is None:
                continue
            posting = self._decrypt_json(
                self.keys.metadata_key,
                row[0],
                row[1],
                KEYWORD_POSTING_AAD + token_id.encode("ascii"),
            )
            for chunk_id, count in posting.get("chunks", {}).items():
                scores[chunk_id] = scores.get(chunk_id, 0.0) + float(count)
        ranked = [SecureCandidate(chunk_id=chunk_id, score=score) for chunk_id, score in scores.items()]
        ranked.sort(key=lambda item: (-item.score, item.chunk_id))
        return ranked[:limit]

    def _upsert_keyword_postings(self, chunk_id: str, content: str) -> None:
        counts: dict[str, int] = {}
        for token in self._tokens(content):
            token_id = self._token_id(token)
            counts[token_id] = counts.get(token_id, 0) + 1
        for token_id, count in counts.items():
            existing = self._keyword_posting(token_id)
            chunks = dict(existing.get("chunks") or {})
            chunks[chunk_id] = count
            nonce, ciphertext = self._encrypt_bytes(
                self.keys.metadata_key,
                json.dumps({"chunks": chunks}, ensure_ascii=False).encode("utf-8"),
                KEYWORD_POSTING_AAD + token_id.encode("ascii"),
            )
            self.conn.execute(
                """
                INSERT OR REPLACE INTO keyword_postings (token_id, posting_nonce, posting_ciphertext)
                VALUES (?, ?, ?)
                """,
                (token_id, nonce, ciphertext),
            )

    def _keyword_posting(self, token_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT posting_nonce, posting_ciphertext
            FROM keyword_postings
            WHERE token_id = ?
            """,
            (token_id,),
        ).fetchone()
        if row is None:
            return {"chunks": {}}
        return self._decrypt_json(
            self.keys.metadata_key,
            row[0],
            row[1],
            KEYWORD_POSTING_AAD + token_id.encode("ascii"),
        )

    def _tokens(self, text: str) -> list[str]:
        return TOKEN_PATTERN.findall((text or "").lower())[:256]

    def _token_id(self, token: str) -> str:
        return hmac.new(self.keys.metadata_key, token.encode("utf-8"), hashlib.sha256).hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_secure_vector_store -v
```

Expected: PASS.

## Task 4: Knowledge Base Merges Secure Candidate IDs Before Text Decryption

**Files:**
- Create: `tests/test_secure_retrieval_flow.py`
- Modify: `core/knowledge_base.py`
- Modify: `core/secure_vector_store.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_secure_retrieval_flow.py`:

```python
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.knowledge_base import LocalKnowledgeBase
from core.security_context import clear_workspace_keys, set_workspace_keys
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


class SecureRetrievalFlowTests(unittest.TestCase):
    def test_secure_retrieval_decrypts_only_merged_candidate_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            embedding_model_path = Path(workspace.models_path) / "bge-small-zh-v1.5"
            embedding_model_path.mkdir(parents=True, exist_ok=True)
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            set_workspace_keys(workspace.root_path, keys)

            load_calls = []
            try:
                with patch("core.knowledge_base._sentence_transformer", return_value=_FakeEmbeddingModel()):
                    kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                    original_load = kb.secure_store.load_candidates

                    def recording_load(chunk_ids):
                        load_calls.append(list(chunk_ids))
                        return original_load(chunk_ids)

                    kb.secure_store.load_candidates = recording_load
                    try:
                        kb.ingest_chunks(
                            "doc-private",
                            [
                                _FakeChunk(
                                    "zero trust architecture requires device verification",
                                    {"source_filename": "policy.pdf", "page": 1, "chunk_index": 0},
                                ),
                                _FakeChunk(
                                    "cafeteria menu and parking instructions",
                                    {"source_filename": "misc.pdf", "page": 1, "chunk_index": 1},
                                ),
                            ],
                        )
                        result = kb.search_with_evidence("zero trust device", top_k=1)
                    finally:
                        kb.close()
            finally:
                clear_workspace_keys(workspace.root_path)

        loaded_ids = [chunk_id for batch in load_calls for chunk_id in batch]
        self.assertTrue(loaded_ids)
        self.assertLessEqual(len(set(loaded_ids)), 4)
        self.assertEqual(result["metadatas"][0]["source_filename"], "policy.pdf")
        self.assertIn("zero trust architecture", result["documents"][0])


class _FakeChunk:
    def __init__(self, page_content: str, metadata: dict):
        self.page_content = page_content
        self.metadata = metadata


class _FakeEmbeddingModel:
    def encode(self, texts, normalize_embeddings=True):
        if isinstance(texts, str):
            texts = [texts]
        vectors = []
        for text in texts:
            lowered = text.lower()
            vectors.append([
                float("zero" in lowered or "trust" in lowered or "device" in lowered),
                float("cafeteria" in lowered or "parking" in lowered),
            ])
        return _EmbeddingResult(vectors)


class _EmbeddingResult(list):
    def tolist(self):
        return list(self)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_secure_retrieval_flow -v
```

Expected: FAIL because secure retrieval still decrypts text during dense and keyword candidate creation.

- [ ] **Step 3: Update secure knowledge-base path**

In `core/knowledge_base.py`:

1. Keep legacy Chroma path unchanged.
2. In secure mode, change `_dense_candidates` to call `self.secure_store.dense_candidates(...)` and return `SearchCandidate` objects with IDs and scores but empty content.
3. In secure mode, change `_keyword_candidates` to call `self.secure_store.keyword_candidates(...)` and return ID-only `SearchCandidate` objects.
4. Add a helper:

```python
    def _hydrate_secure_candidates(self, candidates: list[SearchCandidate], limit: int) -> list[SearchCandidate]:
        if self.secure_store is None:
            return candidates
        ordered_ids = []
        seen = set()
        for candidate in candidates:
            if candidate.id in seen:
                continue
            seen.add(candidate.id)
            ordered_ids.append(candidate.id)
            if len(ordered_ids) >= limit:
                break
        loaded = {item.chunk_id: item for item in self.secure_store.load_candidates(ordered_ids)}
        hydrated = []
        for candidate in candidates:
            item = loaded.get(candidate.id)
            if item is None:
                continue
            hydrated.append(
                SearchCandidate(
                    id=candidate.id,
                    content=item.content or "",
                    metadata=item.metadata or {},
                    dense_rank=candidate.dense_rank,
                    sparse_rank=candidate.sparse_rank,
                    graph_score=candidate.graph_score,
                    vector_distance=candidate.vector_distance,
                    keyword_score=candidate.keyword_score,
                )
            )
        return hydrated
```

5. In `search_with_evidence`, after merging dense/keyword/graph ID candidates, call `_hydrate_secure_candidates(candidates, limit=max(top_k * 40, 120))` before graph context, reranker, and parent expansion.

- [ ] **Step 4: Run test to verify it passes**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_secure_retrieval_flow tests.test_secure_vector_store -v
```

Expected: PASS.

## Task 5: Secure Graph Provenance Recall

**Files:**
- Modify: `tests/test_secure_graph_store.py`
- Modify: `core/secure_graph_store.py`
- Modify: `core/graph_store.py`

- [ ] **Step 1: Write the failing tests**

Add to `SecureGraphStoreTests`:

```python
    def test_graph_provenance_recalls_chunks_without_plaintext_graph_storage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            store = SecureGraphStore(workspace.secure_graph_store_path, keys)
            try:
                store.add_entity("Zero Trust", "security", "device verification", chunk_ids=["chunk-a"])
                store.add_entity("Device Posture", "security", "endpoint state", chunk_ids=["chunk-b"])
                store.add_relationship(
                    "Zero Trust",
                    "Device Posture",
                    "requires",
                    evidence_chunk_ids=["chunk-a", "chunk-b"],
                )
                candidates = store.recall_chunk_ids("zero trust device", limit=10)
            finally:
                store.close()

            persisted = Path(workspace.secure_graph_store_path).read_bytes()

        self.assertEqual(candidates[:2], ["chunk-a", "chunk-b"])
        self.assertNotIn(b"Zero Trust", persisted)
        self.assertNotIn(b"Device Posture", persisted)
        self.assertNotIn(b"requires", persisted)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_secure_graph_store.SecureGraphStoreTests.test_graph_provenance_recalls_chunks_without_plaintext_graph_storage -v
```

Expected: FAIL because graph provenance APIs do not exist.

- [ ] **Step 3: Implement graph provenance APIs**

In `core/graph_store.py`, extend protocol signatures:

```python
    def add_entity(self, name: str, entity_type: str, description: str, chunk_ids: list[str] | None = None):
        ...

    def add_relationship(self, source: str, target: str, relation: str, evidence_chunk_ids: list[str] | None = None):
        ...

    def recall_chunk_ids(self, query: str, limit: int = 40) -> list[str]:
        ...
```

In `core/secure_graph_store.py`, add encrypted provenance fields in entity and relationship payloads. Use HMAC IDs already generated by `_entity_id` and `_relationship_id`.

Implement `recall_chunk_ids`:

```python
    def recall_chunk_ids(self, query: str, limit: int = 40) -> list[str]:
        match_space = (query or "").lower()
        matched = []
        for entity in self._all_entities():
            if entity.name and entity.name.lower() in match_space:
                matched.append(entity.name)
        chunk_ids = []
        seen = set()
        for entity_name in matched:
            for chunk_id in self._entity_chunk_ids(entity_name):
                if chunk_id not in seen:
                    seen.add(chunk_id)
                    chunk_ids.append(chunk_id)
            for source, relation, target in self.get_subgraph(entity_name, depth=1):
                for related_entity in (source, target):
                    for chunk_id in self._entity_chunk_ids(related_entity):
                        if chunk_id not in seen:
                            seen.add(chunk_id)
                            chunk_ids.append(chunk_id)
                for chunk_id in self._relationship_chunk_ids(source, relation, target):
                    if chunk_id not in seen:
                        seen.add(chunk_id)
                        chunk_ids.append(chunk_id)
                if len(chunk_ids) >= limit:
                    return chunk_ids[:limit]
        return chunk_ids[:limit]
```

Update local `LocalGraphDB` method signatures to accept the new optional args and ignore them.

- [ ] **Step 4: Run test to verify it passes**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_secure_graph_store tests.test_graph_store -v
```

Expected: PASS.

## Task 6: Graph Extraction Writes Provenance

**Files:**
- Modify: `tests/test_secure_graph_store.py`
- Modify: `core/graph_extractor.py`
- Modify: `core/indexer.py`

- [ ] **Step 1: Write the failing test**

Add a test using a fake extractor or fake LLM path in `tests/test_secure_graph_store.py`:

```python
    def test_graph_extractor_passes_chunk_provenance_to_store(self):
        store = _RecordingGraphStore()
        extractor = object.__new__(__import__("core.graph_extractor", fromlist=["GraphExtractor"]).GraphExtractor)
        extractor.db = store
        extractor.max_chunks_per_document = 8
        extractor.max_chars_per_chunk = 1800
        extractor.extract_and_store = lambda text, chunk_ids=None: store.record(chunk_ids)

        chunks = [
            type("Chunk", (), {"page_content": "Zero Trust requires Device Posture.", "metadata": {"child_ids": ["chunk-a"]}})(),
            type("Chunk", (), {"page_content": "Device Posture is checked locally.", "metadata": {"child_ids": ["chunk-b"]}})(),
        ]
        extractor.extract_chunks(chunks, "policy.pdf")

        self.assertEqual(store.chunk_ids, [["chunk-a"], ["chunk-b"]])
```

Add helper:

```python
class _RecordingGraphStore:
    def __init__(self):
        self.chunk_ids = []

    def record(self, chunk_ids=None):
        self.chunk_ids.append(chunk_ids or [])
        return {"entities": [], "relationships": []}
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_secure_graph_store.SecureGraphStoreTests.test_graph_extractor_passes_chunk_provenance_to_store -v
```

Expected: FAIL because `extract_chunks` does not pass chunk IDs into `extract_and_store`.

- [ ] **Step 3: Add provenance plumbing**

In `core/knowledge_base.py`, when creating child chunks, add collected child IDs to parent metadata before graph extraction can use it:

```python
            child_ids = []
            for child in children:
                child_ids.append(child.child_id)
                ...
            chunk.metadata["child_ids"] = child_ids
```

In `core/graph_extractor.py`:

1. In `extract_chunks`, compute:

```python
            chunk_ids = list(getattr(chunk, "metadata", {}).get("child_ids") or [])
```

2. Call:

```python
            data = self.extract_and_store(text, chunk_ids=chunk_ids)
```

3. Change `extract_and_store` signature to:

```python
    def extract_and_store(self, text: str, chunk_ids: list[str] | None = None):
```

4. Pass provenance into store writes:

```python
            for ent in entities:
                self.db.add_entity(ent["name"], ent["type"], ent["description"], chunk_ids=chunk_ids)

            for rel in relations:
                self.db.add_relationship(rel["source"], rel["target"], rel["relation"], evidence_chunk_ids=chunk_ids)
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_secure_graph_store tests.test_indexer_scope -v
```

Expected: PASS.

## Task 7: Knowledge Base Includes Graph Candidate IDs

**Files:**
- Modify: `tests/test_secure_retrieval_flow.py`
- Modify: `core/knowledge_base.py`

- [ ] **Step 1: Write the failing test**

Add to `SecureRetrievalFlowTests`:

```python
    def test_secure_retrieval_merges_graph_chunk_candidates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            embedding_model_path = Path(workspace.models_path) / "bge-small-zh-v1.5"
            embedding_model_path.mkdir(parents=True, exist_ok=True)
            initialize_keystore(workspace, "secret-passphrase")
            keys = unlock_keystore(workspace, "secret-passphrase")
            set_workspace_keys(workspace.root_path, keys)
            try:
                with patch("core.knowledge_base._sentence_transformer", return_value=_GraphEmbeddingModel()):
                    kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                    try:
                        kb.ingest_chunks(
                            "doc-graph",
                            [
                                _FakeChunk(
                                    "device posture control evidence",
                                    {"source_filename": "graph.pdf", "page": 1, "chunk_index": 0},
                                )
                            ],
                        )
                        chunk_ids = kb.secure_store.collection.get(include=[])["ids"]
                        graph_db = _FakeGraphStore(chunk_ids)
                        result = kb.search_with_evidence("zero trust", graph_db=graph_db, top_k=1)
                    finally:
                        kb.close()
            finally:
                clear_workspace_keys(workspace.root_path)

        self.assertEqual(result["metadatas"][0]["source_filename"], "graph.pdf")
```

Add helpers:

```python
class _GraphEmbeddingModel:
    def encode(self, texts, normalize_embeddings=True):
        if isinstance(texts, str):
            texts = [texts]
        return _EmbeddingResult([[0.0, 1.0] for _ in texts])


class _FakeGraphStore:
    def __init__(self, chunk_ids):
        self.chunk_ids = chunk_ids

    def recall_chunk_ids(self, query, limit=40):
        return self.chunk_ids[:limit]

    def get_all_entities(self):
        return []

    def get_subgraph(self, entity_name, depth=1):
        return []
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_secure_retrieval_flow.SecureRetrievalFlowTests.test_secure_retrieval_merges_graph_chunk_candidates -v
```

Expected: FAIL because graph chunk IDs are not merged into candidate pool.

- [ ] **Step 3: Merge graph candidates**

In `core/knowledge_base.py`, add:

```python
    def _graph_candidates(self, query: str, graph_db: GraphStore | None, limit: int):
        if not graph_db or not hasattr(graph_db, "recall_chunk_ids"):
            return []
        try:
            chunk_ids = graph_db.recall_chunk_ids(query, limit=limit)
        except Exception as exc:
            runtime_metrics.record_error(f"graph recall failed: {exc}")
            return []
        return [
            SearchCandidate(id=chunk_id, graph_score=max(1, limit - index))
            for index, chunk_id in enumerate(chunk_ids)
        ]
```

In `search_with_evidence`, call `_graph_candidates` and include the result in `_merge_candidates`.

- [ ] **Step 4: Run test to verify it passes**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_secure_retrieval_flow tests.test_secure_graph_store tests.test_secure_vector_store -v
```

Expected: PASS.

## Task 8: Cleanup Runtime Caches And Temp Files

**Files:**
- Modify: `tests/test_mcp_server_security.py`
- Modify: `core/mcp_server.py`

- [ ] **Step 1: Write the failing test**

Add to `McpServerSecurityTests`:

```python
    def test_security_lock_removes_secure_index_temp_dirs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "runtime_state.json")
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            temp_runtime = Path(workspace.runtime_path) / "secure-index-leftover"
            temp_runtime.mkdir(parents=True)
            (temp_runtime / "leak.pdf").write_bytes(b"%PDF leftover")
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                mcp_server,
                "current_workspace",
                return_value=workspace,
            ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False):
                runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
                client = TestClient(mcp_server.app)
                response = client.post(
                    "/security/lock",
                    headers={"X-SoulDrive-Token": "test-token"},
                    json={},
                )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(temp_runtime.exists())
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_mcp_server_security.McpServerSecurityTests.test_security_lock_removes_secure_index_temp_dirs -v
```

Expected: FAIL because `_cleanup_portable_session` clears keys but does not remove leftover secure-index temp dirs.

- [ ] **Step 3: Implement temp cleanup**

In `core/mcp_server.py`, import `shutil` and add:

```python
def _cleanup_workspace_temp_files(workspace: SoulDriveWorkspace | None):
    if workspace is None:
        return
    runtime_path = Path(workspace.runtime_path)
    if not runtime_path.exists():
        return
    for path in runtime_path.glob("secure-index-*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file():
            try:
                path.unlink()
            except OSError:
                pass
```

Call it from `_cleanup_portable_session` after `cleanup_runtime()`.

- [ ] **Step 4: Run test to verify it passes**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_mcp_server_security -v
```

Expected: PASS.

## Task 9: Regression Verification

**Files:**
- Verify only.

- [ ] **Step 1: Run targeted privacy retrieval tests**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_secure_vector_store tests.test_secure_graph_store tests.test_secure_retrieval_flow tests.test_mcp_server_security tests.test_mcp_server_papers -v
```

Expected: PASS.

- [ ] **Step 2: Run existing retrieval and indexing regression**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_retrieval tests.test_rag_prompting tests.test_indexer_scope tests.test_secure_indexer_documents tests.test_knowledge_base_paths -v
```

Expected: PASS.

- [ ] **Step 3: Run full backend suite**

Run:

```powershell
conda run -n souldrive python -m unittest discover -s tests -v
```

Expected: PASS or the known fresh-worktree `souldrive_db` fixture failure only. If the known failure remains, record it explicitly and do not claim full suite pass.

- [ ] **Step 4: Check final git status**

Run:

```powershell
git status --short
```

Expected: implementation files, tests, design doc, and plan doc are modified/untracked. No commit is created.

