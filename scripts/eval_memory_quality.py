"""Run a small, deterministic quality gate against the durable memory store.

This is deliberately a local contract evaluator rather than an LLM judge. It
checks the properties that ordinary RAG metrics miss for memory: recall of the
expected memory, project/workflow isolation, and absence of stale or forbidden
facts after an update.
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "eval" / "memory_quality_v0.1.jsonl"
SCHEMA_VERSION = "souldrive_memory_quality_v0.1"
SUPPORTED_SUBTYPES = {
    "user_preference",
    "project_fact",
    "project_constraint",
    "workflow_fact",
}
SUPPORTED_OPERATIONS = {"remember", "update", "query", "context"}

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.learning_store import LearningStore
from core.memory_runtime import gate_memory_context
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


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
        raise ValueError("memory quality dataset must not be empty")
    seen_ids: set[str] = set()
    for index, record in enumerate(records, start=1):
        if record.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"record {index}: unsupported schema_version")
        record_id = str(record.get("id") or "").strip()
        if not record_id or record_id in seen_ids:
            raise ValueError(f"record {index}: duplicate or empty id")
        seen_ids.add(record_id)
        events = record.get("events")
        if not isinstance(events, list) or not events:
            raise ValueError(f"{record_id}: events must be a non-empty list")
        for event_index, event in enumerate(events, start=1):
            if not isinstance(event, dict):
                raise ValueError(f"{record_id} event {event_index} must be an object")
            operation = str(event.get("op") or "").strip()
            if operation not in SUPPORTED_OPERATIONS:
                raise ValueError(f"unsupported memory event: {operation or '<empty>'}")
            if operation in {"query", "context"}:
                if not str(event.get("query") or "").strip():
                    raise ValueError(f"{record_id} event {event_index}: query is required")
                if not isinstance(event.get("expect") or {}, dict):
                    raise ValueError(f"{record_id} event {event_index}: expect must be an object")
            else:
                subtype = str(event.get("subtype") or "").strip()
                if operation == "remember" and subtype not in SUPPORTED_SUBTYPES:
                    raise ValueError(f"{record_id} event {event_index}: unsupported memory subtype")
                if not str(event.get("alias") or "").strip():
                    raise ValueError(f"{record_id} event {event_index}: alias is required")
                if operation == "remember":
                    if not str(event.get("title") or "").strip() or not str(event.get("content") or "").strip():
                        raise ValueError(f"{record_id} event {event_index}: title and content are required")
                    if subtype == "workflow_fact" and not str(event.get("workflow_id") or "").strip():
                        raise ValueError(f"{record_id} event {event_index}: workflow_id is required")
                    if subtype == "project_constraint" and not str(event.get("project_id") or "").strip():
                        raise ValueError(f"{record_id} event {event_index}: project_id is required")
                if operation == "update":
                    if not str(event.get("title") or "").strip() or not str(event.get("content") or "").strip():
                        raise ValueError(f"{record_id} event {event_index}: title and content are required")
        if not any(event["op"] in {"query", "context"} for event in events):
            raise ValueError(f"{record_id}: at least one query or context event is required")


def _candidate_from_event(event: dict[str, Any]) -> dict[str, Any]:
    candidate = {
        "asset_type": "memory",
        "subtype": str(event["subtype"]),
        "title": str(event["title"]),
        "content": str(event["content"]),
        "evidence": ["frozen memory-quality scenario"],
        "rationale": "deterministic memory quality evaluation",
        "confidence": 1.0,
        "selected_by_default": True,
        "source_message_indexes": [],
    }
    for key in ("project_id", "workflow_id", "conflicts_with"):
        if key in event:
            candidate[key] = event[key]
    return candidate


def _accept_memory(
    store: LearningStore,
    event: dict[str, Any],
    known_memory_ids: dict[str, str],
) -> str:
    project_id = str(event.get("project_id") or "").strip() or None
    candidate = _candidate_from_event(event)
    if event.get("conflicts_with"):
        candidate["conflicts_with"] = [
            known_memory_ids.get(str(reference), str(reference))
            for reference in event.get("conflicts_with") or []
        ]
    review = store.create_review(
        "manual",
        [candidate],
        project_id=project_id,
    )
    candidate_id = review["candidates"][0]["candidate_id"]
    result = store.decide_review(
        review["review_id"],
        [{"candidate_id": candidate_id, "action": "accept"}],
        project_id=project_id,
    )
    accepted = result.get("accepted_assets") or []
    memory_id = str(accepted[0].get("memory_id") or "") if accepted else ""
    if not memory_id:
        raise ValueError("memory scenario did not create a memory")
    return memory_id


def _score_query(
    event: dict[str, Any],
    memories: list[dict[str, Any]],
    aliases_by_memory_id: dict[str, str],
    *,
    gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    expect = event.get("expect") or {}
    required_aliases = [str(item) for item in expect.get("required_aliases") or [] if str(item)]
    forbidden_aliases = [str(item) for item in expect.get("forbidden_aliases") or [] if str(item)]
    expected_content = [str(item) for item in expect.get("expected_content") or [] if str(item)]
    forbidden_content = [str(item) for item in expect.get("forbidden_content") or [] if str(item)]
    expected_gate_reasons = [
        str(item) for item in expect.get("expected_gate_reasons") or [] if str(item)
    ]
    returned_aliases = [
        aliases_by_memory_id[memory_id]
        for memory_id in (
            str(item.get("memory_id") or item.get("asset_id") or "")
            for item in memories
        )
        if memory_id in aliases_by_memory_id
    ]
    returned_text = "\n".join(
        f"{item.get('title') or ''}\n{item.get('content') or ''}" for item in memories
    )
    required_hits = [alias for alias in required_aliases if alias in returned_aliases]
    forbidden_hits = [alias for alias in forbidden_aliases if alias in returned_aliases]
    missing_content = [fragment for fragment in expected_content if fragment not in returned_text]
    forbidden_content_hits = [fragment for fragment in forbidden_content if fragment in returned_text]
    returned_gate_reasons = [
        str(item.get("context_gate_reason") or "")
        for item in memories
        if str(item.get("context_gate_reason") or "")
    ]
    missing_gate_reasons = [
        reason for reason in expected_gate_reasons if reason not in returned_gate_reasons
    ]
    recall = len(required_hits) / len(required_aliases) if required_aliases else 1.0
    leakage = bool(forbidden_hits or forbidden_content_hits)
    passed = (
        len(required_hits) == len(required_aliases)
        and not missing_content
        and not leakage
        and not missing_gate_reasons
    )
    return {
        "query": str(event.get("query") or ""),
        "returned_aliases": returned_aliases,
        "returned_memory_count": len(memories),
        "memory_recall_at_k": recall,
        "missing_required_aliases": [alias for alias in required_aliases if alias not in required_hits],
        "forbidden_aliases_found": forbidden_hits,
        "missing_expected_content": missing_content,
        "forbidden_content_found": forbidden_content_hits,
        "expected_gate_reasons": expected_gate_reasons,
        "returned_gate_reasons": returned_gate_reasons,
        "missing_gate_reasons": missing_gate_reasons,
        "gate_decision": ((gate or {}).get("gate") or {}).get("decision"),
        "stale_or_forbidden_leakage": leakage,
        "passed": passed,
    }


def _run_record(store: LearningStore, record: dict[str, Any]) -> dict[str, Any]:
    memory_ids: dict[str, str] = {}
    aliases_by_memory_id: dict[str, str] = {}
    query_results: list[dict[str, Any]] = []
    for event in record["events"]:
        operation = str(event["op"])
        if operation == "remember":
            alias = str(event["alias"])
            memory_id = _accept_memory(store, event, memory_ids)
            memory_ids[alias] = memory_id
            aliases_by_memory_id[memory_id] = alias
            continue
        if operation == "update":
            alias = str(event["alias"])
            memory_id = memory_ids.get(alias)
            if not memory_id:
                raise ValueError(f"{record['id']}: update references unknown alias {alias}")
            kwargs: dict[str, Any] = {
                "title": str(event["title"]),
                "content": str(event["content"]),
                "project_id": str(event.get("project_id") or "").strip() or None,
            }
            for key in ("valid_from", "invalid_at", "idempotency_key"):
                if key in event:
                    kwargs[key] = event[key]
            if store.update_memory(memory_id, **kwargs) is None:
                raise ValueError(f"{record['id']}: update returned no memory")
            continue

        event_project_id = str(event.get("project_id") or "").strip() or None
        event_include_global = bool(event.get("include_global", True))
        event_workflow_id = str(event.get("workflow_id") or "").strip() or None
        search = store.search(
            str(event["query"]),
            top_k_memory=max(1, int(event.get("top_k") or 5)),
            top_k_skill=0,
            project_id=event_project_id,
            context_only=bool(event.get("context_only")),
            include_global=event_include_global,
            as_of=event.get("as_of"),
            workflow_id=event_workflow_id,
        )
        if operation == "context":
            scored_memories = {
                str(item.get("asset_id") or ""): item
                for item in search.get("memories") or []
            }
            candidates = []
            for memory in store.list_memories(
                project_id=event_project_id,
                context_only=True,
                include_metadata=True,
                include_global=event_include_global,
                as_of=event.get("as_of"),
                workflow_id=event_workflow_id,
            ):
                memory_id = str(memory.get("memory_id") or memory.get("asset_id") or "")
                candidates.append({
                    **memory,
                    "score": float((scored_memories.get(memory_id) or {}).get("score", 0.0) or 0.0),
                })
            gated = gate_memory_context(
                str(event["query"]),
                candidates,
                reference_timestamp=event.get("as_of"),
                project_id=event_project_id,
                include_global=event_include_global,
                workflow_id=event_workflow_id,
            )
            query_results.append(
                _score_query(
                    event,
                    gated.get("memories") or [],
                    aliases_by_memory_id,
                    gate=gated,
                )
            )
            continue
        query_results.append(
            _score_query(event, search.get("memories") or [], aliases_by_memory_id)
        )

    return {
        "id": str(record["id"]),
        "query_results": query_results,
        "passed": all(result["passed"] for result in query_results),
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    query_results = [query for result in results for query in result["query_results"]]
    total_queries = len(query_results)
    passed_queries = sum(bool(query["passed"]) for query in query_results)
    recall = sum(float(query["memory_recall_at_k"]) for query in query_results)
    leakage_count = sum(bool(query["stale_or_forbidden_leakage"]) for query in query_results)
    return {
        "total_scenarios": len(results),
        "passed_scenarios": sum(bool(result["passed"]) for result in results),
        "total_queries": total_queries,
        "passed_queries": passed_queries,
        "query_pass_rate": passed_queries / total_queries if total_queries else 0.0,
        "memory_recall_at_k": recall / total_queries if total_queries else 0.0,
        "stale_or_forbidden_leakage_rate": leakage_count / total_queries if total_queries else 0.0,
    }


def evaluate(records: list[dict[str, Any]]) -> dict[str, Any]:
    validate_records(records)
    results: list[dict[str, Any]] = []
    for record in records:
        with tempfile.TemporaryDirectory(prefix="souldrive-memory-eval-") as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "memory quality eval")
            keys = unlock_keystore(workspace, "memory quality eval")
            store = LearningStore(workspace, keys)
            try:
                results.append(_run_record(store, record))
            finally:
                store.close()
    return {
        "schema_version": "souldrive_memory_quality_eval_v0.1",
        "summary": summarize(results),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate durable SoulDrive memory quality.")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)
    report = evaluate(load_records(Path(args.dataset)))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    summary = report["summary"]
    return 0 if (
        summary["total_queries"] > 0
        and summary["passed_queries"] == summary["total_queries"]
        and summary["passed_scenarios"] == summary["total_scenarios"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
