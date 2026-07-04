import argparse
import json
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "eval" / "learning_loop_v0.1.jsonl"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.memory_runtime import build_memory_capsule
from core.skill_runtime import create_skill_execution_plan, match_skills, normalize_skill_payload


def load_records(path: Path) -> list[dict]:
    records = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            if line.strip():
                records.append(json.loads(line))
    return records


def score_answer(answer: str, expected_keywords: list[str], forbidden_keywords: list[str]) -> dict:
    expected_hits = sum(1 for keyword in expected_keywords if keyword in answer)
    forbidden_hits = sum(1 for keyword in forbidden_keywords if keyword in answer)
    return {
        "expected_hits": expected_hits,
        "expected_total": len(expected_keywords),
        "forbidden_hits": forbidden_hits,
        "forbidden_total": len(forbidden_keywords),
        "passed": expected_hits == len(expected_keywords) and forbidden_hits == 0,
    }


def summarize(results: list[dict]) -> dict:
    total = len(results)
    passed = sum(1 for result in results if result["score"]["passed"])
    average_latency = sum(result["latency_ms"] for result in results) / max(1, total)
    return {
        "total": total,
        "passed": passed,
        "pass_rate": passed / max(1, total),
        "average_latency_ms": round(average_latency, 2),
    }


def run_static_baseline(records: list[dict]) -> list[dict]:
    results = []
    for record in records:
        started = time.time()
        answer = "当前系统未接入长期学习资产。"
        results.append(
            {
                "id": record["id"],
                "metric": record["metric"],
                "answer": answer,
                "score": score_answer(answer, record["expected_keywords"], record["forbidden_keywords"]),
                "latency_ms": int((time.time() - started) * 1000),
            }
        )
    return results


def run_seeded_runtime(records: list[dict]) -> list[dict]:
    results = []
    for record in records:
        started = time.time()
        memories = [
            asset
            for asset in record.get("setup_assets", [])
            if asset.get("asset_type") == "memory"
        ]
        skills = [
            normalize_skill_payload(asset)
            for asset in record.get("setup_assets", [])
            if asset.get("asset_type") == "skill"
        ]
        capsule = build_memory_capsule(memories)
        skill_matches = match_skills(record.get("query") or "", skills, top_k=2)
        skill_execution = None
        if skill_matches:
            skill_execution = create_skill_execution_plan(
                skill_matches[0]["skill"],
                record.get("query") or "",
                matched_score=skill_matches[0]["score"],
            )

        answer = _seeded_runtime_answer(capsule, skill_matches, skill_execution)
        score = score_answer(answer, record["expected_keywords"], record["forbidden_keywords"])
        results.append(
            {
                "id": record["id"],
                "metric": record["metric"],
                "answer": answer,
                "score": score,
                "learning_recall_at_k": int(bool(capsule["items"])) + int(bool(skill_matches)),
                "memory_capsule_hit_rate": 1.0 if capsule["items"] else 0.0,
                "skill_match_precision": 1.0 if skill_matches else 0.0,
                "skill_execution_plan_rate": 1.0 if skill_execution else 0.0,
                "latency_ms": int((time.time() - started) * 1000),
            }
        )
    return results


def compare_baseline_and_runtime(records: list[dict]) -> dict:
    baseline = run_static_baseline(records)
    runtime = run_seeded_runtime(records)
    return {
        "baseline": {"summary": summarize(baseline), "results": baseline},
        "runtime": {"summary": summarize(runtime), "results": runtime},
    }


def _seeded_runtime_answer(capsule: dict, skill_matches: list[dict], skill_execution: dict | None) -> str:
    parts = []
    if capsule.get("text"):
        parts.append(capsule["text"])
    if skill_matches:
        parts.append(f"Matched skill: {skill_matches[0]['title']}")
    if skill_execution:
        parts.extend(step["instruction"] for step in skill_execution.get("steps") or [])
        if skill_execution.get("output_contract"):
            parts.append(skill_execution["output_contract"])
    return "\n".join(str(part) for part in parts if part)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate SoulDrive learning-loop behavior.")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--runtime", action="store_true")
    args = parser.parse_args(argv)

    records = load_records(Path(args.dataset))
    if args.runtime:
        results = run_seeded_runtime(records)
        payload = {"summary": summarize(results), "results": results}
    elif args.baseline:
        results = run_static_baseline(records)
        payload = {"summary": summarize(results), "results": results}
    else:
        payload = compare_baseline_and_runtime(records)
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
