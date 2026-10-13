import math
import json
import re
from dataclasses import dataclass, field
from typing import Any

from core.keyword_index import lexical_tokens
from core.query_expansion import comparison_query_groups, query_requires_source_diversity


@dataclass
class SearchCandidate:
    id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    dense_rank: int | None = None
    sparse_rank: int | None = None
    graph_score: float = 0.0
    vector_distance: float | None = None
    keyword_score: float = 0.0
    rrf_score: float = 0.0
    rerank_score: float = 0.0
    final_score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)


@dataclass
class RankedCandidate:
    candidate: SearchCandidate
    score: float


@dataclass(frozen=True)
class QueryIntent:
    name: str
    requires_source_diversity: bool = False
    explicit_source: bool = False
    clause_references: tuple[str, ...] = ()
    prefers_table: bool = False
    prefers_visual_asset: bool = False
    broad_scope: bool = False
    explicit_global_scope: bool = False


_FORMAL_CLAUSE_PATTERN = re.compile(
    r"第[零〇一二三四五六七八九十百千万两\d]+"
    r"(?:[.．][零〇一二三四五六七八九十百千万两\d]+)*[章节条款项目]"
)
_DECIMAL_SECTION_PATTERN = re.compile(r"(?<![A-Za-z0-9.])\d+(?:[.．]\d+){1,3}(?![A-Za-z0-9.])")
_NUMBERED_SECTION_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])\d+[.．、)](?=\s*[A-Za-z\u4e00-\u9fff])"
)
_TABLE_QUERY_PATTERN = re.compile(
    r"表格|表中|附表|清单|名单|技术参数|配置参数|指标值|验收指标|验收标准|"
    r"金额|比例|数量|日期|期限|分别列出|逐项|指标|准确率|精确率|召回率|"
    r"F1|F-score|AUC|P50|P95|延迟|吞吐量|吞吐率|性能数值"
    ,
    flags=re.IGNORECASE,
)
_VISUAL_ASSET_QUERY_PATTERN = re.compile(
    r"图表|图中|插图|示意图|流程图|架构图|曲线图|柱状图|折线图|图片|图像|"
    r"截图|界面|选区|指标图|分析图|混淆矩阵|误检图|误检分析图|"
    r"figure|diagram|chart|plot|image",
    flags=re.IGNORECASE,
)
_BROAD_QUERY_PATTERN = re.compile(
    r"概述|总结|综述|主要内容|核心要点|总体要求|整体情况|全文|全部内容|"
    r"有哪些方面|归纳|提纲"
)
_EXPLICIT_GLOBAL_SCOPE_PATTERN = re.compile(
    r"全库|整个(?:知识库|资料库|文档库)|全部(?:项目|资料|文档|论文)|"
    r"所有(?:项目|资料|文档|论文)|跨项目|跨多个?项目|多项目|两个项目|不同项目|"
    r"项目(?:之间|间)|跨文档|跨资料|全量(?:项目|资料|文档)|"
    r"\b(?:all\s+projects?|cross[- ]project|global\s+(?:search|scope))\b",
    flags=re.IGNORECASE,
)
_CLAUSE_QUERY_PATTERN = re.compile(r"条款|条文|章节|哪一条|哪一款|法律依据|政策依据")
_DOCUMENT_NUMBER_QUERY_PATTERN = re.compile(
    r"(?:[\u4e00-\u9fffA-Za-z]{1,20}[〔\[]\d{4}[〕\]]\s*\d+\s*号|"
    r"(?:GB(?:/T)?|DB\d*/T|T/[A-Za-z0-9_-]+|Q/[A-Za-z0-9_-]+)"
    r"\s*[A-Za-z0-9./_-]+(?:-\d{4})?)",
    flags=re.IGNORECASE,
)
_SECTION_HEADER_KEYS = tuple(f"Header {level}" for level in range(1, 7))
SOURCE_PROFILE_RANK_WEIGHT = 0.10


def tokenize(text: str) -> list[str]:
    return lexical_tokens(text)


def classify_query_intent(query: str) -> QueryIntent:
    normalized = str(query or "").strip()
    requires_diversity = query_requires_source_diversity(normalized)
    explicit_source = bool(_query_source_hints(normalized))
    clause_references = list(_FORMAL_CLAUSE_PATTERN.findall(normalized))
    if (
        _CLAUSE_QUERY_PATTERN.search(normalized)
        or _DECIMAL_SECTION_PATTERN.search(normalized)
        or _NUMBERED_SECTION_PATTERN.search(normalized)
    ):
        clause_references.extend(_DECIMAL_SECTION_PATTERN.findall(normalized))
        clause_references.extend(_NUMBERED_SECTION_PATTERN.findall(normalized))
    clause_references = list(dict.fromkeys(_normalize_clause_reference(item) for item in clause_references))
    prefers_table = bool(_TABLE_QUERY_PATTERN.search(normalized))
    prefers_visual_asset = bool(_VISUAL_ASSET_QUERY_PATTERN.search(normalized))
    broad_scope = bool(_BROAD_QUERY_PATTERN.search(normalized))
    explicit_global_scope = bool(_EXPLICIT_GLOBAL_SCOPE_PATTERN.search(normalized))

    if requires_diversity:
        name = "multi_source_comparison"
    elif clause_references or _CLAUSE_QUERY_PATTERN.search(normalized):
        name = "clause_lookup"
    elif prefers_table:
        name = "table_lookup"
    elif broad_scope:
        name = "broad_summary"
    elif explicit_source:
        name = "explicit_source"
    else:
        name = "factual_lookup"

    return QueryIntent(
        name=name,
        requires_source_diversity=requires_diversity,
        explicit_source=explicit_source,
        clause_references=tuple(clause_references),
        prefers_table=prefers_table,
        prefers_visual_asset=prefers_visual_asset,
        broad_scope=broad_scope,
        explicit_global_scope=explicit_global_scope,
    )


