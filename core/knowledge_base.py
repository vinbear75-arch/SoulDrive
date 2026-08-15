import os
import re
import time
from pathlib import Path

from core.answer_quality import evaluate_evidence_gate
from core.embedding_models import (
    embedding_query_instruction,
    preferred_embedding_model_path,
)
from core.graph_db import LocalGraphDB
from core.graph_store import GraphStore
from core.keyword_index import KeywordIndex
from core.logging_config import get_logger
from core.observability import runtime_metrics
from core.parent_child_index import split_parent_document
from core.parent_doc_store import ParentDocumentStore
from core.rag_defaults import DEFAULT_RAG_TOP_K
from core.retrieval import (
    QueryIntent,
    SearchCandidate,
    _source_match_score,
    build_citations,
    classify_query_intent,
    rank_hybrid_candidates,
    source_identity,
    tokenize,
)
from core.reranker import LocalReranker
from core.security_context import get_workspace_keys
from core.secure_graph_store import SecureGraphStore
from core.secure_vector_store import SecureVectorStore
from core.query_expansion import expand_query_variants
from core.workspace import SoulDriveWorkspace

logger = get_logger(__name__)

DOCUMENT_PROFILE_LABELS = {
    "government_policy": "政府政策正式文件",
    "government_standard": "政府标准规范",
    "standard_document": "标准规范",
    "project_plan": "项目方案",
    "technical_document": "企业技术文档",
    "formal_document": "正式文件",
}


def _persistent_client(path: str):
    import chromadb
    from chromadb.config import Settings

    return chromadb.PersistentClient(
        path=path,
        settings=Settings(anonymized_telemetry=False),
    )


def _sentence_transformer(model_path: str):
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_path, local_files_only=True, device="cpu")


def _elapsed_ms(started_at: float) -> int:
    return int((time.time() - started_at) * 1000)


def _contextualized_child_text(content: str, metadata: dict) -> tuple[str, str]:
    document_title = str(metadata.get("document_title") or "").strip()
    source = document_title or str(
        metadata.get("source_filename")
        or metadata.get("title")
        or ""
    ).strip()
    if source and not document_title:
        source = Path(source).stem

    section_path = metadata.get("section_path")
    if isinstance(section_path, (list, tuple)):
        sections = [str(item).strip() for item in section_path if str(item).strip()]
    elif section_path:
        sections = [part.strip() for part in str(section_path).split(">") if part.strip()]
    else:
        sections = [
            str(metadata.get(key) or "").strip()
            for key in ("Header 1", "Header 2", "Header 3")
            if str(metadata.get(key) or "").strip()
        ]

    context_lines = []
    if source:
        context_lines.append(f"[DOCUMENT] {source}")
    document_number = str(metadata.get("document_number") or "").strip()
    if document_number:
        context_lines.append(f"[DOCUMENT_NUMBER] {document_number}")
    document_profile = str(metadata.get("document_profile") or "").strip()
    if document_profile and document_profile != "general":
        context_lines.append(
            f"[DOCUMENT_TYPE] {DOCUMENT_PROFILE_LABELS.get(document_profile, document_profile)}"
        )
    if sections:
        context_lines.append(f"[SECTION] {' > '.join(sections)}")
    clause_ids = str(metadata.get("clause_ids") or metadata.get("clause_id") or "").strip()
    if clause_ids:
        context_lines.append(f"[CLAUSE] {clause_ids}")
    prefix = "\n".join(context_lines)
    if not prefix:
        return content, ""
    return f"{prefix}\n\n{content}", prefix


FORMAL_OVERLAP_CONTENT_KINDS = {"prose", "clause"}


def _section_identity(metadata: dict) -> str:
    section_path = metadata.get("section_path")
    if isinstance(section_path, (list, tuple)):
        parts = [str(item).strip() for item in section_path if str(item).strip()]
    elif section_path:
        parts = [part.strip() for part in str(section_path).split(">") if part.strip()]
    else:
        parts = [
            str(metadata.get(key) or "").strip()
            for key in ("Header 1", "Header 2", "Header 3", "section")
            if str(metadata.get(key) or "").strip()
        ]
    return " > ".join(parts)


def _formal_boundary_compatible(previous: dict, current: dict) -> bool:
    if not previous or not current:
        return False
    if not previous.get("structure_aware") or not current.get("structure_aware"):
        return False
    previous_kind = str(previous.get("content_kind") or "prose").lower()
    current_kind = str(current.get("content_kind") or "prose").lower()
    if previous_kind not in FORMAL_OVERLAP_CONTENT_KINDS:
        return False
    if current_kind not in FORMAL_OVERLAP_CONTENT_KINDS:
        return False
    previous_section = _section_identity(previous)
    current_section = _section_identity(current)
    if not previous_section or previous_section != current_section:
        return False
    previous_page = _safe_int(
        previous.get("page_number") or previous.get("page"),
        -1,
    )
    current_page = _safe_int(
        current.get("page_number") or current.get("page"),
        -1,
    )
    return previous_page < 0 or current_page < 0 or 0 <= current_page - previous_page <= 1


