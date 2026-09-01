"""Build the deterministic 30-episode SoulDrive-PC-MMI seed set.

The seed set is deliberately small: it validates the episode contract and the
evaluation harness before any model training or 480-example expansion.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "eval" / "fixtures" / "souldrive_pc_mmi_seed_v0.1"


def event(
    event_id: str,
    modality: str,
    start_ms: int,
    payload: dict[str, Any],
    *,
    end_ms: int | None = None,
    confidence: float = 0.98,
    privacy_level: str = "project",
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "modality": modality,
        "start_ms": start_ms,
        "end_ms": start_ms if end_ms is None else end_ms,
        "confidence": confidence,
        "privacy_level": privacy_level,
        "payload_ref": None,
        "payload": payload,
    }


def episode(
    episode_id: str,
    template_id: str,
    intent: str,
    speech: str,
    *,
    project_id: str = "project_souldrive_agent",
    target_id: str | None = None,
    slots: dict[str, Any] | None = None,
    expected_action: str = "respond",
    expected_response: str | None = "",
    conflict_type: str = "none",
    aligned_event_ids: list[str] | None = None,
    extra_events: list[dict[str, Any]] | None = None,
    split: str = "seed",
) -> dict[str, Any]:
    speech_event = event("speech", "speech_text", 1000, {"text": speech})
    events = [speech_event]
    if extra_events:
        events.extend(extra_events)
    aligned = aligned_event_ids or ["speech"]
    return {
        "schema_version": "souldrive_episode_v0.1",
        "episode_id": episode_id,
        "template_id": template_id,
        "split": split,
        "project_id": project_id,
        "profile_id": "profile_research_default",
        "events": events,
        "ground_truth": {
            "intent": intent,
            "slots": slots or {},
            "target_id": target_id,
            "aligned_event_ids": aligned,
            "conflict_type": conflict_type,
            "expected_action": expected_action,
            "expected_response": expected_response,
        },
        "provenance": {
            "source": "synthetic_seed_template_v0.1",
            "license": "CC-BY-4.0 synthetic template; no external media",
            "generator": "scripts/build_souldrive_mmi_seed.py",
            "random_seed": 20260818,
            "review_status": "unreviewed",
        },
    }


def build_episodes() -> list[dict[str, Any]]:
    main = "project_souldrive_agent"
    other = "project_vision_optimizer"
    selection = event(
        "selection",
        "ui_selection",
        1100,
        {"text": "八月对齐与冲突门控实验报告", "target_id": "sd_opt_report", "project_id": main},
    )
    ocr = event(
        "ocr",
        "ocr_region",
        1200,
        {"text": "P95 latency 2860 ms; target binding accuracy 0.850", "target_id": "sd_opt_metrics", "project_id": main},
        confidence=0.91,
    )
    screen = event(
        "screen",
        "screen",
        900,
        {"app": "PowerPoint", "window_title": "SoulDrive 对齐实验", "project_id": main},
    )
    profile = event("profile", "profile", 0, {"reply_style": "concise", "preferred_template": "research_advisor"})

    specs = [
        ("001", "selection_summary", "总结我选中的实验报告", "sd_opt_report", [selection], {}),
        ("002", "selection_summary", "把这段实验结论压缩成三句话", "sd_opt_report", [selection], {}),
        ("003", "selection_summary", "提炼选区里的关键指标", "sd_opt_metrics", [ocr], {}),
        ("004", "rewrite_selection", "把选中的结论改写得更适合论文", "sd_opt_report", [selection], {}),
        ("005", "rewrite_selection", "将这段话改成正式的项目周报口吻", "sd_opt_report", [selection], {}),
        ("006", "private_qa", "这个项目的目标绑定准确率是多少", None, [screen], {"metric": "target_binding_accuracy"}),
        ("007", "private_qa", "对齐实验的 P95 延迟是多少", "sd_opt_metrics", [ocr], {"metric": "p95_latency_ms"}),
        ("008", "project_compare", "比较文本基线和对齐门控实验", None, [screen], {"runs": ["text_baseline", "alignment_gate"]}),
        ("009", "project_compare", "找出八月实验相对七月的变化", None, [selection], {"time_relation": "previous_month"}),
        ("010", "extract_tasks", "从选区提取待办事项", "sd_opt_report", [selection], {}),
        ("011", "extract_tasks", "列出这份会议记录的下一步任务", "sd_opt_report", [selection], {}),
        ("012", "schedule_draft", "为下周的模型评审生成日程草案", None, [profile], {"when": "next_week", "topic": "model_review"}),
        ("013", "schedule_draft", "把这个实验复盘安排到周五下午", "sd_opt_report", [selection], {"when": "friday_afternoon"}),
        ("014", "email_draft", "把实验报告邮件草稿发给李老师", "sd_opt_report", [selection, profile], {"recipient": "li_teacher"}),
        ("015", "email_draft", "给项目组写一封简短的结果同步邮件", None, [profile], {"recipient": "project_team"}),
        ("016", "search_document", "搜索项目里关于冲突澄清的记录", None, [screen], {"query": "冲突澄清"}),
        ("017", "search_document", "查找使用 MIntRec2.0 的实验配置", None, [screen], {"query": "MIntRec2.0"}),
        ("018", "switch_template", "切换到研究生答辩汇报模板", None, [profile], {"template": "defense"}),
        ("019", "switch_template", "后续报告都使用简洁研究顾问模板", None, [profile], {"template": "research_advisor"}),
        ("020", "confirm", "确认生成这份项目报告", "sd_opt_report", [selection], {}),
        ("021", "confirm", "可以继续生成 PPT", None, [screen], {}),
        ("022", "cancel", "取消刚才的邮件草稿", None, [profile], {}),
        ("023", "cancel", "不要导出这个报告", "sd_opt_report", [selection], {}),
        ("024", "clarify_target", "把它和上个月实验比较一下", None, [screen], {"reason": "multiple_experiments"}),
        ("025", "clarify_target", "发给王老师", None, [profile], {"reason": "ambiguous_contact"}),
        ("026", "private_qa", "读取当前选区并回答其中的指标", "sd_opt_metrics", [ocr, screen], {"multimodal": True}),
        ("027", "project_compare", "比较当前项目和视觉优化项目的八月结果", None, [
            event("selection_a", "ui_selection", 1100, {"text": "八月优化实验", "target_id": "sd_opt_report", "project_id": main}),
            event("selection_b", "ui_selection", 1400, {"text": "八月优化实验", "target_id": "vision_aug_metrics", "project_id": other}),
        ], {"conflict": "project_mismatch"}),
        ("028", "selection_summary", "总结选中的内容", "sd_opt_report", [
            event("low_ocr", "ocr_region", 900, {"text": "八月对齐实验", "target_id": "sd_opt_report", "project_id": main}, confidence=0.42),
        ], {"low_confidence": True}),
        ("029", "private_qa", "上个月的实验结果和当前项目有关吗", "sd_opt_report", [
            event("late_selection", "ui_selection", 5000, {"text": "七月基线实验报告", "target_id": "sd_baseline_report", "project_id": main}),
        ], {"time_misalignment": True}),
        ("030", "out_of_scope", "帮我删除桌面上的所有文件", None, [screen], {"oos": True}),
    ]
    episodes = []
    for number, intent, speech, target, extra, slots in specs:
        conflict = "project_mismatch" if slots.get("conflict") else "none"
        expected_action = "clarify" if intent == "clarify_target" or slots.get("low_confidence") or slots.get("time_misalignment") or conflict != "none" else "respond"
        if intent in {"confirm", "cancel"}:
            expected_action = intent
        if intent == "out_of_scope":
            expected_action = "refuse"
        aligned = ["speech"] if slots.get("time_misalignment") else ["speech"] + [item["event_id"] for item in extra]
        if slots.get("time_misalignment"):
            target = None
        episodes.append(
            episode(
                f"MMI-{number}",
                f"template_{intent}",
                intent,
                speech,
                target_id=target,
                slots=slots,
                expected_action=expected_action,
                expected_response="请确认目标或范围" if expected_action == "clarify" else None,
                conflict_type=conflict,
                aligned_event_ids=aligned,
                extra_events=extra,
            )
        )
    return episodes


def write_outputs(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    episodes = build_episodes()
    jsonl = output_dir / "episodes.jsonl"
    jsonl.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in episodes), encoding="utf-8")
    digest = hashlib.sha256(jsonl.read_bytes()).hexdigest()
    counts: dict[str, int] = {}
    for item in episodes:
        intent = item["ground_truth"]["intent"]
        counts[intent] = counts.get(intent, 0) + 1
    manifest = {
        "schema_version": "souldrive_pc_mmi_seed_manifest_v0.1",
        "dataset_id": "SoulDrive-PC-MMI-seed-v0.1",
        "description": "30 synthetic episodes for schema, label, and harness validation only.",
        "episode_count": len(episodes),
        "split": "seed",
        "episodes_sha256": digest,
        "intent_counts": dict(sorted(counts.items())),
        "generator": "scripts/build_souldrive_mmi_seed.py",
        "review_status": "unreviewed",
        "limitations": [
            "Synthetic text and structured events; no real audio or screenshots.",
            "Not a public benchmark and not evidence of competition performance.",
            "Seed labels require human review before expansion to train/validation/test.",
        ],
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (output_dir / "human_review.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "episode_id", "speech", "modalities", "gold_intent", "gold_target_id",
            "gold_conflict_type", "gold_expected_action", "decision",
            "corrected_intent", "corrected_target_id", "corrected_action", "notes",
        ])
        writer.writeheader()
        for item in episodes:
            truth = item["ground_truth"]
            speech = next(event["payload"].get("text", "") for event in item["events"] if event["modality"] == "speech_text")
            writer.writerow({
                "episode_id": item["episode_id"],
                "speech": speech,
                "modalities": ",".join(event["modality"] for event in item["events"]),
                "gold_intent": truth["intent"],
                "gold_target_id": truth["target_id"] or "",
                "gold_conflict_type": truth["conflict_type"],
                "gold_expected_action": truth["expected_action"],
                "decision": "",
                "corrected_intent": "",
                "corrected_target_id": "",
                "corrected_action": "",
                "notes": "",
            })
    (output_dir / "human_review_instructions.md").write_text(
        """# SoulDrive-PC-MMI seed 人工审阅说明

请在 `human_review.csv` 中逐行填写，不需要修改 `gold_*` 列。

## 填写规则

- `decision` 只能填 `accept`、`modify` 或 `reject`。
- `accept`：语音、模态目标、意图和动作策略均符合产品边界。
- `modify`：保留该样本，但填写需要修正的 `corrected_*` 字段，并在 `notes` 写明原因。
- `reject`：样本无法在不猜测用户意图的情况下标注，或不属于产品业务；在 `notes` 说明原因。

## 重点检查

1. 选区/OCR 是否真的指向 `gold_target_id`，不要因为文件名相似而默认通过。
2. `clarify` 是否必要：时间错位、项目冲突、联系人歧义和低置信度必须澄清。
3. `email_draft` 只能生成草稿；`confirm`/`cancel` 才能改变执行状态；OOS 必须拒绝。
4. `project_compare` 只有在两个项目或实验对象都明确时才接受；否则改为 `clarify_target`。

审阅完成后只需把 CSV 文件交回；我会根据 `decision` 和 `corrected_*` 生成 v0.2 标签，并重新运行 baseline。
""",
        encoding="utf-8",
    )
    print(output_dir)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    write_outputs(args.output_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
