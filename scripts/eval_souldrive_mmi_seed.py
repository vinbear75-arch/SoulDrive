"""Evaluate the minimal text/temporal-rule B1 on the MMI seed set."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "eval" / "schemas"
DEFAULT_DATASET = ROOT / "eval" / "fixtures" / "souldrive_pc_mmi_seed_v0.1" / "episodes.jsonl"
DEFAULT_OUTPUT = ROOT / "eval" / "results" / "souldrive_pc_mmi_seed" / "b1_simple_20260818"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_episodes(path: Path) -> list[dict[str, Any]]:
    validator = Draft202012Validator(read_json(SCHEMA_PATH / "episode_v0.1.schema.json"))
    episodes = []
    for line in path.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        errors = sorted(validator.iter_errors(item), key=lambda error: list(error.path))
        if errors:
            raise ValueError(f"{item.get('episode_id')}: {errors[0].message}")
        episodes.append(item)
    return episodes


def classify(text: str) -> str:
    rules = [
        ("out_of_scope", ("删除桌面", "删除所有", "控制桌面")),
        ("clarify_target", ("发给王老师", "把它和上个月")),
        ("selection_summary", ("总结", "压缩", "提炼")),
        ("rewrite_selection", ("改写", "改成正式", "改成论文")),
        ("project_compare", ("比较", "变化")),
        ("extract_tasks", ("待办", "任务", "下一步")),
        ("schedule_draft", ("日程", "安排到", "安排")),
        ("cancel", ("取消", "不要导出")),
        ("email_draft", ("邮件", "同步邮件")),
        ("search_document", ("搜索", "查找")),
        ("switch_template", ("切换", "模板")),
        ("confirm", ("确认", "可以继续")),
        ("private_qa", ("指标", "多少", "读取当前", "有关吗")),
    ]
    for intent, keywords in rules:
        if any(keyword in text for keyword in keywords):
            return intent
    return "clarify_target"


def infer(episode: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    events = episode["events"]
    speech = next((item for item in events if item["modality"] == "speech_text"), None)
    text = str((speech or {}).get("payload", {}).get("text", ""))
    speech_time = (speech or {}).get("start_ms", 0)
    aligned = [speech["event_id"]] if speech else []
    target_events = []
    all_target_events = []
    for item in events:
        if item is speech:
            continue
        if item["modality"] in {"ui_selection", "ocr_region"} and item["payload"].get("target_id"):
            all_target_events.append(item)
        if abs(item["start_ms"] - speech_time) <= 2500:
            aligned.append(item["event_id"])
            if item["modality"] in {"ui_selection", "ocr_region"} and item["payload"].get("target_id"):
                target_events.append(item)
    target_ids = {item["payload"].get("target_id") for item in target_events}
    project_ids = {item["payload"].get("project_id") for item in target_events if item["payload"].get("project_id")}
    conflict = "project_mismatch" if len(project_ids) > 1 else "none"
    low_confidence = any(item["confidence"] < 0.6 for item in target_events)
    intent = classify(text)
    target_id = target_events[-1]["payload"].get("target_id") if len(target_ids) <= 1 and target_events else None
    clarification = intent == "clarify_target" or conflict != "none" or low_confidence or len(all_target_events) != len(target_events)
    if target_events and not aligned[1:]:
        clarification = True
    if intent == "out_of_scope":
        action = "refuse"
    elif intent in {"confirm", "cancel"}:
        action = intent
    else:
        action = "clarify" if clarification else "respond"
    elapsed = (time.perf_counter() - started) * 1000
    return {
        "schema_version": "souldrive_infer_response_v0.1",
        "episode_id": episode["episode_id"],
        "intent": intent,
        "slots": {},
        "target_id": target_id,
        "aligned_event_ids": aligned,
        "conflict_type": conflict,
        "clarification_required": clarification,
        "action": action,
        "response": "请确认目标或范围" if clarification else None,
        "confidence": 0.55 if clarification else 0.72,
        "latency_ms": {"total": elapsed, "rule_inference": elapsed},
        "model_versions": {"intent": "B1-keyword-v0.1", "alignment": "B1-window-2500ms"},
        "trace_id": None,
    }


def repository_snapshot() -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()

    tracked = subprocess.run(["git", "diff", "--binary", "--no-ext-diff"], cwd=ROOT, check=True, capture_output=True).stdout
    untracked = []
    for raw in git("ls-files", "--others", "--exclude-standard", "-z").split("\0"):
        if not raw:
            continue
        path = ROOT / raw
        if path.is_file():
            untracked.append({"path": Path(raw).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    state = json.dumps({"head": git("rev-parse", "HEAD"), "tracked": hashlib.sha256(tracked).hexdigest(), "untracked": untracked}, sort_keys=True).encode()
    return {
        "head": git("rev-parse", "HEAD"),
        "branch": git("branch", "--show-current"),
        "tracked_diff_sha256": hashlib.sha256(tracked).hexdigest(),
        "untracked_files": untracked,
        "workspace_state_sha256": hashlib.sha256(state).hexdigest(),
    }


def metric_rate(values: list[bool]) -> float:
    return sum(values) / len(values) if values else 0.0


def evaluate(dataset: Path, output: Path) -> None:
    episodes = load_episodes(dataset)
    dataset_id = json.loads((dataset.parent / "manifest.json").read_text(encoding="utf-8"))["dataset_id"]
    output.mkdir(parents=True, exist_ok=True)
    predictions = []
    failures = []
    intent_ok, target_ok, alignment_ok, conflict_ok, action_ok, clarify_ok = [], [], [], [], [], []
    latencies = []
    response_validator = Draft202012Validator(read_json(SCHEMA_PATH / "infer_response_v0.1.schema.json"))
    for item in episodes:
        prediction = infer(item)
        response_errors = list(response_validator.iter_errors(prediction))
        if response_errors:
            raise ValueError(f"{item['episode_id']}: invalid prediction: {response_errors[0].message}")
        truth = item["ground_truth"]
        predictions.append(prediction)
        latencies.append(prediction["latency_ms"]["total"])
        checks = {
            "intent": prediction["intent"] == truth["intent"],
            "target": truth["target_id"] is None or prediction["target_id"] == truth["target_id"],
            "alignment": prediction["aligned_event_ids"] == truth["aligned_event_ids"],
            "conflict": prediction["conflict_type"] == truth["conflict_type"],
            "action": prediction["action"] == truth["expected_action"],
            "clarification": prediction["clarification_required"] == (truth["expected_action"] == "clarify"),
        }
        intent_ok.append(checks["intent"])
        if truth["target_id"] is not None:
            target_ok.append(checks["target"])
        alignment_ok.append(checks["alignment"])
        conflict_ok.append(checks["conflict"])
        action_ok.append(checks["action"])
        clarify_ok.append(checks["clarification"])
        if not all(checks.values()):
            failures.append({"episode_id": item["episode_id"], "checks": checks, "truth": truth, "prediction": prediction})

    metrics = {
        "schema_version": "souldrive_pc_mmi_metrics_v0.1",
        "dataset_id": dataset_id,
        "baseline": "B1_SIMPLE",
        "sample_count": len(episodes),
        "intent_accuracy": metric_rate(intent_ok),
        "target_accuracy_on_targeted": metric_rate(target_ok),
        "alignment_exact_match": metric_rate(alignment_ok),
        "conflict_accuracy": metric_rate(conflict_ok),
        "action_accuracy": metric_rate(action_ok),
        "clarification_accuracy": metric_rate(clarify_ok),
        "latency_ms": {"p50": statistics.median(latencies), "p95": sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], "max": max(latencies)},
        "failure_count": len(failures),
        "limitations": ["Synthetic seed only; no model, audio waveform, screenshot, or public benchmark score."],
    }
    (output / "predictions.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in predictions), encoding="utf-8")
    (output / "failures.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in failures), encoding="utf-8")
    (output / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "schema_version": "souldrive_run_manifest_v0.1",
        "run_id": output.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "baseline_level": "B1_SIMPLE",
        "status": "complete",
        "repository": repository_snapshot(),
        "environment": {"python": sys.version.split()[0], "conda_environment": "souldrive", "hardware": "local PC; see runtime capture for GPU"},
        "inputs": [{"path": dataset.relative_to(ROOT).as_posix(), "sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(), "provenance": "synthetic seed"}],
        "components": {"intent": "keyword rules", "alignment": "2500ms nearest-event window", "conflict": "project-id mismatch and confidence gate"},
        "outputs": {"predictions": "predictions.jsonl", "metrics": "metrics.json", "failures": "failures.jsonl", "summary": "summary.md"},
        "limitations": metrics["limitations"],
    }
    (output / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = "# SoulDrive-PC-MMI seed B1\n\n" + "\n".join(f"- {key}: `{value}`" for key, value in metrics.items() if key not in {"limitations"}) + "\n\n" + "\n".join(f"- {item}" for item in metrics["limitations"]) + "\n"
    (output / "summary.md").write_text(summary, encoding="utf-8")
    print(output)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    evaluate(args.dataset.resolve(), args.output_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
