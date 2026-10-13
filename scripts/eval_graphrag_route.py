"""Evaluate the deterministic GraphRAG route contract on a frozen query set."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "eval" / "graphrag_route_quality_v0.1.jsonl"
SCHEMA_VERSION = "souldrive_graphrag_route_quality_v0.1"
SUPPORTED_MODES = {"hybrid", "local", "global"}

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.graph_rag import route_graph_query


def load_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(f"record {line_number} must be an object")
            records.append(record)
    return records


def validate_records(records: list[dict[str, Any]]) -> None:
    if not records:
        raise ValueError("GraphRAG route dataset must not be empty")
    seen_ids: set[str] = set()
    for index, record in enumerate(records, start=1):
        if record.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"record {index}: unsupported schema_version")
        record_id = str(record.get("id") or "").strip()
        if not record_id or record_id in seen_ids:
            raise ValueError(f"record {index}: duplicate or empty id")
        seen_ids.add(record_id)
        expected_mode = str(record.get("expected_mode") or "").strip()
        if expected_mode not in SUPPORTED_MODES:
            raise ValueError(f"unsupported expected_mode: {expected_mode or '<empty>'}")
        if not isinstance(record.get("expected_use_graph"), bool):
            raise ValueError(f"{record_id}: expected_use_graph must be boolean")
        if not isinstance(record.get("query"), str):
            raise ValueError(f"{record_id}: query must be a string")


def score_record(record: dict[str, Any]) -> dict[str, Any]:
    route = route_graph_query(str(record.get("query") or ""))
    public = route.public_dict()
    expected_mode = str(record["expected_mode"])
    expected_use_graph = bool(record["expected_use_graph"])
    required_signals = [str(item) for item in record.get("required_signals") or [] if str(item)]
    forbidden_signals = [str(item) for item in record.get("forbidden_signals") or [] if str(item)]
    actual_signals = set(public.get("signals") or [])
    missing_signals = [signal for signal in required_signals if signal not in actual_signals]
    forbidden_hits = [signal for signal in forbidden_signals if signal in actual_signals]
    mode_pass = public["mode"] == expected_mode
    graph_gate_pass = bool(public["use_graph"]) == expected_use_graph
    signal_contract_pass = not missing_signals and not forbidden_hits
    return {
        "id": str(record["id"]),
        "query": str(record.get("query") or ""),
        "expected_mode": expected_mode,
        "actual_mode": public["mode"],
        "expected_use_graph": expected_use_graph,
        "actual_use_graph": bool(public["use_graph"]),
        "signals": list(public.get("signals") or []),
        "missing_signals": missing_signals,
        "forbidden_signals_found": forbidden_hits,
        "mode_pass": mode_pass,
        "graph_gate_pass": graph_gate_pass,
        "signal_contract_pass": signal_contract_pass,
        "passed": mode_pass and graph_gate_pass and signal_contract_pass,
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    return {
        "total": total,
        "passed": sum(bool(result["passed"]) for result in results),
        "pass_rate": sum(bool(result["passed"]) for result in results) / total if total else 0.0,
        "route_accuracy": sum(bool(result["mode_pass"]) for result in results) / total if total else 0.0,
        "graph_gate_accuracy": sum(bool(result["graph_gate_pass"]) for result in results) / total if total else 0.0,
        "signal_contract_accuracy": sum(bool(result["signal_contract_pass"]) for result in results) / total if total else 0.0,
    }


def evaluate(records: list[dict[str, Any]]) -> dict[str, Any]:
    validate_records(records)
    results = [score_record(record) for record in records]
    return {
        "schema_version": "souldrive_graphrag_route_quality_eval_v0.1",
        "summary": summarize(results),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate SoulDrive GraphRAG route contracts.")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)
    report = evaluate(load_records(Path(args.dataset)))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["summary"]["passed"] == report["summary"]["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
