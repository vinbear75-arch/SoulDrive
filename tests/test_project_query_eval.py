import json
from pathlib import Path

from scripts.eval_project_query import (
    load_benchmark,
    run_catalog_filtered,
    run_unscoped_lexical,
    summarize,
)


ROOT = Path(__file__).resolve().parent.parent


def test_wp1_benchmark_has_cross_project_time_and_version_cases():
    benchmark = load_benchmark(ROOT / "eval" / "project_query_benchmark_v0.1.json")

    assert len(benchmark["queries"]) == 10
    assert len({asset["project_id"] for asset in benchmark["assets"]}) == 2
    assert sum(asset["asset_id"] == "battery_report_jul" for asset in benchmark["assets"]) == 2
    assert sum(bool(query.get("performed_from")) for query in benchmark["queries"]) >= 6


def test_unscoped_b1_exposes_project_and_version_errors():
    benchmark = load_benchmark(ROOT / "eval" / "project_query_benchmark_v0.1.json")
    predictions = run_unscoped_lexical(benchmark, top_k=3)
    metrics = summarize(benchmark, predictions)

    assert metrics["sample_count"] == 10
    assert metrics["wrong_project_at_1_rate"] > 0
    assert metrics["wrong_time_at_1_rate"] > 0
    assert metrics["version_hit_at_1"] < 1
    json.dumps(predictions, ensure_ascii=False)


def test_catalog_candidate_eliminates_project_time_and_version_errors():
    benchmark = load_benchmark(ROOT / "eval" / "project_query_benchmark_v0.1.json")
    predictions = run_catalog_filtered(benchmark, top_k=3)
    metrics = summarize(benchmark, predictions)

    assert metrics["version_hit_at_1"] == 1
    assert metrics["wrong_project_at_1_rate"] == 0
    assert metrics["wrong_time_at_1_rate"] == 0
