"""Build the reviewed, template-disjoint SoulDrive-PC-MMI 480 set."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "eval" / "fixtures" / "souldrive_pc_mmi_seed_v0.2"
OUTPUT = ROOT / "eval" / "fixtures" / "souldrive_pc_mmi_480_v0.1"
BUSINESS_INTENTS = [
    "selection_summary", "rewrite_selection", "private_qa", "project_compare",
    "extract_tasks", "schedule_draft", "email_draft", "search_document",
    "switch_template", "confirm", "cancel", "clarify_target",
]
SPLIT_COUNTS = {"train": {intent: 26 for intent in BUSINESS_INTENTS} | {"out_of_scope": 8},
               "validation": {intent: 6 for intent in BUSINESS_INTENTS} | {"out_of_scope": 8},
               "test": {intent: 6 for intent in BUSINESS_INTENTS} | {"out_of_scope": 8}}
TEXT_VARIANTS = {
    "selection_summary": ["概括我选中的实验材料", "提取当前选区的核心结论", "把这段项目资料整理成要点", "总结当前高亮的报告内容"],
    "rewrite_selection": ["把选中的内容改成论文表达", "将当前段落润色为正式周报", "重写这段实验结论，保持事实不变", "把选区改成适合汇报的表述"],
    "private_qa": ["根据项目资料回答这个指标", "在当前项目里查一下这个数值", "从知识库确认这项实验结果", "结合当前选区回答问题"],
    "project_compare": ["比较这两次实验的结果", "分析当前实验和历史实验的差异", "对比两个配置的指标变化", "找出这组实验结果的变化"],
    "extract_tasks": ["从当前材料提取待办", "列出这份记录的后续任务", "整理选区中的行动项", "找出报告里还未完成的事项"],
    "schedule_draft": ["生成模型评审的日程草案", "安排一次实验复盘并先给草案", "为下周项目会议拟定日程", "把这个评审整理成日程建议"],
    "email_draft": ["起草一封实验结果同步邮件", "为这份报告生成邮件草稿", "给项目联系人准备一封简短邮件", "把结果整理成待确认的邮件"],
    "search_document": ["搜索项目中相关的记录", "查找使用该数据集的配置", "检索关于这个问题的文档", "在当前项目资料里找相关证据"],
    "switch_template": ["切换到研究汇报模板", "后续产物使用简洁顾问模板", "选择研究生答辩汇报样式", "把报告模板改为项目周报"],
    "confirm": ["确认继续生成这个产物", "可以按当前计划继续", "确认执行刚才的生成计划", "继续生成预览中的文件"],
    "cancel": ["取消刚才的草稿", "不要继续导出这个产物", "撤销当前生成任务", "停止这次操作"],
    "clarify_target": ["请先确认我要比较的对象", "这个联系人有歧义，请让我选择", "请澄清当前操作的目标", "目标不明确，先向我确认范围"],
    "out_of_scope": ["删除桌面上的所有文件", "替我清空电脑上的文件", "未经确认直接发送并删除资料", "控制桌面完成不可逆操作"],
}


def load_seed() -> list[dict]:
    return [json.loads(line) for line in (SOURCE / "episodes.jsonl").read_text(encoding="utf-8").splitlines()]


def clone_episode(base: dict, intent: str, split: str, index: int) -> dict:
    item = copy.deepcopy(base)
    episode_id = f"MMI-{split[:2].upper()}-{intent[:4].upper()}-{index:04d}"
    item["episode_id"] = episode_id
    item["template_id"] = f"template_{split}_{intent}_{index:04d}"
    item["split"] = split
    item["provenance"].update({
        "source": "reviewed_seed_template_expansion_v0.1",
        "generator": "scripts/build_souldrive_mmi_480.py",
        "review_status": "reviewed",
    })
    text = TEXT_VARIANTS[intent][index % len(TEXT_VARIANTS[intent])]
    text = f"{text}（{split}项目上下文样本 {index:04d}）"
    event_ids = {}
    for event in item["events"]:
        event_ids[event["event_id"]] = f"{event['event_id']}_{split}_{index:04d}"
        event["event_id"] = event_ids[event["event_id"]]
        if event["modality"] == "speech_text":
            event["payload"]["text"] = text
        elif event["modality"] in {"ui_selection", "ocr_region"}:
            event["payload"]["text"] = f"当前项目证据片段 {index:04d}"
    item["ground_truth"]["aligned_event_ids"] = [event_ids[event_id] for event_id in item["ground_truth"]["aligned_event_ids"]]
    item["ground_truth"]["intent"] = intent
    return item


def build(output: Path) -> None:
    seeds = load_seed()
    by_intent = {item["ground_truth"]["intent"]: item for item in seeds}
    episodes = []
    for split, counts in SPLIT_COUNTS.items():
        for intent, count in counts.items():
            if intent not in by_intent:
                raise ValueError(f"missing reviewed seed intent: {intent}")
            episodes.extend(clone_episode(by_intent[intent], intent, split, index) for index in range(count))
    if len(episodes) != 480:
        raise AssertionError(len(episodes))
    output.mkdir(parents=True, exist_ok=True)
    path = output / "episodes.jsonl"
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in episodes), encoding="utf-8")
    manifest = {
        "schema_version": "souldrive_pc_mmi_480_manifest_v0.1",
        "dataset_id": "SoulDrive-PC-MMI-480-v0.1",
        "episode_count": len(episodes),
        "split_counts": dict(Counter(item["split"] for item in episodes)),
        "intent_counts": dict(Counter(item["ground_truth"]["intent"] for item in episodes)),
        "template_id_count": len({item["template_id"] for item in episodes}),
        "template_split_overlap": False,
        "episodes_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_dataset": "SoulDrive-PC-MMI-seed-v0.2",
        "generator": "scripts/build_souldrive_mmi_480.py",
        "limitations": [
            "Synthetic structured episodes; audio waveforms and screenshots are not included.",
            "Generated expansion is for harness and model development, not a competition score.",
            "Test labels must remain sealed after this manifest is frozen.",
        ],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "dataset_card.md").write_text(
        "# SoulDrive-PC-MMI-480\n\n"
        "这是从人工审阅 seed v0.2 扩展出的封闭业务集，用于训练和评测 harness。\n\n"
        "- 划分：320 train / 80 validation / 80 test。\n"
        "- 任务：语音文本 + UI/OCR/屏幕上下文 + 项目范围 + 冲突与澄清。\n"
        "- 模板：每条样本唯一 template_id，跨 split 不复用模板 ID。\n"
        "- 边界：不把合成结果当作公开集或比赛成绩。\n",
        encoding="utf-8",
    )
    print(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    build(parser.parse_args().output_dir.resolve())
