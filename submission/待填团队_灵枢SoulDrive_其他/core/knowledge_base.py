import os
import re
import time
import json
from urllib.request import Request, urlopen
from pathlib import Path
from typing import Any

from core.answer_quality import evaluate_evidence_gate
from core.embedding_models import (
    embedding_query_instruction,
    preferred_embedding_model_path,
)
from core.graph_db import LocalGraphDB
from core.graph_rag import route_graph_query
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
    _quoted_section_topics,
    _section_route_match,
    _source_match_score,
    build_citations,
    classify_query_intent,
    lexical_rerank_score,
    rank_hybrid_candidates,
    source_identity,
    source_key,
    tokenize,
)
from core.document_usage import document_weight, record_document_hits
from core.reranker import LocalReranker
from core.security_context import get_workspace_keys
from core.secure_graph_store import SecureGraphStore
from core.secure_vector_store import SecureVectorStore
from core.query_expansion import expand_query_variants
from core.document_tree import (
    SECTION_TREE_VERSION,
    build_section_tree,
    normalize_section_path,
    section_node_id,
)
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


def _normalize_document_names(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    names: list[str] = []
    for item in value:
        name = " ".join(str(item or "").strip().split())[:260]
        if name and name not in names:
            names.append(name)
        if len(names) >= 12:
            break
    return names


def _document_name_matches(metadata: dict[str, Any] | None, document_names: list[str]) -> bool:
    if not document_names:
        return True
    metadata = metadata or {}
    expected = {
        " ".join(str(item or "").strip().split()).casefold()
        for item in document_names
        if " ".join(str(item or "").strip().split())
    }
    identities = set()
    for key in ("source_filename", "document_name", "catalog_file_ref"):
        value = " ".join(str(metadata.get(key) or "").strip().split())
        if value:
            identities.add(value.casefold())
            identities.add(Path(value).name.casefold())
            identities.add(value.replace("\\", "/").rsplit("/", 1)[-1].casefold())
    return bool(identities & expected)


def _filter_document_candidates(
    candidates: list[SearchCandidate],
    document_names: list[str],
) -> list[SearchCandidate]:
    if not document_names:
        return candidates
    return [
        candidate
        for candidate in candidates
        if _document_name_matches(candidate.metadata, document_names)
    ]

# The profile stage is deliberately opt-in until it clears a matched
# retrieval evaluation.  It is a document-level recall aid, not a replacement
# for child retrieval or evidence ranking.
SOURCE_PROFILE_MIN_SIMILARITY = 0.34


def _retrieval_stage_budget(query: str, query_intent: QueryIntent, top_k: int) -> dict[str, int | bool]:
    graph_route = route_graph_query(query)
    if query_intent.requires_source_diversity or query_intent.broad_scope:
        dense_pool_size = max(top_k * 8, 24)
        aux_limit = max(top_k * 5, 16)
    elif (
        query_intent.prefers_table
        or query_intent.prefers_visual_asset
        or query_intent.explicit_source
        or query_intent.clause_references
    ):
        dense_pool_size = max(top_k * 6, 18)
        aux_limit = max(top_k * 4, 12)
    else:
        dense_pool_size = max(top_k * 4, 12)
        aux_limit = max(top_k * 3, 10)

    if not graph_route.use_graph:
        graph_limit = max(top_k * 8, 24)
    elif graph_route.mode == "global":
        graph_limit = max(top_k * 16, 48)
    else:
        graph_limit = max(top_k * 12, 40)

    return {
        "dense_pool_size": dense_pool_size,
        "aux_limit": aux_limit,
        "graph_limit": graph_limit,
        "use_graph": graph_route.use_graph,
    }


def _visual_asset_records(
    metadata: dict[str, Any] | None,
    *,
    limit: int = 8,
) -> list[dict[str, str]]:
    """Read bounded figure metadata without treating it as executable input."""
    raw = (metadata or {}).get("image_metadata_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []

    records: list[dict[str, str]] = []
    for item in raw[: max(1, min(int(limit), 8))]:
        if not isinstance(item, dict):
            continue
        caption = re.sub(r"\s+", " ", str(item.get("caption") or "")).strip()[:300]
        if not caption:
            continue
        record = {
            "asset_id": re.sub(r"\s+", " ", str(item.get("asset_id") or "")).strip()[:96],
            "page": re.sub(r"\s+", " ", str(item.get("page") or "")).strip()[:16],
            "caption": caption,
            "source_locator": re.sub(
                r"\s+", " ", str(item.get("source_locator") or "")
            ).strip()[:128],
            "vision_status": re.sub(
                r"\s+", " ", str(item.get("vision_status") or "")
            ).strip()[:64],
        }
        records.append({key: value for key, value in record.items() if value})
    return records


def _visual_asset_context(metadata: dict[str, Any] | None) -> str:
    records = _visual_asset_records(metadata)
    if not records:
        return ""
    lines = ["[VISUAL_ASSETS]"]
    for record in records:
        parts = []
        if record.get("asset_id"):
            parts.append(f"asset={record['asset_id']}")
        if record.get("page"):
            parts.append(f"page={record['page']}")
        parts.append(f"caption={record['caption']}")
        if record.get("vision_status"):
            parts.append(f"status={record['vision_status']}")
        if record.get("source_locator"):
            parts.append(f"locator={record['source_locator']}")
        lines.append("; ".join(parts))
    return "\n".join(lines)


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


class _EmbeddingBatch(list):
    def tolist(self):
        return list(self)


class _OllamaEmbeddingModel:
    max_seq_length = 8192
    tokenizer = None

    def __init__(self, model: str, base_url: str):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.keep_alive = os.environ.get(
            "SOULDRIVE_OLLAMA_EMBEDDING_KEEP_ALIVE", "10m"
        )

    def encode(self, texts, normalize_embeddings=True):
        _ = normalize_embeddings
        payload = json.dumps(
            {"model": self.model, "input": list(texts), "keep_alive": self.keep_alive},
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            f"{self.base_url}/api/embed",
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urlopen(request, timeout=180) as response:
            result = json.loads(response.read().decode("utf-8"))
        return _EmbeddingBatch(result.get("embeddings") or [])


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
            for key in ("Header 1", "Header 2", "Header 3", "Header 4", "Header 5", "Header 6")
            if str(metadata.get(key) or "").strip()
        ]

    context_lines = []
    if source:
        context_lines.append(f"[DOCUMENT] {source}")
    document_number = str(metadata.get("document_number") or "").strip()
    if document_number:
        context_lines.append(f"[DOCUMENT_NUMBER] {document_number}")
    document_profile = str(metadata.get("document_profile") or "").strip()
    if document_profile and document_profile != "general" and not metadata.get("_compact_retrieval_context"):
        context_lines.append(
            f"[DOCUMENT_TYPE] {DOCUMENT_PROFILE_LABELS.get(document_profile, document_profile)}"
        )
    if sections and not metadata.get("_compact_retrieval_context"):
        context_lines.append(f"[SECTION] {' > '.join(sections)}")
    document_summary = str(metadata.get("document_summary") or "").strip()
    if document_summary and not metadata.get("_compact_retrieval_context"):
        context_lines.append(f"[DOCUMENT_SUMMARY] {document_summary[:600]}")
    clause_ids = str(metadata.get("clause_ids") or metadata.get("clause_id") or "").strip()
    if clause_ids and not metadata.get("_compact_retrieval_context"):
        context_lines.append(f"[CLAUSE] {clause_ids}")
    visual_assets = _visual_asset_context(metadata)
    if visual_assets:
        context_lines.append(visual_assets)
    chunk_summary = _chunk_contextual_summary(content, metadata)
    if chunk_summary:
        context_lines.append(f"[CHUNK_SUMMARY] {chunk_summary}")
    prefix = "\n".join(context_lines)
    if not prefix:
        return content, ""
    return f"{prefix}\n\n{content}", prefix


def _chunk_contextual_summary(content: str, metadata: dict) -> str:
    """生成可复核的短摘录，避免为每个子块额外调用生成模型。"""
    normalized = " ".join(str(content or "").split())
    if not normalized:
        return ""
    first_sentence = re.split(r"(?<=[。！？.!?])\s*", normalized, maxsplit=1)[0].strip()
    try:
        max_chars = max(0, int(metadata.get("_chunk_summary_max_chars", 32)))
    except (TypeError, ValueError):
        max_chars = 32
    anchor = first_sentence[:max_chars].rstrip("，,；;：:")
    if not anchor:
        return ""
    section = "" if metadata.get("_compact_retrieval_context") else _section_identity(metadata)
    return f"{section}：{anchor}"[:72] if section else anchor


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
            for key in ("Header 1", "Header 2", "Header 3", "Header 4", "Header 5", "Header 6", "section")
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
    # PDF parsers commonly keep a heading as its own block.  A heading-only
    # hit is useful for navigation but insufficient for answering; include the
    # first following paragraph so the evidence contains the section's claim.
    heading_targets = [
        index
        for index in target_indices
        if re.match(r"^\s{0,3}#{1,6}\s+", blocks[index][2])
    ]
    for heading_index in heading_targets:
        following_index = heading_index + 1
        if following_index >= len(blocks) or following_index in selected_indices:
            continue
        proposed = {*selected_indices, following_index}
        if len(_join_blocks(blocks, proposed)) <= max_chars:
            selected_indices.add(following_index)
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


# CRAG-style corrective retrieval: a project hit must cover a meaningful part
# of the query before it is allowed to suppress the shared global corpus.
PROJECT_RELEVANCE_FLOOR = 0.34
PROJECT_FOCUS_WEIGHT = 1.12
_PROJECT_SCOPE_NOISE_TERMS = {
    "当前", "项目", "方案", "系统", "资料", "文档", "内容", "主要", "概述",
    "总结", "介绍", "分析", "回答", "请问", "什么", "如何", "一下", "核心",
    "project", "current", "document", "documents", "system", "solution",
}


def _reranker_passage(candidate: SearchCandidate) -> str:
    """给 cross-encoder 发送可控长度的子块证据，而不是完整父文档。"""
    metadata = candidate.metadata or {}
    title = (
        metadata.get("source_filename")
        or metadata.get("document_title")
        or metadata.get("document_number")
        or ""
    )
    section = (
        metadata.get("Header 6")
        or metadata.get("Header 5")
        or metadata.get("Header 4")
        or metadata.get("Header 3")
        or metadata.get("Header 2")
        or metadata.get("Header 1")
        or metadata.get("section")
        or ""
    )
    excerpt = metadata.get("child_excerpt") or candidate.content
    parts = [str(title).strip(), str(section).strip(), str(excerpt).strip()[:600]]
    return "\n".join(part for part in parts if part)


def _project_query_signal_tokens(query: str) -> set[str]:
    """Extract scope-independent anchors for the corrective project check."""
    normalized = str(query or "")
    for noise_term in sorted(_PROJECT_SCOPE_NOISE_TERMS, key=len, reverse=True):
        normalized = re.sub(re.escape(noise_term), " ", normalized, flags=re.IGNORECASE)
    return {
        token
        for token in tokenize(normalized)
        if len(token) >= 2
        and (
            not re.fullmatch(r"[\u4e00-\u9fff]+", token)
            or len(token) == 2
        )
        and token not in _PROJECT_SCOPE_NOISE_TERMS
    }


def _project_candidate_relevance(query: str, candidate: SearchCandidate) -> float:
    """CRAG 风格的轻量召回质量评估，不额外调用模型。"""
    metadata = candidate.metadata or {}
    content = "\n".join(
        part
        for part in (
            str(candidate.content or "").strip(),
            str(metadata.get("document_title") or "").strip(),
            str(metadata.get("source_filename") or "").strip(),
            str(metadata.get("title") or "").strip(),
        )
        if part
    )
    if not content:
        return 0.0

    signal_tokens = _project_query_signal_tokens(query)
    if not signal_tokens:
        return 1.0

    content_tokens = set(tokenize(content))
    lexical = lexical_rerank_score(query, content)
    signal_coverage = len(signal_tokens & content_tokens) / max(1, len(signal_tokens))
    lexical_signal = max(lexical, signal_coverage)
    score = lexical_signal

    # Sentence-transformers/Chroma 的 cosine distance 越小越相关；对没有
    # 距离信息的稀疏/图召回候选，只使用可解释的词项信号。
    if candidate.vector_distance is not None and lexical_signal >= PROJECT_RELEVANCE_FLOOR:
        dense = max(0.0, min(1.0, 1.0 - float(candidate.vector_distance)))
        score = max(score, dense)
    return round(score, 6)



def _evaluate_project_candidates(
    query: str,
    candidates: list[SearchCandidate],
) -> dict[str, Any]:
    query_intent = classify_query_intent(query)
    relevance_floor = 0.18 if (query_intent.prefers_visual_asset or query_intent.explicit_source) else PROJECT_RELEVANCE_FLOOR
    scored = [
        (candidate, _project_candidate_relevance(query, candidate))
        for candidate in candidates
    ]
    scored.sort(key=lambda item: (-item[1], str(item[0].id)))
    return {
        "threshold": relevance_floor,
        "max_score": round(scored[0][1], 6) if scored else 0.0,
        "relevant": bool(scored and scored[0][1] >= relevance_floor),
        "candidate_scores": [
            {"candidate_id": str(candidate.id), "score": score}
            for candidate, score in scored[:8]
        ],
    }


def _merge_query_variants(
    query: str,
    extra_query_variants: list[str] | None = None,
    *,
    limit: int = 8,
) -> list[str]:
    """Put explicitly confirmed variants ahead of generic rewrites, bounded."""
    base = str(query or "").strip()
    variants = [base]
    variants.extend(
        " ".join(str(item or "").strip().split())[:6000]
        for item in (extra_query_variants or [])
    )
    variants.extend(expand_query_variants(base, limit=limit))
    deduped = []
    seen = set()
    for item in variants:
        normalized = " ".join(item.split())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
        if len(deduped) >= max(1, int(limit)):
            break
    return deduped


class LocalKnowledgeBase:
    def __init__(
        self,
        db_path: str | None = None,
        parent_doc_path: str | None = None,
        keyword_index_path: str | None = None,
        workspace_path: str | None = None,
        *,
        enable_usage_ledger: bool = True,
        enable_comparison_source_reservation: bool = False,
        enable_source_profile_recall: bool = False,
        rerank_candidate_limit: int | None = None,
    ):
        if db_path is None and workspace_path is None:
            raise ValueError("workspace_path is required when db_path is not provided")

        # Production keeps the lightweight document-use signal, while sealed
        # and candidate evaluations disable it to prevent the evaluation run
        # from changing the ranking state of the next sample/run.
        self.enable_usage_ledger = bool(enable_usage_ledger)
        self.enable_comparison_source_reservation = bool(
            enable_comparison_source_reservation
        )
        self.enable_source_profile_recall = bool(enable_source_profile_recall)
        self.rerank_candidate_limit = (
            None
            if rerank_candidate_limit is None
            else max(1, min(int(rerank_candidate_limit), 120))
        )

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
        self._source_profile_cache = None
        self._source_profile_embeddings = {}
        self._active_source_profile_scores = {}
        workspace_keys = get_workspace_keys(self.workspace.root_path)
        if (
            workspace_keys is None
            and Path(self.workspace.secure_vector_store_path).exists()
        ):
            # A keystore-backed index must never silently downgrade to an
            # empty plaintext Chroma index while the process is locked.
            raise RuntimeError("workspace keys are not available for encrypted index")
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

        embedding_provider = str(os.environ.get("SOULDRIVE_EMBEDDING_PROVIDER") or "").strip().lower()
        if embedding_provider == "ollama":
            self.embedding_model_name = str(
                os.environ.get("SOULDRIVE_OLLAMA_EMBEDDING_MODEL") or "qwen3-embedding:4b"
            ).strip()
            self.embedding_model = _OllamaEmbeddingModel(
                self.embedding_model_name,
                str(os.environ.get("SOULDRIVE_OLLAMA_URL") or "http://127.0.0.1:11434"),
            )
        else:
            model_path = preferred_embedding_model_path(self.workspace.root_path)
            if not os.path.exists(model_path):
                raise FileNotFoundError(f"local embedding model not found: {model_path}")
            self.embedding_model = None
            self._embedding_model_path = model_path
            self.embedding_model_name = Path(model_path).name
        self.embedding_query_instruction = embedding_query_instruction(
            self.embedding_model_name
        )
        self.reranker = LocalReranker(self.workspace.root_path)
        logger.info(
            "[VectorDB] Embedding model configured: %s.",
            self.embedding_model_name,
        )

    def _ensure_embedding_model(self):
        if self.embedding_model is None:
            model_path = getattr(self, "_embedding_model_path", None)
            if model_path:
                self.embedding_model = _sentence_transformer(model_path)
                logger.info("[VectorDB] Embedding model loaded on demand: %s", self.embedding_model_name)
        return self.embedding_model

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
            "section_tree_version": SECTION_TREE_VERSION,
            "section_tree_node_count": 0,
            "section_tree_leaf_count": 0,
        }
        if not chunks:
            return summary
        section_tree = build_section_tree(document_id, chunks)
        summary["section_tree_node_count"] = len(section_tree["nodes"])
        summary["section_tree_leaf_count"] = sum(
            1 for node in section_tree["nodes"] if not node["child_node_ids"]
        )
        self._ensure_embedding_model()

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
            tree_metadata = dict(section_tree["by_parent_index"].get(parent_index) or {})
            chunk.metadata.update(tree_metadata)
            parent_content = chunk.page_content
            parent_metadata = {
                **chunk.metadata,
                **tree_metadata,
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
                summary_metadata = parent_metadata
                if embedding_token_limit and embedding_input_tokens > embedding_token_limit:
                    base_metadata = {
                        **parent_metadata,
                        "_chunk_summary_max_chars": 0,
                        "_compact_retrieval_context": True,
                    }
                    base_text, _ = _contextualized_child_text(indexed_content, base_metadata)
                    available_summary_tokens = max(0, embedding_token_limit - token_counter(base_text))
                    summary_chars = max(0, available_summary_tokens - token_counter("[CHUNK_SUMMARY] "))
                    summary_metadata = {**base_metadata, "_chunk_summary_max_chars": summary_chars}
                    retrieval_text, retrieval_context = _contextualized_child_text(indexed_content, summary_metadata)
                    embedding_input_tokens = token_counter(retrieval_text)
                    if embedding_input_tokens > embedding_token_limit:
                        summary_metadata = {**base_metadata, "_chunk_summary_max_chars": 0}
                        retrieval_text, retrieval_context = indexed_content, ""
                        embedding_input_tokens = token_counter(retrieval_text)
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
                    "chunk_contextual_summary": _chunk_contextual_summary(
                        indexed_content,
                        summary_metadata,
                    ),
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
        embeddings = self._ensure_embedding_model().encode(texts, normalize_embeddings=True).tolist()
        summary["embedding_ms"] = _elapsed_ms(embedding_started_at)
        try:
            vector_started_at = time.time()
            vector_write = getattr(self.collection, "upsert", None) or self.collection.add
            vector_write(
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
        self._source_profile_cache = None
        self._source_profile_embeddings = {}
        self._active_source_profile_scores = {}
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
        if reserved_tokens >= embedding_token_limit:
            # Keep the document identity, but drop verbose navigation metadata when it consumes the budget.
            compact_metadata = {
                **metadata,
                "_compact_retrieval_context": True,
                "_chunk_summary_max_chars": 0,
            }
            _, compact_context = _contextualized_child_text("", compact_metadata)
            reserved_tokens = token_counter(compact_context) + 8
        available_tokens = embedding_token_limit - reserved_tokens
        if available_tokens <= 0:
            raise ValueError(
                "retrieval context exceeds embedding model token limit: "
                f"{reserved_tokens} >= {embedding_token_limit}"
            )
        return available_tokens

    def search_with_evidence(
        self,
        query: str,
        graph_db: GraphStore | None = None,
        top_k: int = DEFAULT_RAG_TOP_K,
        project_id: str | None = None,
        extra_query_variants: list[str] | None = None,
        document_names: list[str] | None = None,
    ):
        self._ensure_embedding_model()
        retrieval_started_at = time.time()
        query_intent = classify_query_intent(query)
        scoped_document_names = _normalize_document_names(document_names)
        if self.enable_source_profile_recall:
            self._active_source_profile_scores = {}
        timings = {
            "query_expansion_ms": 0,
            "query_embedding_ms": 0,
            "dense_ms": 0,
            "keyword_ms": 0,
            "graph_recall_ms": 0,
            "source_title_ms": 0,
            "source_profile_ms": 0,
            "merge_ms": 0,
            "hydrate_ms": 0,
            "graph_context_ms": 0,
            "rerank_ms": 0,
            "rank_ms": 0,
            "parent_expand_ms": 0,
        }
        retrieval_budget = _retrieval_stage_budget(query, query_intent, top_k)
        dense_pool_size = int(retrieval_budget["dense_pool_size"])
        aux_limit = int(retrieval_budget["aux_limit"])
        graph_limit = int(retrieval_budget["graph_limit"])
        # A selected document is an explicit corpus boundary. GraphRAG reports
        # are corpus-level summaries and cannot be safely narrowed by filename.
        use_graph_recall = bool(retrieval_budget["use_graph"]) and not scoped_document_names
        scoped_project_id = (
            str(project_id).strip()
            if project_id and not query_intent.explicit_global_scope
            else None
        )
        stage_started_at = time.time()
        base_query_variants = expand_query_variants(query, limit=8)
        query_variants = _merge_query_variants(query, extra_query_variants)
        feedback_variants = {
            " ".join(str(item or "").strip().split())[:6000]
            for item in (extra_query_variants or [])
            if " ".join(str(item or "").strip().split())
        }
        timings["query_expansion_ms"] = _elapsed_ms(stage_started_at)

        dense_candidates = []
        keyword_candidates = []
        stage_started_at = time.time()
        graph_candidates = []
        if graph_db is not None and not scoped_document_names:
            graph_candidates = self._graph_candidates(
                query,
                graph_db,
                limit=graph_limit,
                project_id=scoped_project_id,
            )
            timings["graph_recall_ms"] = _elapsed_ms(stage_started_at)
        stage_started_at = time.time()
        query_vectors = self.embedding_model.encode(
            [self._query_embedding_text(variant) for variant in query_variants],
            normalize_embeddings=True,
        ).tolist()
        timings["query_embedding_ms"] = _elapsed_ms(stage_started_at)
        for variant, query_vector in zip(query_variants, query_vectors):
            stage_started_at = time.time()
            dense_candidates.extend(
                self._dense_candidates(
                    [query_vector],
                    dense_pool_size,
                    project_id=scoped_project_id,
                    document_names=scoped_document_names,
                )
            )
            timings["dense_ms"] += _elapsed_ms(stage_started_at)
            stage_started_at = time.time()
            keyword_candidates.extend(
                self._keyword_candidates(
                    variant,
                    dense_pool_size,
                    project_id=scoped_project_id,
                    document_names=scoped_document_names,
                )
            )
            timings["keyword_ms"] += _elapsed_ms(stage_started_at)
        stage_started_at = time.time()
        source_title_candidates = self._source_title_candidates(
            query,
            limit=aux_limit,
            project_id=scoped_project_id,
            query_variants=query_variants,
            document_names=scoped_document_names,
        )
        timings["source_title_ms"] = _elapsed_ms(stage_started_at)
        stage_started_at = time.time()
        source_profile_candidates = self._source_profile_candidates(
            query_vectors,
            query_variants=query_variants,
            limit=aux_limit,
            project_id=scoped_project_id,
            document_names=scoped_document_names,
        )
        timings["source_profile_ms"] += _elapsed_ms(stage_started_at)
        section_route_diagnostics = []
        section_candidates = self._section_candidates(
            query,
            limit=aux_limit,
            project_id=scoped_project_id,
            document_names=scoped_document_names,
        )
        section_route_diagnostics.append(dict(getattr(self, "_section_route_diagnostics", {})))
        stage_started_at = time.time()
        candidates = self._merge_candidates(
            dense_candidates,
            keyword_candidates,
            graph_candidates,
            source_title_candidates,
            source_profile_candidates,
            section_candidates,
        )
        timings["merge_ms"] = _elapsed_ms(stage_started_at)
        merged_candidate_count = len(candidates)
        stage_started_at = time.time()
        candidates = self._hydrate_secure_candidates(candidates, limit=max(top_k * 40, 120))
        candidates = _filter_document_candidates(candidates, scoped_document_names)
        timings["hydrate_ms"] = _elapsed_ms(stage_started_at)

        merged_candidate_count = len(candidates)
        indexed_candidate_count = self._indexed_candidate_count(len(candidates))
        global_candidate_count = 0
        shared_global_candidate_count = 0
        scope_project = bool(scoped_project_id)
        project_route = "global"
        scoped_candidate_count = 0
        project_relevance = {
            "threshold": PROJECT_RELEVANCE_FLOOR,
            "max_score": 0.0,
            "relevant": False,
            "candidate_scores": [],
        }
        if project_id and not scoped_project_id:
            project_route = "global_explicit"
        elif scoped_project_id:
            scoped_candidate_count = len(candidates)
            project_relevance = _evaluate_project_candidates(query, candidates)
            if project_relevance["relevant"]:
                # 项目相关命中不能遮蔽未绑定项目的共享知识。先保留当前
                # 项目候选，再以同样的多路召回召回全局候选，合并后统一重排。
                shared_graph_candidates = self._graph_candidates(
                    query,
                    graph_db,
                    limit=graph_limit,
                    project_id=None,
                    global_only=True,
                ) if graph_db is not None and not scoped_document_names else []
                shared_dense_candidates = []
                shared_keyword_candidates = []
                for variant, query_vector in zip(query_variants, query_vectors):
                    shared_dense_candidates.extend(
                        self._dense_candidates(
                            [query_vector],
                            dense_pool_size,
                            project_id=None,
                            global_only=True,
                            document_names=scoped_document_names,
                        )
                    )
                    shared_keyword_candidates.extend(
                        self._keyword_candidates(
                            variant,
                            dense_pool_size,
                            project_id=None,
                            global_only=True,
                            document_names=scoped_document_names,
                        )
                    )
                shared_source_title_candidates = self._source_title_candidates(
                    query,
                    limit=aux_limit,
                    project_id=None,
                    global_only=True,
                    query_variants=query_variants,
                    document_names=scoped_document_names,
                )
                stage_started_at = time.time()
                shared_source_profile_candidates = self._source_profile_candidates(
                    query_vectors,
                    query_variants=query_variants,
                    limit=aux_limit,
                    project_id=None,
                    global_only=True,
                    document_names=scoped_document_names,
                )
                timings["source_profile_ms"] += _elapsed_ms(stage_started_at)
                shared_section_candidates = self._section_candidates(
                    query,
                    limit=aux_limit,
                    project_id=None,
                    global_only=True,
                    document_names=scoped_document_names,
                )
                section_route_diagnostics.append(dict(getattr(self, "_section_route_diagnostics", {})))
                shared_candidates = self._merge_candidates(
                    shared_dense_candidates,
                    shared_keyword_candidates,
                    shared_graph_candidates,
                    shared_source_title_candidates,
                    shared_source_profile_candidates,
                    shared_section_candidates,
                )
                shared_global_candidate_count = len(shared_candidates)
                candidates = self._merge_candidates(candidates, shared_candidates)
                candidates = self._hydrate_secure_candidates(
                    candidates,
                    limit=max(top_k * 40, 120),
                )
                candidates = _filter_document_candidates(candidates, scoped_document_names)
                dense_candidates.extend(shared_dense_candidates)
                keyword_candidates.extend(shared_keyword_candidates)
                graph_candidates.extend(shared_graph_candidates)
                source_title_candidates.extend(shared_source_title_candidates)
                source_profile_candidates.extend(shared_source_profile_candidates)
                section_candidates.extend(shared_section_candidates)
                merged_candidate_count = len(candidates)
                project_route = (
                    "project_first_with_global"
                    if shared_candidates
                    else "project_first"
                )
            else:
                # CRAG 风格的纠错分支：项目召回存在但与问题不相关时，
                # 只从未绑定项目的全局文档重新召回，避免项目噪声挡住全局知识。
                graph_candidates = self._graph_candidates(
                    query,
                    graph_db,
                    limit=graph_limit,
                    project_id=None,
                    global_only=True,
                ) if graph_db is not None and not scoped_document_names else []
                dense_candidates = []
                keyword_candidates = []
                for variant, query_vector in zip(query_variants, query_vectors):
                    dense_candidates.extend(
                        self._dense_candidates(
                            [query_vector],
                            dense_pool_size,
                            project_id=None,
                            global_only=True,
                            document_names=scoped_document_names,
                        )
                    )
                    keyword_candidates.extend(
                        self._keyword_candidates(
                            variant,
                            dense_pool_size,
                            project_id=None,
                            global_only=True,
                            document_names=scoped_document_names,
                        )
                    )
                source_title_candidates = self._source_title_candidates(
                    query,
                    limit=aux_limit,
                    project_id=None,
                    global_only=True,
                    query_variants=query_variants,
                    document_names=scoped_document_names,
                )
                stage_started_at = time.time()
                source_profile_candidates = self._source_profile_candidates(
                    query_vectors,
                    query_variants=query_variants,
                    limit=aux_limit,
                    project_id=None,
                    global_only=True,
                    document_names=scoped_document_names,
                )
                timings["source_profile_ms"] += _elapsed_ms(stage_started_at)
                section_candidates = self._section_candidates(
                    query,
                    limit=aux_limit,
                    project_id=None,
                    global_only=True,
                    document_names=scoped_document_names,
                )
                section_route_diagnostics.append(dict(getattr(self, "_section_route_diagnostics", {})))
                candidates = self._merge_candidates(
                    dense_candidates,
                    keyword_candidates,
                    graph_candidates,
                    source_title_candidates,
                    source_profile_candidates,
                    section_candidates,
                )
                candidates = self._hydrate_secure_candidates(
                    candidates,
                    limit=max(top_k * 40, 120),
                )
                candidates = _filter_document_candidates(candidates, scoped_document_names)
                merged_candidate_count = len(candidates)
                project_route = "global_fallback"

        self._annotate_source_profile_scores(candidates)
        global_candidate_count = sum(
            1
            for candidate in candidates
            if not str((candidate.metadata or {}).get("project_id") or "").strip()
        )

        stage_started_at = time.time()
        if graph_db is not None and use_graph_recall:
            graph_context, matched_entities = self._build_graph_context(
                query=query,
                docs=[candidate.content for candidate in dense_candidates[:2]],
                graph_db=graph_db,
                project_id=(
                    None
                    if query_intent.explicit_global_scope or project_route == "global_fallback"
                    else scoped_project_id
                ),
                global_only=project_route == "global_fallback",
            )
            timings["graph_context_ms"] = _elapsed_ms(stage_started_at)
        else:
            graph_context, matched_entities = [], []

        if matched_entities:
            for candidate in candidates:
                entity_graph_score = sum(
                    1 for entity in matched_entities if entity.lower() in candidate.content.lower()
                )
                candidate.graph_score = max(candidate.graph_score, float(entity_graph_score))

        rerank_candidates = candidates
        rerank_deferred_count = 0
        rerank_limit_bypassed_reason = ""
        rerank_limit_protected_route = (
            query_intent.requires_source_diversity
            or query_intent.broad_scope
            or query_intent.prefers_table
            or query_intent.prefers_visual_asset
            or bool(query_intent.clause_references)
        )
        if (
            self.rerank_candidate_limit is not None
            and len(candidates) > self.rerank_candidate_limit
        ):
            if rerank_limit_protected_route:
                # Complex or structure-sensitive questions are recall-sensitive:
                # a cheap preselection can remove a needed source, clause, table,
                # or visual asset before the final selector sees it. Keep the
                # complete rerank pool for these high-risk routes.
                rerank_limit_bypassed_reason = (
                    "source_diversity"
                    if query_intent.requires_source_diversity
                    else "structured_or_broad_query"
                )
            else:
                # RAGFlow/Dify-style bounded reranking: keep the larger hybrid
                # recall pool, but spend the expensive cross-encoder pass only
                # on the strongest cheap candidates. The final rank still
                # considers every candidate's lexical/dense/source signals.
                pre_ranked = rank_hybrid_candidates(
                    query,
                    candidates,
                    top_k=self.rerank_candidate_limit,
                    query_variants=query_variants,
                    source_query_groups=(
                        None
                        if self.enable_comparison_source_reservation
                        else []
                    ),
                )
                rerank_candidates = pre_ranked
                rerank_deferred_count = max(0, len(candidates) - len(rerank_candidates))

        if rerank_candidates and self.reranker.ready:
            stage_started_at = time.time()
            scores = self.reranker.score(
                query,
                [_reranker_passage(candidate) for candidate in rerank_candidates],
            )
            for candidate, score in zip(rerank_candidates, scores):
                candidate.rerank_score = float(score)
            timings["rerank_ms"] = _elapsed_ms(stage_started_at)

        project_focus_enabled = bool(
            scoped_project_id
            and project_route in {"project_first", "project_first_with_global"}
        )
        focused_candidate_count = 0
        for candidate in candidates:
            project_focus_weight = 1.0
            if project_focus_enabled and str(
                (candidate.metadata or {}).get("project_id") or ""
            ) == str(scoped_project_id):
                focused_candidate_count += 1
                project_focus_weight = PROJECT_FOCUS_WEIGHT
            candidate.metadata["_project_focus_weight"] = project_focus_weight
            usage_weight = (
                document_weight(
                    self.workspace.root_path,
                    candidate.metadata or {},
                )
                if self.enable_usage_ledger
                else 1.0
            )
            candidate.metadata["_knowledge_weight"] = usage_weight * project_focus_weight

        stage_started_at = time.time()
        ranking_diagnostics = {}
        reranked_candidates = rank_hybrid_candidates(
            query,
            candidates,
            top_k=max(top_k * 2, top_k),
            diagnostics=ranking_diagnostics,
            query_variants=query_variants,
            source_query_groups=(
                None
                if self.enable_comparison_source_reservation
                else []
            ),
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
        evidence = build_citations(parent_candidates, query=query)
        if self.enable_usage_ledger:
            record_document_hits(
                self.workspace.root_path,
                evidence,
            )
        sufficiency = evaluate_evidence_gate(
            evidence,
            query=query,
        ).public_dict()
        observability = {
            "schema_version": "retrieval_observability_v1",
            "schema_revision": 3,
            "query_intent": query_intent.name,
            "query_variant_count": len(query_variants),
            "feedback_variant_count": len(feedback_variants - set(base_query_variants)),
            "dense_query_instruction": bool(
                getattr(self, "embedding_query_instruction", "")
            ),
            "embedding_model": str(
                getattr(self, "embedding_model_name", "") or "unknown"
            ),
            "dense_pool_size": dense_pool_size,
            "secure_index": self.secure_store is not None,
            "reranker_enabled": bool(self.reranker.ready),
            "rerank_candidate_limit": self.rerank_candidate_limit,
            "rerank_limit_bypassed_reason": rerank_limit_bypassed_reason,
            "usage_ledger_enabled": self.enable_usage_ledger,
            "comparison_source_reservation_enabled": self.enable_comparison_source_reservation,
            "source_profile_recall_enabled": self.enable_source_profile_recall,
            "stage_counts": {
                "dense": len(dense_candidates),
                "keyword": len(keyword_candidates),
                "graph": len(graph_candidates),
                "source_title": len(source_title_candidates),
                "source_profile": len(source_profile_candidates),
                "merged": merged_candidate_count,
                "hydrated": len(candidates),
                "retrieved_candidate_count": len(candidates),
                "reranked": len(reranked_candidates),
                "rerank_input": len(rerank_candidates),
                "rerank_deferred": rerank_deferred_count,
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
            "section_route": section_route_diagnostics,
            "project_focus": {
                "enabled": project_focus_enabled,
                "requested": bool(project_id),
                "explicit_global_scope": query_intent.explicit_global_scope,
                "boost": PROJECT_FOCUS_WEIGHT if project_focus_enabled else 1.0,
                "boosted_candidate_count": focused_candidate_count,
                "global_candidate_count": global_candidate_count,
                "shared_global_candidate_count": shared_global_candidate_count,
                "indexed_candidate_count": indexed_candidate_count,
                "scoped_candidate_count": scoped_candidate_count,
                "route": project_route,
                "relevance_evaluation": project_relevance,
            },
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

    def evidence_for_chunk_ids(
        self,
        chunk_ids: list[str],
        *,
        top_k: int = 4,
        query: str = "",
        project_id: str | None = None,
        global_only: bool = False,
    ) -> dict[str, list]:
        """按 GraphRAG 的证据指针回读原始子块，避免社区摘要成为唯一事实来源。"""
        ordered_ids = []
        seen_ids = set()
        for chunk_id in chunk_ids or []:
            normalized = str(chunk_id or "").strip()
            if normalized and normalized not in seen_ids:
                seen_ids.add(normalized)
                ordered_ids.append(normalized)
        if not ordered_ids:
            return {"documents": [], "metadatas": [], "evidence": []}

        records: dict[str, tuple[str, dict]] = {}
        if self.secure_store is not None:
            records = {
                item.chunk_id: (str(item.content or ""), dict(item.metadata or {}))
                for item in self.secure_store.load_candidates(ordered_ids)
            }
        else:
            try:
                payload = self.collection.get(
                    ids=ordered_ids,
                    include=["documents", "metadatas"],
                )
            except Exception as exc:
                runtime_metrics.record_error(f"GraphRAG source evidence load failed: {exc}")
                logger.warning("[VectorDB] GraphRAG source evidence degraded: %s", exc)
                return {"documents": [], "metadatas": [], "evidence": []}
            for chunk_id, content, metadata in zip(
                payload.get("ids") or [],
                payload.get("documents") or [],
                payload.get("metadatas") or [],
            ):
                records[str(chunk_id)] = (str(content or ""), dict(metadata or {}))

        candidates = []
        for index, chunk_id in enumerate(ordered_ids):
            record = records.get(chunk_id)
            if record is None:
                continue
            content, metadata = record
            metadata_project_id = str(metadata.get("project_id") or "").strip()
            if project_id and metadata_project_id != str(project_id):
                continue
            if global_only and metadata_project_id:
                continue
            candidates.append(
                SearchCandidate(
                    id=chunk_id,
                    content=content,
                    metadata=metadata,
                    graph_score=max(1.0, float(len(ordered_ids) - index)),
                    final_score=0.22,
                    score_breakdown={"graph_provenance": 1.0},
                )
            )

        # GraphRAG 的 chunk_ids 是社区导航指针，不是查询排序结果。先按当前
        # 查询对回读的原文证据重排，避免无关的首个指针遮蔽后面的有效证据，
        # 进而触发证据门的误拒答。
        ranked_candidates = (
            rank_hybrid_candidates(
                query,
                candidates,
                top_k=min(len(candidates), max(1, int(top_k))),
            )
            if candidates and query
            else candidates[:max(1, int(top_k))]
        )
        expanded = self._expand_parent_context(
            ranked_candidates,
            top_k=max(1, int(top_k)),
            query=query,
        )
        return {
            "documents": [candidate.content for candidate in expanded],
            "metadatas": [candidate.metadata for candidate in expanded],
            "evidence": build_citations(expanded, query=query),
        }

    def metadata_for_chunk_ids(self, chunk_ids: list[str]) -> dict[str, dict]:
        """读取图谱指针对应的元数据，用于构建项目级派生图索引。"""
        ordered_ids = []
        seen_ids = set()
        for chunk_id in chunk_ids or []:
            normalized = str(chunk_id or "").strip()
            if normalized and normalized not in seen_ids:
                seen_ids.add(normalized)
                ordered_ids.append(normalized)
        if not ordered_ids:
            return {}

        if self.secure_store is not None:
            candidates = self.secure_store.load_candidates(ordered_ids)
            return {
                candidate.chunk_id: dict(candidate.metadata or {})
                for candidate in candidates
            }

        payload = self.collection.get(ids=ordered_ids, include=["metadatas"])
        return {
            str(chunk_id): dict(metadata or {})
            for chunk_id, metadata in zip(
                payload.get("ids") or [],
                payload.get("metadatas") or [],
            )
        }

    def _dense_candidates(
        self,
        query_vector: list[list[float]],
        n_results: int,
        project_id: str | None = None,
        global_only: bool = False,
        document_names: list[str] | None = None,
    ):
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
                for index, result in enumerate(
                    self.secure_store.dense_candidates(
                        embedding,
                        limit=n_results,
                        project_id=project_id,
                        global_only=global_only,
                        document_names=document_names,
                    )
                )
            ]

        try:
            query_kwargs = {
                "query_embeddings": query_vector,
                "n_results": (
                    max(n_results, self._indexed_candidate_count())
                    if global_only or document_names
                    else n_results
                ),
                "include": ["documents", "metadatas", "distances"],
            }
            if project_id is not None:
                query_kwargs["where"] = {"project_id": str(project_id)}
            vector_results = self.collection.query(**query_kwargs)
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
        if project_id is not None:
            candidates = [
                candidate
                for candidate in candidates
                if str((candidate.metadata or {}).get("project_id") or "")
                == str(project_id)
            ]
        elif global_only:
            candidates = [
                candidate
                for candidate in candidates
                if not str((candidate.metadata or {}).get("project_id") or "").strip()
            ]
        candidates = _filter_document_candidates(candidates, document_names or [])
        for index, candidate in enumerate(candidates[:n_results], start=1):
            candidate.dense_rank = index
        return candidates[:n_results]

    def _keyword_candidates(
        self,
        query: str,
        n_results: int,
        project_id: str | None = None,
        global_only: bool = False,
        document_names: list[str] | None = None,
    ):
        if self.secure_store is not None:
            candidates = self.secure_store.keyword_candidates(
                query,
                limit=n_results,
                project_id=project_id,
                global_only=global_only,
                document_names=document_names,
            )
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
            results = self.keyword_index.search(
                query,
                limit=n_results,
                project_id=project_id,
                global_only=global_only,
                document_names=document_names,
            )
        return [
            SearchCandidate(
                id=result.doc_id,
                content=result.content,
                metadata=result.metadata,
                sparse_rank=index + 1,
                keyword_score=result.score,
            )
            for index, result in enumerate(
                result for result in results
                if not document_names or _document_name_matches(result.metadata, document_names)
            )
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

    def _indexed_candidate_count(self, fallback: int = 0) -> int:
        try:
            return max(0, int(self.collection.count()))
        except (AttributeError, TypeError, ValueError):
            return max(0, int(fallback))

    def _graph_candidates(
        self,
        query: str,
        graph_db: GraphStore | None,
        limit: int,
        project_id: str | None = None,
        global_only: bool = False,
    ) -> list[SearchCandidate]:
        if not graph_db or not hasattr(graph_db, "recall_chunk_ids"):
            return []
        try:
            if project_id is not None or global_only:
                try:
                    chunk_ids = graph_db.recall_chunk_ids(
                        query,
                        limit=limit,
                        project_id=project_id,
                        global_only=global_only,
                    )
                except TypeError as exc:
                    if "project_id" not in str(exc) and "global_only" not in str(exc):
                        raise
                    # A graph backend that cannot enforce scope is not safe
                    # for project retrieval; dense/keyword paths remain usable.
                    return []
            else:
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

    def _source_title_candidates(
        self,
        query: str,
        limit: int = 12,
        project_id: str | None = None,
        global_only: bool = False,
        query_variants: list[str] | None = None,
        document_names: list[str] | None = None,
    ):
        scored: dict[str, tuple[float, SearchCandidate]] = {}
        source_queries = [query, *(query_variants or [])]
        for record_id, document, metadata in self._source_candidate_records():
            metadata_project_id = str(metadata.get("project_id") or "").strip()
            if project_id is not None and metadata_project_id != str(project_id):
                continue
            if global_only and metadata_project_id:
                continue
            if document_names and not _document_name_matches(metadata, document_names):
                continue
            score = max(
                (_source_match_score(item, metadata) for item in source_queries),
                default=0.0,
            )
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

    def _section_candidates(
        self,
        query: str,
        limit: int = 12,
        project_id: str | None = None,
        global_only: bool = False,
        document_names: list[str] | None = None,
    ):
        """Inject exact quoted section hits before dense/sparse candidate truncation."""
        topics = _quoted_section_topics(query)
        if not topics:
            self._section_route_diagnostics = {
                "topic_count": 0,
                "matched_record_count": 0,
                "selected_record_count": 0,
                "covered_topic_count": 0,
            }
            return []

        records = self._source_candidate_records()
        tree_nodes = self._section_tree_nodes(records)
        matched = []
        for record_id, document, metadata in records:
            metadata_project_id = str(metadata.get("project_id") or "").strip()
            if project_id is not None and metadata_project_id != str(project_id):
                continue
            if global_only and metadata_project_id:
                continue
            if document_names and not _document_name_matches(metadata, document_names):
                continue
            route = _section_route_match(query, metadata, document, tree_nodes=tree_nodes)
            if not route["matched_topic_indexes"]:
                continue
            route_metadata = {
                **metadata,
                "_section_route_mode": route["mode"],
                "_section_route_coverage": round(float(route["coverage"]), 6),
                "_section_route_exact_coverage": round(float(route["exact_coverage"]), 6),
                "_section_route_node": route["node_paths"][0] if route["node_paths"] else "",
                "_section_route_source": route.get("source") or "section_path",
            }
            matched.append((route, SearchCandidate(
                id=record_id,
                content=document,
                metadata=route_metadata,
                keyword_score=2.0,
            )))

        ordered = sorted(
            matched,
            key=lambda item: (
                len(item[0]["matched_topic_indexes"]),
                len(item[0]["exact_topic_indexes"]),
                item[0]["max_node_depth"],
                source_identity(item[1].metadata or {}, item[1].id),
                item[1].id,
            ),
            reverse=True,
        )
        selected: list[SearchCandidate] = []
        selected_ids: set[str] = set()
        for topic_index in range(len(topics)):
            for route, candidate in ordered:
                if (
                    topic_index in route["matched_topic_indexes"]
                    and candidate.id not in selected_ids
                ):
                    selected.append(candidate)
                    selected_ids.add(candidate.id)
                    break
        for _route, candidate in ordered:
            if candidate.id in selected_ids:
                continue
            selected.append(candidate)
            selected_ids.add(candidate.id)
            if len(selected) >= max(1, int(limit)):
                break
        selected = selected[:max(1, int(limit))]
        self._section_route_diagnostics = {
            "topic_count": len(topics),
            "matched_record_count": len(matched),
            "selected_record_count": len(selected),
            "covered_topic_count": len({
                topic_index
                for route, _candidate in ordered
                for topic_index in route["matched_topic_indexes"]
            }),
            "modes": sorted({
                str(route["mode"] or "unknown")
                for route, _candidate in matched
            }),
        }
        return selected

    @staticmethod
    def _section_tree_nodes(records):
        nodes = {}
        for _record_id, _document, metadata in records:
            metadata = metadata or {}
            path = normalize_section_path(metadata.get("section_path") or metadata.get("section"))
            tree_document_id = str(metadata.get("section_tree_document_id") or "").strip()
            if not tree_document_id:
                parent_match = re.match(
                    r"^(.*)_parent_\d+$",
                    str(metadata.get("parent_id") or "").strip(),
                )
                tree_document_id = parent_match.group(1).strip() if parent_match else ""
            if tree_document_id and path:
                for depth in range(1, len(path) + 1):
                    node_path = path[:depth]
                    node_id = section_node_id(tree_document_id, node_path)
                    nodes.setdefault(
                        node_id,
                        {
                            "title": node_path[-1],
                            "path_key": " > ".join(node_path),
                            "depth": depth,
                        },
                    )
            node_id = str(metadata.get("section_node_id") or "").strip()
            if not node_id or node_id in nodes:
                continue
            nodes[node_id] = {
                "title": metadata.get("section_node_title") or (path[-1] if path else ""),
                "path_key": metadata.get("section_path_key") or " > ".join(path),
                "depth": metadata.get("section_node_depth") or len(path),
            }
        return nodes

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

    def _source_profile_records(self):
        """Build bounded document profiles from identity and section metadata."""
        if self._source_profile_cache is not None:
            return self._source_profile_cache

        grouped: dict[str, dict[str, Any]] = {}
        for record_id, document, metadata in self._source_candidate_records():
            key = source_key(metadata or {}, record_id)
            if not key:
                continue
            entry = grouped.setdefault(
                key,
                {
                    "profile_values": [],
                    "records": [],
                },
            )
            values = [
                metadata.get("document_title"),
                metadata.get("source_filename"),
                metadata.get("document_number"),
                metadata.get("title"),
                metadata.get("section"),
                metadata.get("Header 1"),
                metadata.get("Header 2"),
                metadata.get("Header 3"),
                metadata.get("Header 4"),
                metadata.get("Header 5"),
                metadata.get("Header 6"),
            ]
            section_path = metadata.get("section_path")
            if isinstance(section_path, (list, tuple)):
                values.extend(section_path)
            elif section_path:
                values.extend(str(section_path).split(">"))
            for value in values:
                normalized = " ".join(str(value or "").strip().split())
                if normalized and normalized not in entry["profile_values"]:
                    entry["profile_values"].append(normalized)
            entry["records"].append((record_id, document, metadata))

        for entry in grouped.values():
            entry["profile_text"] = " ".join(entry["profile_values"])[:6000]
        self._source_profile_cache = grouped
        return grouped

    def _source_profile_candidates(
        self,
        query_vectors: list[list[float]],
        *,
        query_variants: list[str] | None = None,
        limit: int = 12,
        project_id: str | None = None,
        global_only: bool = False,
        document_names: list[str] | None = None,
    ) -> list[SearchCandidate]:
        """Recall representative child chunks from semantically matched sources.

        This is an opt-in document-first stage.  It widens recall using only
        stable document/section metadata, then hands the representative child
        back to the existing hybrid reranker and parent expansion stages.
        """
        if not self.enable_source_profile_recall or not query_vectors:
            return []

        profiles = self._source_profile_records()
        if not profiles:
            return []

        missing = [
            (key, entry["profile_text"])
            for key, entry in profiles.items()
            if key not in self._source_profile_embeddings
            and entry.get("profile_text")
        ]
        if missing:
            embeddings = self._ensure_embedding_model().encode(
                [text for _key, text in missing],
                normalize_embeddings=True,
            )
            for (key, _text), embedding in zip(missing, embeddings):
                self._source_profile_embeddings[key] = [float(value) for value in embedding]

        variants = [
            " ".join(str(item or "").strip().split())
            for item in (query_variants or [])
            if " ".join(str(item or "").strip().split())
        ]
        profile_scores = []
        for key, entry in profiles.items():
            embedding = self._source_profile_embeddings.get(key)
            if not embedding:
                continue
            records = [
                record
                for record in entry["records"]
                if (
                    project_id is None
                    or str((record[2] or {}).get("project_id") or "").strip()
                    == str(project_id)
                )
                and (
                    not global_only
                    or not str((record[2] or {}).get("project_id") or "").strip()
                )
                and (
                    not document_names
                    or _document_name_matches(record[2] or {}, document_names)
                )
            ]
            if not records:
                continue
            similarity = max(
                sum(float(left) * float(right) for left, right in zip(embedding, query_vector))
                for query_vector in query_vectors
            )
            if similarity < SOURCE_PROFILE_MIN_SIMILARITY:
                continue
            profile_scores.append((similarity, key, records))

        profile_scores.sort(key=lambda item: (item[0], item[1]), reverse=True)
        if self.enable_source_profile_recall:
            active_scores = getattr(self, "_active_source_profile_scores", None)
            if not isinstance(active_scores, dict):
                active_scores = {}
                self._active_source_profile_scores = active_scores
            active_scores.update(
                {key: float(similarity) for similarity, key, _records in profile_scores}
            )
        selected = []
        for profile_rank, (similarity, _key, records) in enumerate(
            profile_scores[: max(1, min(int(limit), 120))],
            start=1,
        ):
            representative = max(
                records,
                key=lambda record: max(
                    (
                        lexical_rerank_score(
                            variant,
                            " ".join(
                                [
                                    str(record[2].get("document_title") or ""),
                                    str(record[2].get("section") or ""),
                                    str(record[2].get("section_path") or ""),
                                    str(record[1] or "")[:2400],
                                ]
                            ),
                        )
                        for variant in variants
                    ),
                    default=0.0,
                ),
            )
            record_id, document, metadata = representative
            profile_metadata = {
                **(metadata or {}),
                "_source_profile_score": round(float(similarity), 6),
                "_source_profile_rank": profile_rank,
            }
            selected.append(
                SearchCandidate(
                    id=record_id,
                    content=document,
                    metadata=profile_metadata,
                    dense_rank=profile_rank,
                    keyword_score=float(similarity),
                )
            )
        return selected

    def _annotate_source_profile_scores(
        self,
        candidates: list[SearchCandidate],
    ) -> None:
        if not self.enable_source_profile_recall:
            return
        for candidate in candidates:
            key = source_key(candidate.metadata or {}, candidate.id)
            score = self._active_source_profile_scores.get(key)
            if score is None:
                continue
            candidate.metadata["_source_profile_score"] = round(float(score), 6)

    def _merge_candidates(self, *candidate_groups: list[SearchCandidate]):
        merged: dict[str, SearchCandidate] = {}
        for group in candidate_groups:
            for candidate in group:
                merge_key = str(candidate.metadata.get("child_id") or candidate.id)
                if merge_key not in merged:
                    merged[merge_key] = candidate
                    continue
                existing = merged[merge_key]
                existing.metadata = {**existing.metadata, **candidate.metadata}
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
            if (
                len(context.strip()) < 120
                and re.match(r"^\s{0,3}#{1,6}\s+", context)
                and hasattr(self.secure_store or self.parent_store, "get_adjacent_parent")
            ):
                store = self.secure_store or self.parent_store
                adjacent = store.get_adjacent_parent(parent_id, offset=1)
                adjacent_content = str((adjacent or {}).get("content") or "").strip()
                if adjacent_content:
                    context = f"{context.strip()}\n\n{adjacent_content}"
                    expansion_metadata = {
                        **expansion_metadata,
                        "context_expansion_mode": "heading_adjacent_parent_window",
                        "context_char_count": len(context),
                    }
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

        if query_intent.requires_source_diversity:
            # Source reservation above protects coverage during parent
            # expansion.  Reorder only after the parent-level slots are fixed,
            # so the strongest hybrid-scored citation is still shown first.
            expanded.sort(
                key=lambda candidate: (
                    candidate.final_score,
                    candidate.rerank_score,
                    candidate.keyword_score,
                ),
                reverse=True,
            )
        return expanded[:top_k]

    def _build_graph_context(
        self,
        query: str,
        docs: list[str],
        graph_db: GraphStore | None = None,
        project_id: str | None = None,
        global_only: bool = False,
    ):
        if not graph_db:
            return [], []

        try:
            graph_match_space = "\n".join([query, *docs[:2]]).lower()
            matched_entities = []
            if project_id is not None or global_only:
                try:
                    entities = graph_db.get_all_entities(
                        project_id=project_id,
                        global_only=global_only,
                    )
                except TypeError as exc:
                    if "project_id" not in str(exc) and "global_only" not in str(exc):
                        raise
                    return [], []
            else:
                entities = graph_db.get_all_entities()
            for entity in entities:
                if entity and entity.lower() in graph_match_space:
                    matched_entities.append(entity)
                if len(matched_entities) >= 8:
                    break

            seen_relations = set()
            graph_context = []
            for entity in matched_entities:
                if project_id is not None or global_only:
                    try:
                        relations = graph_db.get_subgraph(
                            entity,
                            depth=1,
                            project_id=project_id,
                            global_only=global_only,
                        )
                    except TypeError as exc:
                        if "project_id" not in str(exc) and "global_only" not in str(exc):
                            raise
                        return [], []
                else:
                    relations = graph_db.get_subgraph(entity, depth=1)
                for source, relation, target in relations:
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
