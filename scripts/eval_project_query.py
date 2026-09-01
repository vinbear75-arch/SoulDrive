import argparse
import json
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.capture_b0_baseline import input_record, repository_snapshot, write_json, write_jsonl
from core.project_catalog import ProjectCatalog
from core.workspace import SoulDriveWorkspace


TOKEN_PATTERN = re.compile(r"[a-z0-9_.%-]+|[\u4e00-\u9fff]", re.IGNORECASE)


def load_benchmark(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "souldrive_project_query_benchmark_v0.1":
        raise ValueError("unsupported project query benchmark")
    if not payload.get("assets") or not payload.get("queries"):
        raise ValueError("project query benchmark requires assets and queries")
    return payload


def lexical_score(query: str, asset: dict[str, Any]) -> float:
    query_tokens = set(TOKEN_PATTERN.findall(query.lower()))
    asset_tokens = set(
        TOKEN_PATTERN.findall(f"{asset.get('title', '')} {asset.get('text', '')}".lower())
    )
    if not query_tokens:
        return 0.0
    return len(query_tokens & asset_tokens) / len(query_tokens)


def run_unscoped_lexical(benchmark: dict[str, Any], top_k: int) -> list[dict[str, Any]]:
    assets = list(benchmark["assets"])
    predictions = []
    for query in benchmark["queries"]:
        started_at = time.perf_counter()
        ranked = sorted(
            enumerate(assets),
            key=lambda pair: (
                lexical_score(query["query"], pair[1]),
                str(pair[1].get("imported_at") or ""),
                str(pair[1].get("version_id") or ""),
            ),
            reverse=True,
        )[:top_k]
        candidates = [
            {
                "rank": rank,
                "asset_id": asset["asset_id"],
                "version_id": asset["version_id"],
                "project_id": asset["project_id"],
                "asset_type": asset["asset_type"],
                "performed_at": asset.get("performed_at"),
                "score": round(lexical_score(query["query"], asset), 6),
            }
            for rank, (_index, asset) in enumerate(ranked, start=1)
        ]
        predictions.append(
            {
                "id": query["id"],
                "expected_version_ids": query["expected_version_ids"],
                "expected_project_id": query["project_id"],
                "candidates": candidates,
                "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 3),
            }
        )
    return predictions


def run_catalog_filtered(benchmark: dict[str, Any], top_k: int) -> list[dict[str, Any]]:
    assets_by_version = {asset["version_id"]: asset for asset in benchmark["assets"]}
    predictions = []
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            for project_id in sorted({asset["project_id"] for asset in benchmark["assets"]}):
                catalog.create_project(project_id, project_id=project_id)
            created_assets = set()
            created_experiments = set()
            for asset in benchmark["assets"]:
                asset_id = asset["asset_id"]
                if asset_id not in created_assets:
                    catalog.create_asset(
                        asset["project_id"],
                        asset["title"],
                        asset["asset_type"],
                        asset_id=asset_id,
                    )
                    created_assets.add(asset_id)
                catalog.create_asset_version(
                    asset_id,
                    f"synthetic-{asset['version_id']}",
                    f"{asset['version_id']}.json",
                    version_id=asset["version_id"],
                    valid_from=asset["valid_from"],
                    imported_at=asset["imported_at"],
                )
                if asset.get("performed_at"):
                    experiment_id = f"experiment_{asset_id}"
                    if experiment_id not in created_experiments:
                        catalog.create_experiment(
                            asset["project_id"],
                            asset["title"],
                            experiment_id=experiment_id,
                        )
                        created_experiments.add(experiment_id)
                    catalog.create_experiment_run(
                        experiment_id,
                        asset["performed_at"],
                        run_id=f"run_{asset['version_id']}",
                        evidence_version_id=asset["version_id"],
                    )

            for query in benchmark["queries"]:
                started_at = time.perf_counter()
                filtered = catalog.find_current_versions(
                    query["project_id"],
                    asset_type=query.get("asset_type"),
                    performed_from=query.get("performed_from"),
                    performed_to=query.get("performed_to"),
                    as_of=query.get("as_of"),
                )
                ranked = sorted(
                    filtered,
                    key=lambda row: (
                        lexical_score(query["query"], assets_by_version[row["version_id"]]),
                        row["valid_from"],
                        row["version_id"],
                    ),
                    reverse=True,
                )[:top_k]
                candidates = []
                for rank, row in enumerate(ranked, start=1):
                    asset = assets_by_version[row["version_id"]]
                    candidates.append(
                        {
                            "rank": rank,
                            "asset_id": row["asset_id"],
                            "version_id": row["version_id"],
                            "project_id": row["project_id"],
                            "asset_type": row["asset_type"],
                            "performed_at": row.get("performed_at"),
                            "score": round(lexical_score(query["query"], asset), 6),
                        }
                    )
                predictions.append(
                    {
                        "id": query["id"],
                        "expected_version_ids": query["expected_version_ids"],
                        "expected_project_id": query["project_id"],
                        "candidates": candidates,
                        "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 3),
                    }
                )
        finally:
            catalog.close()
    return predictions


def summarize(benchmark: dict[str, Any], predictions: list[dict[str, Any]]) -> dict[str, Any]:
    queries = {query["id"]: query for query in benchmark["queries"]}
    hit_at_1 = 0
    hit_at_k = 0
    reciprocal_rank = 0.0
    wrong_project_at_1 = 0
    wrong_time_at_1 = 0
    time_query_count = 0
    wrong_type_at_1 = 0
    rows = []
    for prediction in predictions:
        query = queries[prediction["id"]]
        candidates = prediction["candidates"]
        expected = set(prediction["expected_version_ids"])
        ranks = [item["rank"] for item in candidates if item["version_id"] in expected]
        first = candidates[0] if candidates else None
        if ranks:
            hit_at_k += 1
            reciprocal_rank += 1.0 / min(ranks)
            if min(ranks) == 1:
                hit_at_1 += 1
        wrong_project = bool(first and first["project_id"] != query["project_id"])
        wrong_project_at_1 += int(wrong_project)
        wrong_type = bool(first and first["asset_type"] != query["asset_type"])
        wrong_type_at_1 += int(wrong_type)
        wrong_time = False
        if query.get("performed_from") and query.get("performed_to"):
            time_query_count += 1
            performed_at = str(first.get("performed_at") or "") if first else ""
            wrong_time = not (query["performed_from"] <= performed_at < query["performed_to"])
            wrong_time_at_1 += int(wrong_time)
        rows.append(
            {
                "id": prediction["id"],
                "hit_at_1": bool(ranks and min(ranks) == 1),
                "hit_at_k": bool(ranks),
                "wrong_project_at_1": wrong_project,
                "wrong_time_at_1": wrong_time,
                "wrong_type_at_1": wrong_type,
            }
        )
    sample_count = len(predictions)
    return {
        "schema_version": "souldrive_project_query_metrics_v0.1",
        "sample_count": sample_count,
        "version_hit_at_1": hit_at_1 / sample_count,
        "version_hit_at_k": hit_at_k / sample_count,
        "mean_reciprocal_rank": reciprocal_rank / sample_count,
        "wrong_project_at_1_rate": wrong_project_at_1 / sample_count,
        "wrong_time_at_1_rate": wrong_time_at_1 / time_query_count if time_query_count else 0.0,
        "wrong_asset_type_at_1_rate": wrong_type_at_1 / sample_count,
        "rows": rows,
    }


def write_bundle(
    dataset_path: Path,
    output_dir: Path,
    run_id: str,
    top_k: int,
    mode: str = "unscoped_lexical",
) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    repository = repository_snapshot(output_dir)
    benchmark = load_benchmark(dataset_path)
    if mode == "unscoped_lexical":
        predictions = run_unscoped_lexical(benchmark, top_k)
        baseline_level = "B1_SIMPLE"
        component = "character/token overlap; ties prefer latest imported_at; no project, performed_at, type, or version filtering"
    elif mode == "catalog_filtered":
        predictions = run_catalog_filtered(benchmark, top_k)
        baseline_level = "CANDIDATE"
        component = "project/type/performed_at/current-version catalog filter followed by unchanged character/token overlap"
    else:
        raise ValueError(f"unsupported mode: {mode}")
    metrics = summarize(benchmark, predictions)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "metrics.json", metrics)
    write_jsonl(output_dir / "predictions.jsonl", predictions)
    write_jsonl(
        output_dir / "latency.jsonl",
        [{"id": row["id"], "elapsed_ms": row["elapsed_ms"], "stage": mode} for row in predictions],
    )
    failures = [row for row in metrics["rows"] if not row["hit_at_1"]]
    write_jsonl(output_dir / "failures.jsonl", failures)
    manifest = {
        "schema_version": "souldrive_run_manifest_v0.1",
        "run_id": run_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "baseline_level": baseline_level,
        "status": "complete",
        "repository": repository,
        "environment": {"python": sys.version.split()[0], "mode": mode},
        "inputs": [input_record(dataset_path, "frozen synthetic WP1 evaluation dataset")],
        "components": {
            "retriever": component,
            "top_k": top_k,
        },
        "outputs": {
            "predictions": "predictions.jsonl",
            "metrics": "metrics.json",
            "latency": "latency.jsonl",
            "failures": "failures.jsonl",
            "summary": "summary.md",
        },
        "limitations": [
            "This B1 is a deterministic simple baseline, not a live replay of the archived RAG system.",
            "The benchmark is synthetic and measures only project, time, type, and version isolation semantics.",
        ],
    }
    write_json(output_dir / "run_manifest.json", manifest)
    summary = (
        f"# WP1 {baseline_level} {mode}\n\n"
        f"- Run: `{run_id}`\n"
        f"- Samples: {metrics['sample_count']}\n"
        f"- Version Hit@1: {metrics['version_hit_at_1']:.1%}\n"
        f"- Version Hit@{top_k}: {metrics['version_hit_at_k']:.1%}\n"
        f"- MRR: {metrics['mean_reciprocal_rank']:.3f}\n"
        f"- Wrong project@1: {metrics['wrong_project_at_1_rate']:.1%}\n"
        f"- Wrong time@1: {metrics['wrong_time_at_1_rate']:.1%}\n"
        f"- Wrong asset type@1: {metrics['wrong_asset_type_at_1_rate']:.1%}\n"
    )
    (output_dir / "summary.md").write_text(summary, encoding="utf-8")
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the WP1 simple unscoped project-query baseline.")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--mode", choices=("unscoped_lexical", "catalog_filtered"), default="unscoped_lexical")
    args = parser.parse_args()
    metrics = write_bundle(args.dataset.resolve(), args.output_dir, args.run_id, args.top_k, args.mode)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
