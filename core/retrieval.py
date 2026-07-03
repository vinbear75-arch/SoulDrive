import math
import re
from dataclasses import dataclass, field
from typing import Any


TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


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


def tokenize(text: str) -> list[str]:
    return TOKEN_PATTERN.findall((text or "").lower())


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
) -> list[SearchCandidate]:
    if not candidates:
        return []

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
        candidate.rrf_score = rrf_scores.get(candidate.id, 0.0)
        candidate.rerank_score = combined_rerank
        candidate.final_score = (
            0.36 * candidate.rrf_score
            + 0.24 * candidate.rerank_score
            + 0.10 * graph_norm
            + 0.30 * source_score
        )
        candidate.score_breakdown = {
            "rrf": round(candidate.rrf_score, 6),
            "lexical_rerank": round(lexical_score, 6),
            "semantic_rerank": round(semantic_score, 6),
            "graph": round(graph_norm, 6),
            "keyword": round(candidate.keyword_score, 6),
            "source": round(source_score, 6),
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
    return _select_diverse_sources(ranked, top_k=top_k)


def build_citations(
    candidates: list[SearchCandidate],
    max_snippet_chars: int = 420,
) -> list[dict[str, Any]]:
    citations = []
    for index, candidate in enumerate(candidates, start=1):
        metadata = candidate.metadata or {}
        page = metadata.get("page_number") or metadata.get("page")
        section = metadata.get("Header 3") or metadata.get("Header 2") or metadata.get("Header 1")
        snippet_source = metadata.get("child_excerpt") or candidate.content
        snippet = re.sub(r"\s+", " ", str(snippet_source)).strip()
        if len(snippet) > max_snippet_chars:
            snippet = snippet[:max_snippet_chars] + "…"

        citations.append({
            "id": f"E{index}",
            "candidate_id": candidate.id,
            "source_filename": metadata.get("source_filename") or "未知文件",
            "page": page,
            "page_label": str(page) if page is not None else "未知",
            "chunk_index": metadata.get("chunk_index"),
            "section": section,
            "score": round(candidate.final_score, 6),
            "breakdown": candidate.score_breakdown,
            "snippet": snippet,
        })
    return citations


def _source_match_score(query: str, metadata: dict[str, Any]) -> float:
    source = str(
        metadata.get("source_filename")
        or metadata.get("title")
        or metadata.get("document_title")
        or metadata.get("source_path")
        or ""
    ).lower()
    if not source:
        return 0.0

    source_compact = _compact_source_text(source)
    query_compact = _compact_source_text(query)
    if not source_compact or not query_compact:
        return 0.0

    for title_hint in re.findall(r"《([^》]+)》", query or ""):
        hint_compact = _compact_source_text(title_hint)
        if hint_compact and (hint_compact in source_compact or source_compact in hint_compact):
            return 1.0

    if source_compact in query_compact:
        return 1.0

    query_tokens = set(tokenize(query))
    source_tokens = set(tokenize(source))
    if not query_tokens or not source_tokens:
        return 0.0

    return min(1.0, len(query_tokens & source_tokens) / max(min(len(query_tokens), len(source_tokens)), 1))


def _compact_source_text(value: str) -> str:
    lowered = (value or "").lower()
    for suffix in (".pdf", ".docx", ".md", ".html", ".txt"):
        if lowered.endswith(suffix):
            lowered = lowered[: -len(suffix)]
            break
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", lowered)


def _select_diverse_sources(candidates: list[SearchCandidate], top_k: int) -> list[SearchCandidate]:
    if top_k <= 1 or len(candidates) <= top_k:
        return candidates[:top_k]

    def source_key(candidate: SearchCandidate) -> str:
        return str((candidate.metadata or {}).get("source_filename") or candidate.id)

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