def _semantic_tail(text: str, max_chars: int = 420) -> tuple[str, bool]:
    blocks = [part.strip() for part in re.split(r"\n\s*\n", str(text or "")) if part.strip()]
    if not blocks:
        return "", False
    content = blocks[-1]
    if len(content) <= max_chars:
        return content, False

    sentences = [
        part.strip()
        for part in re.findall(r".+?(?:[。！？!?；;.]|$)", content, flags=re.DOTALL)
        if part.strip()
    ]
    selected: list[str] = []
    selected_chars = 0
    for sentence in reversed(sentences):
        additional = len(sentence) + (1 if selected else 0)
        if selected and selected_chars + additional > max_chars:
            break
        selected.append(sentence)
        selected_chars += additional
        if selected_chars >= max_chars:
            break
    tail = " ".join(reversed(selected)).strip()
    if not tail or len(tail) > max_chars:
        tail = content[-max_chars:].strip()
    return tail, True


def _fit_formal_boundary_overlap(
    previous_content: str,
    current_content: str,
    metadata: dict,
    token_counter,
    embedding_token_limit: int | None,
) -> tuple[str, bool]:
    overlap, truncated = _semantic_tail(previous_content)
    if not overlap:
        return "", truncated
    if token_counter is None or embedding_token_limit is None:
        return overlap, truncated

    while overlap:
        retrieval_text, _ = _contextualized_child_text(
            f"{overlap}\n\n{current_content}",
            metadata,
        )
        if token_counter(retrieval_text) <= embedding_token_limit:
            return overlap, truncated
        if len(overlap) <= 24:
            return "", True
        next_length = max(24, int(len(overlap) * 0.75))
        overlap = overlap[-next_length:].lstrip()
        truncated = True
    return "", truncated


