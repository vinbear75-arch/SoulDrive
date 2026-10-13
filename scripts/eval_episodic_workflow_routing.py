import argparse
from datetime import datetime
import json
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "eval" / "episodic_workflow_routing_v0.1.jsonl"
SUPPORTED_WORKFLOW_IDS = frozenset(
    {"paper_review", "experiment_compare", "meeting_actions"}
)
SUPPORTED_SOURCE_TYPES = frozenset(
    {
        "conversation_session",
        "decision",
        "experiment_run",
        "project_task",
        "durable_task",
    }
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.episodic_view import build_episodic_view, select_episodic_events_for_query


def load_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if line.strip():
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError(f"record {line_number} must be an object")
                records.append(record)
    return records


def _unique_string_list(
    record_id: str,
    record: dict[str, Any],
    field: str,
) -> list[str]:
    value = record.get(field)
    if not isinstance(value, list):
        raise ValueError(f"{record_id}: {field} must be a list")
    result = [str(item).strip() for item in value if str(item).strip()]
    if len(result) != len(value) or len(set(result)) != len(result):
        raise ValueError(f"{record_id}: {field} must contain unique non-empty strings")
    return result


def _valid_timestamp(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return True


def validate_records(records: list[dict[str, Any]]) -> None:
    if not records:
        raise ValueError("episodic workflow routing dataset must not be empty")

    seen_ids: set[str] = set()
    for index, record in enumerate(records, start=1):
        record_id = str(record.get("id") or "").strip()
        if not record_id or record_id in seen_ids:
            raise ValueError(f"record {index}: duplicate or empty id")
        seen_ids.add(record_id)

        workflow_id = str(record.get("workflow_id") or "").strip()
        if workflow_id not in SUPPORTED_WORKFLOW_IDS:
            raise ValueError(f"{record_id}: unsupported workflow_id")
        if not str(record.get("query") or "").strip():
            raise ValueError(f"{record_id}: query is required")
        if not str(record.get("metric") or "").strip():
            raise ValueError(f"{record_id}: metric is required")

        events = record.get("events")
        if not isinstance(events, list) or not events:
            raise ValueError(f"{record_id}: events must be a non-empty list")
        event_ids: set[str] = set()
        project_ids: set[str] = set()
        for event_index, event in enumerate(events, start=1):
            if not isinstance(event, dict):
                raise ValueError(f"{record_id} event {event_index} must be an object")
            source_type = str(event.get("source_type") or "").strip()
            source_id = str(event.get("source_id") or "").strip()
            project_id = str(event.get("project_id") or "").strip()
            if source_type not in SUPPORTED_SOURCE_TYPES:
                raise ValueError(f"{record_id}: unsupported source_type")
            if not source_id or not project_id:
                raise ValueError(f"{record_id}: event source_id and project_id are required")
            if not _valid_timestamp(event.get("event_time")):
                raise ValueError(f"{record_id}: event_time must be an ISO-8601 timestamp")
            event_id = f"{source_type}:{source_id}"
            if event_id in event_ids:
                raise ValueError(f"{record_id}: duplicate event_id {event_id}")
            event_ids.add(event_id)
            project_ids.add(project_id)
            event_workflow = str(event.get("workflow_id") or "").strip()
            if event_workflow and event_workflow not in SUPPORTED_WORKFLOW_IDS:
                raise ValueError(f"{record_id}: unsupported event workflow_id")

        declared_project_id = str(record.get("project_id") or "").strip()
        if declared_project_id and declared_project_id not in project_ids:
            raise ValueError(f"{record_id}: project_id does not match events")
        if len(project_ids) != 1:
            raise ValueError(f"{record_id}: events must share one project_id")

        expected = _unique_string_list(record_id, record, "expected_event_ids")
        forbidden = _unique_string_list(record_id, record, "forbidden_event_ids")
        overlap = sorted(set(expected) & set(forbidden))
        if overlap:
            raise ValueError(f"{record_id}: expected_event_ids and forbidden_event_ids overlap")
        unknown = sorted(set(expected) - event_ids)
        if unknown:
            raise ValueError(f"{record_id}: expected_event_ids reference unknown event_ids: {unknown}")


def _record_project_id(record: dict[str, Any]) -> str:
    declared = str(record.get("project_id") or "").strip()
    if declared:
        return declared
    project_ids = {
        str(event.get("project_id") or "").strip()
        for event in record.get("events") or []
        if str(event.get("project_id") or "").strip()
    }
    return next(iter(project_ids))


def _score(record: dict, selected: dict) -> dict:
    actual = [str(item.get("event_id") or "") for item in selected.get("events") or []]
    expected = [str(item) for item in record.get("expected_event_ids") or []]
    forbidden = [str(item) for item in record.get("forbidden_event_ids") or []]
    expected_hits = sum(item in actual for item in expected)
    forbidden_hits = sum(item in actual for item in forbidden)
    return {
        "expected_hits": expected_hits,
        "expected_total": len(expected),
        "forbidden_hits": forbidden_hits,
        "forbidden_total": len(forbidden),
        "event_ids": actual,
        "passed": expected_hits == len(expected) and forbidden_hits == 0,
    }


def _run(records: list[dict[str, Any]], *, runtime: bool) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for record in records:
        started = time.time()
        if runtime:
            project_id = _record_project_id(record)
            view = build_episodic_view(
                project_id=project_id,
                sessions=[
                    {
                        **item,
                        "session_id": item.get("source_id"),
                        "updated_at": item.get("event_time"),
                    }
                    for item in record.get("events") or []
                    if item.get("source_type") == "conversation_session"
                ],
                experiment_runs=[
                    {
                        **item,
                        "run_id": item.get("source_id"),
                        "experiment_name": item.get("title"),
                        "performed_at": item.get("event_time"),
                        "dataset_version": item.get("summary"),
                    }
                    for item in record.get("events") or []
                    if item.get("source_type") == "experiment_run"
                ],
                decisions=[
                    {
                        **item,
                        "decision_id": item.get("source_id"),
                        "decided_at": item.get("event_time"),
                        "statement": item.get("summary"),
                    }
                    for item in record.get("events") or []
                    if item.get("source_type") == "decision"
                ],
                project_tasks=[
                    {
                        **item,
                        "task_id": item.get("source_id"),
                    }
                    for item in record.get("events") or []
                    if item.get("source_type") == "project_task"
                ],
                durable_tasks=[
                    {
                        **item,
                        "task_id": item.get("source_id"),
                    }
                    for item in record.get("events") or []
                    if item.get("source_type") == "durable_task"
                ],
            )
            selected = select_episodic_events_for_query(
                view,
                query=record.get("query") or "",
                workflow_id=record.get("workflow_id"),
            )
        else:
            selected = {"events": [], "selection_reason": "baseline_no_activity_projection"}
        results.append({
            "id": record["id"],
            "metric": record["metric"],
            "workflow_id": record.get("workflow_id"),
            "selection_reason": selected.get("selection_reason"),
            "score": _score(record, selected),
            "latency_ms": int((time.time() - started) * 1000),
        })
    return results


def _summary(results: list[dict]) -> dict:
    passed = sum(bool(item["score"]["passed"]) for item in results)
    return {
        "total": len(results),
        "passed": passed,
        "pass_rate": passed / max(1, len(results)),
    }


def evaluate(records: list[dict[str, Any]], *, runtime: bool = True) -> dict[str, Any]:
    validate_records(records)
    results = _run(records, runtime=runtime)
    return {
        "schema_version": "souldrive_episodic_workflow_routing_eval_v0.1",
        "mode": "runtime" if runtime else "baseline",
        "summary": _summary(results),
        "results": results,
    }


def _gate_passes(report: dict[str, Any]) -> bool:
    summary = report["summary"]
    return bool(summary["total"] > 0 and summary["passed"] == summary["total"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate bounded episodic workflow routing.")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--baseline",
        action="store_true",
        help="只输出无 episodic 投影的对照结果；该模式用于诊断，不作为通过门禁。",
    )
    mode.add_argument(
        "--runtime",
        action="store_true",
        help="只运行当前 runtime；所有样本通过才返回成功。",
    )
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)

    records = load_records(Path(args.dataset))
    if args.runtime:
        payload = evaluate(records, runtime=True)
        status = 0 if _gate_passes(payload) else 1
    elif args.baseline:
        payload = evaluate(records, runtime=False)
        status = 0
    else:
        baseline = evaluate(records, runtime=False)
        runtime = evaluate(records, runtime=True)
        payload = {
            "schema_version": "souldrive_episodic_workflow_routing_comparison_v0.1",
            "baseline": baseline,
            "runtime": runtime,
        }
        status = 0 if _gate_passes(runtime) else 1
    rendered = json.dumps(payload, ensure_ascii=True, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
