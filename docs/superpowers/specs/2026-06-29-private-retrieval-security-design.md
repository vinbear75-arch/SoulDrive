# Private Retrieval Security Design

## 1. Goal

Upgrade SoulDrive's privacy layer from encrypted-at-rest storage plus coarse unlock gating to a retrieval path that keeps private workspace data encrypted on disk and minimizes plaintext exposure during query execution.

The target product shape is a portable, removable-drive, endpoint-side private knowledge engine for a department-scale corpus. The expected corpus size is about 100 to 300 documents, not a large enterprise cluster.

Success means:

- A person who gets the removable drive cannot access documents, document counts, indexes, graph data, or retrieval evidence without the workspace passphrase.
- New imports are stored as encrypted workspace objects, not plaintext PDFs in the managed workspace.
- Query retrieval uses dense, keyword, and graph signals to produce candidate chunk IDs before decrypting chunk text.
- Only candidate chunk text needed for reranking and answer generation is decrypted.
- Lock, shutdown, or drive removal clears process keys, in-memory indexes, temporary plaintext files, and SoulDrive-controlled local runtime state.

## 2. Non-Goals

This design does not attempt fully homomorphic encrypted retrieval or private computation while the host machine is compromised.

This design does not defend against malware that can read SoulDrive process memory after the user has unlocked the workspace.

This design does not prioritize network interception, remote multi-user authorization, or cloud account recovery.

This design does not require graph retrieval to outperform dense and keyword retrieval. Graph retrieval becomes a third recall signal with provenance, not the primary retrieval engine.

## 3. Current Gaps

The current secure workspace already encrypts documents, vector records, graph records, and metadata at rest. The main gap is query-time behavior.

Current secure retrieval can decrypt broad record sets to perform dense search, keyword search, or graph matching. For a small demo corpus this is acceptable. For 100 to 300 documents, it increases query latency and expands plaintext exposure unnecessarily.

The current graph layer stores entities and relationships, but it does not strongly bind those graph facts back to source chunks. As a result, graph context can supplement an answer, but it cannot reliably recall supporting chunks across documents.

Legacy plaintext document compatibility under `data/papers` also conflicts with the portable privacy product positioning. It can remain for migration, but it should not be the normal import or retrieval path.

## 4. Security Boundary

The accepted security model is:

- Before passphrase unlock, the application may report only high-level locked status.
- Before passphrase unlock, it must not reveal document names, document counts, indexed counts, chunk counts, graph entities, relationship text, source filenames, or evidence snippets.
- After passphrase unlock, workspace keys may exist in process memory.
- After passphrase unlock, lightweight retrieval indexes may be loaded into memory.
- Persisted workspace data remains encrypted on the removable drive.
- SoulDrive-controlled local runtime files must be cleared or kept non-sensitive on lock, shutdown, and drive removal.

The practical invariant is:

```text
Encrypted disk records -> unlocked in-memory retrieval indexes -> candidate chunk IDs -> decrypt candidate text only
```

## 5. Retrieval Architecture

### 5.1 Dense Recall

Dense recall remains the main semantic recall path.

Persisted embeddings are stored encrypted. After unlock, SoulDrive may load an in-memory embedding matrix and chunk ID table for the active workspace. Query execution computes the query embedding and compares it against the in-memory matrix.

Dense recall should return chunk IDs and vector scores first. It should not decrypt every chunk's text or metadata during the initial similarity scan.

For the target 100 to 300 document corpus, an in-memory matrix scan is acceptable and simpler than adding a heavier approximate nearest neighbor dependency. If the corpus grows beyond that target, an ANN index can be evaluated later.

### 5.2 Keyword Recall

Keyword recall is required because dense recall can miss exact terms, document numbers, regulation names, project codes, abbreviations, and people or department names.

The keyword index should be encrypted at rest and queryable without full-text plaintext scans:

- During indexing, normalize and tokenize chunk text.
- Store token lookup keys as `HMAC(metadata_key, normalized_token)`.
- Store postings as encrypted payloads that map token keys to chunk IDs and compact term statistics.
- During query, tokenize the query, compute the same HMAC token keys, decrypt only matching postings, and return candidate chunk IDs.

This preserves exact-match recall while avoiding per-query full-corpus plaintext text scans.

### 5.3 Graph Recall

Graph recall should be promoted to the second-stage graph design: graph facts must carry provenance back to source chunks.

The graph store should support:

