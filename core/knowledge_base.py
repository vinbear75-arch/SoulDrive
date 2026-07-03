import os
from pathlib import Path

from core.graph_store import GraphStore
from core.keyword_index import KeywordIndex
from core.logging_config import get_logger
from core.observability import runtime_metrics
from core.parent_child_index import split_parent_document
from core.parent_doc_store import ParentDocumentStore
from core.paths import resolve_model_path
from core.rag_defaults import DEFAULT_RAG_TOP_K
from core.retrieval import SearchCandidate, _source_match_score, build_citations, rank_hybrid_candidates
from core.reranker import LocalReranker
from core.security_context import get_workspace_keys
from core.secure_vector_store import SecureVectorStore
from core.query_expansion import expand_query_variants
from core.torch_device import resolve_torch_device
from core.workspace import SoulDriveWorkspace

logger = get_logger(__name__)


def _persistent_client(path: str):
    import chromadb
    from chromadb.config import Settings

    return chromadb.PersistentClient(
        path=path,
        settings=Settings(anonymized_telemetry=False),
    )


def _sentence_transformer(model_path: str):
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_path, local_files_only=True, device=resolve_torch_device())


class LocalKnowledgeBase:
    def __init__(
        self,
        db_path: str | None = None,
        parent_doc_path: str | None = None,
        keyword_index_path: str | None = None,
        workspace_path: str | None = None,
    ):
        if db_path is None and workspace_path is None:
            raise ValueError("workspace_path is required when db_path is not provided")

        workspace = SoulDriveWorkspace(workspace_path).ensure() if workspace_path else None
        db_path = db_path or workspace.chroma_path
        index_root = Path(db_path).parent
        workspace_path = workspace_path or str(index_root.parent)
        self.workspace = SoulDriveWorkspace(workspace_path).ensure()
        parent_doc_path = parent_doc_path or str(index_root / "parent_docs.sqlite")
        keyword_index_path = keyword_index_path or str(index_root / "keyword_index.sqlite")

        self.secure_store = None
        self.parent_store = None
        self.keyword_index = None
        self._source_candidate_cache = None
        workspace_keys = get_workspace_keys(self.workspace.root_path)
        if workspace_keys is not None:
            logger.info("[VectorDB] Loading encrypted local index: %s", self.workspace.secure_vector_store_path)
            self.client = None
            self.secure_store = SecureVectorStore(self.workspace.secure_vector_store_path, workspace_keys)
            self.collection = self.secure_store.collection
        else:
            logger.info("[VectorDB] Loading Chroma index: %s", db_path)
            self.client = _persistent_client(path=db_path)
            self.collection = self.client.get_or_create_collection(name="souldrive_papers")
            self.parent_store = ParentDocumentStore(parent_doc_path)
            self.keyword_index = KeywordIndex(keyword_index_path)

        logger.info("[VectorDB] Loading BGE-Small embedding model...")
        model_path = resolve_model_path("bge-small-zh-v1.5", self.workspace.root_path)
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"local embedding model not found: {model_path}")
        self.embedding_model = _sentence_transformer(model_path)
        self.reranker = LocalReranker(self.workspace.root_path)
        logger.info("[VectorDB] Embedding model loaded.")

    def ingest_chunks(self, document_id: str, chunks: list):
        if not chunks:
            return

        texts = []
        metadatas = []
        ids = []

        for parent_index, chunk in enumerate(chunks):
            parent_id = f"{document_id}_parent_{parent_index}"
            parent_content = chunk.page_content
            parent_metadata = {
                **chunk.metadata,
                "parent_id": parent_id,
                "parent_index": parent_index,
            }

            if self.secure_store is not None:
                self.secure_store.upsert_parent(parent_id, parent_content, parent_metadata)
            else:
                self.parent_store.upsert_parent(parent_id, parent_content, parent_metadata)

            children = split_parent_document(parent_id, parent_content)
            for child in children:
                child_metadata = {
                    **parent_metadata,
                    "parent_id": parent_id,
                    "child_id": child.child_id,
                    "child_index": child.child_index,
                    "child_start_char": child.start_char,
                    "child_end_char": child.end_char,
                    "child_excerpt": child.content[:420],
                }
                texts.append(child.content)
                metadatas.append(child_metadata)
                ids.append(child.child_id)
                if self.keyword_index is not None:
                    self.keyword_index.upsert_document(child.child_id, child.content, child_metadata)

        if not texts:
            return

        embeddings = self.embedding_model.encode(texts, normalize_embeddings=True).tolist()
        self.collection.add(
            embeddings=embeddings,
            documents=texts,
            metadatas=metadatas,
            ids=ids,
        )
        logger.info("[VectorDB] %s indexed.", document_id)

    def search_with_evidence(self, query: str, graph_db: GraphStore | None = None, top_k: int = DEFAULT_RAG_TOP_K):
        dense_pool_size = max(top_k * 6, 18)
        query_variants = expand_query_variants(query)

        dense_candidates = []
        keyword_candidates = []
        for variant in query_variants:
            query_vector = self.embedding_model.encode([variant], normalize_embeddings=True).tolist()
            dense_candidates.extend(self._dense_candidates(query_vector, dense_pool_size))
            keyword_candidates.extend(self._keyword_candidates(variant, dense_pool_size))
        source_title_candidates = self._source_title_candidates(query, limit=max(top_k * 4, 12))
        candidates = self._merge_candidates(dense_candidates, keyword_candidates, source_title_candidates)

        graph_context, matched_entities = self._build_graph_context(
            query=query,
            docs=[candidate.content for candidate in dense_candidates[:2]],
            graph_db=graph_db,
        )

        if matched_entities:
            for candidate in candidates:
                candidate.graph_score = sum(
                    1 for entity in matched_entities if entity.lower() in candidate.content.lower()
                )

        if candidates and self.reranker.ready:
            scores = self.reranker.score(query, [candidate.content for candidate in candidates])
            for candidate, score in zip(candidates, scores):
                candidate.rerank_score = float(score)

        reranked_candidates = rank_hybrid_candidates(query, candidates, top_k=max(top_k * 2, top_k))
        parent_candidates = self._expand_parent_context(reranked_candidates, top_k=top_k)
        return {
            "documents": [candidate.content for candidate in parent_candidates],
            "metadatas": [candidate.metadata for candidate in parent_candidates],
            "graph_context": graph_context,
            "matched_entities": matched_entities,
            "evidence": build_citations(parent_candidates),
            "retrieval_mode": "parent_child_hybrid_dense_fts_graph_rrf",
        }

    def search(self, query: str, graph_db: GraphStore | None = None, top_k: int = DEFAULT_RAG_TOP_K):
        result = self.search_with_evidence(query=query, graph_db=graph_db, top_k=top_k)
        return result["documents"], result["metadatas"], result["graph_context"]

    def _dense_candidates(self, query_vector: list[list[float]], n_results: int):
        try:
            vector_results = self.collection.query(
                query_embeddings=query_vector,
                n_results=n_results,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:
            runtime_metrics.increment("retrieval_failures")
            runtime_metrics.record_error(f"dense retrieval failed: {exc}")
            logger.warning("[VectorDB] Dense retrieval degraded: %s", exc)
            return []

        documents = vector_results.get("documents", [[]])[0] or []
        metadatas = vector_results.get("metadatas", [[]])[0] or []
        distances = vector_results.get("distances", [[]])[0] or []
        ids = vector_results.get("ids", [[]])[0] or []

        candidates = []
        for index, document in enumerate(documents):
            candidates.append(
                SearchCandidate(
                    id=ids[index] if index < len(ids) else f"dense_{index}",
                    content=document,
                    metadata=metadatas[index] if index < len(metadatas) and metadatas[index] else {},
                    dense_rank=index + 1,
                    vector_distance=distances[index] if index < len(distances) else None,
                )
            )
        return candidates

    def _keyword_candidates(self, query: str, n_results: int):
        if self.secure_store is not None:
            results = self.secure_store.keyword_search(query, limit=n_results)
        else:
            results = self.keyword_index.search(query, limit=n_results)
        return [
            SearchCandidate(
                id=result.doc_id,
                content=result.content,
                metadata=result.metadata,
                sparse_rank=index + 1,
                keyword_score=result.score,
            )
            for index, result in enumerate(results)
        ]

    def _source_title_candidates(self, query: str, limit: int = 12):
        scored: dict[str, tuple[float, SearchCandidate]] = {}
        for record_id, document, metadata in self._source_candidate_records():
            score = _source_match_score(query, metadata)
            if score < 0.72:
                continue
            key = str(metadata.get("source_filename") or metadata.get("parent_id") or record_id)
            candidate = SearchCandidate(
                id=f"source_title:{record_id}",
                content=document,
                metadata=metadata,
                keyword_score=score,
            )
            previous = scored.get(key)
            if previous is None or score > previous[0]:
                scored[key] = (score, candidate)

        return [
            candidate
            for _, candidate in sorted(
                scored.values(),
                key=lambda item: (
                    item[0],
                    str((item[1].metadata or {}).get("source_filename") or ""),
                    item[1].id,
                ),
                reverse=True,
            )[:limit]
        ]

    def _source_candidate_records(self):
        if self._source_candidate_cache is not None:
            return self._source_candidate_cache

        try:
            payload = self.collection.get(include=["documents", "metadatas"])
        except Exception as exc:
            runtime_metrics.record_error(f"source title retrieval failed: {exc}")
            logger.warning("[VectorDB] Source title retrieval degraded: %s", exc)
            self._source_candidate_cache = []
            return self._source_candidate_cache

        ids = payload.get("ids", []) or []
        documents = payload.get("documents", []) or []
        metadatas = payload.get("metadatas", []) or []
        self._source_candidate_cache = [
            (str(record_id), str(document or ""), metadata or {})
            for record_id, document, metadata in zip(ids, documents, metadatas)
            if metadata
        ]
        return self._source_candidate_cache

    def _merge_candidates(self, *candidate_groups: list[SearchCandidate]):
        merged: dict[str, SearchCandidate] = {}
        for group in candidate_groups:
            for candidate in group:
                merge_key = str(candidate.metadata.get("parent_id") or candidate.id)
                if merge_key not in merged:
                    merged[merge_key] = candidate
                    continue
                existing = merged[merge_key]
                if existing.dense_rank is None or (
                    candidate.dense_rank is not None and candidate.dense_rank < existing.dense_rank
                ):
                    existing.dense_rank = candidate.dense_rank
                if existing.sparse_rank is None or (
                    candidate.sparse_rank is not None and candidate.sparse_rank < existing.sparse_rank
                ):
                    existing.sparse_rank = candidate.sparse_rank
                if candidate.keyword_score > existing.keyword_score:
                    existing.content = candidate.content
                    existing.metadata = {**existing.metadata, **candidate.metadata}
                    existing.keyword_score = candidate.keyword_score
                if existing.vector_distance is None or (
                    candidate.vector_distance is not None and candidate.vector_distance < existing.vector_distance
                ):
                    existing.vector_distance = candidate.vector_distance
                existing.graph_score = max(existing.graph_score, candidate.graph_score)
                existing.rerank_score = max(existing.rerank_score, candidate.rerank_score)
        return list(merged.values())

    def _expand_parent_context(self, candidates: list[SearchCandidate], top_k: int):
        expanded = []
        seen_parent_ids = set()

        for candidate in candidates:
            parent_id = candidate.metadata.get("parent_id")
            if not parent_id or parent_id in seen_parent_ids:
                continue

            if self.secure_store is not None:
                parent = self.secure_store.get_parent(parent_id)
            else:
                parent = self.parent_store.get_parent(parent_id)
            if not parent:
                expanded.append(candidate)
                seen_parent_ids.add(parent_id)
                continue

            expanded.append(
                SearchCandidate(
                    id=candidate.id,
                    content=parent["content"],
                    metadata={**parent["metadata"], **candidate.metadata},
                    dense_rank=candidate.dense_rank,
                    sparse_rank=candidate.sparse_rank,
                    graph_score=candidate.graph_score,
                    vector_distance=candidate.vector_distance,
                    keyword_score=candidate.keyword_score,
                    rrf_score=candidate.rrf_score,
                    rerank_score=candidate.rerank_score,
                    final_score=candidate.final_score,
                    score_breakdown=candidate.score_breakdown,
                )
            )
            seen_parent_ids.add(parent_id)
            if len(expanded) >= top_k:
                break

        return expanded[:top_k]

    def _build_graph_context(self, query: str, docs: list[str], graph_db: GraphStore | None = None):
        if not graph_db:
            return [], []

        try:
            graph_match_space = "\n".join([query, *docs[:2]]).lower()
            matched_entities = []
            for entity in graph_db.get_all_entities():
                if entity and entity.lower() in graph_match_space:
                    matched_entities.append(entity)
                if len(matched_entities) >= 8:
                    break

            seen_relations = set()
            graph_context = []
            for entity in matched_entities:
                for source, relation, target in graph_db.get_subgraph(entity, depth=1):
                    relation_key = (source, relation, target)
                    if relation_key in seen_relations:
                        continue
                    seen_relations.add(relation_key)
                    graph_context.append(f"宸茬煡閫昏緫鍏崇郴: [{source}] --({relation})--> [{target}]")
                    if len(graph_context) >= 24:
                        break
                if len(graph_context) >= 24:
                    break
        except Exception as exc:
            runtime_metrics.record_error(f"graph retrieval failed: {exc}")
            logger.warning("[VectorDB] Graph retrieval degraded: %s", exc)
            return [], []

        return graph_context, matched_entities

    def delete_document_indexes(self, document_hash: str):
        if self.secure_store is not None:
            self.secure_store.delete_by_document_hash(document_hash)
            return
        self.parent_store.delete_by_document_hash(document_hash)
        self.keyword_index.delete_by_document_hash(document_hash)

    def close(self):
        if self.secure_store is not None:
            self.secure_store.close()
            return
        self.parent_store.close()
        self.keyword_index.close()
