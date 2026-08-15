import math
import re
from dataclasses import dataclass, field
from typing import Any

from core.keyword_index import lexical_tokens
from core.query_expansion import query_requires_source_diversity


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
    broad_scope: bool = False


_FORMAL_CLAUSE_PATTERN = re.compile(
    r"第[零〇一二三四五六七八九十百千万两\d]+"
    r"(?:[.．][零〇一二三四五六七八九十百千万两\d]+)*[章节条款项目]"
)
_DECIMAL_SECTION_PATTERN = re.compile(r"(?<![A-Za-z0-9.])\d+(?:[.．]\d+){1,3}(?![A-Za-z0-9.])")
_TABLE_QUERY_PATTERN = re.compile(
    r"表格|表中|附表|清单|名单|技术参数|配置参数|指标值|验收指标|验收标准|"
    r"金额|比例|数量|日期|期限|分别列出|逐项"
)
_BROAD_QUERY_PATTERN = re.compile(
    r"概述|总结|综述|主要内容|核心要点|总体要求|整体情况|全文|全部内容|"
    r"有哪些方面|归纳|提纲"
)
_CLAUSE_QUERY_PATTERN = re.compile(r"条款|条文|章节|哪一条|哪一款|法律依据|政策依据")
_DOCUMENT_NUMBER_QUERY_PATTERN = re.compile(
    r"(?:[\u4e00-\u9fffA-Za-z]{1,20}[〔\[]\d{4}[〕\]]\s*\d+\s*号|"
    r"(?:GB(?:/T)?|DB\d*/T|T/[A-Za-z0-9_-]+|Q/[A-Za-z0-9_-]+)"
    r"\s*[A-Za-z0-9./_-]+(?:-\d{4})?)",
    flags=re.IGNORECASE,
)


def tokenize(text: str) -> list[str]:
    return lexical_tokens(text)


def classify_query_intent(query: str) -> QueryIntent:
    normalized = str(query or "").strip()
    requires_diversity = query_requires_source_diversity(normalized)
    explicit_source = bool(_query_source_hints(normalized))
    clause_references = list(_FORMAL_CLAUSE_PATTERN.findall(normalized))
    if _CLAUSE_QUERY_PATTERN.search(normalized) or _DECIMAL_SECTION_PATTERN.match(normalized):
        clause_references.extend(_DECIMAL_SECTION_PATTERN.findall(normalized))
    clause_references = list(dict.fromkeys(_normalize_clause_reference(item) for item in clause_references))
    prefers_table = bool(_TABLE_QUERY_PATTERN.search(normalized))
    broad_scope = bool(_BROAD_QUERY_PATTERN.search(normalized))

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
        broad_scope=broad_scope,
    )


