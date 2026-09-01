"""Build the first auditable product corpus for SoulDrive PC.

The files are synthetic public demo material, not a training set and not user data.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.project_catalog import ProjectCatalog
from core.document_types import SUPPORTED_DOCUMENT_EXTENSIONS
from core.workspace import SoulDriveWorkspace
DEFAULT_CORPUS = ROOT / "eval" / "fixtures" / "product_b1_v0.1"
MANIFEST_NAME = "product_benchmark_manifest_v0.1.json"


def asset(
    asset_id: str,
    project_id: str,
    title: str,
    asset_type: str,
    filename: str,
    text: str,
    *,
    performed_at: str | None = None,
    experiment: str | None = None,
    dataset_version: str = "",
    valid_from: str = "2026-08-17T12:00:00+08:00",
) -> dict[str, Any]:
    return {
        "asset_id": asset_id,
        "version_id": f"{asset_id}_v1",
        "project_id": project_id,
        "title": title,
        "asset_type": asset_type,
        "filename": filename,
        "text": text,
        "performed_at": performed_at,
        "experiment": experiment,
        "dataset_version": dataset_version,
        "valid_from": valid_from,
        "authored_at": performed_at or valid_from,
        "source_kind": "synthetic_public_demo",
    }


def corpus_assets() -> list[dict[str, Any]]:
    main = "project_souldrive_agent"
    other = "project_vision_optimizer"
    return [
        asset("sd_requirement", main, "端侧多模态智能体需求说明", "requirement", "01_requirement.pdf", "项目目标：在 PC 端完成语音、选区和项目上下文理解。验收目标为单轮 P95 小于 5 秒，所有邮件仅生成草案。"),
        asset("sd_architecture", main, "SoulAlign 技术方案", "technical_plan", "02_architecture.pdf", "采用语音、系统选区和当前窗口三类输入。先做结构化项目过滤，再做证据检索；低置信度或冲突时主动澄清。"),
        asset("sd_dataset", main, "MIntRec2.0 与 ScreenSpot 数据审计", "dataset_note", "03_dataset_audit.pdf", "公开主基准为 MIntRec2.0，ScreenSpot 只用于屏幕目标定位。两者不用于证明私有项目知识问答。"),
        asset("sd_baseline_report", main, "七月文本检索基线实验报告", "experiment_report", "07_baseline_report.pdf", "实验结论：文本检索基线在 MIntRec2.0 验证集 Macro-F1 为 0.741；SoulDrive-PC-MMI 目标绑定准确率为 0.620。", performed_at="2026-07-12T10:00:00+08:00", experiment="text_baseline", dataset_version="MIntRec2.0-v1"),
        asset("sd_baseline_config", main, "七月文本检索基线配置", "experiment_config", "07_baseline_config.yaml", "model: Qwen2.5-7B-Instruct\nretrieval: lexical\ncontext: text_only\ndataset: MIntRec2.0-v1", performed_at="2026-07-12T10:00:00+08:00", experiment="text_baseline", dataset_version="MIntRec2.0-v1"),
        asset("sd_baseline_metrics", main, "七月文本检索基线指标", "metrics", "07_baseline_metrics.csv", "metric,value\nmacro_f1,0.741\ntarget_binding_accuracy,0.620\np95_latency_ms,1840", performed_at="2026-07-12T10:00:00+08:00", experiment="text_baseline", dataset_version="MIntRec2.0-v1"),
        asset("sd_baseline_log", main, "七月文本检索基线训练日志", "training_log", "07_baseline_training.log", "2026-07-12 INFO epoch=3 validation_macro_f1=0.741\n2026-07-12 INFO no screen context used", performed_at="2026-07-12T10:00:00+08:00", experiment="text_baseline", dataset_version="MIntRec2.0-v1"),
        asset("sd_ui_report", main, "八月屏幕上下文实验报告", "experiment_report", "08_ui_context_report.pdf", "实验结论：加入系统可读选区与当前窗口后，目标绑定准确率提升至 0.790；Macro-F1 为 0.758。", performed_at="2026-08-05T14:00:00+08:00", experiment="ui_context", dataset_version="MIntRec2.0-v1"),
        asset("sd_ui_config", main, "八月屏幕上下文配置", "experiment_config", "08_ui_context_config.json", '{"ui_selection":"uia","ocr_fallback":"explicit_region","alignment_window_ms":2500}', performed_at="2026-08-05T14:00:00+08:00", experiment="ui_context", dataset_version="MIntRec2.0-v1"),
        asset("sd_ui_metrics", main, "八月屏幕上下文指标", "metrics", "08_ui_context_metrics.xlsx", "metric,value\nmacro_f1,0.758\ntarget_binding_accuracy,0.790\np95_latency_ms,2310", performed_at="2026-08-05T14:00:00+08:00", experiment="ui_context", dataset_version="MIntRec2.0-v1"),
        asset("sd_opt_report", main, "八月对齐与冲突门控优化报告", "experiment_report", "08_alignment_report.pdf", "实验结论：时间对齐、冲突门控和偏好模板使目标绑定准确率达到 0.850，冲突澄清准确率达到 0.880，P95 为 2860ms。", performed_at="2026-08-16T16:00:00+08:00", experiment="alignment_gate", dataset_version="SoulDrive-PC-MMI-seed-v1"),
        asset("sd_opt_config", main, "八月对齐与冲突门控配置", "experiment_config", "08_alignment_config.yaml", "alignment_window_ms: 2500\nconflict_threshold: 0.72\nprofile_template: research_advisor", performed_at="2026-08-16T16:00:00+08:00", experiment="alignment_gate", dataset_version="SoulDrive-PC-MMI-seed-v1"),
        asset("sd_opt_metrics", main, "八月对齐与冲突门控指标", "metrics", "08_alignment_metrics.csv", "metric,value\ntarget_binding_accuracy,0.850\nconflict_clarification_accuracy,0.880\np95_latency_ms,2860", performed_at="2026-08-16T16:00:00+08:00", experiment="alignment_gate", dataset_version="SoulDrive-PC-MMI-seed-v1"),
        asset("sd_meeting", main, "八月评审会议纪要", "meeting_minutes", "08_review_minutes.docx", "评审确认：邮件功能只生成草案，不允许自动发送。下一步冻结 30 条种子 episode，再扩展自建集。", performed_at="2026-08-17T11:00:00+08:00"),
        asset("sd_decision", main, "多模态主链路技术决策", "decision", "08_decision.pdf", "决定使用语音加系统选区作为主链路，用户框选 OCR 为降级路径；不常驻录屏，不将人体情绪识别纳入首版。", performed_at="2026-08-17T12:00:00+08:00"),
        asset("sd_task", main, "项目待办清单", "task_list", "08_tasks.md", "- [ ] 冻结 MIntRec2.0 官方划分\n- [ ] 完成 B1 多格式导入基线\n- [ ] 生成 30 条多模态种子 episode"),
        asset("sd_screen", main, "选区绑定界面示意", "screenshot", "08_selection_ui.png", "界面截图：当前项目为端侧多模态智能体优化项目，用户选中实验指标表中的 target_binding_accuracy。"),
        asset("sd_chart", main, "三轮实验指标图", "chart", "08_metrics_chart.png", "指标图：目标绑定准确率从 0.620 增至 0.790，再增至 0.850。"),
        asset("sd_ppt_template", main, "科研项目答辩模板", "presentation_template", "presentation_template.pptx", "PPT 模板：封面、问题、方法、实验、消融、风险、计划七页结构。"),
        asset("sd_report_template", main, "实验报告模板", "report_template", "report_template.md", "# 实验报告\n## 目标\n## 配置\n## 指标\n## 证据\n## 风险与下一步"),
        asset("vis_plan", other, "视觉模型优化技术方案", "technical_plan", "vision_plan.pdf", "视觉模型优化项目目标：改进工业缺陷识别准确率，验证集为 VisionSet-v2。"),
        asset("vis_jul_report", other, "七月基线实验报告", "experiment_report", "vision_july_report.pdf", "视觉模型七月基线：VisionSet-v2 验证集准确率 0.889，批大小 8。", performed_at="2026-07-11T09:00:00+08:00", experiment="vision_baseline", dataset_version="VisionSet-v2"),
        asset("vis_jul_config", other, "七月基线配置", "experiment_config", "vision_july_config.yaml", "model: vision-transformer\nbatch_size: 8\ndataset: VisionSet-v2", performed_at="2026-07-11T09:00:00+08:00", experiment="vision_baseline", dataset_version="VisionSet-v2"),
        asset("vis_jul_metrics", other, "七月基线指标", "metrics", "vision_july_metrics.csv", "metric,value\naccuracy,0.889\np95_latency_ms,920", performed_at="2026-07-11T09:00:00+08:00", experiment="vision_baseline", dataset_version="VisionSet-v2"),
        asset("vis_aug_report", other, "八月优化实验报告", "experiment_report", "vision_august_report.pdf", "视觉模型八月优化：VisionSet-v2 验证集准确率 0.921，批大小 8。", performed_at="2026-08-05T10:00:00+08:00", experiment="vision_optimized", dataset_version="VisionSet-v2"),
        asset("vis_aug_metrics", other, "八月优化指标", "metrics", "vision_august_metrics.xlsx", "metric,value\naccuracy,0.921\np95_latency_ms,960", performed_at="2026-08-05T10:00:00+08:00", experiment="vision_optimized", dataset_version="VisionSet-v2"),
        asset("vis_meeting", other, "八月评审会议纪要", "meeting_minutes", "vision_review.docx", "评审决定继续使用 VisionSet-v2，并在下一轮验证误检类别。", performed_at="2026-08-07T16:00:00+08:00"),
        asset("vis_screen", other, "视觉误检分析图", "chart", "vision_confusion_matrix.png", "混淆矩阵截图：类别 C 的误检比例仍然偏高。"),
        asset("missing_date", main, "日期缺失的实验笔记", "experiment_note", "undated_note.txt", "实验笔记：尝试了更小的时间窗口，目标绑定似乎改善；未记录实验日期和数据集版本。"),
        asset("ambiguous_metric", main, "指标定义不完整的记录", "experiment_note", "ambiguous_metric.txt", "记录：准确率为 85，但未写明单位、划分和数据集版本，不允许做严格数值比较。"),
    ]


def _pdf_bytes(title: str, text: str) -> bytes:
    import pymupdf

    content = title + "\n\n" + text
    document = pymupdf.open()
    page = document.new_page()
    font_candidates = [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/simsun.ttc"),
    ]
    fontfile = next((str(path) for path in font_candidates if path.exists()), None)
    kwargs = {"fontsize": 11}
    if fontfile:
        kwargs["fontfile"] = fontfile
        kwargs["fontname"] = "zhfont"
    page.insert_textbox(pymupdf.Rect(48, 48, 560, 740), content[:4000], **kwargs)
    payload = document.tobytes()
    document.close()
    return payload


def _png_bytes() -> bytes:
    # Keep a real, readable text surface so the OCR baseline tests a screenshot rather than a blank placeholder.
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (900, 300), "white")
    draw = ImageDraw.Draw(image)
    draw.text((24, 24), "SoulDrive PC project workspace", fill="black")
    draw.text((24, 88), "target_binding_accuracy 0.620 -> 0.790 -> 0.850", fill="black")
    draw.text((24, 152), "conflict_clarification_accuracy 0.880", fill="black")
    draw.text((24, 216), "dataset SoulDrive-PC-MMI-seed-v1", fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _office_bytes(suffix: str, text: str) -> bytes:
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if suffix == ".docx":
            archive.writestr("word/document.xml", f"<document><body><p><t>{escaped}</t></p></body></document>")
        elif suffix == ".pptx":
            archive.writestr("ppt/slides/slide1.xml", f"<slide><txBody><p><t>{escaped}</t></p></txBody></slide>")
        else:
            archive.writestr("xl/worksheets/sheet1.xml", f"<worksheet><sheetData><row><c><v>{escaped}</v></c></row></sheetData></worksheet>")
    return buffer.getvalue()


def write_asset(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        path.write_bytes(_pdf_bytes(record["title"], record["text"]))
    elif suffix == ".png":
        path.write_bytes(_png_bytes())
    elif suffix in {".docx", ".xlsx", ".pptx"}:
        path.write_bytes(_office_bytes(suffix, record["text"]))
    else:
        path.write_text(record["text"] + "\n", encoding="utf-8")


def build(corpus_root: Path, workspace_root: Path) -> dict[str, Any]:
    if corpus_root.exists():
        shutil.rmtree(corpus_root)
    assets = corpus_assets()
    for record in assets:
        record["relative_path"] = str(Path(record["project_id"]) / record["filename"]).replace("\\", "/")
        write_asset(corpus_root / record["relative_path"], record)
        record["sha256"] = hashlib.sha256((corpus_root / record["relative_path"]).read_bytes()).hexdigest()
        record["supported_by_current_indexer"] = Path(record["filename"]).suffix.lower() in SUPPORTED_DOCUMENT_EXTENSIONS

    workspace = SoulDriveWorkspace(str(workspace_root)).ensure()
    for record in assets:
        source = corpus_root / record["relative_path"]
        target = Path(workspace.papers_path) / record["relative_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    catalog = ProjectCatalog(workspace)
    try:
        projects = {item["project_id"] for item in assets}
        for project_id in sorted(projects):
            catalog.create_project(
                "端侧多模态智能体优化项目" if project_id == "project_souldrive_agent" else "视觉模型优化项目",
                project_id=project_id,
                created_at="2026-07-01T09:00:00+08:00",
            )
        created_experiments: set[str] = set()
        created_runs: set[str] = set()
        for record in assets:
            catalog.create_asset(record["project_id"], record["title"], record["asset_type"], asset_id=record["asset_id"], source_kind=record["source_kind"])
            catalog.create_asset_version(record["asset_id"], record["sha256"], record["relative_path"], version_id=record["version_id"], valid_from=record["valid_from"], authored_at=record["authored_at"], imported_at="2026-08-18T09:00:00+08:00", metadata={"corpus": "product_b1_v0.1", "supported_by_current_indexer": record["supported_by_current_indexer"]})
            if record["performed_at"] and record["experiment"]:
                experiment_id = f"experiment_{record['project_id']}_{record['experiment']}"
                if experiment_id not in created_experiments:
                    catalog.create_experiment(record["project_id"], record["experiment"], experiment_id=experiment_id)
                    created_experiments.add(experiment_id)
                run_key = f"{experiment_id}:{record['performed_at']}"
                if run_key not in created_runs:
                    catalog.create_experiment_run(experiment_id, record["performed_at"], run_id=f"run_{record['version_id']}", evidence_version_id=record["version_id"], dataset_version=record["dataset_version"])
                    created_runs.add(run_key)
    finally:
        catalog.close()

    manifest = {
        "schema_version": "souldrive_product_benchmark_manifest_v0.1",
        "name": "SoulDrive B1 Product Corpus",
        "purpose": "Synthetic public demo corpus for product baseline; not public benchmark or training data.",
        "corpus_root": str(corpus_root),
        "workspace_root": str(workspace_root),
        "projects": ["project_souldrive_agent", "project_vision_optimizer"],
        "asset_count": len(assets),
        "supported_asset_count": sum(item["supported_by_current_indexer"] for item in assets),
        "assets": assets,
    }
    (corpus_root / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the SoulDrive B1 product demo corpus.")
    parser.add_argument("--corpus-root", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--workspace-root", type=Path, required=True)
    args = parser.parse_args()
    manifest = build(args.corpus_root.resolve(), args.workspace_root.resolve())
    print(json.dumps({key: manifest[key] for key in ("asset_count", "supported_asset_count", "projects")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
