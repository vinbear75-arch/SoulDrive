import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.query_expansion import expand_query_variants
from core.rag_defaults import DEFAULT_RAG_TOP_K
from core.retrieval import SearchCandidate, rank_hybrid_candidates
from core.workspace import SoulDriveWorkspace
from scripts import eval_rag


DEFAULT_DATASET = PROJECT_ROOT / "eval" / "retrieval_quality_blind_v0.3.jsonl"
DEFAULT_OUTPUT = PROJECT_ROOT / "eval" / "results" / "rag_pipeline_diagnostics_v0.3.json"
STAGE_ORDER = [
    "dense",
    "keyword",
    "source_title",
    "merged",
    "graph_boosted",
    "reranked_pool",
    "final",
]


def configure_utf8_stdio() -> None:
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose SoulDrive RAG retrieval pipeline stage by stage.")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET), help="Evaluation dataset JSONL path.")
    parser.add_argument("--workspace", default=None, help="SoulDrive workspace root.")
    parser.add_argument("--passphrase", default=None, help="Workspace passphrase for encrypted local indexes.")
    parser.add_argument("--top-k", type=int, default=DEFAULT_RAG_TOP_K, help="Final evidence depth.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="JSON report output path.")
    parser.add_argument("--compare", nargs=2, metavar=("BASELINE_JSON", "CANDIDATE_JSON"), help="Compare two saved diagnostics reports.")
    parser.add_argument("--baseline-label", default="baseline", help="Label for the first --compare report.")
    parser.add_argument("--candidate-label", default="candidate", help="Label for the second --compare report.")
    parser.add_argument("--include-graph", action="store_true", help="Open the workspace graph DB and diagnose graph boosting.")
    parser.add_argument("--recall-depths", default="18,30,60,100", help="Comma-separated dense/keyword pool depths to scan.")
    parser.add_argument("--final-k-values", default="3,5,6", help="Comma-separated final evidence K values to scan.")
    parser.add_argument("--max-samples", type=int, default=None, help="Optional smoke-test limit.")
    parser.add_argument("--stage-top-limit", type=int, default=10, help="Candidates stored per stage in sample details.")
    parser.add_argument("--limit", type=int, default=10, help="Miss samples to print in the terminal summary.")
    args = parser.parse_args(argv)

    if args.compare:
        baseline = load_report_json(Path(args.compare[0]))
        candidate = load_report_json(Path(args.compare[1]))
        comparison = compare_reports(baseline, candidate)
        print_comparison_report(comparison, args.baseline_label, args.candidate_label)
        return 0

    dataset_path = Path(args.dataset)
    workspace = eval_rag.resolve_workspace_arg(args.workspace)
    if not workspace:
        parser.error("live diagnostics require --workspace <path> or an unlocked SoulDrive workspace.")
    eval_rag.prepare_workspace_keys(workspace, args.passphrase)
    if eval_rag.workspace_requires_key(workspace) and eval_rag.get_workspace_keys(workspace) is None:
        parser.error("encrypted workspace requires --passphrase <workspace password>.")

    records = load_dataset_payloads(dataset_path, max_samples=args.max_samples)
    if not records:
        parser.error(f"dataset has no records: {dataset_path}")
    recall_depths = parse_int_list(args.recall_depths)
    final_k_values = parse_int_list(args.final_k_values)

    print(f"诊断工作区: {workspace}")
    print(f"诊断样本数: {len(records)}")
    print(f"图谱阶段: {'启用' if args.include_graph else '未启用，graph_boosted 仅记录为合并候选池'}")
    print(f"召回深度扫描: {recall_depths or '关闭'}")
    print(f"Final K 扫描: {final_k_values or '关闭'}")
    samples = diagnose_records(
        records,
        workspace_path=Path(workspace),
        top_k=args.top_k,
        include_graph=args.include_graph,
        recall_depths=recall_depths,
        final_k_values=final_k_values,
        stage_top_limit=args.stage_top_limit,
    )
    report = build_report(
        dataset_path=dataset_path,
        workspace_path=Path(workspace),
        top_k=args.top_k,
        samples=samples,
        stage_order=STAGE_ORDER,
        graph_enabled=args.include_graph,
    )
    output_path = Path(args.output)
    save_report_json(report, output_path)
    print_human_report(report, limit=args.limit)
    print(f"\n诊断报告已保存: {output_path}")
    return 0


def load_dataset_payloads(path: Path, max_samples: int | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        payload["_line_number"] = line_number
        records.append(payload)
        if max_samples is not None and len(records) >= max_samples:
            break
    return records


def diagnose_records(
    records: list[dict[str, Any]],
    workspace_path: Path,
    top_k: int,
    include_graph: bool,
    recall_depths: list[int] | None = None,
    final_k_values: list[int] | None = None,
    stage_top_limit: int = 10,
) -> list[dict[str, Any]]:
    from core.graph_db import LocalGraphDB
    from core.knowledge_base import LocalKnowledgeBase

    workspace = SoulDriveWorkspace(str(workspace_path)).ensure()
    kb = LocalKnowledgeBase(
        workspace_path=workspace.root_path,
        enable_usage_ledger=False,
    )
    graph_db = LocalGraphDB(db_path=workspace.graph_db_path) if include_graph else None
    samples: list[dict[str, Any]] = []
    try:
        for index, record in enumerate(records, start=1):
            record_id = str(record.get("id") or f"line-{record.get('_line_number', index)}")
            print(f"[{index}/{len(records)}] 诊断检索链路: {record_id}", flush=True)
            samples.append(
                diagnose_record(
                    kb=kb,
                    record=record,
                    top_k=top_k,
                    graph_db=graph_db,
                    recall_depths=recall_depths or [],
                    final_k_values=final_k_values or [],
                    stage_top_limit=stage_top_limit,
                )
            )
    finally:
        if graph_db is not None:
            graph_db.close()
        kb.close()
    return samples


def hydrate_stage_candidates(kb: Any, candidates: list[SearchCandidate], limit: int) -> list[SearchCandidate]:
    """Apply the same secure-ID hydration used by production retrieval."""
    return kb._hydrate_secure_candidates(candidates, limit)


def diagnose_record(
    kb: Any,
    record: dict[str, Any],
    top_k: int,
    graph_db: Any | None = None,
    recall_depths: list[int] | None = None,
    final_k_values: list[int] | None = None,
    stage_top_limit: int = 10,
) -> dict[str, Any]:
    started_at = time.time()
    question = str(record.get("question") or "")
    expected_sources = [str(item) for item in record.get("expected_sources", [])]
    expected_document_ids = [
        str(item)
        for item in (record.get("doc_ids") or record.get("expected_document_ids") or [])
    ]
    if expected_document_ids and len(expected_document_ids) != len(expected_sources):
        raise ValueError(f"评测样本 {record.get('id', '')} 的 doc_ids 数量必须与 expected_sources 一致")
    dense_pool_size = max(top_k * 6, 18)
    recall_depths = sorted({depth for depth in (recall_depths or []) if depth > 0})
    final_k_values = sorted({value for value in (final_k_values or []) if value > 0})
    max_recall_depth = max(recall_depths) if recall_depths else dense_pool_size
    query_variants = expand_query_variants(question)
    embedding_model = kb._ensure_embedding_model()

    dense_candidate_groups: list[list[SearchCandidate]] = []
    keyword_candidate_groups: list[list[SearchCandidate]] = []
    for variant in query_variants:
        query_vector = embedding_model.encode(
            [kb._query_embedding_text(variant)],
            normalize_embeddings=True,
        ).tolist()
        dense_candidate_groups.append(kb._dense_candidates(query_vector, max_recall_depth))
        keyword_candidate_groups.append(kb._keyword_candidates(variant, max_recall_depth))

    dense_candidates = flatten_candidate_groups(dense_candidate_groups, dense_pool_size)
    keyword_candidates = flatten_candidate_groups(keyword_candidate_groups, dense_pool_size)
    # Secure recall intentionally returns IDs first.  Hydrate before taking
    # stage snapshots, otherwise source-level evaluation sees empty metadata.
    hydrate_limit = max(top_k * 40, max_recall_depth)
    dense_candidates = hydrate_stage_candidates(kb, dense_candidates, hydrate_limit)
    keyword_candidates = hydrate_stage_candidates(kb, keyword_candidates, hydrate_limit)
    source_title_candidates = kb._source_title_candidates(
        question,
        limit=max(top_k * 4, 12),
        query_variants=query_variants,
    )
    merged_candidates = kb._merge_candidates(dense_candidates, keyword_candidates, source_title_candidates)
    stages = {
        "dense": build_stage_result(
            expected_sources,
            dense_candidates,
            top_limit=stage_top_limit,
            expected_document_ids=expected_document_ids,
        ),
        "keyword": build_stage_result(
            expected_sources,
            keyword_candidates,
            top_limit=stage_top_limit,
            expected_document_ids=expected_document_ids,
        ),
        "source_title": build_stage_result(
            expected_sources,
            source_title_candidates,
            top_limit=stage_top_limit,
            expected_document_ids=expected_document_ids,
        ),
        "merged": build_stage_result(
            expected_sources,
            merged_candidates,
            top_limit=stage_top_limit,
            expected_document_ids=expected_document_ids,
        ),
    }

    graph_context, matched_entities = kb._build_graph_context(
        query=question,
        docs=[candidate.content for candidate in dense_candidates[:2]],
        graph_db=graph_db,
    )

    if matched_entities:
        for candidate in merged_candidates:
            candidate.graph_score = sum(
                1 for entity in matched_entities if entity.lower() in candidate.content.lower()
            )

    graph_boosted_candidates = list(merged_candidates)
    stages["graph_boosted"] = build_stage_result(
        expected_sources,
        graph_boosted_candidates,
        top_limit=stage_top_limit,
        expected_document_ids=expected_document_ids,
    )
    if merged_candidates and getattr(kb.reranker, "ready", False):
        scores = kb.reranker.score(question, [candidate.content for candidate in merged_candidates])
        for candidate, score in zip(merged_candidates, scores):
            candidate.rerank_score = float(score)

    reranked_candidates = rank_hybrid_candidates(
        question,
        clone_candidates(merged_candidates),
        top_k=max(top_k * 2, top_k),
        query_variants=query_variants,
    )
    final_candidates = kb._expand_parent_context(reranked_candidates, top_k=top_k)
    stages["reranked_pool"] = build_stage_result(
        expected_sources,
        reranked_candidates,
        top_limit=stage_top_limit,
        expected_document_ids=expected_document_ids,
    )
    stages["final"] = build_stage_result(
        expected_sources,
        final_candidates,
        top_limit=stage_top_limit,
        expected_document_ids=expected_document_ids,
    )
    sample = {
        "id": str(record.get("id") or ""),
        "question": question,
        "type": str(record.get("type") or ""),
        "expected_sources": expected_sources,
        "expected_document_ids": expected_document_ids,
        "document_identity_required": bool(expected_document_ids),
        "requires_multi_doc": bool(record.get("requires_multi_doc", False)),
        "graph_relevant": bool(record.get("graph_relevant", False)),
        "allows_title_reference": allows_title_reference(record),
        "query_variants": query_variants,
        "matched_entities": matched_entities,
        "graph_context_count": len(graph_context),
        "stages": stages,
        "elapsed_ms": int((time.time() - started_at) * 1000),
    }
    if recall_depths:
        sample["recall_depth_scan"] = {
            "dense": build_stage_depth_scan(
                expected_sources,
                dense_candidate_groups,
                recall_depths,
                expected_document_ids=expected_document_ids,
            ),
            "keyword": build_stage_depth_scan(
                expected_sources,
                keyword_candidate_groups,
                recall_depths,
                expected_document_ids=expected_document_ids,
            ),
            "merged_pool": build_merged_pool_depth_scan(
                kb,
                expected_sources,
                dense_candidate_groups,
                keyword_candidate_groups,
                source_title_candidates,
                recall_depths,
                expected_document_ids=expected_document_ids,
            ),
        }
    if final_k_values:
        sample["final_k_scan"] = build_final_k_scan(
            kb=kb,
            question=question,
            expected_sources=expected_sources,
            merged_candidates=merged_candidates,
            final_k_values=final_k_values,
            stage_top_limit=stage_top_limit,
            expected_document_ids=expected_document_ids,
        )
    sample["failure_buckets"] = classify_failure_buckets(sample)
    return sample


def flatten_candidate_groups(candidate_groups: list[list[Any]], depth: int) -> list[Any]:
    candidates: list[Any] = []
    for group in candidate_groups:
        candidates.extend(group[:depth])
    return candidates


def build_stage_depth_scan(
    expected_sources: list[str],
    candidate_groups: list[list[Any]],
    depths: list[int],
    expected_document_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        str(depth): compact_stage_result(
            build_stage_result(
                expected_sources,
                flatten_candidate_groups(candidate_groups, depth),
                expected_document_ids=expected_document_ids,
            )
        )
        for depth in depths
    }


def build_merged_pool_depth_scan(
    kb: Any,
    expected_sources: list[str],
    dense_candidate_groups: list[list[Any]],
    keyword_candidate_groups: list[list[Any]],
    source_title_candidates: list[Any],
    depths: list[int],
    expected_document_ids: list[str] | None = None,
) -> dict[str, Any]:
    scan = {}
    for depth in depths:
        merged_candidates = kb._merge_candidates(
            clone_candidates(flatten_candidate_groups(dense_candidate_groups, depth)),
            clone_candidates(flatten_candidate_groups(keyword_candidate_groups, depth)),
            clone_candidates(source_title_candidates),
        )
        scan[str(depth)] = compact_stage_result(
            build_stage_result(
                expected_sources,
                merged_candidates,
                expected_document_ids=expected_document_ids,
            )
        )
    return scan


def build_final_k_scan(
    kb: Any,
    question: str,
    expected_sources: list[str],
    merged_candidates: list[SearchCandidate],
    final_k_values: list[int],
    stage_top_limit: int = 10,
    expected_document_ids: list[str] | None = None,
) -> dict[str, Any]:
    scan = {}
    for final_k in final_k_values:
        reranked_candidates = rank_hybrid_candidates(
            question,
            clone_candidates(merged_candidates),
            top_k=max(final_k * 2, final_k),
            query_variants=expand_query_variants(question),
        )
        final_candidates = kb._expand_parent_context(reranked_candidates, top_k=final_k)
        scan[str(final_k)] = compact_stage_result(
            build_stage_result(
                expected_sources,
                final_candidates,
                top_limit=stage_top_limit,
                expected_document_ids=expected_document_ids,
            )
        )
    return scan


def build_stage_result(
    expected_sources: list[str],
    candidates: list[Any],
    top_limit: int = 10,
    expected_document_ids: list[str] | None = None,
) -> dict[str, Any]:
    expected_document_ids = [str(item) for item in (expected_document_ids or [])]
    if expected_document_ids and len(expected_document_ids) != len(expected_sources):
        raise ValueError("doc_ids 数量必须与 expected_sources 一致")
    candidate_snapshots = [candidate_snapshot(candidate) for candidate in candidates]
    hit_sources = [
        expected
        for index, expected in enumerate(expected_sources)
        if any(
            eval_rag.evidence_matches_expected_source(
                candidate,
                expected,
                expected_document_ids[index] if expected_document_ids else None,
            )
            for candidate in candidate_snapshots
        )
    ]
    missing_sources = [source for source in expected_sources if source not in hit_sources]
    source_coverage = len(hit_sources) / len(expected_sources) if expected_sources else 0.0
    return {
        "candidate_count": len(candidate_snapshots),
        "expected_document_ids": expected_document_ids,
        "document_identity_required": bool(expected_document_ids),
        "top_sources": [
            candidate.get("source_filename") or candidate.get("candidate_id") or ""
            for candidate in candidate_snapshots[:top_limit]
        ],
        "top_document_ids": [candidate.get("document_id", "") for candidate in candidate_snapshots[:top_limit]],
        "top_candidates": candidate_snapshots[:top_limit],
        "hit_sources": hit_sources,
        "missing_sources": missing_sources,
        "any_hit": bool(expected_sources) and bool(hit_sources),
        "all_hit": bool(expected_sources) and len(hit_sources) == len(expected_sources),
        "source_coverage": source_coverage,
    }


def candidate_snapshot(candidate: Any) -> dict[str, Any]:
    if isinstance(candidate, dict):
        metadata = dict(candidate.get("metadata") or {})
        source_filename = (
            candidate.get("source_filename")
            or metadata.get("source_filename")
            or metadata.get("title")
            or metadata.get("document_title")
            or metadata.get("source_path")
            or ""
        )
        identity = {
            key: str(candidate.get(key) or metadata.get(key) or "")
            for key in (
                "document_id",
                "evaluation_doc_id",
                "corpus_doc_id",
                "gold_document_id",
            )
        }
        return {
            "candidate_id": str(candidate.get("candidate_id") or candidate.get("id") or ""),
            "source_filename": str(source_filename),
            **identity,
            "parent_id": str(candidate.get("parent_id") or metadata.get("parent_id") or ""),
            "dense_rank": candidate.get("dense_rank"),
            "sparse_rank": candidate.get("sparse_rank"),
            "vector_distance": candidate.get("vector_distance"),
            "keyword_score": candidate.get("keyword_score", 0.0),
            "graph_score": candidate.get("graph_score", 0.0),
            "rerank_score": candidate.get("rerank_score", 0.0),
            "final_score": candidate.get("final_score", 0.0),
            "score_breakdown": dict(candidate.get("score_breakdown") or {}),
        }

    metadata = dict(getattr(candidate, "metadata", {}) or {})
    source_filename = (
        metadata.get("source_filename")
        or metadata.get("title")
        or metadata.get("document_title")
        or metadata.get("source_path")
        or ""
    )
    identity = {
        key: str(metadata.get(key) or "")
        for key in (
            "document_id",
            "evaluation_doc_id",
            "corpus_doc_id",
            "gold_document_id",
        )
    }
    return {
        "candidate_id": str(getattr(candidate, "id", "")),
        "source_filename": str(source_filename),
        **identity,
        "parent_id": str(metadata.get("parent_id") or ""),
        "dense_rank": getattr(candidate, "dense_rank", None),
        "sparse_rank": getattr(candidate, "sparse_rank", None),
        "vector_distance": getattr(candidate, "vector_distance", None),
        "keyword_score": getattr(candidate, "keyword_score", 0.0),
        "graph_score": getattr(candidate, "graph_score", 0.0),
        "rerank_score": getattr(candidate, "rerank_score", 0.0),
        "final_score": getattr(candidate, "final_score", 0.0),
        "score_breakdown": dict(getattr(candidate, "score_breakdown", {}) or {}),
    }


def clone_candidates(candidates: list[Any]) -> list[SearchCandidate]:
    cloned: list[SearchCandidate] = []
    for candidate in candidates:
        if isinstance(candidate, SearchCandidate):
            cloned.append(
                SearchCandidate(
                    id=candidate.id,
                    content=candidate.content,
                    metadata=dict(candidate.metadata or {}),
                    dense_rank=candidate.dense_rank,
                    sparse_rank=candidate.sparse_rank,
                    graph_score=candidate.graph_score,
                    vector_distance=candidate.vector_distance,
                    keyword_score=candidate.keyword_score,
                    rrf_score=candidate.rrf_score,
                    rerank_score=candidate.rerank_score,
                    final_score=candidate.final_score,
                    score_breakdown=dict(candidate.score_breakdown or {}),
                )
            )
            continue

        snapshot = candidate_snapshot(candidate)
        metadata = {
            "source_filename": snapshot["source_filename"],
            "parent_id": snapshot["parent_id"],
        }
        for key in ("document_id", "evaluation_doc_id", "corpus_doc_id", "gold_document_id"):
            if snapshot.get(key):
                metadata[key] = snapshot[key]
        cloned.append(
            SearchCandidate(
                id=snapshot["candidate_id"],
                content=str(candidate.get("content") or "") if isinstance(candidate, dict) else "",
                metadata=metadata,
                dense_rank=snapshot["dense_rank"],
                sparse_rank=snapshot["sparse_rank"],
                graph_score=float(snapshot["graph_score"] or 0.0),
                vector_distance=snapshot["vector_distance"],
                keyword_score=float(snapshot["keyword_score"] or 0.0),
                rerank_score=float(snapshot["rerank_score"] or 0.0),
                final_score=float(snapshot["final_score"] or 0.0),
                score_breakdown=dict(snapshot["score_breakdown"] or {}),
            )
        )
    return cloned


def compact_stage_result(stage_result: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_count": stage_result["candidate_count"],
        "expected_document_ids": stage_result.get("expected_document_ids", []),
        "document_identity_required": stage_result.get("document_identity_required", False),
        "top_sources": stage_result["top_sources"],
        "top_document_ids": stage_result.get("top_document_ids", []),
        "hit_sources": stage_result["hit_sources"],
        "missing_sources": stage_result["missing_sources"],
        "any_hit": stage_result["any_hit"],
        "all_hit": stage_result["all_hit"],
        "source_coverage": stage_result["source_coverage"],
    }


def classify_failure_buckets(sample: dict[str, Any]) -> list[str]:
    expected_sources = sample.get("expected_sources") or []
    if not expected_sources:
        return ["no_expected_source"]

    stages = sample.get("stages") or {}
    dense = stage_or_empty(stages, "dense")
    keyword = stage_or_empty(stages, "keyword")
    source_title = stage_or_empty(stages, "source_title")
    merged = stage_or_empty(stages, "merged")
    reranked_pool = stage_or_empty(stages, "reranked_pool")
    final = stage_or_empty(stages, "final")

    buckets: list[str] = []
    if final.get("all_hit"):
        buckets.append("final_hit")
        if sample.get("allows_title_reference"):
            buckets.append("known_title_hit")
        return buckets

    if not merged.get("any_hit"):
        buckets.append("no_candidate_recall")
    elif not reranked_pool.get("any_hit"):
        buckets.append("rerank_loss")
    elif set(reranked_pool.get("hit_sources") or []) - set(final.get("hit_sources") or []):
        buckets.append("topk_loss")
    elif final.get("any_hit"):
        buckets.append("partial_source_hit")
    else:
        buckets.append("ranking_or_selection_loss")

    if not dense.get("any_hit"):
        buckets.append("dense_recall_gap")
    if not keyword.get("any_hit"):
        buckets.append("keyword_recall_gap")
    if source_title.get("any_hit") and not dense.get("any_hit") and not keyword.get("any_hit"):
        buckets.append("source_title_rescue")
    if sample.get("requires_multi_doc") and final.get("any_hit") and not final.get("all_hit"):
        buckets.append("partial_multi_doc")
    if sample.get("graph_relevant") and not final.get("all_hit"):
        buckets.append("graph_related_miss")
    if sample.get("allows_title_reference"):
        buckets.append("known_title_miss")
    else:
        buckets.append("blind_query_miss")
    return dedupe_preserving_order(buckets)


def stage_or_empty(stages: dict[str, Any], stage_name: str) -> dict[str, Any]:
    return stages.get(
        stage_name,
        {
            "candidate_count": 0,
            "top_sources": [],
            "top_candidates": [],
            "hit_sources": [],
            "missing_sources": [],
            "any_hit": False,
            "all_hit": False,
            "source_coverage": 0.0,
        },
    )


def summarize_diagnostics(samples: list[dict[str, Any]], stage_order: list[str] | None = None) -> dict[str, Any]:
    stage_order = stage_order or STAGE_ORDER
    positive_samples = [sample for sample in samples if sample.get("expected_sources")]
    stage_summary = {
        stage_name: summarize_stage(positive_samples, stage_name)
        for stage_name in stage_order
    }
    return {
        "stage_summary": stage_summary,
        "failure_bucket_counts": dict(
            sorted(Counter(bucket for sample in samples for bucket in sample.get("failure_buckets", [])).items())
        ),
        "transition_summary": summarize_transitions(positive_samples, stage_order),
        "type_summary": summarize_by_field(positive_samples, "type"),
        "expected_source_summary": summarize_expected_sources(positive_samples, stage_order),
        "recall_depth_summary": summarize_recall_depth_scan(positive_samples),
        "final_k_summary": summarize_final_k_scan(positive_samples),
    }


def summarize_stage(samples: list[dict[str, Any]], stage_name: str) -> dict[str, Any]:
    rows = [stage_or_empty(sample.get("stages") or {}, stage_name) for sample in samples]
    return summarize_stage_rows(rows)


def summarize_stage_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    sample_count = len(rows)
    candidate_counts = [int(row.get("candidate_count") or 0) for row in rows]
    return {
        "sample_count": sample_count,
        "any_hit_rate": ratio(sum(1 for row in rows if row.get("any_hit")), sample_count),
        "all_hit_rate": ratio(sum(1 for row in rows if row.get("all_hit")), sample_count),
        "average_source_coverage": ratio(sum(float(row.get("source_coverage") or 0.0) for row in rows), sample_count),
        "miss_count": sum(1 for row in rows if not row.get("all_hit")),
        "zero_candidate_count": sum(1 for count in candidate_counts if count <= 0),
        "avg_candidate_count": ratio(sum(candidate_counts), sample_count),
        "max_candidate_count": max(candidate_counts) if candidate_counts else 0,
        "hit_source_mentions": sum(len(row.get("hit_sources") or []) for row in rows),
        "missed_source_mentions": sum(len(row.get("missing_sources") or []) for row in rows),
    }


def summarize_recall_depth_scan(samples: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for sample in samples:
        scan = sample.get("recall_depth_scan") or {}
        for stage_name, depth_rows in scan.items():
            for depth, row in depth_rows.items():
                grouped[stage_name][str(depth)].append(row)

    summary = {}
    for stage_name, depth_rows in sorted(grouped.items()):
        summary[stage_name] = {
            depth: summarize_stage_rows(rows)
            for depth, rows in sorted(depth_rows.items(), key=lambda item: int(item[0]))
        }
    return summary


def summarize_final_k_scan(samples: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        scan = sample.get("final_k_scan") or {}
        for final_k, row in scan.items():
            grouped[str(final_k)].append(row)
    return {
        final_k: summarize_stage_rows(rows)
        for final_k, rows in sorted(grouped.items(), key=lambda item: int(item[0]))
    }


def summarize_transitions(samples: list[dict[str, Any]], stage_order: list[str]) -> dict[str, Any]:
    transitions: dict[str, Any] = {}
    for previous, current in zip(stage_order, stage_order[1:]):
        lost_sample_count = 0
        gained_sample_count = 0
        lost_source_mentions = 0
        gained_source_mentions = 0
        for sample in samples:
            stages = sample.get("stages") or {}
            previous_hits = set(stage_or_empty(stages, previous).get("hit_sources") or [])
            current_hits = set(stage_or_empty(stages, current).get("hit_sources") or [])
            lost = previous_hits - current_hits
            gained = current_hits - previous_hits
            if lost:
                lost_sample_count += 1
                lost_source_mentions += len(lost)
            if gained:
                gained_sample_count += 1
                gained_source_mentions += len(gained)
        transitions[f"{previous}->{current}"] = {
            "lost_sample_count": lost_sample_count,
            "lost_source_mentions": lost_source_mentions,
            "gained_sample_count": gained_sample_count,
            "gained_source_mentions": gained_source_mentions,
        }
    return transitions


def summarize_by_field(samples: list[dict[str, Any]], field_name: str) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        grouped[str(sample.get(field_name) or "unknown")].append(sample)
    summary = {}
    for value, group in sorted(grouped.items()):
        final_rows = [stage_or_empty(sample.get("stages") or {}, "final") for sample in group]
        summary[value] = {
            "sample_count": len(group),
            "final_any_hit_rate": ratio(sum(1 for row in final_rows if row.get("any_hit")), len(final_rows)),
            "final_all_hit_rate": ratio(sum(1 for row in final_rows if row.get("all_hit")), len(final_rows)),
            "final_average_source_coverage": ratio(
                sum(float(row.get("source_coverage") or 0.0) for row in final_rows),
                len(final_rows),
            ),
        }
    return summary


def summarize_expected_sources(samples: list[dict[str, Any]], stage_order: list[str]) -> dict[str, Any]:
    counts: dict[str, dict[str, Any]] = {}
    for sample in samples:
        expected_sources = sample.get("expected_sources") or []
        stages = sample.get("stages") or {}
        for expected_source in expected_sources:
            row = counts.setdefault(
                expected_source,
                {
                    "expected_count": 0,
                    **{f"{stage_name}_hit_count": 0 for stage_name in stage_order},
                },
            )
            row["expected_count"] += 1
            for stage_name in stage_order:
                if expected_source in (stage_or_empty(stages, stage_name).get("hit_sources") or []):
                    row[f"{stage_name}_hit_count"] += 1

    for row in counts.values():
        expected_count = row["expected_count"]
        for stage_name in stage_order:
            row[f"{stage_name}_hit_rate"] = ratio(row[f"{stage_name}_hit_count"], expected_count)
    return dict(sorted(counts.items(), key=lambda item: (-item[1]["expected_count"], item[0])))


def build_report(
    dataset_path: Path,
    workspace_path: Path,
    top_k: int,
    samples: list[dict[str, Any]],
    stage_order: list[str] | None = None,
    graph_enabled: bool = False,
) -> dict[str, Any]:
    stage_order = stage_order or STAGE_ORDER
    for sample in samples:
        if "failure_buckets" not in sample:
            sample["failure_buckets"] = classify_failure_buckets(sample)
    summary = summarize_diagnostics(samples, stage_order=stage_order)
    positive_count = sum(1 for sample in samples if sample.get("expected_sources"))
    return {
        "schema_version": "rag_pipeline_diagnostics_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": str(dataset_path),
        "workspace": str(workspace_path),
        "top_k": top_k,
        "graph_enabled": graph_enabled,
        "stage_order": stage_order,
        "sample_count": len(samples),
        "positive_sample_count": positive_count,
        **summary,
        "samples": samples,
    }


def save_report_json(report: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_report_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def compare_reports(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    stage_order = [
        stage_name
        for stage_name in baseline.get("stage_order", STAGE_ORDER)
        if stage_name in baseline.get("stage_summary", {}) or stage_name in candidate.get("stage_summary", {})
    ]
    for stage_name in candidate.get("stage_order", []):
        if stage_name not in stage_order:
            stage_order.append(stage_name)

    stage_deltas = {}
    for stage_name in stage_order:
        before = (baseline.get("stage_summary") or {}).get(stage_name, {})
        after = (candidate.get("stage_summary") or {}).get(stage_name, {})
        stage_deltas[stage_name] = {
            "any_hit_rate_delta": rounded_delta(before.get("any_hit_rate"), after.get("any_hit_rate")),
            "all_hit_rate_delta": rounded_delta(before.get("all_hit_rate"), after.get("all_hit_rate")),
            "average_source_coverage_delta": rounded_delta(
                before.get("average_source_coverage"),
                after.get("average_source_coverage"),
            ),
            "miss_count_delta": int(after.get("miss_count", 0) or 0) - int(before.get("miss_count", 0) or 0),
            "avg_candidate_count_delta": rounded_delta(
                before.get("avg_candidate_count"),
                after.get("avg_candidate_count"),
            ),
        }

    baseline_buckets = baseline.get("failure_bucket_counts") or {}
    candidate_buckets = candidate.get("failure_bucket_counts") or {}
    bucket_names = sorted(set(baseline_buckets) | set(candidate_buckets))
    return {
        "baseline_dataset": baseline.get("dataset"),
        "candidate_dataset": candidate.get("dataset"),
        "baseline_workspace": baseline.get("workspace"),
        "candidate_workspace": candidate.get("workspace"),
        "stage_order": stage_order,
        "stage_deltas": stage_deltas,
        "failure_bucket_deltas": {
            bucket: int(candidate_buckets.get(bucket, 0) or 0) - int(baseline_buckets.get(bucket, 0) or 0)
            for bucket in bucket_names
        },
    }


def print_human_report(report: dict[str, Any], limit: int = 10) -> None:
    print("\nRAG 链路诊断摘要")
    print(f"样本数: {report['sample_count']}")
    print(f"正样本数: {report['positive_sample_count']}")
    print(f"Top-K: {report['top_k']}")

    print("\n阶段召回指标:")
    for stage_name in report["stage_order"]:
        stage = report["stage_summary"][stage_name]
        print(
            f"- {stage_name}: 全命中 {format_percent(stage['all_hit_rate'])}; "
            f"任一命中 {format_percent(stage['any_hit_rate'])}; "
            f"平均覆盖 {format_percent(stage['average_source_coverage'])}; "
            f"平均候选 {stage['avg_candidate_count']:.1f}; "
            f"未全命中 {stage['miss_count']}"
        )

    print("\n失败分桶:")
    for bucket, count in sorted(report["failure_bucket_counts"].items(), key=lambda item: (-item[1], item[0])):
        print(f"- {bucket}: {count}")

    print("\n阶段间命中流失/补回:")
    for transition, payload in report["transition_summary"].items():
        print(
            f"- {transition}: 流失样本 {payload['lost_sample_count']} / "
            f"流失来源 {payload['lost_source_mentions']}; "
            f"补回样本 {payload['gained_sample_count']} / 补回来源 {payload['gained_source_mentions']}"
        )

    if report.get("recall_depth_summary"):
        print("\n召回深度扫描:")
        for stage_name, depth_rows in report["recall_depth_summary"].items():
            parts = []
            for depth, payload in depth_rows.items():
                parts.append(
                    f"@{depth} 任一 {format_percent(payload['any_hit_rate'])} / "
                    f"全命中 {format_percent(payload['all_hit_rate'])}"
                )
            print(f"- {stage_name}: {'; '.join(parts)}")

    if report.get("final_k_summary"):
        print("\nFinal K 扫描:")
        for final_k, payload in report["final_k_summary"].items():
            print(
                f"- @{final_k}: 任一 {format_percent(payload['any_hit_rate'])}; "
                f"全命中 {format_percent(payload['all_hit_rate'])}; "
                f"平均覆盖 {format_percent(payload['average_source_coverage'])}"
            )

    miss_samples = [
        sample
        for sample in report["samples"]
        if sample.get("expected_sources") and not stage_or_empty(sample.get("stages") or {}, "final").get("all_hit")
    ]
    if miss_samples:
        print("\n未全命中样例:")
        for sample in miss_samples[:limit]:
            final_stage = stage_or_empty(sample.get("stages") or {}, "final")
            print(
                f"- {sample['id']}: bucket={','.join(sample.get('failure_buckets', []))}; "
                f"期望={'; '.join(sample.get('expected_sources', []))}; "
                f"最终={'; '.join(final_stage.get('top_sources') or [])}"
            )


def print_comparison_report(comparison: dict[str, Any], baseline_label: str, candidate_label: str) -> None:
    print(f"RAG 链路诊断对比: {baseline_label} -> {candidate_label}")
    print("\n阶段指标变化:")
    for stage_name in comparison["stage_order"]:
        delta = comparison["stage_deltas"][stage_name]
        print(
            f"- {stage_name}: 全命中 {format_delta_percent(delta['all_hit_rate_delta'])}; "
            f"任一命中 {format_delta_percent(delta['any_hit_rate_delta'])}; "
            f"平均覆盖 {format_delta_percent(delta['average_source_coverage_delta'])}; "
            f"未全命中 {format_signed_int(delta['miss_count_delta'])}; "
            f"平均候选 {format_signed_float(delta['avg_candidate_count_delta'])}"
        )

    print("\n失败分桶变化:")
    for bucket, delta in sorted(comparison["failure_bucket_deltas"].items(), key=lambda item: (-abs(item[1]), item[0])):
        print(f"- {bucket}: {format_signed_int(delta)}")


def allows_title_reference(record: dict[str, Any]) -> bool:
    for key in ("allows_title_reference", "title_reference", "known_title_reference", "uses_title_reference"):
        if key in record:
            return bool(record.get(key))
    sample_type = str(record.get("type") or "").lower()
    return "title" in sample_type or "known" in sample_type


def parse_int_list(value: str | None) -> list[int]:
    if not value:
        return []
    items = []
    for raw_item in value.split(","):
        raw_item = raw_item.strip()
        if not raw_item:
            continue
        number = int(raw_item)
        if number > 0 and number not in items:
            items.append(number)
    return items


def ratio(numerator: float, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(float(numerator) / denominator, 6)


def rounded_delta(before: Any, after: Any) -> float:
    return round(float(after or 0.0) - float(before or 0.0), 6)


def format_percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def format_delta_percent(value: float) -> str:
    return f"{value * 100:+.1f}pct"


def format_signed_int(value: int) -> str:
    return f"{value:+d}"


def format_signed_float(value: float) -> str:
    return f"{value:+.1f}"


def dedupe_preserving_order(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


if __name__ == "__main__":
    configure_utf8_stdio()
    raise SystemExit(main(sys.argv[1:]))
