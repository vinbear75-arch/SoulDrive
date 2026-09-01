"""Evaluate paired answers with and without a confirmed personalization profile.

The evaluator deliberately uses visible markers and citation IDs only. It is a
contract checker for frozen paired samples, not a semantic judge and not an
accuracy claim by itself.
"""

import argparse
import json
import re
from pathlib import Path
from typing import Any


CITATION_PATTERN = re.compile(r"\[((?:E|V|A|M)\d+)\]")
CITATION_ID_PATTERN = re.compile(r"^(?:E|V|A|M)\d+$")
SCHEMA_VERSION = "souldrive_personalization_pair_v0.1"
REQUIRED_FIELDS = {
    "pair_id",
    "question",
    "evidence_ids",
    "baseline_answer",
    "personalized_answer",
    "expected_facts",
    "expected_profile_markers",
    "forbidden_profile_markers",
    "forbidden_scope_markers",
}


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
        raise ValueError("paired records must not be empty")
    seen: set[str] = set()
    for index, record in enumerate(records, start=1):
        missing = sorted(REQUIRED_FIELDS - set(record))
        if missing:
            raise ValueError(f"record {index} missing fields: {', '.join(missing)}")
        pair_id = str(record["pair_id"] or "").strip()
        if not pair_id or pair_id in seen:
            raise ValueError(f"duplicate or empty pair_id: {pair_id or '<empty>'}")
        seen.add(pair_id)
        if record.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"{pair_id}: unsupported schema_version")
        raw_evidence_ids = [str(item) for item in record.get("evidence_ids") or [] if str(item)]
        if len(raw_evidence_ids) != len(set(raw_evidence_ids)):
            raise ValueError(f"{pair_id}: duplicate evidence_ids")
        if not all(CITATION_ID_PATTERN.fullmatch(item) for item in raw_evidence_ids):
            raise ValueError(f"{pair_id}: evidence_ids contain an invalid citation ID")
        evidence_ids = set(raw_evidence_ids)
        if not evidence_ids:
            raise ValueError(f"{pair_id}: evidence_ids must not be empty")
        for answer_key in ("baseline_answer", "personalized_answer"):
            if not isinstance(record.get(answer_key), str):
                raise ValueError(f"{pair_id}: {answer_key} must be a string")


def score_pair(record: dict[str, Any]) -> dict[str, Any]:
    pair_id = str(record["pair_id"])
    evidence_ids = {str(item) for item in record.get("evidence_ids") or []}
    baseline_answer = str(record.get("baseline_answer") or "")
    personalized_answer = str(record.get("personalized_answer") or "")
    baseline_citations = set(CITATION_PATTERN.findall(baseline_answer))
    personalized_citations = set(CITATION_PATTERN.findall(personalized_answer))
    expected_facts = [str(item) for item in record.get("expected_facts") or [] if str(item)]
    expected_profile_markers = [
        str(item) for item in record.get("expected_profile_markers") or [] if str(item)
    ]
    forbidden_profile_markers = [
        str(item) for item in record.get("forbidden_profile_markers") or [] if str(item)
    ]
    forbidden_scope_markers = [
        str(item) for item in record.get("forbidden_scope_markers") or [] if str(item)
    ]

    baseline_facts = {fact: fact in baseline_answer for fact in expected_facts}
    personalized_facts = {fact: fact in personalized_answer for fact in expected_facts}
    facts_invariant = baseline_facts == personalized_facts
    facts_covered = all(personalized_facts.values()) if expected_facts else True
    citations_invariant = baseline_citations == personalized_citations
    citations_present = bool(baseline_citations) and bool(personalized_citations)
    citations_allowed = baseline_citations <= evidence_ids and personalized_citations <= evidence_ids
    profile_adherence = all(marker in personalized_answer for marker in expected_profile_markers)
    forbidden_profile_rejected = all(marker not in personalized_answer for marker in forbidden_profile_markers)
    scope_leakage = any(marker in personalized_answer for marker in forbidden_scope_markers)

    return {
        "pair_id": pair_id,
        "baseline_citations": sorted(baseline_citations),
        "personalized_citations": sorted(personalized_citations),
        "factual_invariance": facts_invariant,
        "factual_coverage": facts_covered,
        "citation_invariance": citations_invariant and citations_present and citations_allowed,
        "preference_adherence": profile_adherence,
        "irrelevant_preference_rejection": forbidden_profile_rejected,
        "cross_project_leakage": scope_leakage,
        "passed": all(
            (
                facts_invariant,
                facts_covered,
                citations_invariant,
                citations_present,
                citations_allowed,
                profile_adherence,
                forbidden_profile_rejected,
                not scope_leakage,
            )
        ),
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    if not total:
        return {
            "total": 0,
            "passed": 0,
            "pass_rate": 0.0,
            "preference_adherence": 0.0,
            "factual_invariance": 0.0,
            "citation_invariance": 0.0,
            "irrelevant_preference_rejection": 0.0,
            "cross_project_leakage_rate": 0.0,
        }
    return {
        "total": total,
        "passed": sum(1 for result in results if result["passed"]),
        "pass_rate": sum(1 for result in results if result["passed"]) / total,
        "preference_adherence": sum(bool(result["preference_adherence"]) for result in results) / total,
        "factual_invariance": sum(bool(result["factual_invariance"]) for result in results) / total,
        "citation_invariance": sum(bool(result["citation_invariance"]) for result in results) / total,
        "irrelevant_preference_rejection": sum(
            bool(result["irrelevant_preference_rejection"]) for result in results
        ) / total,
        "cross_project_leakage_rate": sum(bool(result["cross_project_leakage"]) for result in results) / total,
    }


def evaluate(records: list[dict[str, Any]]) -> dict[str, Any]:
    validate_records(records)
    results = [score_pair(record) for record in records]
    return {
        "schema_version": "souldrive_personalization_pair_eval_v0.1",
        "summary": summarize(results),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate paired personalization answers.")
    parser.add_argument("--dataset", required=True, help="Frozen paired JSONL dataset.")
    parser.add_argument("--output", default=None, help="Optional JSON report path.")
    args = parser.parse_args(argv)
    report = evaluate(load_records(Path(args.dataset)))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