def _query_source_hints(query: str) -> list[str]:
    hints = [
        hint.strip()
        for hint in re.findall(r"《([^》]+)》", query or "")
        if hint.strip()
    ]
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
        key=lambda item: item.score,
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

    query_intent = classify_query_intent(query)
    keyword_ranked = bm25_rank(query, candidates)
    for rank, ranked_candidate in enumerate(keyword_ranked, start=1):
        candidate = ranked_candidate.candidate
        candidate.keyword_score = ranked_candidate.score
        candidate.sparse_rank = rank if ranked_candidate.score > 0 else None

    dense_ranking = [
        candidate.id
        for candidate in sorted(
            candidates,
            key=lambda item: item.dense_rank if item.dense_rank is not None else 10**9,
        )
        if candidate.dense_rank is not None
    ]
    sparse_ranking = [
        ranked_candidate.candidate.id
        for ranked_candidate in keyword_ranked
        if ranked_candidate.score > 0
    ]
    source_scores = {
        candidate.id: _source_match_score(query, candidate.metadata)
        for candidate in candidates
    }
    source_ranking = [
        candidate.id
        for candidate in sorted(
            candidates,
            key=lambda item: source_scores.get(item.id, 0.0),
            reverse=True,
        )
        if source_scores.get(candidate.id, 0.0) >= 0.55
    ]
    rrf_scores = reciprocal_rank_fusion([dense_ranking, sparse_ranking, source_ranking])
    max_graph_score = max((candidate.graph_score for candidate in candidates), default=0.0)

    for candidate in candidates:
        graph_norm = candidate.graph_score / max_graph_score if max_graph_score > 0 else 0.0
        lexical_score = lexical_rerank_score(query, candidate.content)
        semantic_score = max(candidate.rerank_score, 0.0)
        combined_rerank = max(lexical_score, semantic_score)
        source_score = source_scores.get(candidate.id, 0.0)
        structure_score = _structure_match_score(query, query_intent, candidate)
        candidate.rrf_score = rrf_scores.get(candidate.id, 0.0)
        candidate.rerank_score = combined_rerank
        candidate.final_score = (
            0.36 * candidate.rrf_score
            + 0.24 * candidate.rerank_score
            + 0.10 * graph_norm
            + 0.30 * source_score
            + 0.14 * structure_score
        )
        candidate.score_breakdown = {
            "rrf": round(candidate.rrf_score, 6),
            "lexical_rerank": round(lexical_score, 6),
            "semantic_rerank": round(semantic_score, 6),
            "graph": round(graph_norm, 6),
            "keyword": round(candidate.keyword_score, 6),
            "source": round(source_score, 6),
            "structure": round(structure_score, 6),
            "final": round(candidate.final_score, 6),
        }

    ranked = sorted(
        candidates,
        key=lambda candidate: (
            candidate.final_score,
            candidate.keyword_score,
            -1 * (candidate.vector_distance or 0.0),
        ),
        reverse=True,
    )
    return _select_ranked_evidence(
        query,
        ranked,
        top_k=top_k,
        diagnostics=diagnostics,
        query_intent=query_intent,
    )


def build_citations(
    candidates: list[SearchCandidate],
    max_snippet_chars: int = 420,
) -> list[dict[str, Any]]:
    citations = []
    for index, candidate in enumerate(candidates, start=1):
        metadata = candidate.metadata or {}
        page = metadata.get("page_number") or metadata.get("page")
        section = (
            metadata.get("section")
            or metadata.get("Header 3")
            or metadata.get("Header 2")
            or metadata.get("Header 1")
        )
        snippet_source = metadata.get("child_excerpt") or candidate.content
        snippet = re.sub(r"\s+", " ", str(snippet_source)).strip()
        if len(snippet) > max_snippet_chars:
            snippet = snippet[:max_snippet_chars] + "…"

        citations.append({
            "id": f"E{index}",
            "candidate_id": candidate.id,
            "source_filename": source_identity(metadata, "未知文件"),
            "document_title": metadata.get("document_title"),
            "document_number": metadata.get("document_number"),
            "page": page,
            "page_label": str(page) if page is not None else "未知",
            "chunk_index": metadata.get("chunk_index"),
            "section": section,
            "clause_id": metadata.get("clause_id"),
            "content_kind": metadata.get("content_kind"),
            "score": round(candidate.final_score, 6),
            "breakdown": candidate.score_breakdown,
            "snippet": snippet,
        })
    return citations


def source_identity(metadata: dict[str, Any], fallback: str = "") -> str:
    return str(
        metadata.get("source_filename")
        or metadata.get("document_title")
        or metadata.get("document_number")
        or fallback
    )


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

    query_compact = _compact_source_text(query)
    if not query_compact:
        return 0.0

    for title_hint in re.findall(r"《([^》]+)》", query or ""):
        hint_compact = _compact_source_text(title_hint)
        for source in sources:
            source_compact = _compact_source_text(source)
            if hint_compact and (hint_compact in source_compact or source_compact in hint_compact):
                return 1.0

    best_score = 0.0
    query_tokens = set(tokenize(query))
    for source in sources:
        source_compact = _compact_source_text(source)
        if not source_compact:
            continue
        if source_compact in query_compact:
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