def _with_candidate_context(
    candidate: SearchCandidate,
    content: str,
    metadata: dict,
) -> SearchCandidate:
    return SearchCandidate(
        id=candidate.id,
        content=content,
        metadata=metadata,
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


def _select_parent_context(
    parent_content: str,
    candidate: SearchCandidate,
    query_intent: QueryIntent,
) -> tuple[str, dict]:
    parent_text = str(parent_content or "")
    child_text = _candidate_child_text(candidate)
    parent_length = len(parent_text)
    content_kind = str((candidate.metadata or {}).get("content_kind") or "").lower()

    boundary_overlap_chars = _safe_int(
        (candidate.metadata or {}).get("boundary_overlap_chars"),
        0,
    )
    if boundary_overlap_chars > 0 and child_text:
        return child_text, {
            "context_expansion_mode": "formal_two_paragraph_boundary_window",
            "context_start_char": _safe_int(candidate.metadata.get("child_start_char"), 0),
            "context_end_char": _safe_int(
                candidate.metadata.get("child_end_char"),
                parent_length,
            ),
            "context_char_count": len(child_text),
            "parent_char_count": parent_length,
        }

    if content_kind == "table" and child_text:
        return child_text, {
            "context_expansion_mode": "child_table",
            "context_start_char": _safe_int(candidate.metadata.get("child_start_char"), 0),
            "context_end_char": _safe_int(
                candidate.metadata.get("child_end_char"),
                parent_length,
            ),
            "context_char_count": len(child_text),
            "parent_char_count": parent_length,
        }

    blocks = _paragraph_blocks(parent_text)
    target_span = _candidate_parent_span(parent_text, candidate)
    target_indices = [
        index
        for index, (start, end, _) in enumerate(blocks)
        if target_span and end > target_span[0] and start < target_span[1]
    ]
    if not blocks or not target_indices:
        fallback = child_text or parent_text
        return fallback, {
            "context_expansion_mode": "child_fallback",
            "context_start_char": target_span[0] if target_span else 0,
            "context_end_char": target_span[1] if target_span else len(fallback),
            "context_char_count": len(fallback),
            "parent_char_count": parent_length,
        }

    max_chars = 4200 if query_intent.broad_scope else 2800 if query_intent.name == "clause_lookup" else 2400
    selected_indices = set(target_indices)
    if len(_join_blocks(blocks, selected_indices)) > max_chars and child_text:
        return child_text, {
            "context_expansion_mode": "child_limit",
            "context_start_char": target_span[0],
            "context_end_char": target_span[1],
            "context_char_count": len(child_text),
            "parent_char_count": parent_length,
        }

    first_target = min(target_indices)
    last_target = max(target_indices)
    expansion_radius = len(blocks) if query_intent.broad_scope else 1 if query_intent.name == "clause_lookup" else 0
    for distance in range(1, expansion_radius + 1):
        added = False
        for index in (first_target - distance, last_target + distance):
            if index < 0 or index >= len(blocks) or index in selected_indices:
                continue
            proposed = {*selected_indices, index}
            if len(_join_blocks(blocks, proposed)) <= max_chars:
                selected_indices.add(index)
                added = True
        if query_intent.broad_scope and not added:
            break

    heading_index = next(
        (
            index
            for index in range(first_target - 1, -1, -1)
            if re.match(r"^\s{0,3}#{1,6}\s+", blocks[index][2])
        ),
        None,
    )
    if heading_index is not None and heading_index not in selected_indices:
        proposed = {*selected_indices, heading_index}
        if len(_join_blocks(blocks, proposed)) <= max_chars:
            selected_indices.add(heading_index)

    context = _join_blocks(blocks, selected_indices)
    context_start = min(blocks[index][0] for index in selected_indices)
    context_end = max(blocks[index][1] for index in selected_indices)
    if len(selected_indices) == len(blocks):
        mode = "full_parent"
    elif query_intent.broad_scope:
        mode = "broad_parent_window"
    else:
        mode = "local_parent_window"
    return context, {
        "context_expansion_mode": mode,
        "context_start_char": context_start,
        "context_end_char": context_end,
        "context_char_count": len(context),
        "parent_char_count": parent_length,
    }


def _candidate_child_text(candidate: SearchCandidate) -> str:
    content = str(candidate.content or "").strip()
    retrieval_context = str((candidate.metadata or {}).get("retrieval_context") or "").strip()
    if retrieval_context and content.startswith(retrieval_context):
        return content[len(retrieval_context):].lstrip()
    return content


def _paragraph_blocks(text: str) -> list[tuple[int, int, str]]:
    return [
        (match.start(), match.end(), match.group(0).strip())
        for match in re.finditer(r"\S(?:.*(?:\n(?!\s*\n).*)*)", str(text or ""))
        if match.group(0).strip()
    ]


def _candidate_parent_span(
    parent_text: str,
    candidate: SearchCandidate,
) -> tuple[int, int] | None:
    metadata = candidate.metadata or {}
    start = _safe_int(metadata.get("child_start_char"), -1)
    end = _safe_int(metadata.get("child_end_char"), -1)
    if 0 <= start < end <= len(parent_text):
        return start, end

    excerpt = str(metadata.get("child_excerpt") or "").strip()
    if excerpt:
        for probe_length in (160, 80, 40):
            probe = excerpt[:probe_length].strip()
            if len(probe) < 20:
                continue
            position = parent_text.find(probe)
            if position >= 0:
                return position, min(len(parent_text), position + len(excerpt))
    return None


def _join_blocks(
    blocks: list[tuple[int, int, str]],
    selected_indices: set[int],
) -> str:
    return "\n\n".join(
        blocks[index][2]
        for index in sorted(selected_indices)
        if blocks[index][2]
    ).strip()


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _context_expansion_diagnostics(candidates: list[SearchCandidate]) -> dict:
    mode_counts: dict[str, int] = {}
    context_lengths = []
    paired_context_lengths = []
    parent_lengths = []
    avoided_full_parent = 0
    for candidate in candidates:
        metadata = candidate.metadata or {}
        mode = str(metadata.get("context_expansion_mode") or "unknown")
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
        context_length = _safe_int(metadata.get("context_char_count"), len(candidate.content))
        parent_length = _safe_int(metadata.get("parent_char_count"), 0)
        context_lengths.append(context_length)
        if parent_length > 0:
            paired_context_lengths.append(min(context_length, parent_length))
            parent_lengths.append(parent_length)
            if mode != "full_parent" and context_length < parent_length:
                avoided_full_parent += 1

    return {
        "mode_counts": mode_counts,
        "average_context_chars": round(
            sum(context_lengths) / len(context_lengths),
            2,
        ) if context_lengths else 0.0,
        "max_context_chars": max(context_lengths, default=0),
        "context_to_parent_ratio": round(
            sum(paired_context_lengths) / sum(parent_lengths),
            6,
        ) if parent_lengths else 0.0,
        "full_parent_avoided_count": avoided_full_parent,
    }


def _retrieval_quality_signals(
    query: str,
    candidates: list[SearchCandidate],
) -> dict:
    scores = [max(0.0, float(candidate.final_score)) for candidate in candidates]
    query_tokens = set(tokenize(query))
    evidence_tokens = set()
    sources = set()
    for candidate in candidates:
        metadata = candidate.metadata or {}
        evidence_tokens.update(
            tokenize(
                " ".join(
                    str(metadata.get(key) or "")
                    for key in (
                        "document_title",
                        "document_number",
                        "section",
                        "clause_id",
                        "child_excerpt",
                    )
                )
            )
        )
        source = source_identity(metadata)
        if source:
            sources.add(source)

    return {
        "max_final_score": round(max(scores, default=0.0), 6),
        "mean_final_score": round(sum(scores) / len(scores), 6) if scores else 0.0,
        "query_token_coverage": round(
            len(query_tokens & evidence_tokens) / len(query_tokens),
            6,
        ) if query_tokens else 1.0,
        "evidence_source_count": len(sources),
    }


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

        model_path = preferred_embedding_model_path(self.workspace.root_path)
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"local embedding model not found: {model_path}")
        self.embedding_model = _sentence_transformer(model_path)
        self.embedding_model_name = Path(model_path).name
        self.embedding_query_instruction = embedding_query_instruction(
            self.embedding_model_name
        )
        self.reranker = LocalReranker(self.workspace.root_path)
        logger.info(
            "[VectorDB] Embedding model loaded: %s.",
            self.embedding_model_name,
        )

    def ingest_chunks(self, document_id: str, chunks: list):
        summary = {
            "parent_count": len(chunks),
            "child_count": 0,
            "formal_window_count": 0,
            "boundary_overlap_opportunity_count": 0,
            "boundary_overlap_count": 0,
            "boundary_overlap_truncated_count": 0,
            "boundary_overlap_token_skip_count": 0,
            "max_child_tokens": 0,
            "max_embedding_input_tokens": 0,
            "embedding_token_limit": 0,
            "child_strategy_counts": {},
            "parent_store_ms": 0,
            "child_split_ms": 0,
            "keyword_index_ms": 0,
            "embedding_ms": 0,
            "vector_store_ms": 0,
        }
        if not chunks:
            return summary

        texts = []
        metadatas = []
        ids = []
        parent_records = []
        pending_chunk_child_ids = []
        token_counter = self._embedding_token_counter()
        embedding_token_limit = self._embedding_token_limit(token_counter)
        summary["embedding_token_limit"] = embedding_token_limit or 0
        previous_boundary: dict | None = None

        for parent_index, chunk in enumerate(chunks):
            parent_id = f"{document_id}_parent_{parent_index}"
            parent_content = chunk.page_content
            parent_metadata = {
                **chunk.metadata,
                "parent_id": parent_id,
                "parent_index": parent_index,
            }
            parent_records.append((parent_id, parent_content, parent_metadata))

            split_started_at = time.time()
            content_token_limit = self._embedding_content_token_limit(
                parent_metadata,
                token_counter,
                embedding_token_limit,
            )
            children = split_parent_document(
                parent_id,
                parent_content,
                formal_structure=bool(parent_metadata.get("structure_aware")),
                token_counter=token_counter,
                max_tokens=content_token_limit,
            )
            summary["child_split_ms"] += _elapsed_ms(split_started_at)
            summary["child_count"] += len(children)
            child_ids = [child.child_id for child in children]
            parent_metadata["child_ids"] = child_ids
            pending_chunk_child_ids.append((chunk, child_ids))
            for child_index, child in enumerate(children):
                strategy_counts = summary["child_strategy_counts"]
                strategy_counts[child.chunk_strategy] = int(strategy_counts.get(child.chunk_strategy) or 0) + 1
                if child.chunk_strategy == "formal_structure_paragraph_window_v2":
                    summary["formal_window_count"] += 1

                boundary_overlap = ""
                boundary_truncated = False
                if (
                    child_index == 0
                    and child.semantic_unit_count <= 1
                    and previous_boundary is not None
                    and _formal_boundary_compatible(
                        previous_boundary["metadata"],
                        parent_metadata,
                    )
                ):
                    summary["boundary_overlap_opportunity_count"] += 1
                    boundary_overlap, boundary_truncated = _fit_formal_boundary_overlap(
                        previous_boundary["content"],
                        child.content,
                        parent_metadata,
                        token_counter,
                        embedding_token_limit,
                    )
                    if boundary_overlap:
                        summary["boundary_overlap_count"] += 1
                        if boundary_truncated:
                            summary["boundary_overlap_truncated_count"] += 1
                    else:
                        summary["boundary_overlap_token_skip_count"] += 1

                indexed_content = child.content
                if boundary_overlap:
                    indexed_content = f"{boundary_overlap}\n\n{child.content}"
                effective_overlap_chars = max(child.overlap_chars, len(boundary_overlap))
                effective_semantic_units = child.semantic_unit_count + (1 if boundary_overlap else 0)
                effective_token_count = (
                    token_counter(indexed_content)
                    if token_counter is not None
                    else child.token_count
                )
                summary["max_child_tokens"] = max(
                    summary["max_child_tokens"],
                    effective_token_count,
                )
                retrieval_text, retrieval_context = _contextualized_child_text(
                    indexed_content,
                    parent_metadata,
                )
                embedding_input_tokens = token_counter(retrieval_text) if token_counter is not None else 0
                summary["max_embedding_input_tokens"] = max(
                    summary["max_embedding_input_tokens"],
                    embedding_input_tokens,
                )
                if embedding_token_limit and embedding_input_tokens > embedding_token_limit:
                    raise ValueError(
                        "embedding input exceeds model token limit: "
                        f"{embedding_input_tokens} > {embedding_token_limit}"
                    )
                child_metadata = {
                    **parent_metadata,
                    "parent_id": parent_id,
                    "child_id": child.child_id,
                    "child_index": child.child_index,
                    "previous_child_id": (
                        child_ids[child_index - 1]
                        if child_index > 0
                        else str((previous_boundary or {}).get("child_id") or "")
                        if boundary_overlap
                        else ""
                    ),
                    "next_child_id": child_ids[child_index + 1] if child_index + 1 < len(child_ids) else "",
                    "child_start_char": child.start_char,
                    "child_end_char": child.end_char,
                    "chunk_strategy": child.chunk_strategy,
                    "chunk_overlap_chars": effective_overlap_chars,
                    "boundary_overlap_chars": len(boundary_overlap),
                    "boundary_overlap_parent_id": str(
                        (previous_boundary or {}).get("parent_id") or ""
                    ) if boundary_overlap else "",
                    "boundary_overlap_child_id": str(
                        (previous_boundary or {}).get("child_id") or ""
                    ) if boundary_overlap else "",
                    "boundary_overlap_page": _safe_int(
                        ((previous_boundary or {}).get("metadata") or {}).get("page_number")
                        or ((previous_boundary or {}).get("metadata") or {}).get("page"),
                        0,
                    ) if boundary_overlap else 0,
                    "boundary_overlap_truncated": bool(boundary_overlap and boundary_truncated),
                    "chunk_token_count": effective_token_count,
                    "embedding_input_tokens": embedding_input_tokens,
                    "embedding_token_limit": embedding_token_limit or 0,
                    "semantic_unit_count": effective_semantic_units,
                    "paragraph_window_size": min(2, effective_semantic_units)
                    if child.chunk_strategy == "formal_structure_paragraph_window_v2"
                    else 0,
                    "paragraph_overlap": 1 if effective_overlap_chars > 0 else 0,
                    "child_excerpt": indexed_content[:420],
                    "retrieval_context": retrieval_context,
                }
                texts.append(retrieval_text)
                metadatas.append(child_metadata)
                ids.append(child.child_id)

            if children and str(parent_metadata.get("content_kind") or "prose").lower() in FORMAL_OVERLAP_CONTENT_KINDS:
                previous_boundary = {
                    "content": children[-1].content,
                    "metadata": parent_metadata,
                    "parent_id": parent_id,
                    "child_id": children[-1].child_id,
                }
            else:
                previous_boundary = None

        if not texts:
            for chunk, child_ids in pending_chunk_child_ids:
                chunk.metadata["child_ids"] = child_ids
            return summary

        embedding_started_at = time.time()
        embeddings = self.embedding_model.encode(texts, normalize_embeddings=True).tolist()
        summary["embedding_ms"] = _elapsed_ms(embedding_started_at)
        try:
            vector_started_at = time.time()
            self.collection.add(
                embeddings=embeddings,
                documents=texts,
                metadatas=metadatas,
                ids=ids,
            )
            summary["vector_store_ms"] = _elapsed_ms(vector_started_at)

            parent_store_started_at = time.time()
            for parent_id, parent_content, parent_metadata in parent_records:
                if self.secure_store is not None:
                    self.secure_store.upsert_parent(
                        parent_id,
                        parent_content,
                        parent_metadata,
                    )
                else:
                    self.parent_store.upsert_parent(
                        parent_id,
                        parent_content,
                        parent_metadata,
                    )
            summary["parent_store_ms"] = _elapsed_ms(parent_store_started_at)

            if self.keyword_index is not None:
                keyword_started_at = time.time()
                for child_id, retrieval_text, child_metadata in zip(
                    ids,
                    texts,
                    metadatas,
                ):
                    self.keyword_index.upsert_document(
                        child_id,
                        retrieval_text,
                        child_metadata,
                    )
                summary["keyword_index_ms"] = _elapsed_ms(keyword_started_at)
        except Exception:
            self._rollback_ingest(
                child_ids=ids,
                parent_ids=[record[0] for record in parent_records],
            )
            raise
        for chunk, child_ids in pending_chunk_child_ids:
            chunk.metadata["child_ids"] = child_ids
        self._source_candidate_cache = None
        logger.info("[VectorDB] %s indexed.", document_id)
        return summary

    def _rollback_ingest(
        self,
        *,
        child_ids: list[str],
        parent_ids: list[str],
    ) -> None:
        try:
            self.collection.delete(ids=child_ids)
        except Exception:
            logger.exception("[VectorDB] Failed to roll back child vectors.")

        if self.secure_store is not None:
            try:
                self.secure_store.delete_parent_ids(parent_ids)
            except Exception:
                logger.exception("[VectorDB] Failed to roll back secure parent records.")
            return

        try:
            self.parent_store.delete(parent_ids)
        except Exception:
            logger.exception("[VectorDB] Failed to roll back parent records.")
        try:
            self.keyword_index.delete(child_ids)
        except Exception:
            logger.exception("[VectorDB] Failed to roll back keyword records.")

    def _embedding_token_counter(self):
        tokenizer = getattr(self.embedding_model, "tokenizer", None)
        if tokenizer is None or not hasattr(tokenizer, "tokenize"):
            return None
        backend_tokenizer = getattr(tokenizer, "backend_tokenizer", None)

        def count_tokens(text: str) -> int:
            if backend_tokenizer is not None:
                return len(
                    backend_tokenizer.encode(
                        text or "",
                        add_special_tokens=False,
                    ).ids
                )
            return len(tokenizer.tokenize(text or ""))

        return count_tokens

    def _embedding_token_limit(self, token_counter) -> int | None:
        if token_counter is None:
            return None
        try:
            limit = int(getattr(self.embedding_model, "max_seq_length", 0) or 0)
        except (TypeError, ValueError):
            return None
        return limit if limit > 0 else None

    def _query_embedding_text(self, query: str) -> str:
        instruction = str(getattr(self, "embedding_query_instruction", "") or "")
        return f"{instruction}{query}" if instruction else query

    def _embedding_content_token_limit(
        self,
        metadata: dict,
        token_counter,
        embedding_token_limit: int | None,
    ) -> int | None:
        if token_counter is None or embedding_token_limit is None:
            return None
        _, context = _contextualized_child_text("", metadata)
        reserved_tokens = token_counter(context) + 8
        available_tokens = embedding_token_limit - reserved_tokens
        if available_tokens <= 0:
            raise ValueError(
                "retrieval context exceeds embedding model token limit: "
                f"{reserved_tokens} >= {embedding_token_limit}"
            )
        return available_tokens

    def search_with_evidence(self, query: str, graph_db: GraphStore | None = None, top_k: int = DEFAULT_RAG_TOP_K):
        retrieval_started_at = time.time()
        query_intent = classify_query_intent(query)
        timings = {
            "query_expansion_ms": 0,
            "query_embedding_ms": 0,
            "dense_ms": 0,
            "keyword_ms": 0,
            "graph_recall_ms": 0,
            "source_title_ms": 0,
            "merge_ms": 0,
            "hydrate_ms": 0,
            "graph_context_ms": 0,
            "rerank_ms": 0,
            "rank_ms": 0,
            "parent_expand_ms": 0,
        }
        dense_pool_size = max(top_k * 6, 18)
        stage_started_at = time.time()
        query_variants = expand_query_variants(query)
        timings["query_expansion_ms"] = _elapsed_ms(stage_started_at)

        dense_candidates = []
        keyword_candidates = []
        stage_started_at = time.time()
        graph_candidates = self._graph_candidates(query, graph_db, limit=max(top_k * 12, 40))
        timings["graph_recall_ms"] = _elapsed_ms(stage_started_at)
        for variant in query_variants:
            stage_started_at = time.time()
            query_vector = self.embedding_model.encode(
                [self._query_embedding_text(variant)],
                normalize_embeddings=True,
            ).tolist()
            timings["query_embedding_ms"] += _elapsed_ms(stage_started_at)
            stage_started_at = time.time()
            dense_candidates.extend(self._dense_candidates(query_vector, dense_pool_size))
            timings["dense_ms"] += _elapsed_ms(stage_started_at)
            stage_started_at = time.time()
            keyword_candidates.extend(self._keyword_candidates(variant, dense_pool_size))
            timings["keyword_ms"] += _elapsed_ms(stage_started_at)
        stage_started_at = time.time()
        source_title_candidates = self._source_title_candidates(query, limit=max(top_k * 4, 12))
        timings["source_title_ms"] = _elapsed_ms(stage_started_at)
        stage_started_at = time.time()
        candidates = self._merge_candidates(
            dense_candidates,
            keyword_candidates,
            graph_candidates,
            source_title_candidates,
        )
        timings["merge_ms"] = _elapsed_ms(stage_started_at)
        merged_candidate_count = len(candidates)
        stage_started_at = time.time()
        candidates = self._hydrate_secure_candidates(candidates, limit=max(top_k * 40, 120))
        timings["hydrate_ms"] = _elapsed_ms(stage_started_at)

        stage_started_at = time.time()
        graph_context, matched_entities = self._build_graph_context(
            query=query,
            docs=[candidate.content for candidate in dense_candidates[:2]],
            graph_db=graph_db,
        )
        timings["graph_context_ms"] = _elapsed_ms(stage_started_at)

        if matched_entities:
            for candidate in candidates:
                entity_graph_score = sum(
                    1 for entity in matched_entities if entity.lower() in candidate.content.lower()
                )
                candidate.graph_score = max(candidate.graph_score, float(entity_graph_score))

        if candidates and self.reranker.ready:
            stage_started_at = time.time()
            scores = self.reranker.score(query, [candidate.content for candidate in candidates])
            for candidate, score in zip(candidates, scores):
                candidate.rerank_score = float(score)
            timings["rerank_ms"] = _elapsed_ms(stage_started_at)

        stage_started_at = time.time()
        ranking_diagnostics = {}
        reranked_candidates = rank_hybrid_candidates(
            query,
            candidates,
            top_k=max(top_k * 2, top_k),
            diagnostics=ranking_diagnostics,
        )
        timings["rank_ms"] = _elapsed_ms(stage_started_at)
        stage_started_at = time.time()
        parent_candidates = self._expand_parent_context(
            reranked_candidates,
            top_k=top_k,
            query=query,
        )
        timings["parent_expand_ms"] = _elapsed_ms(stage_started_at)
        total_ms = _elapsed_ms(retrieval_started_at)
        context_diagnostics = _context_expansion_diagnostics(parent_candidates)
        quality_signals = _retrieval_quality_signals(query, parent_candidates)
        evidence = build_citations(parent_candidates)
        sufficiency = evaluate_evidence_gate(
            evidence,
            query=query,
        ).public_dict()
        observability = {
            "schema_version": "retrieval_observability_v1",
            "schema_revision": 3,
            "query_intent": query_intent.name,
            "query_variant_count": len(query_variants),
            "dense_query_instruction": bool(
                getattr(self, "embedding_query_instruction", "")
            ),
            "embedding_model": str(
                getattr(self, "embedding_model_name", "") or "unknown"
            ),
            "dense_pool_size": dense_pool_size,
            "secure_index": self.secure_store is not None,
            "reranker_enabled": bool(self.reranker.ready),
            "stage_counts": {
                "dense": len(dense_candidates),
                "keyword": len(keyword_candidates),
                "graph": len(graph_candidates),
                "source_title": len(source_title_candidates),
                "merged": merged_candidate_count,
                "hydrated": len(candidates),
                "reranked": len(reranked_candidates),
                "redundant_deferred": int(
                    ranking_diagnostics.get("redundant_deferred_count") or 0
                ),
                "final": len(parent_candidates),
                "final_sources": len({
                    source_identity(candidate.metadata or {})
                    for candidate in parent_candidates
                    if source_identity(candidate.metadata or {})
                }),
            },
            "ranking": ranking_diagnostics,
            "context_expansion": context_diagnostics,
            "quality_signals": quality_signals,
            "sufficiency": sufficiency,
            "timings_ms": {**timings, "total": total_ms},
        }
        runtime_metrics.record_retrieval_run(
            total_ms,
            candidate_count=len(candidates),
            evidence_count=len(parent_candidates),
        )
        return {
            "documents": [candidate.content for candidate in parent_candidates],
            "metadatas": [candidate.metadata for candidate in parent_candidates],
            "graph_context": graph_context,
            "matched_entities": matched_entities,
            "evidence": evidence,
            "retrieval_mode": "parent_child_hybrid_dense_fts_graph_rrf",
            "observability": observability,
        }

    def search(self, query: str, graph_db: GraphStore | None = None, top_k: int = DEFAULT_RAG_TOP_K):
        result = self.search_with_evidence(query=query, graph_db=graph_db, top_k=top_k)
        return result["documents"], result["metadatas"], result["graph_context"]

    def _dense_candidates(self, query_vector: list[list[float]], n_results: int):
        if self.secure_store is not None:
            embedding = query_vector[0] if query_vector else []
            return [
                SearchCandidate(
                    id=result.chunk_id,
                    content="",
                    metadata={},
                    dense_rank=index + 1,
                    vector_distance=1.0 - result.score,
                )
                for index, result in enumerate(self.secure_store.dense_candidates(embedding, limit=n_results))
            ]

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
            candidates = self.secure_store.keyword_candidates(query, limit=n_results)
            return [
                SearchCandidate(
                    id=result.chunk_id,
                    content="",
                    metadata={},
                    sparse_rank=index + 1,
                    keyword_score=result.score,
                )
                for index, result in enumerate(candidates)
            ]
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

    def _hydrate_secure_candidates(self, candidates: list[SearchCandidate], limit: int) -> list[SearchCandidate]:
        if self.secure_store is None:
            return candidates
        ordered_ids = []
        seen = set()
        for candidate in candidates:
            if candidate.content and candidate.metadata:
                continue
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
                if candidate.content and candidate.metadata:
                    hydrated.append(candidate)
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

    def _graph_candidates(self, query: str, graph_db: GraphStore | None, limit: int) -> list[SearchCandidate]:
        if not graph_db or not hasattr(graph_db, "recall_chunk_ids"):
            return []
        try:
            chunk_ids = graph_db.recall_chunk_ids(query, limit=limit)
        except Exception as exc:
            runtime_metrics.record_error(f"graph recall failed: {exc}")
            logger.warning("[VectorDB] Graph recall degraded: %s", exc)
            return []
        return [
            SearchCandidate(
                id=chunk_id,
                content="",
                metadata={},
                graph_score=max(1.0, float(limit - index)),
            )
            for index, chunk_id in enumerate(chunk_ids)
        ]

    def _source_title_candidates(self, query: str, limit: int = 12):
        scored: dict[str, tuple[float, SearchCandidate]] = {}
        for record_id, document, metadata in self._source_candidate_records():
            score = _source_match_score(query, metadata)
            if score < 0.72:
                continue
            key = source_identity(metadata, str(metadata.get("parent_id") or record_id))
            candidate = SearchCandidate(
                id=record_id,
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
                    source_identity(item[1].metadata or {}),
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
                merge_key = str(candidate.metadata.get("child_id") or candidate.id)
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

    def _expand_parent_context(
        self,
        candidates: list[SearchCandidate],
        top_k: int,
        query: str = "",
    ):
        expanded = []
        seen_parent_ids = set()
        query_intent = classify_query_intent(query)

        for candidate in candidates:
            parent_id = candidate.metadata.get("parent_id")
            if not parent_id:
                expanded.append(
                    _with_candidate_context(
                        candidate,
                        candidate.content,
                        {
                            **candidate.metadata,
                            "context_expansion_mode": "child_only",
                            "context_char_count": len(candidate.content),
                            "parent_char_count": 0,
                        },
                    )
                )
                if len(expanded) >= top_k:
                    break
                continue
            if parent_id in seen_parent_ids:
                continue

            if self.secure_store is not None:
                parent = self.secure_store.get_parent(parent_id)
            else:
                parent = self.parent_store.get_parent(parent_id)
            if not parent:
                expanded.append(
                    _with_candidate_context(
                        candidate,
                        candidate.content,
                        {
                            **candidate.metadata,
                            "context_expansion_mode": "child_fallback",
                            "context_char_count": len(candidate.content),
                            "parent_char_count": 0,
                        },
                    )
                )
                seen_parent_ids.add(parent_id)
                continue

            context, expansion_metadata = _select_parent_context(
                str(parent.get("content") or ""),
                candidate,
                query_intent,
            )
            expanded.append(
                _with_candidate_context(
                    candidate,
                    context,
                    {
                        **(parent.get("metadata") or {}),
                        **candidate.metadata,
                        **expansion_metadata,
                    },
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
            existing = self.secure_store.collection.get(where={"document_hash": document_hash}, include=[])
            child_ids = list(existing.get("ids") or [])
            self.secure_store.delete_by_document_hash(document_hash)
            self._remove_secure_graph_provenance(child_ids)
            self._source_candidate_cache = None
            return
        existing = self.collection.get(where={"document_hash": document_hash}, include=[])
        child_ids = list(existing.get("ids") or [])
        if child_ids:
            self.collection.delete(ids=child_ids)
        self.parent_store.delete_by_document_hash(document_hash)
        self.keyword_index.delete_by_document_hash(document_hash)
        self._remove_local_graph_provenance(child_ids)
        self._source_candidate_cache = None

    def delete_index_records(
        self,
        *,
        child_ids: list[str],
        parent_ids: list[str],
    ) -> None:
        if not child_ids and not parent_ids:
            return
        if self.secure_store is not None:
            self.secure_store.delete(child_ids)
            self.secure_store.delete_parent_ids(parent_ids)
            self._remove_secure_graph_provenance(child_ids)
            self._source_candidate_cache = None
            return

        if child_ids:
            self.collection.delete(ids=child_ids)
        self.parent_store.delete(parent_ids)
        self.keyword_index.delete(child_ids)
        self._remove_local_graph_provenance(child_ids)
        self._source_candidate_cache = None

    def _remove_local_graph_provenance(self, chunk_ids: list[str]) -> None:
        if not chunk_ids or not os.path.exists(self.workspace.graph_db_path):
            return
        graph_store = LocalGraphDB(self.workspace.graph_db_path)
        try:
            graph_store.remove_chunk_ids(chunk_ids)
        finally:
            graph_store.close()

    def _remove_secure_graph_provenance(self, chunk_ids: list[str]) -> None:
        if not chunk_ids:
            return
        workspace_keys = get_workspace_keys(self.workspace.root_path)
        if workspace_keys is None:
            return
        graph_store = SecureGraphStore(self.workspace.secure_graph_store_path, workspace_keys)
        try:
            graph_store.remove_chunk_ids(chunk_ids)
        finally:
            graph_store.close()

    def close(self):
        if self.secure_store is not None:
            self.secure_store.close()
            return
        self.parent_store.close()
        self.keyword_index.close()
        if self.client is not None and hasattr(self.client, "close"):
            self.client.close()
