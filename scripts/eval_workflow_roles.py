"""Evaluate the deterministic contracts of SoulDrive research windows.

This is a local contract evaluator, not an LLM quality judge.  It checks that
paper review, experiment comparison, and meeting actions keep different input
gates, retrieval profiles, focus terms, output sections, and evidence
namespaces.  The frozen records are intentionally small so this gate remains
cheap enough to run on every change.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "eval" / "workflow_role_quality_v0.1.jsonl"
SCHEMA_VERSION = "souldrive_workflow_role_quality_v0.1"
SUPPORTED_WORKFLOW_IDS = frozenset(
    {"paper_review", "experiment_compare", "meeting_actions"}
)
SUPPORTED_CITATION_PREFIXES = frozenset({"A", "E", "M", "V"})

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.multimodal_workflows import (  # noqa: E402
    evaluate_workflow_output_contract,
    plan_workflow,
    validate_workflow_citation_namespaces,
    workflow_retrieval_policy,
    workflow_retrieval_variants,
)


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


def _string_list(record_id: str, record: dict[str, Any], field: str) -> list[str]:
    value = record.get(field)
    if not isinstance(value, list) or not value:
        raise ValueError(f"{record_id}: {field} must be a non-empty list")
    result = [str(item).strip() for item in value if str(item).strip()]
    if len(result) != len(value) or len(set(result)) != len(result):
        raise ValueError(f"{record_id}: {field} must contain unique non-empty strings")
    return result


def validate_records(records: list[dict[str, Any]]) -> None:
    if not records:
        raise ValueError("workflow role quality dataset must not be empty")
    seen_ids: set[str] = set()
    for index, record in enumerate(records, start=1):
        if record.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"record {index}: unsupported schema_version")
        record_id = str(record.get("id") or "").strip()
        if not record_id or record_id in seen_ids:
            raise ValueError(f"record {index}: duplicate or empty id")
        seen_ids.add(record_id)

        workflow_id = str(record.get("workflow_id") or "").strip()
        if workflow_id not in SUPPORTED_WORKFLOW_IDS:
            raise ValueError(f"{record_id}: unsupported workflow_id: {workflow_id or '<empty>'}")
        if not str(record.get("query") or "").strip():
            raise ValueError(f"{record_id}: query is required")
        if not str(record.get("project_id") or "").strip():
            raise ValueError(f"{record_id}: project_id is required")

        if "document_names" in record:
            document_names = record["document_names"]
            if not isinstance(document_names, list):
                raise ValueError(f"{record_id}: document_names must be a list")
            if any(not str(item).strip() for item in document_names):
                raise ValueError(f"{record_id}: document_names must contain non-empty strings")
        for field in ("speech_text", "text_note", "selected_text"):
            if field in record and not isinstance(record[field], str):
                raise ValueError(f"{record_id}: {field} must be a string")
        for field in ("has_audio", "has_image"):
            if field in record and not isinstance(record[field], bool):
                raise ValueError(f"{record_id}: {field} must be boolean")
        for field in ("expected_ready", "expected_needs_rag", "expected_source_diversity"):
            if not isinstance(record.get(field), bool):
                raise ValueError(f"{record_id}: {field} must be boolean")

        expected_profile = str(record.get("expected_profile") or "").strip()
        if not expected_profile:
            raise ValueError(f"{record_id}: expected_profile is required")
        _string_list(record_id, record, "expected_required_sections")
        prefixes = _string_list(record_id, record, "expected_allowed_prefixes")
        if any(prefix.upper() not in SUPPORTED_CITATION_PREFIXES for prefix in prefixes):
            raise ValueError(f"{record_id}: expected_allowed_prefixes contains an unsupported prefix")
        _string_list(record_id, record, "expected_focus_terms")


def _synthetic_contract_answer(required_sections: list[str], prefixes: list[str]) -> str:
    citation = f"[{prefixes[0].upper()}1]"
    return "\n".join(
        line
        for section in required_sections
        for line in (section, f"示例证据 {citation}。")
    )


def score_record(record: dict[str, Any]) -> dict[str, Any]:
    record_id = str(record["id"])
    workflow_id = str(record["workflow_id"])
    plan = plan_workflow(
        workflow_id,
        project_id=str(record.get("project_id") or ""),
        document_names=list(record.get("document_names") or []),
        has_audio=bool(record.get("has_audio")),
        has_image=bool(record.get("has_image")),
        speech_text=str(record.get("speech_text") or ""),
        text_note=str(record.get("text_note") or ""),
        selected_text=str(record.get("selected_text") or ""),
        baseline_run_id=record.get("baseline_run_id"),
        current_run_id=record.get("current_run_id"),
    )
    policy = workflow_retrieval_policy(workflow_id)
    variants = workflow_retrieval_variants(workflow_id, str(record["query"]))
    contract = plan.get("response_contract") or {}
    actual_required_sections = [
        str(item).strip()
        for item in contract.get("required_sections") or []
        if str(item).strip()
    ]
    actual_prefixes = [
        str(item).strip().upper()[:1]
        for item in contract.get("allowed_citation_prefixes") or []
        if str(item).strip()
    ]
    synthetic_answer = _synthetic_contract_answer(actual_required_sections, actual_prefixes)
    output_contract = evaluate_workflow_output_contract(workflow_id, synthetic_answer)
    citation_contract = validate_workflow_citation_namespaces(workflow_id, synthetic_answer)

    expected_focus_terms = [
        str(item).strip() for item in record.get("expected_focus_terms") or [] if str(item).strip()
    ]
    focus_pass = all(
        any(term in variant for variant in variants) for term in expected_focus_terms
    )
    actual_profile = str(policy.get("profile") or "")
    actual_source_diversity = bool(policy.get("requires_source_diversity"))
    required_sections_pass = actual_required_sections == list(record["expected_required_sections"])
    prefix_pass = set(actual_prefixes) == {
        str(item).strip().upper()[:1] for item in record["expected_allowed_prefixes"]
    }
    checks = {
        "role_pass": plan.get("workflow_id") == workflow_id,
        "readiness_pass": bool(plan.get("ready")) == bool(record["expected_ready"]),
        "retrieval_profile_pass": actual_profile == str(record["expected_profile"]),
        "retrieval_focus_pass": focus_pass,
        "output_sections_pass": required_sections_pass,
        "citation_namespace_pass": prefix_pass,
        "output_contract_pass": output_contract.get("status") == "pass",
        "citation_contract_pass": bool(citation_contract.get("valid")),
        "rag_gate_pass": bool(plan.get("routing", {}).get("needs_rag"))
        == bool(record["expected_needs_rag"]),
        "source_diversity_pass": actual_source_diversity
        == bool(record["expected_source_diversity"]),
    }
    return {
        "id": record_id,
        "workflow_id": workflow_id,
        "expected_ready": bool(record["expected_ready"]),
        "actual_ready": bool(plan.get("ready")),
        "missing_inputs": list(plan.get("missing_inputs") or []),
        "retrieval_profile": actual_profile,
        "retrieval_variants": variants,
        "requires_source_diversity": actual_source_diversity,
        "required_sections": actual_required_sections,
        "allowed_citation_prefixes": actual_prefixes,
        "needs_rag": bool(plan.get("routing", {}).get("needs_rag")),
        "workflow_output_contract": output_contract,
        "workflow_citation_contract": citation_contract,
        **checks,
        "passed": all(checks.values()),
    }


def _rate(results: list[dict[str, Any]], field: str) -> float:
    return (
        sum(bool(result[field]) for result in results) / len(results)
        if results
        else 0.0
    )


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    workflow_breakdown: dict[str, dict[str, Any]] = {}
    for workflow_id in sorted({str(result["workflow_id"]) for result in results}):
        group = [result for result in results if result["workflow_id"] == workflow_id]
        profiles = sorted({str(result["retrieval_profile"]) for result in group})
        workflow_breakdown[workflow_id] = {
            "sample_count": len(group),
            "passed": sum(bool(result["passed"]) for result in group),
            "pass_rate": _rate(group, "passed"),
            "readiness_accuracy": _rate(group, "readiness_pass"),
            "retrieval_profile": profiles[0] if len(profiles) == 1 else None,
            "retrieval_profiles": profiles,
            "retrieval_profile_accuracy": _rate(group, "retrieval_profile_pass"),
            "retrieval_focus_accuracy": _rate(group, "retrieval_focus_pass"),
            "output_contract_accuracy": _rate(group, "output_contract_pass"),
            "citation_namespace_accuracy": _rate(group, "citation_contract_pass"),
            "rag_gate_accuracy": _rate(group, "rag_gate_pass"),
            "source_diversity_accuracy": _rate(group, "source_diversity_pass"),
        }

    total = len(results)
    return {
        "total": total,
        "passed": sum(bool(result["passed"]) for result in results),
        "pass_rate": sum(bool(result["passed"]) for result in results) / total if total else 0.0,
        "role_accuracy": _rate(results, "role_pass"),
        "readiness_accuracy": _rate(results, "readiness_pass"),
        "retrieval_profile_accuracy": _rate(results, "retrieval_profile_pass"),
        "retrieval_focus_accuracy": _rate(results, "retrieval_focus_pass"),
        "output_contract_accuracy": _rate(results, "output_contract_pass"),
        "citation_namespace_accuracy": _rate(results, "citation_contract_pass"),
        "rag_gate_accuracy": _rate(results, "rag_gate_pass"),
        "source_diversity_accuracy": _rate(results, "source_diversity_pass"),
        "workflow_breakdown": workflow_breakdown,
    }


def evaluate(records: list[dict[str, Any]]) -> dict[str, Any]:
    validate_records(records)
    results = [score_record(record) for record in records]
    return {
        "schema_version": "souldrive_workflow_role_quality_eval_v0.1",
        "summary": summarize(results),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate SoulDrive research-window contracts.")
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
