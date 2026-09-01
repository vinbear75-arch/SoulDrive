import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.answer_quality import evaluate_evidence_gate
from core.retrieval import SearchCandidate, build_citations, rank_hybrid_candidates


@dataclass
class RetrievalEvalCase:
    query: str
    expected_ids: list[str]
    candidates: list[SearchCandidate]
    id: str = ""
    category: str = "unspecified"
    difficulty: str = "unspecified"
    expected_groups: list[list[str]] | None = None


def load_retrieval_eval_cases(path: str | Path) -> list[RetrievalEvalCase]:
    cases: list[RetrievalEvalCase] = []
    with open(path, "r", encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            payload = json.loads(line)
            cases.append(RetrievalEvalCase(
                query=payload["query"],
                expected_ids=list(payload["expected_ids"]),
                candidates=[
                    SearchCandidate(
                        id=item["id"],
                        content=item["content"],
                        metadata=item.get("metadata") or {},
                        dense_rank=item.get("dense_rank"),
                        sparse_rank=item.get("sparse_rank"),
                        graph_score=item.get("graph_score", 0.0),
                        vector_distance=item.get("vector_distance"),
                        keyword_score=item.get("keyword_score", 0.0),
                        rerank_score=item.get("rerank_score", 0.0),
                    )
                    for item in payload["candidates"]
                ],
                id=str(payload.get("id") or ""),
                category=str(payload.get("category") or "unspecified"),
                difficulty=str(payload.get("difficulty") or "unspecified"),
                expected_groups=[
                    [str(candidate_id) for candidate_id in group]
                    for group in payload.get("expected_groups", [])
                    if group
                ] or None,
            ))
    return cases


def evaluate_retrieval_cases(cases: list[RetrievalEvalCase], top_k: int = 3) -> dict[str, Any]:
    if not cases:
        return {"case_count": 0, "hit_rate_at_k": 0.0, "mean_reciprocal_rank": 0.0}

    hits = 0
    reciprocal_ranks = []
    all_hits = 0
    expected_recalls = []
    dense_hits = 0
    dense_all_hits = 0
    dense_expected_recalls = []
    dense_reciprocal_ranks = []
    accepted_cases = 0
    average_top_score = 0.0
    low_evidence_cases = []
    rows = []

    for case in cases:
        ranked = rank_hybrid_candidates(case.query, case.candidates, top_k=top_k)
        ranked_ids = [candidate.id for candidate in ranked]
        expected_groups = case.expected_groups or [
            [candidate_id]
            for candidate_id in case.expected_ids
        ]
        hybrid_score = _score_ranked_ids(ranked_ids, expected_groups)
        dense_ranked_ids = [
            candidate.id
            for candidate in sorted(
                case.candidates,
                key=lambda candidate: (
                    candidate.dense_rank
                    if candidate.dense_rank is not None
                    else 10**9
                ),
            )[:top_k]
        ]
        dense_score = _score_ranked_ids(dense_ranked_ids, expected_groups)
        evidence = build_citations(ranked, query=case.query)
        gate = evaluate_evidence_gate(evidence, query=case.query)
        if gate.allowed:
            accepted_cases += 1
        else:
            low_evidence_cases.append(case.query)
        if ranked:
            average_top_score += ranked[0].final_score

        if hybrid_score["any_hit"]:
            hits += 1
        reciprocal_ranks.append(hybrid_score["reciprocal_rank"])
        expected_recalls.append(hybrid_score["expected_recall"])
        if hybrid_score["all_hit"]:
            all_hits += 1
        if dense_score["any_hit"]:
            dense_hits += 1
        if dense_score["all_hit"]:
            dense_all_hits += 1
        dense_expected_recalls.append(dense_score["expected_recall"])
        dense_reciprocal_ranks.append(dense_score["reciprocal_rank"])
        rows.append({
            "id": case.id,
            "category": case.category,
            "difficulty": case.difficulty,
            **hybrid_score,
            "dense_any_hit": dense_score["any_hit"],
            "dense_all_hit": dense_score["all_hit"],
            "dense_reciprocal_rank": dense_score["reciprocal_rank"],
            "dense_expected_recall": dense_score["expected_recall"],
        })

    all_hit_rate = all_hits / len(cases)
    dense_all_hit_rate = dense_all_hits / len(cases)
    return {
        "case_count": len(cases),
        "hit_rate_at_k": round(hits / len(cases), 6),
        "all_hit_rate_at_k": round(all_hit_rate, 6),
        "mean_expected_recall_at_k": round(sum(expected_recalls) / len(cases), 6),
        "mean_reciprocal_rank": round(sum(reciprocal_ranks) / len(cases), 6),
        "dense_baseline_hit_rate_at_k": round(dense_hits / len(cases), 6),
        "dense_baseline_all_hit_rate_at_k": round(dense_all_hit_rate, 6),
        "dense_baseline_mean_expected_recall_at_k": round(
            sum(dense_expected_recalls) / len(cases),
            6,
        ),
        "dense_baseline_mean_reciprocal_rank": round(
            sum(dense_reciprocal_ranks) / len(cases),
            6,
        ),
        "hybrid_all_hit_lift": round(all_hit_rate - dense_all_hit_rate, 6),
        "evidence_acceptance_rate": round(accepted_cases / len(cases), 6),
        "average_top_score": round(average_top_score / len(cases), 6),
        "low_evidence_cases": low_evidence_cases,
        "confidence_intervals_95": {
            "hit_rate_at_k": wilson_interval(hits, len(cases)),
            "all_hit_rate_at_k": wilson_interval(all_hits, len(cases)),
            "dense_baseline_hit_rate_at_k": wilson_interval(
                dense_hits,
                len(cases),
            ),
            "dense_baseline_all_hit_rate_at_k": wilson_interval(
                dense_all_hits,
                len(cases),
            ),
        },
        "category_metrics": _stratified_metrics(rows, "category"),
        "difficulty_metrics": _stratified_metrics(rows, "difficulty"),
        "rows": rows,
    }


def wilson_interval(successes: int, total: int, z: float = 1.96) -> dict[str, float | int | None]:
    if total <= 0:
        return {"low": None, "high": None, "successes": successes, "total": total}
    proportion = successes / total
    denominator = 1 + (z * z / total)
    center = (
        proportion + (z * z / (2 * total))
    ) / denominator
    margin = (
        z
        * math.sqrt(
            (proportion * (1 - proportion) / total)
            + (z * z / (4 * total * total))
        )
        / denominator
    )
    return {
        "low": round(max(0.0, center - margin), 6),
        "high": round(min(1.0, center + margin), 6),
        "successes": successes,
        "total": total,
    }


def _score_ranked_ids(
    ranked_ids: list[str],
    expected_groups: list[list[str]],
) -> dict[str, Any]:
    expected = {
        candidate_id
        for group in expected_groups
        for candidate_id in group
    }
    first_hit_rank = next(
        (
            index
            for index, candidate_id in enumerate(ranked_ids, start=1)
            if candidate_id in expected
        ),
        None,
    )
    ranked_id_set = set(ranked_ids)
    hit_count = sum(
        bool(ranked_id_set & set(group))
        for group in expected_groups
    )
    expected_count = len(expected_groups)
    expected_recall = hit_count / expected_count if expected_count else 1.0
    return {
        "expected_count": expected_count,
        "hit_count": hit_count,
        "any_hit": first_hit_rank is not None,
        "all_hit": bool(expected_count) and hit_count == expected_count,
        "reciprocal_rank": (1 / first_hit_rank) if first_hit_rank else 0.0,
        "expected_recall": expected_recall,
    }


def _stratified_metrics(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get(key) or "unspecified"), []).append(row)

    return {
        name: {
            "case_count": len(group),
            "any_hit_rate_at_k": round(
                sum(bool(row["any_hit"]) for row in group) / len(group),
                6,
            ),
            "all_hit_rate_at_k": round(
                sum(bool(row["all_hit"]) for row in group) / len(group),
                6,
            ),
            "mean_expected_recall_at_k": round(
                sum(float(row["expected_recall"]) for row in group) / len(group),
                6,
            ),
            "mean_reciprocal_rank": round(
                sum(float(row["reciprocal_rank"]) for row in group) / len(group),
                6,
            ),
            "dense_baseline_all_hit_rate_at_k": round(
                sum(bool(row["dense_all_hit"]) for row in group) / len(group),
                6,
            ),
            "dense_baseline_mean_expected_recall_at_k": round(
                sum(float(row["dense_expected_recall"]) for row in group)
                / len(group),
                6,
            ),
            "all_hit_confidence_interval_95": wilson_interval(
                sum(bool(row["all_hit"]) for row in group),
                len(group),
            ),
        }
        for name, group in sorted(grouped.items())
    }
