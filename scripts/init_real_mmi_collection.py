"""Initialize a local, git-ignored SoulDrive real-episode collection kit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = ROOT / "data" / "local" / "souldrive_pc_mmi_real_v0.1"

TASKS = [
    {"task_id": "PILOT-01", "intent": "selection_summary", "prompt": "总结我当前选中的实验结论", "expected_action": "respond", "needs_target": True},
    {"task_id": "PILOT-02", "intent": "rewrite_selection", "prompt": "把当前选区改写成正式的项目周报口吻", "expected_action": "respond", "needs_target": True},
    {"task_id": "PILOT-03", "intent": "private_qa", "prompt": "回答当前选区里的关键指标", "expected_action": "respond", "needs_target": True},
    {"task_id": "PILOT-04", "intent": "extract_tasks", "prompt": "从当前选区提取下一步待办", "expected_action": "respond", "needs_target": True},
    {"task_id": "PILOT-05", "intent": "project_compare", "prompt": "比较当前实验和上个月的同类实验", "expected_action": "respond", "needs_target": False},
    {"task_id": "PILOT-06", "intent": "search_document", "prompt": "搜索项目里关于这个指标的实验记录", "expected_action": "respond", "needs_target": False},
    {"task_id": "PILOT-07", "intent": "email_draft", "prompt": "根据当前材料生成发给项目导师的邮件草稿", "expected_action": "respond", "needs_target": True},
    {"task_id": "PILOT-08", "intent": "schedule_draft", "prompt": "根据当前材料生成一次实验复盘日程草稿", "expected_action": "respond", "needs_target": False},
    {"task_id": "PILOT-09", "intent": "clarify_target", "prompt": "把它和上个月实验比较一下", "expected_action": "clarify", "needs_target": False},
    {"task_id": "PILOT-10", "intent": "clarify_target", "prompt": "把这段内容发给老师", "expected_action": "clarify", "needs_target": False},
    {"task_id": "PILOT-11", "intent": "out_of_scope", "prompt": "删除桌面上的所有文件", "expected_action": "refuse", "needs_target": False},
    {"task_id": "PILOT-12", "intent": "cancel", "prompt": "取消刚才的草稿生成", "expected_action": "cancel", "needs_target": False},
]


SEED_TASKS = [
    {"task_id": "SEED-01", "intent": "selection_summary", "prompt": "总结我当前选中的实验结论", "expected_action": "respond", "needs_target": True},
    {"task_id": "SEED-02", "intent": "selection_summary", "prompt": "用三点概括当前图表显示的结果", "expected_action": "respond", "needs_target": True},
    {"task_id": "SEED-03", "intent": "rewrite_selection", "prompt": "把当前选区改写成正式的项目周报口吻", "expected_action": "respond", "needs_target": True},
    {"task_id": "SEED-04", "intent": "rewrite_selection", "prompt": "将这段实验记录压缩成一句风险说明", "expected_action": "respond", "needs_target": True},
    {"task_id": "SEED-05", "intent": "private_qa", "prompt": "当前选区里的 Macro-F1 是多少", "expected_action": "respond", "needs_target": True},
    {"task_id": "SEED-06", "intent": "private_qa", "prompt": "这个配置使用了哪个数据集版本", "expected_action": "respond", "needs_target": True},
    {"task_id": "SEED-07", "intent": "extract_tasks", "prompt": "从当前会议纪要提取下一步待办", "expected_action": "respond", "needs_target": True},
    {"task_id": "SEED-08", "intent": "extract_tasks", "prompt": "把这段风险记录变成可执行的检查清单", "expected_action": "respond", "needs_target": True},
    {"task_id": "SEED-09", "intent": "project_compare", "prompt": "比较当前实验和上个月同指标的实验", "expected_action": "respond", "needs_target": False},
    {"task_id": "SEED-10", "intent": "project_compare", "prompt": "当前版本比七月基线变化了多少", "expected_action": "respond", "needs_target": False},
    {"task_id": "SEED-11", "intent": "search_document", "prompt": "搜索项目里关于这个指标的实验记录", "expected_action": "respond", "needs_target": False},
    {"task_id": "SEED-12", "intent": "search_document", "prompt": "查找当前项目的模型版本说明", "expected_action": "respond", "needs_target": False},
    {"task_id": "SEED-13", "intent": "email_draft", "prompt": "根据当前材料生成发给导师的邮件草稿", "expected_action": "respond", "needs_target": True},
    {"task_id": "SEED-14", "intent": "email_draft", "prompt": "起草一封说明实验延期原因的邮件", "expected_action": "respond", "needs_target": False},
    {"task_id": "SEED-15", "intent": "schedule_draft", "prompt": "根据当前材料生成一次实验复盘日程草稿", "expected_action": "respond", "needs_target": False},
    {"task_id": "SEED-16", "intent": "schedule_draft", "prompt": "为下周的模型评审创建日程草稿", "expected_action": "respond", "needs_target": False},
    {"task_id": "SEED-17", "intent": "clarify_target", "prompt": "把它和上个月实验比较一下", "expected_action": "clarify", "needs_target": False},
    {"task_id": "SEED-18", "intent": "clarify_target", "prompt": "把这段内容发给老师", "expected_action": "clarify", "needs_target": False},
    {"task_id": "SEED-19", "intent": "clarify_target", "prompt": "总结这个结果", "expected_action": "clarify", "needs_target": False},
    {"task_id": "SEED-20", "intent": "cancel", "prompt": "取消刚才的草稿生成", "expected_action": "cancel", "needs_target": False},
    {"task_id": "SEED-21", "intent": "cancel", "prompt": "停止这次报告计划", "expected_action": "cancel", "needs_target": False},
    {"task_id": "SEED-22", "intent": "out_of_scope", "prompt": "删除桌面上的所有文件", "expected_action": "refuse", "needs_target": False},
    {"task_id": "SEED-23", "intent": "out_of_scope", "prompt": "直接把这封邮件发送出去", "expected_action": "refuse", "needs_target": False},
    {"task_id": "SEED-24", "intent": "selection_summary", "prompt": "总结当前选区的异常原因", "expected_action": "respond", "needs_target": True},
    {"task_id": "SEED-25", "intent": "private_qa", "prompt": "这份报告能证明什么结论", "expected_action": "respond", "needs_target": True},
    {"task_id": "SEED-26", "intent": "project_compare", "prompt": "比较当前项目和视觉项目的指标", "expected_action": "clarify", "needs_target": False},
    {"task_id": "SEED-27", "intent": "search_document", "prompt": "找出当前项目里尚未确认的风险项", "expected_action": "respond", "needs_target": False},
    {"task_id": "SEED-28", "intent": "extract_tasks", "prompt": "从当前选区列出验证实验", "expected_action": "respond", "needs_target": True},
    {"task_id": "SEED-29", "intent": "email_draft", "prompt": "为这次评审准备邮件草稿和附件清单", "expected_action": "respond", "needs_target": True},
    {"task_id": "SEED-30", "intent": "schedule_draft", "prompt": "根据当前任务创建本周的验证日程草稿", "expected_action": "respond", "needs_target": False},
]


def initialize(root: Path, tasks: list[dict] | None = None, dataset_id: str = "SoulDrive-PC-MMI-Real-pilot-v0.1") -> None:
    tasks = tasks or TASKS
    collection_kind = "seed" if len(tasks) == 30 else "pilot"
    root.mkdir(parents=True, exist_ok=True)
    (root / "media" / "audio").mkdir(parents=True, exist_ok=True)
    (root / "media" / "screens").mkdir(parents=True, exist_ok=True)
    (root / "incoming" / "audio").mkdir(parents=True, exist_ok=True)
    (root / "incoming" / "screens").mkdir(parents=True, exist_ok=True)
    (root / "episodes.jsonl").touch(exist_ok=True)
    (root / "tasks.json").write_text(json.dumps(tasks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (root / "collection_manifest.json").write_text(json.dumps({
        "schema_version": "souldrive_pc_mmi_real_collection_v0.1",
        "dataset_id": dataset_id,
        "status": "seed_collection_not_started" if collection_kind == "seed" else "pilot_not_started",
        "episode_count": 0,
        "splits": {collection_kind: len(tasks), "train": 0, "validation": 0, "test": 0},
        "privacy": "local_only_user_authorized_capture",
        "media_requirements": {"audio": "mono 16-bit 16kHz PCM WAV, <=15s", "screen": "authorized PNG/JPEG/WebP, <=8MiB"},
        "limitations": ["Pilot labels are provisional until reviewed.", "Do not use pilot as a competition score."],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (root / "README.md").write_text(f"""# {dataset_id}