```text
entity_id -> chunk_ids
relationship_id -> evidence_chunk_ids
entity_id -> neighbor_entity_ids
neighbor_entity_id -> chunk_ids
```

Entity and relationship lookup keys should be HMAC-derived from normalized names or relation identifiers. Plain entity names, descriptions, relation text, and source references remain encrypted payloads.

At query time:

1. Extract or match likely entities from the query and early dense/keyword candidates.
2. Resolve entity lookup keys.
3. Fetch one-hop neighbors and provenance chunk IDs.
4. Add those chunk IDs to the candidate pool with graph scores.
5. Decrypt graph payloads only when needed for prompt context.

Graph recall is a recall supplement. Dense and keyword recall remain the quality baseline.

## 6. Candidate Merge And Decryption

Each retrieval path returns candidates as chunk IDs plus scores:

- Dense: `chunk_id`, vector distance or similarity.
- Keyword: `chunk_id`, term score.
- Graph: `chunk_id`, graph proximity or provenance score.

SoulDrive merges these candidates, removes duplicates, and keeps a bounded candidate pool. A practical starting point is:

- Dense top 80.
- Keyword top 80.
- Graph top 40.
- Merged candidate cap 120 to 160.

Only after this merge should SoulDrive decrypt chunk text and metadata for reranking. The reranker then selects final evidence chunks for parent expansion and answer generation.

This keeps recall broad enough for quality while avoiding full-corpus plaintext decryption.

## 7. Indexing Flow

Import and indexing should follow this order:

1. User imports source document.
2. Source document is copied into the workspace only as an encrypted object.
3. Indexing decrypts one source document at a time into a controlled workspace runtime temp path when required by the parser.
4. Parser output is chunked.
5. Chunk text and metadata are encrypted at rest.
6. Embeddings are encrypted at rest.
7. Keyword postings are built and encrypted.
8. Graph extraction runs as a background enhancement and writes encrypted graph payloads plus provenance.
9. Temporary plaintext files are deleted after each document and on failure cleanup.

Graph extraction must not block basic dense and keyword retrieval. The UI may show graph enhancement as building or unavailable while dense and keyword retrieval are ready.

## 8. Lock, Exit, And Drive Removal

Lock, shutdown, and removable-drive removal must clear:

- Workspace keys in `security_context`.
- In-memory embedding matrices.
- In-memory keyword lookup caches.
- In-memory graph lookup caches.
- Loaded RAG and knowledge-base objects.
- Indexer worker process and inherited key environment.
- Temporary decrypted PDFs under the workspace runtime directory.
- SoulDrive-controlled local runtime state and logs that could contain workspace paths, source filenames, snippets, or errors with sensitive content.

Runtime logs should avoid writing document text, source snippets, or raw metadata. Where paths are useful for diagnosis, public status should redact or hash them.

## 9. API And UI Behavior

Before software unlock:

- `/security/status` may return locked state and whether the workspace needs initialization.
- Document list endpoints should not reveal document names or counts.
- Index status should not reveal indexed count, filenames, chunk counts, or graph counts.
- Chat, import, indexing, evidence, graph, and audit-sensitive views remain locked.

After software unlock:

- Document list and index counts are available.
- Import stores encrypted objects.
- Retrieval uses dense, keyword, and graph candidate IDs before decrypting candidate text.

## 10. Testing Strategy

Tests should prove:

- Locked workspaces do not expose document names, counts, indexed counts, graph entities, or source metadata.
- New imports do not create plaintext managed PDFs.
- Dense retrieval can return correct results without decrypting all chunk text.
- Keyword retrieval can return exact-term matches through HMAC postings.
- Graph retrieval can recall chunks through entity and relationship provenance.
- Query execution decrypts only bounded candidate chunk payloads.
- Lock and runtime shutdown clear workspace keys, in-memory indexes, temp files, and worker state.
- Existing answer-quality tests remain passing for dense plus keyword retrieval.

## 11. Open Implementation Choices

The following choices should be resolved during implementation planning:

- Whether embeddings remain encrypted JSON blobs in SQLite or move to a compact encrypted binary segment per workspace.
- Whether the keyword postings table is stored in the existing secure vector SQLite database or a separate secure keyword SQLite database.
- How aggressively to cap graph extraction per document for the target 100 to 300 document corpus.
- Whether legacy `data/papers` plaintext compatibility is migration-only or remains visible after unlock.