def _compact_source_text(value: str) -> str:
    lowered = (value or "").lower()
    for suffix in (".pdf", ".docx", ".md", ".html", ".txt"):
        if lowered.endswith(suffix):
            lowered = lowered[: -len(suffix)]
            break
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", lowered)


def _normalize_clause_reference(value: str) -> str:
    return re.sub(r"[\s.．、（）()]+", "", str(value or "")).lower()


def _structure_match_score(
    query: str,
    intent: QueryIntent,
    candidate: SearchCandidate,
) -> float:
    metadata = candidate.metadata or {}
    content_kind = str(metadata.get("content_kind") or "").strip().lower()
    score = 0.0

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
                )
            )
            + " "
            + str(metadata.get("child_excerpt") or candidate.content[:500])
        )
        if any(reference and reference in clause_space for reference in intent.clause_references):
            score = 1.0

    if intent.prefers_table:
        if content_kind == "table":
            score = max(score, 0.92)
        elif metadata.get("table_count") or metadata.get("tables_json"):
            score = max(score, 0.78)
        elif _looks_like_markdown_table(candidate.content):
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
    if top_k <= 1 or len(candidates) <= top_k:
        return candidates[:top_k]

    def source_key(candidate: SearchCandidate) -> str:
        return source_identity(candidate.metadata or {}, candidate.id)

    selected: list[SearchCandidate] = []
    deferred: list[SearchCandidate] = []
    seen_sources: set[str] = set()

    for candidate in candidates:
        if candidate.score_breakdown.get("source", 0.0) < 0.99:
            continue
        source = source_key(candidate)
        if source in seen_sources:
            continue
        selected.append(candidate)
        seen_sources.add(source)
        if len(selected) >= top_k:
            return selected

    for candidate in candidates:
        if candidate in selected:
            continue
        source = source_key(candidate)
        if source not in seen_sources:
            selected.append(candidate)
            seen_sources.add(source)
        else:
            deferred.append(candidate)
        if len(selected) >= top_k:
            return selected

    return (selected + deferred)[:top_k]


def _select_ranked_evidence(
    query: str,
    candidates: list[SearchCandidate],
    top_k: int,
    diagnostics: dict[str, Any] | None = None,
    query_intent: QueryIntent | None = None,
) -> list[SearchCandidate]:
    intent = query_intent or classify_query_intent(query)
    if top_k <= 0:
        _record_selection_diagnostics(diagnostics, intent, candidates, [], 0)
        return []

    ordered_candidates, redundant_count = _defer_redundant_candidates(candidates)
    source_hints = _query_source_hints(query)
    if len(source_hints) == 1 and not intent.requires_source_diversity:
        exact_source = [
            candidate
            for candidate in ordered_candidates
            if candidate.score_breakdown.get("source", 0.0) >= 0.99
        ]
        if exact_source:
            selected = exact_source[:top_k]
            if len(selected) < top_k:
                selected = _fill_unselected(selected, ordered_candidates, top_k)
            _record_selection_diagnostics(
                diagnostics,
                intent,
                candidates,
                selected,
                redundant_count,
            )
            return selected

    if intent.requires_source_diversity:
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
) -> None:
    if diagnostics is None:
        return
    content_kind_counts: dict[str, int] = {}
    sources = set()
    for candidate in selected:
        metadata = candidate.metadata or {}
        content_kind = str(metadata.get("content_kind") or "unknown")
        content_kind_counts[content_kind] = content_kind_counts.get(content_kind, 0) + 1
        source = source_identity(metadata)
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
        "broad_scope": intent.broad_scope,
        "input_candidate_count": len(candidates),
        "redundant_deferred_count": redundant_count,
        "selected_candidate_count": len(selected),
        "selected_source_count": len(sources),
        "selected_content_kind_counts": content_kind_counts,
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
        source = source_identity(candidate.metadata or {}, candidate.id)
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