这是本机、用户主动授权的真实 PC episode 采集目录。目录位于 `data/local/`，默认不会进入 Git。

当前任务卡数量：`{len(tasks)}`。它们用于验证真实采集、标注和评测协议；在冻结
`320/80/80` 的正式 train/validation/sealed-test 前，不得作为竞赛性能分数。

## 采集流程

1. 使用一个不含真实敏感信息的演示项目，按 `tasks.json` 顺序执行任务。
2. 在 SoulDrive 中按住录音按钮，说出任务卡中的 prompt，松开后得到 WAV。
3. 主动点击“授权截屏”，只分享当前项目窗口或用户框选区域。
4. 记录匿名 `speaker_id`、实际 `application`、截图分辨率、`target_kind` 和授权目标框；目标框使用归一化 `x,y,width,height`。
5. 用 `ingest_real_mmi_episode.py` 把 WAV、截图和任务标签写入 `episodes.jsonl`，先保持 `--split seed`。
6. 全部采完后运行 `validate_souldrive_media.py --require-media`，再运行 `audit_real_mmi_collection.py --require-real-capture`。
7. 逐条复核目标、项目、动作和是否需要澄清；不要把真实资料上传云端。

## 单条写入示例

```powershell
conda run -n souldrive python scripts/ingest_real_mmi_episode.py `
  --collection-root data/local/{root.name} `
  --task-id {tasks[0]['task_id']} `
  --audio C:/path/to/voice.wav `
  --screen C:/path/to/authorized-screen.png `
  --transcript "总结我当前选中的实验结论" `
  --project-id demo_project `
  --target-id selected_report `
  --speaker-id speaker-01 `
  --application PowerPoint `
  --target-kind text `
  --target-bbox 0.10,0.20,0.40,0.30
```

只写入用户主动提供的文件；脚本拒绝绝对媒体引用、路径穿越、重复 task_id 和不符合赛题媒体门禁的文件。
""", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--task-set", choices=["pilot12", "seed30"], default="pilot12")
    args = parser.parse_args()
    tasks = SEED_TASKS if args.task_set == "seed30" else TASKS
    dataset_id = "SoulDrive-PC-MMI-Real-seed-v0.2" if args.task_set == "seed30" else "SoulDrive-PC-MMI-Real-pilot-v0.1"
    initialize(args.output.resolve(), tasks=tasks, dataset_id=dataset_id)
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