def _query_source_hints(query: str) -> list[str]:
    hints = [
        hint.strip()
        for hint in re.findall(r"《([^》]+)》", query or "")
        if hint.strip()
    ]
    # Product queries often quote a document title with Chinese quotation
    # marks, e.g. 查找“配置说明”里关于“...”。 Only accept the quoted span
    # directly followed by a document-scope marker; the later quoted span is
    # normally the requested content, not a source hint.
    quoted_scope_pattern = re.compile(
        r"[“\"]([^”\"]+)[”\"]\s*(?:里|中|中的|文档中|文件中|报告中|说明中)"
    )
    hints.extend(
        match.group(1).strip()
        for match in quoted_scope_pattern.finditer(query or "")
        if match.group(1).strip()
    )
    hints.extend(
        match.group(0).strip()
        for match in _DOCUMENT_NUMBER_QUERY_PATTERN.finditer(query or "")
        if match.group(0).strip()
    )
    return list(dict.fromkeys(hints))


def bm25_scores(query: str, documents: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    query_terms = tokenize(query)
    if not query_terms or not documents:
        return [0.0 for _ in documents]

    tokenized_docs = [tokenize(document) for document in documents]
    doc_count = len(tokenized_docs)
    avg_doc_len = sum(len(tokens) for tokens in tokenized_docs) / max(doc_count, 1)
    if avg_doc_len <= 0:
        return [0.0 for _ in documents]

    document_frequency: dict[str, int] = {}
    for tokens in tokenized_docs:
        for term in set(tokens):
            document_frequency[term] = document_frequency.get(term, 0) + 1

    scores = []
    for tokens in tokenized_docs:
        term_frequency: dict[str, int] = {}
        for term in tokens:
            term_frequency[term] = term_frequency.get(term, 0) + 1

        doc_len = len(tokens)
        score = 0.0
        for term in query_terms:
            tf = term_frequency.get(term, 0)
            if tf <= 0:
                continue

            df = document_frequency.get(term, 0)
            idf = math.log(1 + (doc_count - df + 0.5) / (df + 0.5))
            denominator = tf + k1 * (1 - b + b * doc_len / avg_doc_len)
            score += idf * (tf * (k1 + 1)) / denominator
        scores.append(score)

    return scores


def bm25_rank(query: str, candidates: list[SearchCandidate]) -> list[RankedCandidate]:
    scores = bm25_scores(query, [candidate.content for candidate in candidates])
    return sorted(
        [
            RankedCandidate(candidate=candidate, score=score)
            for candidate, score in zip(candidates, scores)
        ],
        key=lambda item: (item.score, str(item.candidate.id)),
        reverse=True,
    )


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, candidate_id in enumerate(ranking, start=1):
            scores[candidate_id] = scores.get(candidate_id, 0.0) + 1.0 / (k + rank)
    return scores


def lexical_rerank_score(query: str, content: str) -> float:
    query_tokens = tokenize(query)
    content_tokens = tokenize(content)
    if not query_tokens or not content_tokens:
        return 0.0

    content_token_set = set(content_tokens)
    overlap = sum(1 for token in query_tokens if token in content_token_set)
    coverage = overlap / max(len(query_tokens), 1)
    density = overlap / max(len(content_tokens), 1)
    return (0.82 * coverage) + (0.18 * density)


def rank_hybrid_candidates(
    query: str,
    candidates: list[SearchCandidate],
    top_k: int,
    diagnostics: dict[str, Any] | None = None,
    query_variants: list[str] | None = None,
    source_query_groups: list[list[str]] | None = None,
) -> list[SearchCandidate]:
    if not candidates:
        if diagnostics is not None:
            diagnostics.update({
                "query_intent": classify_query_intent(query).name,
                "input_candidate_count": 0,
                "redundant_deferred_count": 0,
                "selected_candidate_count": 0,
                "selected_source_count": 0,
                "selected_content_kind_counts": {},
            })
        return []

    # Retrieval backends and rerankers are external numeric boundaries.  A
    # non-finite value must not poison ranking, diagnostics, or citation score
    # serialization.
    for candidate in candidates:
        candidate.graph_score = _finite_score(candidate.graph_score)
        candidate.rerank_score = _finite_score(candidate.rerank_score)
        if candidate.vector_distance is not None:
            candidate.vector_distance = _finite_score(
                candidate.vector_distance,
                default=None,
            )

    query_intent = classify_query_intent(query)
    search_queries = [str(query or "").strip()]
    search_queries.extend(str(variant or "").strip() for variant in query_variants or [])
    search_queries = list(dict.fromkeys(item for item in search_queries if item))
    source_query_groups = (
        source_query_groups
        if source_query_groups is not None
        else comparison_query_groups(query)
    )
    sparse_rankings: list[list[str]] = []
    keyword_scores: dict[str, float] = {}
    sparse_ranks: dict[str, int] = {}
    for search_query in search_queries:
        keyword_ranked = bm25_rank(search_query, candidates)
        ranking = []
        for rank, ranked_candidate in enumerate(keyword_ranked, start=1):
            candidate = ranked_candidate.candidate
            if ranked_candidate.score <= 0:
                continue
            ranking.append(candidate.id)
            keyword_scores[candidate.id] = max(
                keyword_scores.get(candidate.id, 0.0),
                ranked_candidate.score,
            )
            sparse_ranks[candidate.id] = min(sparse_ranks.get(candidate.id, rank), rank)
        if ranking:
            sparse_rankings.append(ranking)

    sparse_rrf_scores = reciprocal_rank_fusion(sparse_rankings)
    sparse_ranking = [
        candidate.id
        for candidate in sorted(
            candidates,
            key=lambda item: (sparse_rrf_scores.get(item.id, 0.0), str(item.id)),
            reverse=True,
        )
        if sparse_rrf_scores.get(candidate.id, 0.0) > 0
    ]
    for candidate in candidates:
        candidate.keyword_score = keyword_scores.get(candidate.id, 0.0)
        candidate.sparse_rank = sparse_ranks.get(candidate.id)

    dense_ranking = [
        candidate.id
        for candidate in sorted(
            candidates,
            key=lambda item: (
                item.dense_rank if item.dense_rank is not None else 10**9,
                str(item.id),
            ),
        )
        if candidate.dense_rank is not None
    ]
    source_scores = {
        candidate.id: max(
            (_source_match_score(search_query, candidate.metadata) for search_query in search_queries),
            default=0.0,
        )
        for candidate in candidates
    }
    source_ranking = [
        candidate.id
        for candidate in sorted(
            candidates,
            key=lambda item: (source_scores.get(item.id, 0.0), str(item.id)),
            reverse=True,
        )
        if source_scores.get(candidate.id, 0.0) >= 0.55
    ]
    rrf_scores = reciprocal_rank_fusion([dense_ranking, sparse_ranking, source_ranking])
    max_graph_score = max((candidate.graph_score for candidate in candidates), default=0.0)

    for candidate in candidates:
        graph_norm = candidate.graph_score / max_graph_score if max_graph_score > 0 else 0.0
        lexical_score = max(
            (
                lexical_rerank_score(search_query, candidate.content)
                for search_query in search_queries
            ),
            default=0.0,
        )
        semantic_score = max(candidate.rerank_score, 0.0)
        combined_rerank = max(lexical_score, semantic_score)
        source_score = source_scores.get(candidate.id, 0.0)
        structure_score = _structure_match_score(query, query_intent, candidate)
        source_profile_score = _bounded_score(
            (candidate.metadata or {}).get("_source_profile_score")
        )
        knowledge_weight = _finite_score(
            (candidate.metadata or {}).get("_knowledge_weight") or 1.0,
            default=1.0,
        )
        project_focus_weight = _finite_score(
            (candidate.metadata or {}).get("_project_focus_weight") or 1.0,
            default=1.0,
        )
        candidate.rrf_score = rrf_scores.get(candidate.id, 0.0)
        candidate.rerank_score = combined_rerank
        base_score = (
            0.36 * candidate.rrf_score
            + 0.24 * candidate.rerank_score
            + 0.10 * graph_norm
            + 0.30 * source_score
            + 0.14 * structure_score
            + SOURCE_PROFILE_RANK_WEIGHT * source_profile_score
        )
        candidate.final_score = base_score * knowledge_weight
        candidate.score_breakdown = {
            "rrf": round(candidate.rrf_score, 6),
            "lexical_rerank": round(lexical_score, 6),
            "semantic_rerank": round(semantic_score, 6),
            "graph": round(graph_norm, 6),
            "keyword": round(candidate.keyword_score, 6),
            "source": round(source_score, 6),
            "source_profile": round(source_profile_score, 6),
            "structure": round(structure_score, 6),
            "knowledge_weight": round(knowledge_weight, 6),
            "project_focus": round(project_focus_weight, 6),
            "final": round(candidate.final_score, 6),
        }

    ranked = sorted(
        candidates,
        key=lambda candidate: (
            candidate.final_score,
            candidate.keyword_score,
            -1 * (candidate.vector_distance or 0.0),
            str(candidate.id),
        ),
        reverse=True,
    )
    return _select_ranked_evidence(
        query,
        ranked,
        top_k=top_k,
        diagnostics=diagnostics,
        query_intent=query_intent,
        source_query_groups=source_query_groups,
    )


def build_citations(
    candidates: list[SearchCandidate],
    max_snippet_chars: int = 420,
    query: str = "",
) -> list[dict[str, Any]]:
    citations = []
    for index, candidate in enumerate(candidates, start=1):
        metadata = candidate.metadata or {}
        page = metadata.get("page_number") or metadata.get("page")
        section = (
            metadata.get("section")
            or metadata.get("Header 6")
            or metadata.get("Header 5")
            or metadata.get("Header 4")
            or metadata.get("Header 3")
            or metadata.get("Header 2")
            or metadata.get("Header 1")
        )
        if _looks_like_replacement_text(str(section or "")):
            section = "未标注章节（原文编码异常）"
        # The indexed child excerpt often starts with a heading.  The candidate
        # content has already been expanded to the relevant parent window, so it
        # is the stronger, source-grounded citation text for answer generation.
        snippet_source = candidate.content or metadata.get("child_excerpt")
        snippet_source = _strip_retrieval_context(str(snippet_source or ""))
        snippet = re.sub(r"\s+", " ", str(snippet_source)).strip()
        if _looks_like_replacement_text(snippet):
            snippet = "该文档切片编码异常，请重新导入原始文件并重建索引。"
        if len(snippet) > max_snippet_chars:
            snippet = snippet[:max_snippet_chars] + "…"
        support = _candidate_support_signal(query, candidate)
        visual_assets = _visual_asset_records(metadata)

        citations.append({
            "id": f"E{index}",
            "candidate_id": candidate.id,
            "document_id": metadata.get("document_id"),
            "project_id": metadata.get("project_id"),
            "asset_id": metadata.get("asset_id"),
            "asset_version_id": metadata.get("asset_version_id"),
            "source_filename": source_identity(metadata, "未知文件"),
            "document_title": metadata.get("document_title"),
            "document_number": metadata.get("document_number"),
            "page": page,
            "page_label": str(page) if page is not None else "未知",
            "source_locator": metadata.get("source_locator"),
            "source_locator_kind": metadata.get("source_locator_kind"),
            "chunk_index": metadata.get("chunk_index"),
            "section_path": _section_path_values(metadata),
            "section": section,
            "section_route_mode": metadata.get("_section_route_mode"),
            "section_route_coverage": metadata.get("_section_route_coverage"),
            "section_route_node": metadata.get("_section_route_node"),
            "clause_id": metadata.get("clause_id"),
            "content_kind": metadata.get("content_kind"),
            "visual_asset_count": len(visual_assets),
            "visual_assets": visual_assets,
            "visual_asset_captions": [item["caption"] for item in visual_assets],
            "score": round(_finite_score(candidate.final_score), 6),
            "breakdown": candidate.score_breakdown,
            "snippet": snippet,
            "support_score": support["score"],
            "support_summary": support["summary"],
            "support_terms": support["terms"],
            "support_method": "extractive_query_overlap_v0.1",
        })
    return citations


def _candidate_support_signal(
    query: str,
    candidate: SearchCandidate,
    max_summary_chars: int = 360,
) -> dict[str, Any]:
    """Expose a deterministic, extractive support signal for human review.

    This is not a probability and never replaces the evidence gate.  It makes
    the reason a candidate supports a query inspectable without another model
    call or a generated summary.
    """
    content = _strip_retrieval_context(str(candidate.content or "")).strip()
    if not str(query or "").strip():
        return {"score": None, "summary": _support_summary("", content, max_summary_chars), "terms": []}

    query_terms = list(dict.fromkeys(tokenize(query)))
    content_text = " ".join(
        str((candidate.metadata or {}).get(key) or "")
        for key in (
            "section",
            "section_path",
            "clause_id",
            "child_excerpt",
        )
    ) + " " + content
    content_terms = set(tokenize(content_text))
    matched_terms = [term for term in query_terms if term in content_terms]
    coverage = len(matched_terms) / max(len(query_terms), 1)
    breakdown = candidate.score_breakdown or {}
    lexical = _bounded_score(breakdown.get("lexical_rerank"))
    semantic = _bounded_score(breakdown.get("semantic_rerank"))
    structure = _bounded_score(breakdown.get("structure"))
    phrase = _compact_match(query, content)
    score = min(
        1.0,
        0.58 * coverage
        + 0.16 * max(lexical, semantic)
        + 0.14 * structure
        + 0.12 * phrase,
    )
    return {
        "score": round(score, 6),
        "summary": _support_summary(query, content, max_summary_chars),
        "terms": matched_terms[:12],
    }


def _support_summary(query: str, content: str, max_chars: int) -> str:
    sentences = [
        sentence.strip(" -\t")
        for sentence in re.split(r"(?<=[。！？!?；;])\s*|\n+", content or "")
        if sentence.strip(" -\t")
    ]
    if not sentences:
        return ""
    query_terms = set(tokenize(query))
    if query_terms:
        ranked = sorted(
            enumerate(sentences),
            key=lambda item: (
                -len(query_terms & set(tokenize(item[1]))),
                item[0],
            ),
        )
        selected = [sentence for _, sentence in ranked if query_terms & set(tokenize(sentence))][:2]
        sentences = selected or sentences[:1]
    else:
        sentences = sentences[:2]
    summary = " ".join(sentences)
    return summary[:max_chars] + ("…" if len(summary) > max_chars else "")


def _bounded_score(value: Any) -> float:
    parsed = _finite_score(value)
    return max(0.0, min(1.0, parsed))


def _finite_score(value: Any, *, default: Any = 0.0) -> Any:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _compact_match(query: str, content: str) -> float:
    query_compact = re.sub(r"\s+", "", str(query or "")).strip("？?。！!，,；;")
    content_compact = re.sub(r"\s+", "", str(content or ""))
    if len(query_compact) < 4 or query_compact not in content_compact:
        return 0.0
    return 1.0


def _strip_retrieval_context(value: str) -> str:
    """Remove navigation labels while keeping the cited source passage."""
    lines = []
    for line in str(value or "").splitlines():
        if re.match(r"^\[(?:DOCUMENT|DOCUMENT_NUMBER|DOCUMENT_TYPE|SECTION|DOCUMENT_SUMMARY|CLAUSE|CHUNK_SUMMARY)\]", line.strip()):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _looks_like_replacement_text(value: str) -> bool:
    text = str(value or "")
    replacement_count = text.count("?")
    return len(text) >= 8 and replacement_count >= 4 and replacement_count / len(text) >= 0.18


def source_identity(metadata: dict[str, Any], fallback: str = "") -> str:
    return str(
        metadata.get("source_filename")
        or metadata.get("document_title")
        or metadata.get("document_number")
        or fallback
    )


def source_key(metadata: dict[str, Any], fallback: str = "") -> str:
    """Stable deduplication key; display labels remain human-readable."""
    return str(metadata.get("document_id") or source_identity(metadata, fallback))


def source_diversity_key(metadata: dict[str, Any], fallback: str = "") -> str:
    """Identify a logical source without collapsing same-named scoped assets."""
    metadata = metadata or {}
    identity = next(
        (
            str(metadata.get(field) or "").strip()
            for field in ("asset_version_id", "document_id", "asset_id")
            if str(metadata.get(field) or "").strip()
        ),
        source_identity(metadata, fallback).strip(),
    )
    if not identity:
        return ""
    project_id = str(metadata.get("project_id") or "").strip()
    scope = f"project:{project_id}" if project_id else "global"
    return f"{scope}:{identity}"


def _source_match_score(query: str, metadata: dict[str, Any]) -> float:
    sources = [
        str(value).lower()
        for value in (
            metadata.get("document_title"),
            metadata.get("document_number"),
            metadata.get("source_filename"),
            metadata.get("title"),
            metadata.get("source_path"),
        )
        if str(value or "").strip()
    ]
    if not sources:
        return 0.0

    query_source_tokens = _source_match_tokens(query)
    if not query_source_tokens:
        return 0.0

    for title_hint in _query_source_hints(query):
        hint_tokens = _source_match_tokens(title_hint)
        for source in sources:
            source_tokens = _source_match_tokens(source)
            if hint_tokens and (
                _source_token_sequence_in(hint_tokens, source_tokens)
                or _source_token_sequence_in(source_tokens, hint_tokens)
            ):
                return 1.0

    best_score = 0.0
    query_tokens = set(tokenize(query))
    for source in sources:
        source_match_tokens = _source_match_tokens(source)
        if not source_match_tokens:
            continue
        if _source_token_sequence_in(source_match_tokens, query_source_tokens):
            return 1.0
        source_tokens = set(tokenize(source))
        if query_tokens and source_tokens:
            best_score = max(
                best_score,
                min(
                    1.0,
                    len(query_tokens & source_tokens)
                    / max(min(len(query_tokens), len(source_tokens)), 1),
                ),
            )

    return best_score


def _source_match_tokens(value: str) -> list[str]:
    normalized = str(value or "").lower()
    for suffix in (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".docx", ".md", ".html", ".txt"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return re.findall(r"[0-9a-z]+|[\u4e00-\u9fff]+", normalized)


def _source_token_sequence_in(needle: list[str], haystack: list[str]) -> bool:
    if not needle or not haystack or len(needle) > len(haystack):
        return False
    width = len(needle)
    return any(haystack[index : index + width] == needle for index in range(len(haystack) - width + 1))


def _normalize_clause_reference(value: str) -> str:
    return re.sub(r"[\s.．、（）()]+", "", str(value or "")).lower()


def _quoted_section_topics(query: str) -> list[str]:
    return list(dict.fromkeys(
        re.sub(r"\s+", "", item.strip())
        for item in re.findall(r"[“\"]([^”\"]+)[”\"]", query or "")
        if item.strip()
    ))


def _section_path_values(metadata: dict[str, Any] | None) -> list[str]:
    metadata = metadata or {}
    section_path = metadata.get("section_path")
    if isinstance(section_path, (list, tuple)):
        parts = [str(item).strip() for item in section_path if str(item).strip()]
    elif section_path:
        parts = [part.strip() for part in str(section_path).split(">") if part.strip()]
    else:
        parts = [
            str(metadata.get(key) or "").strip()
            for key in _SECTION_HEADER_KEYS
            if str(metadata.get(key) or "").strip()
        ]
    if not parts and str(metadata.get("section") or "").strip():
        parts = [str(metadata["section"]).strip()]
    return parts


def _section_route_match(
    query: str,
    metadata: dict[str, Any] | None,
    content: str = "",
    tree_nodes: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    tree_route = _section_tree_route_match(query, metadata, tree_nodes)
    if tree_route is not None:
        return tree_route

    topics = _quoted_section_topics(query)
    path = _section_path_values(metadata)
    compact_path = [re.sub(r"\s+", "", item) for item in path]
    matched_indexes: list[int] = []
    exact_indexes: list[int] = []
    node_paths: list[str] = []
    modes: list[str] = []

    for topic_index, topic in enumerate(topics):
        exact_positions = [
            index
            for index, section in enumerate(compact_path)
            if section == topic
        ]
        partial_positions = [
            index
            for index, section in enumerate(compact_path)
            if topic in section
        ]
        if exact_positions:
            position = exact_positions[-1]
            matched_indexes.append(topic_index)
            exact_indexes.append(topic_index)
            node_paths.append(" > ".join(path[: position + 1]))
            modes.append("descendant" if position + 1 < len(path) else "exact_node")
            continue
        if partial_positions:
            position = partial_positions[-1]
            matched_indexes.append(topic_index)
            node_paths.append(" > ".join(path[: position + 1]))
            modes.append("partial_node")
            continue

        # Older indexes may preserve the heading only in the child excerpt.
        fallback_text = " ".join(
            str((metadata or {}).get(key) or "")
            for key in (*_SECTION_HEADER_KEYS, "section", "child_excerpt")
        ) + " " + str(content or "")[:900]
        if topic in re.sub(r"\s+", "", fallback_text):
            matched_indexes.append(topic_index)
            modes.append("heading_fallback")

    mode = ""
    if modes:
        mode = (
            "exact_node"
            if all(item == "exact_node" for item in modes)
            else "descendant"
            if any(item in {"exact_node", "descendant"} for item in modes)
            else modes[0]
        )
    return {
        "topics": topics,
        "matched_topic_indexes": tuple(matched_indexes),
        "exact_topic_indexes": tuple(exact_indexes),
        "node_paths": tuple(node_paths),
        "coverage": len(matched_indexes) / max(len(topics), 1),
        "exact_coverage": len(exact_indexes) / max(len(topics), 1),
        "max_node_depth": max(
            (len(path.split(" > ")) for path in node_paths if path),
            default=0,
        ),
        "mode": mode,
    }


def _section_tree_route_match(
    query: str,
    metadata: dict[str, Any] | None,
    tree_nodes: dict[str, dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Route through persisted node ancestry when a rebuilt tree is present."""
    if not tree_nodes:
        return None
    topics = _quoted_section_topics(query)
    current_id = str((metadata or {}).get("section_node_id") or "").strip()
    if not topics or not current_id or current_id not in tree_nodes:
        return None

    current_ancestors = {
        item
        for item in str((metadata or {}).get("section_ancestor_node_ids") or "").split("|")
        if item
    }
    matched_indexes: list[int] = []
    exact_indexes: list[int] = []
    node_paths: list[str] = []
    modes: list[str] = []
    matched_node_ids: list[str] = []

    def normalized_node_values(node: dict[str, Any]) -> tuple[str, ...]:
        values = []
        for key in ("title", "path_key"):
            value = re.sub(r"\s+", "", str(node.get(key) or ""))
            if value and value not in values:
                values.append(value)
        return tuple(values)

    for topic_index, topic in enumerate(topics):
        exact_ids = {
            node_id
            for node_id, node in tree_nodes.items()
            if topic in normalized_node_values(node)
        }
        partial_ids = {
            node_id
            for node_id, node in tree_nodes.items()
            if any(topic in value for value in normalized_node_values(node))
        }
        exact_ancestor = current_id in exact_ids or bool(current_ancestors & exact_ids)
        partial_match = current_id in partial_ids or bool(current_ancestors & partial_ids)
        if exact_ancestor:
            matched_indexes.append(topic_index)
            exact_indexes.append(topic_index)
            matched_ids = ({current_id} | current_ancestors) & exact_ids
            matched_id = max(
                matched_ids,
                key=lambda node_id: int(tree_nodes[node_id].get("depth") or 0),
            )
            matched_node_ids.append(matched_id)
            node_paths.append(str(tree_nodes[matched_id].get("path_key") or ""))
            modes.append("exact_node" if matched_id == current_id else "descendant")
        elif partial_match:
            matched_indexes.append(topic_index)
            matched_ids = ({current_id} | current_ancestors) & partial_ids
            matched_id = max(
                matched_ids,
                key=lambda node_id: int(tree_nodes[node_id].get("depth") or 0),
            )
            matched_node_ids.append(matched_id)
            node_paths.append(str(tree_nodes[matched_id].get("path_key") or ""))
            modes.append("partial_node")
        else:
            return None

    mode = (
        "exact_node"
        if all(item == "exact_node" for item in modes)
        else "descendant"
        if any(item in {"exact_node", "descendant"} for item in modes)
        else modes[0]
    )
    return {
        "topics": topics,
        "matched_topic_indexes": tuple(matched_indexes),
        "exact_topic_indexes": tuple(exact_indexes),
        "node_paths": tuple(node_paths),
        "coverage": len(matched_indexes) / max(len(topics), 1),
        "exact_coverage": len(exact_indexes) / max(len(topics), 1),
        "max_node_depth": max(
            (int(tree_nodes[node_id].get("depth") or 0) for node_id in matched_node_ids),
            default=0,
        ),
        "mode": mode,
        "source": "persisted_section_tree_v1",
    }


def _structure_match_score(
    query: str,
    intent: QueryIntent,
    candidate: SearchCandidate,
) -> float:
    metadata = candidate.metadata or {}
    content_kind = str(metadata.get("content_kind") or "").strip().lower()
    score = 0.0

    section_route = _section_route_match(query, metadata, candidate.content)
    if section_route["matched_topic_indexes"]:
        # Multi-topic questions should reward coverage, not just one lucky hit.
        score = max(score, float(section_route["coverage"]))

    if intent.clause_references or intent.name == "clause_lookup":
        if content_kind == "clause":
            score = max(score, 0.72)
        if metadata.get("clause_id") or metadata.get("clause_ids"):
            score = max(score, 0.58)

        clause_space = _normalize_clause_reference(
            " ".join(
                str(metadata.get(key) or "")
                for key in (
                    "clause_id",
                    "clause_ids",
                    "section",
                    "Header 1",
                    "Header 2",
                    "Header 3",
                    "Header 4",
                    "Header 5",
                    "Header 6",
                )
            )
            + " "
            + str(metadata.get("child_excerpt") or candidate.content[:500])
        )
        if any(reference and reference in clause_space for reference in intent.clause_references):
            # A quoted decimal section such as “2.1 RAG 范式” carries both a
            # section number and a title.  Do not let a different section
            # with the same number receive an exact structural match merely
            # because its normalized clause text contains “21”.
            decimal_section_query = bool(
                _DECIMAL_SECTION_PATTERN.search(query)
                or _NUMBERED_SECTION_PATTERN.search(query)
            )
            has_quoted_section_title = any(
                re.match(r"\d+(?:[.．]\d+){1,3}", topic)
                or re.match(r"\d+[.．、)]", topic)
                for topic in _quoted_section_topics(query)
            )
            if not (
                decimal_section_query
                and has_quoted_section_title
                and not section_route["matched_topic_indexes"]
            ):
                score = 1.0

    if intent.prefers_table:
        if content_kind == "table":
            score = max(score, 0.92)
        elif metadata.get("table_count") or metadata.get("tables_json"):
            score = max(score, 0.78)
        elif _looks_like_markdown_table(candidate.content):
            score = max(score, 0.64)

    if intent.prefers_visual_asset:
        if metadata.get("image_count") or metadata.get("image_metadata_json"):
            score = max(score, 0.78)
        elif "[VISUAL_ASSETS]" in candidate.content:
            score = max(score, 0.64)

    if intent.broad_scope:
        if metadata.get("section_path") or metadata.get("section"):
            score = max(score, 0.24)
        if metadata.get("structure_aware"):
            score = max(score, 0.18)

    if intent.name == "factual_lookup" and content_kind in {"clause", "table"}:
        query_tokens = set(tokenize(query))
        content_tokens = set(tokenize(str(metadata.get("child_excerpt") or candidate.content)))
        if query_tokens and len(query_tokens & content_tokens) >= 2:
            score = max(score, 0.12)

    return min(score, 1.0)


def _visual_asset_records(
    metadata: dict[str, Any] | None,
    *,
    limit: int = 8,
) -> list[dict[str, str]]:
    """Expose bounded, non-executable figure metadata as citation fields."""
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


def _looks_like_markdown_table(content: str) -> bool:
    lines = [line.strip() for line in str(content or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    return any(
        "|" in lines[index - 1]
        and re.fullmatch(r"\|?[\s:|-]+\|?", lines[index])
        for index in range(1, len(lines))
    )


def _select_diverse_sources(candidates: list[SearchCandidate], top_k: int) -> list[SearchCandidate]:
    if top_k <= 1:
        return candidates[:top_k]

    def candidate_source_key(candidate: SearchCandidate) -> str:
        return source_diversity_key(candidate.metadata or {}, candidate.id)

    selected: list[SearchCandidate] = []
    deferred: list[SearchCandidate] = []
    seen_sources: set[str] = set()

    for candidate in candidates:
        if candidate.score_breakdown.get("source", 0.0) < 0.99:
            continue
        source = candidate_source_key(candidate)
        if source in seen_sources:
            continue
        selected.append(candidate)
        seen_sources.add(source)
        if len(selected) >= top_k:
            return selected

    for candidate in candidates:
        if candidate in selected:
            continue
        source = candidate_source_key(candidate)
        if source not in seen_sources:
            selected.append(candidate)
            seen_sources.add(source)
        else:
            deferred.append(candidate)
        if len(selected) >= top_k:
            return selected

    return (selected + deferred)[:top_k]


# A blind sub-query has no full title to match.  This bounded lexical floor
# reserves only candidates with observable evidence overlap, while exact title
# matches still win through _source_group_match_score().
SOURCE_EVIDENCE_COVERAGE_FLOOR = 0.22
SOURCE_EVIDENCE_RELEVANCE_MARGIN = 0.12


def _source_group_match_score(
    candidate: SearchCandidate,
    query_group: list[str],
) -> float:
    return max(
        (_source_match_score(query, candidate.metadata or {}) for query in query_group),
        default=0.0,
    )


def _source_group_relevance_score(
    candidate: SearchCandidate,
    query_group: list[str],
) -> float:
    """Score a decomposed comparison side using source identity and evidence.

    Blind comparison questions intentionally omit full document titles.  A
    title-only reservation therefore cannot cover the second source even when
    its retrieved passage is clearly about that side of the question.  Keep
    exact source identity as the strongest signal, then use bounded lexical
    overlap over the source metadata and cited passage as a deterministic
    fallback.  This is a routing signal, not a relevance probability.
    """
    source_score = _source_group_match_score(candidate, query_group)
    metadata = candidate.metadata or {}
    evidence_text = " ".join(
        str(metadata.get(key) or "")
        for key in (
            "document_title",
            "source_filename",
            "document_number",
            "section",
            "section_path",
            "child_excerpt",
        )
    )
    evidence_text = f"{evidence_text} {str(candidate.content or '')[:1800]}"
    evidence_score = max(
        (lexical_rerank_score(query, evidence_text) for query in query_group),
        default=0.0,
    )
    return max(source_score, evidence_score)


def _select_source_covered_candidates(
    candidates: list[SearchCandidate],
    top_k: int,
    source_query_groups: list[list[str]],
) -> list[SearchCandidate]:
    """Reserve one distinct source for each sufficiently matched sub-query."""
    selected: list[SearchCandidate] = []
    selected_ids: set[int] = set()
    seen_sources: set[str] = set()

    for query_group in source_query_groups:
        scored_candidates = []
        for candidate in candidates:
            if id(candidate) in selected_ids:
                continue
            source = source_diversity_key(candidate.metadata or {}, candidate.id)
            if source in seen_sources:
                continue
            score = _source_group_relevance_score(candidate, query_group)
            if score < SOURCE_EVIDENCE_COVERAGE_FLOOR:
                continue
            scored_candidates.append((candidate, score))
        if not scored_candidates:
            continue

        best_score = max(score for _, score in scored_candidates)
        exact_candidates = [
            (candidate, score)
            for candidate, score in scored_candidates
            if score >= 0.99
        ]
        eligible_candidates = exact_candidates or [
            (candidate, score)
            for candidate, score in scored_candidates
            if score >= max(
                SOURCE_EVIDENCE_COVERAGE_FLOOR,
                best_score - SOURCE_EVIDENCE_RELEVANCE_MARGIN,
            )
        ]
        # Exact source identity remains authoritative.  For blind sub-queries
        # within a narrow lexical margin, keep the candidate with stronger
        # overall retrieval evidence instead of forcing the lexical winner.
        best, best_score = max(
            eligible_candidates,
            key=lambda item: (
                item[0].final_score,
                item[0].keyword_score,
                item[1],
            ),
        )
        selected.append(best)
        selected_ids.add(id(best))
        seen_sources.add(source_diversity_key(best.metadata or {}, best.id))
        if len(selected) >= top_k:
            return selected[:top_k]

    remaining = [candidate for candidate in candidates if id(candidate) not in selected_ids]
    if len(selected) < top_k:
        covered_remaining = [
            candidate
            for candidate in remaining
            if any(
                _source_group_relevance_score(candidate, query_group)
                >= SOURCE_EVIDENCE_COVERAGE_FLOOR
                for query_group in source_query_groups
            )
        ]
        selected.extend(
            _select_diverse_sources(covered_remaining, top_k=top_k - len(selected))
        )
    # Keep the reserved candidates first so parent expansion cannot consume all
    # slots with duplicate chunks from one side of a comparison.
    return selected[:top_k]


def _single_section_route_source(
    candidates: list[SearchCandidate],
    intent: QueryIntent,
) -> str:
    """Return a unique source selected by a high-confidence section route.

    A structural lookup without an explicit document title should remain
    source-cohesive when the section router identifies exactly one source.
    Ambiguous routes and multi-source questions deliberately return no source
    so ordinary hybrid ranking remains in control.
    """
    if intent.requires_source_diversity:
        return ""
    routed_sources = {
        source_diversity_key(candidate.metadata or {}, candidate.id)
        for candidate in candidates
        if str((candidate.metadata or {}).get("_section_route_mode") or "").strip()
        in {"exact_node", "heading_fallback"}
        and _bounded_score((candidate.metadata or {}).get("_section_route_coverage"))
        >= 0.99
        and source_diversity_key(candidate.metadata or {}, candidate.id)
    }
    return next(iter(routed_sources)) if len(routed_sources) == 1 else ""


def _select_ranked_evidence(
    query: str,
    candidates: list[SearchCandidate],
    top_k: int,
    diagnostics: dict[str, Any] | None = None,
    query_intent: QueryIntent | None = None,
    source_query_groups: list[list[str]] | None = None,
) -> list[SearchCandidate]:
    intent = query_intent or classify_query_intent(query)
    if top_k <= 0:
        _record_selection_diagnostics(diagnostics, intent, candidates, [], 0)
        return []

    ordered_candidates, redundant_count = _defer_redundant_candidates(candidates)
    section_route_source = _single_section_route_source(ordered_candidates, intent)
    if section_route_source:
        cohesive = [
            candidate
            for candidate in ordered_candidates
            if source_diversity_key(candidate.metadata or {}, candidate.id) == section_route_source
        ]
        cohesive.sort(
            key=lambda candidate: (
                _bounded_score((candidate.metadata or {}).get("_section_route_exact_coverage")),
                _bounded_score((candidate.metadata or {}).get("_section_route_coverage")),
                1
                if str((candidate.metadata or {}).get("_section_route_mode") or "")
                == "exact_node"
                else 0,
                candidate.final_score,
                candidate.keyword_score,
                str(candidate.id),
            ),
            reverse=True,
        )
        selected = cohesive[:top_k]
        if selected:
            _record_selection_diagnostics(
                diagnostics,
                intent,
                candidates,
                selected,
                redundant_count,
                source_query_groups,
                section_route_source=section_route_source,
            )
            return selected
    source_hints = _query_source_hints(query)
    if len(source_hints) == 1 and not intent.requires_source_diversity:
        exact_source = [
            candidate
            for candidate in ordered_candidates
            if candidate.score_breakdown.get("source", 0.0) >= 0.99
        ]
        if exact_source:
            # An explicit document title is a hard scope, not a ranking hint.
            # Filling the remaining slots with other documents contaminates
            # section-level questions and makes grounded generation unstable.
            selected = exact_source[:top_k]
            _record_selection_diagnostics(
                diagnostics,
                intent,
                candidates,
                selected,
                redundant_count,
                source_query_groups,
            )
            return selected

    if intent.requires_source_diversity:
        if source_query_groups:
            selected = _select_source_covered_candidates(
                ordered_candidates,
                top_k=top_k,
                source_query_groups=source_query_groups,
            )
        else:
            selected = _select_diverse_sources(ordered_candidates, top_k=top_k)
    else:
        selected = _select_with_source_cap(
            ordered_candidates,
            top_k=top_k,
            max_per_source=2,
        )

    _record_selection_diagnostics(
        diagnostics,
        intent,
        candidates,
        selected,
        redundant_count,
        source_query_groups,
    )
    return selected


def _defer_redundant_candidates(
    candidates: list[SearchCandidate],
    overlap_threshold: float = 0.42,
) -> tuple[list[SearchCandidate], int]:
    unique: list[SearchCandidate] = []
    deferred: list[SearchCandidate] = []
    for candidate in candidates:
        if any(
            _candidates_are_redundant(candidate, selected, overlap_threshold)
            for selected in unique
        ):
            deferred.append(candidate)
        else:
            unique.append(candidate)
    return [*unique, *deferred], len(deferred)


def _candidates_are_redundant(
    left: SearchCandidate,
    right: SearchCandidate,
    overlap_threshold: float,
) -> bool:
    left_metadata = left.metadata or {}
    right_metadata = right.metadata or {}
    left_child = str(left_metadata.get("child_id") or left.id)
    right_child = str(right_metadata.get("child_id") or right.id)
    if left_child == right_child:
        return True

    left_parent = str(left_metadata.get("parent_id") or "")
    right_parent = str(right_metadata.get("parent_id") or "")
    if not left_parent or left_parent != right_parent:
        return False

    left_range = _candidate_char_range(left_metadata)
    right_range = _candidate_char_range(right_metadata)
    if left_range and right_range:
        intersection = max(
            0,
            min(left_range[1], right_range[1]) - max(left_range[0], right_range[0]),
        )
        shorter_length = min(
            left_range[1] - left_range[0],
            right_range[1] - right_range[0],
        )
        if shorter_length > 0 and intersection / shorter_length >= overlap_threshold:
            return True

    left_text = str(left_metadata.get("child_excerpt") or left.content).strip()
    right_text = str(right_metadata.get("child_excerpt") or right.content).strip()
    if min(len(left_text), len(right_text)) < 40:
        return False
    if left_text in right_text or right_text in left_text:
        return True
    left_tokens = set(tokenize(left_text))
    right_tokens = set(tokenize(right_text))
    union = left_tokens | right_tokens
    return bool(union) and len(left_tokens & right_tokens) / len(union) >= 0.72


def _candidate_char_range(metadata: dict[str, Any]) -> tuple[int, int] | None:
    try:
        start = int(metadata.get("child_start_char"))
        end = int(metadata.get("child_end_char"))
    except (TypeError, ValueError):
        return None
    if start < 0 or end <= start:
        return None
    return start, end


def _record_selection_diagnostics(
    diagnostics: dict[str, Any] | None,
    intent: QueryIntent,
    candidates: list[SearchCandidate],
    selected: list[SearchCandidate],
    redundant_count: int,
    source_query_groups: list[list[str]] | None = None,
    section_route_source: str = "",
) -> None:
    if diagnostics is None:
        return
    content_kind_counts: dict[str, int] = {}
    sources = set()
    for candidate in selected:
        metadata = candidate.metadata or {}
        content_kind = str(metadata.get("content_kind") or "unknown")
        content_kind_counts[content_kind] = content_kind_counts.get(content_kind, 0) + 1
        source = source_diversity_key(metadata)
        if source:
            sources.add(source)

    diagnostics.update({
        "query_intent": intent.name,
        "source_mode": (
            "multi_source"
            if intent.requires_source_diversity
            else "explicit_source"
            if intent.explicit_source
            else "open"
        ),
        "clause_references": list(intent.clause_references),
        "prefers_table": intent.prefers_table,
        "prefers_visual_asset": intent.prefers_visual_asset,
        "broad_scope": intent.broad_scope,
        "input_candidate_count": len(candidates),
        "redundant_deferred_count": redundant_count,
        "selected_candidate_count": len(selected),
        "selected_source_count": len(sources),
        "selected_content_kind_counts": content_kind_counts,
        "source_coverage_required": len(source_query_groups or []),
        "source_coverage_satisfied": sum(
            any(
                _source_group_relevance_score(candidate, query_group)
                >= SOURCE_EVIDENCE_COVERAGE_FLOOR
                for candidate in selected
            )
            for query_group in source_query_groups or []
        ),
        "section_route_source_gate": bool(section_route_source),
        "section_route_source": section_route_source,
    })


def _select_with_source_cap(
    candidates: list[SearchCandidate],
    top_k: int,
    max_per_source: int,
) -> list[SearchCandidate]:
    selected: list[SearchCandidate] = []
    deferred: list[SearchCandidate] = []
    source_counts: dict[str, int] = {}

    for candidate in candidates:
        source = source_diversity_key(candidate.metadata or {}, candidate.id)
        if source_counts.get(source, 0) >= max_per_source:
            deferred.append(candidate)
            continue
        selected.append(candidate)
        source_counts[source] = source_counts.get(source, 0) + 1
        if len(selected) >= top_k:
            return selected

    return (selected + deferred)[:top_k]


def _fill_unselected(
    selected: list[SearchCandidate],
    candidates: list[SearchCandidate],
    top_k: int,
) -> list[SearchCandidate]:
    selected_ids = {id(candidate) for candidate in selected}
    filled = list(selected)
    for candidate in candidates:
        if id(candidate) in selected_ids:
            continue
        filled.append(candidate)
        if len(filled) >= top_k:
            break
    return filled[:top_k]
