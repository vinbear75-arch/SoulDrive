import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


POSITIONING = (
    "用户指着屏幕中的实验结果说“为什么比上个月差”，SoulDrive 在当前项目内对齐语音、"
    "授权选区、实验版本和历史偏好，在端侧给出可验证差异、原因证据等级和最小验证实验，"
    "并生成带引用的汇报草稿；歧义时先澄清，未经确认不执行外部动作。"
)

EVIDENCE_FILES = [
    ("docs/plans/2026-08-29-souldrive-competition-value-roadmap.md", "执行路线图"),
    ("README.md", "项目主文档定位"),
    ("docs/demo_runbook_v0.1.md", "3 分钟演示 runbook"),
    ("docs/known_issues_v0.1.md", "限制与问题清单"),
    ("docs/product_b1_corpus_and_baseline.md", "产品 B1 语料与 baseline"),
    ("eval/results/competition_scorecard_v0.1.json", "竞赛分项 scorecard"),
    ("eval/results/competition_release_gate_current.json", "旧版 release gate 快照"),
    ("eval/results/rag_rebuild_baseline_v0.1.json", "RAG 冻结 baseline"),
    ("eval/results/product_b1_scoped_current_20260830.jsonl", "项目作用域产品 RAG 结果"),
    ("eval/results/rag_global_scope_answer_recheck_20260830.json", "全局文档跨项目最终回答回归"),
    ("eval/results/rag_xlsx_table_ingestion_smoke_recheck_20260830.json", "XLSX 表格摄取烟测"),
    ("eval/results/rag_structured_source_locator_recheck_20260830.json", "结构化来源定位回归"),
    ("eval/results/real_mmi_stage_ledger_recheck_20260830.json", "真实多模态阶段账本回归"),
    ("eval/results/multimodal_structured_event_contract_recheck_20260830.json", "多模态事件契约回归"),
]

CONFIG_AND_DATA_FILES = [
    ("configs/soulalign_lite_v0.1.json", "SoulAlign-lite 配置"),
    ("requirements.txt", "Python 依赖"),
    ("requirements-topic5-gpu.txt", "Topic5 GPU 依赖"),
    ("souldrive-ui/package.json", "前端依赖声明"),
    ("souldrive-ui/src-tauri/Cargo.toml", "Tauri/Rust 依赖声明"),
    ("eval/fixtures/product_b1_current_v0.1/product_benchmark_manifest_v0.1.json", "产品 B1 当前语料 manifest"),
    ("eval/fixtures/product_b1_current_v0.1/product_rag_eval_v0.1.jsonl", "产品 B1 RAG 评测集"),
    ("eval/fixtures/souldrive_pc_mmi_media_480_v0.2/manifest.json", "多模态媒体集 manifest"),
    ("eval/fixtures/souldrive_pc_mmi_media_480_v0.2/episodes.jsonl", "多模态媒体 episode"),
]

STAGE_AUDIT = [
    {
        "stage": "S0",
        "name": "冻结定位与评测契约",
        "status": "partial",
        "done": [
            "一句话定位已写入 README、演示 runbook 和 closeout 快照。",
            "主场景、B0/B1/Candidate 和证据边界已有文档化。",
            "Git HEAD、未提交 diff 哈希、配置与数据哈希由 closeout 脚本生成。",
        ],
        "gap": [
            "PPT 首页是否已更新需要人工检查最终参赛 PPT 文件。",
            "sealed-test 最终冻结和演示材料冻结仍需提交前人工确认。",
        ],
    },
    {
        "stage": "S1",
        "name": "真实数据与完整端到端基线",
        "status": "blocked",
        "done": [
            "已有 10 条真实授权 pilot 和合成/媒体回归证据。",
            "已有阶段账本和部分 text-only/full-multimodal 对比记录。",
        ],
        "gap": [
            "不足 30 条独立真实业务 episode。",
            "缺少跨说话人、分辨率、项目、指代方式的完整冻结集。",
            "缺少完整 ASR/OCR/VLM/对齐/检索/首 token/完整回答逐样本 P50/P95。",
        ],
    },
    {
        "stage": "S2",
        "name": "实验账本与严格比较",
        "status": "partial",
        "done": [
            "CSV/XLSX、YAML/JSON、日志和实验报告摄取已落地。",
            "measurement 版本化、项目隔离和来源定位已有回归。",
            "结构化指标可带 sheet/row/line 来源进入证据链。",
        ],
        "gap": [
            "确认卡 UI 对日期、单位、数据集版本和 split 的完整人工流程仍需演示机复核。",
            "复杂实验原因分析仍不能宣称因果结论，只能给证据等级和验证计划。",
        ],
    },
    {
        "stage": "S3",
        "name": "SoulAlign-T 与 GUI 目标验证",
        "status": "partial",
        "done": [
            "UIA-first、OCR 降级、授权裁剪 VLM、多候选 verifier、低置信澄清均已有实现/回归。",
            "多模态事件 start/end/project/time origin 已贯通前端、API 和归一化层。",
        ],
        "gap": [
            "VLM 慢路径完整回答 P95 未稳定小于 5 秒。",
            "图标目标相对 ScreenSpot 约 1.9% 基线的显著提升仍未证明。",
        ],
    },
    {
        "stage": "S4",
        "name": "可验证个性化",
        "status": "partial",
        "done": [
            "结构化偏好、全局/项目偏好、查看修改删除和成对画像评测已有证据。",
            "事实和引用不应被个性化改写的边界已有测试约束。",
        ],
        "gap": [
            "真实 30 episode 上的成对画像收益、撤销残留率和人工事实复核仍未完成。",
        ],
    },
    {
        "stage": "S5",
        "name": "证据式生成与项目 Playbook",
        "status": "partial",
        "done": [
            "候选摘要/支持度、检索测试 UI、版本化 Playbook、确认门和产物来源记录已有实现/回归。",
            "全局文档跨项目最终回答和结构化来源定位已回归。",
        ],
        "gap": [
            "长论文和标准的真实层级文档树路由仍需在生产加密工作区重建后验收。",
            "产物级 E/V/A/M 引用语义仍需人工或 NLI 评测。",
        ],
    },
    {
        "stage": "S6",
        "name": "竞赛冻结与回归",
        "status": "partial",
        "done": [
            "closeout 快照、声明清单、限制清单和关键回归已经生成。",
            "公开/合成/真实业务/隐藏集结果边界已分开披露。",
        ],
        "gap": [
            "最终真实演示视频需要人工录制。",
            "正式提交包的代码、模型、配置、数据和随机种子需要在提交前最后冻结。",
            "Tauri cargo check 和真实端到端演示仍需在最终演示机上确认。",
        ],
    },
]

PASSABLE_CLAIMS = [
    "端侧 Windows PC 本地运行，不默认依赖云端推理。",
    "支持语音、授权屏幕/UIA/OCR、项目上下文和 RAG 证据的组合链路。",
    "项目内优先检索并支持全局文档回退，已有跨项目私有来源隔离回归。",
    "实验指标 CSV/XLSX、配置、日志和文档能带来源定位进入证据链。",
    "邮件、日历、删除和外部动作保留确认门，不自动执行不可逆动作。",
]

FORBIDDEN_CLAIMS = [
    "不能宣称通过外部未公开评测或获得官方成绩。",
    "不能把 targeted tests、烟测或合成集称为整体答案准确率。",
    "不能宣称 GraphRAG 生产业务验收已经完成。",
    "不能宣称 VLM 慢路径完整回答 P95 已稳定小于 5 秒。",
    "不能宣称已有 30 条以上独立真实多模态业务 episode。",
]

BLOCKERS = [
    "正式加密工作区生产索引重建和人工验收仍需在演示机上完成。",
    "真实多模态独立 episode 数量仍不足路线图 S1 的 30 条门槛。",
    "完整 VLM+RAG 首 token/完整回答 P95 仍缺真实冻结集实测。",
    "外部未公开评测协议未在当前工作区发布。",
]

VERIFICATION_SUMMARY = [
    "后端关键组合测试：136 passed, 1 warning",
    "closeout 报告测试：2 passed",
    "前端媒体事件测试：8 passed",
    "前端项目/文档测试：3 passed",
    "Python compileall：通过",
    "前端 npm run build：通过，仅有既有 chunk size warning",
    "Tauri cargo check --locked：通过",
]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except Exception:
        return None
    return result.stdout.strip()


def _workspace_state(root: Path) -> dict[str, Any]:
    status = _git(root, "status", "--short") or ""
    diff = _git(root, "diff", "--binary") or ""
    rows = [line for line in status.splitlines() if line.strip()]
    return {
        "git_head": _git(root, "rev-parse", "HEAD"),
        "tracked_diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "changed_path_count": len(rows),
        "untracked_path_count": sum(1 for row in rows if row.startswith("??")),
        "dirty": bool(rows),
    }


def _evidence(root: Path) -> list[dict[str, Any]]:
    items = []
    for relative, label in EVIDENCE_FILES:
        path = root / relative
        item: dict[str, Any] = {
            "path": relative,
            "label": label,
            "exists": path.exists(),
        }
        if path.is_file():
            item["sha256"] = _sha256(path)
            item["bytes"] = path.stat().st_size
        items.append(item)
    return items


def _hashed_files(root: Path, files: list[tuple[str, str]]) -> list[dict[str, Any]]:
    items = []
    for relative, label in files:
        path = root / relative
        item: dict[str, Any] = {"path": relative, "label": label, "exists": path.exists()}
        if path.is_file():
            item["sha256"] = _sha256(path)
            item["bytes"] = path.stat().st_size
        items.append(item)
    return items


def build_report(root: Path, *, created_at: str | None = None) -> dict[str, Any]:
    evidence = _evidence(root)
    missing = [item["path"] for item in evidence if not item["exists"]]
    hashes = _hashed_files(root, CONFIG_AND_DATA_FILES)
    return {
        "schema_version": "souldrive_competition_closeout_v0.1",
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "product": "SoulDrive-PC",
        "roadmap": "docs/plans/2026-08-29-souldrive-competition-value-roadmap.md",
        "positioning": POSITIONING,
        "workspace": _workspace_state(root),
        "evidence": evidence,
        "config_and_data_hashes": hashes,
        "evidence_missing": missing,
        "roadmap_stage_audit": STAGE_AUDIT,
        "mvp_ready_for_demo": not missing,
        "competition_ready": False,
        "passable_claims": PASSABLE_CLAIMS,
        "forbidden_claims": FORBIDDEN_CLAIMS,
        "blocking_items": BLOCKERS,
        "verification_summary": VERIFICATION_SUMMARY,
        "today_manual_closeout": [
            "用 docs/demo_runbook_v0.1.md 录制一条真实演示。",
            "演示前确认工作区已解锁、索引已完成、模型已预热。",
            "提交材料中保留 forbidden_claims 和 blocking_items，不写成已完成项。",
        ],
    }


def render_markdown(report: dict[str, Any]) -> str:
    evidence_rows = "\n".join(
        f"| {item['label']} | `{item['path']}` | {'OK' if item['exists'] else 'MISSING'} |"
        for item in report["evidence"]
    )
    claims = "\n".join(f"- {item}" for item in report["passable_claims"])
    forbidden = "\n".join(f"- {item}" for item in report["forbidden_claims"])
    blockers = "\n".join(f"- {item}" for item in report["blocking_items"])
    verification = "\n".join(f"- {item}" for item in report["verification_summary"])
    manual = "\n".join(f"- {item}" for item in report["today_manual_closeout"])
    stage_rows = "\n".join(
        f"| {item['stage']} | {item['name']} | {item['status']} | {'；'.join(item['gap'])} |"
        for item in report["roadmap_stage_audit"]
    )
    hash_rows = "\n".join(
        f"| {item['label']} | `{item['path']}` | {'OK' if item['exists'] else 'MISSING'} | `{item.get('sha256', '')}` |"
        for item in report["config_and_data_hashes"]
    )
    workspace = report["workspace"]
    return f"""# SoulDrive 竞赛提交收尾快照

生成时间：{report['created_at']}

## 一句话定位

{report['positioning']}

## 当前状态

- MVP 演示可用：{'是' if report['mvp_ready_for_demo'] else '否'}
- 竞赛完整验收：否
- Git HEAD：`{workspace.get('git_head') or 'unknown'}`
- 工作区有未提交改动：{'是' if workspace.get('dirty') else '否'}
- 未提交路径数量：{workspace.get('changed_path_count')}
- 未提交 diff 哈希：`{workspace.get('tracked_diff_sha256')}`

## 可在材料中陈述

{claims}

## 不得宣称

{forbidden}

## 仍需人工收尾

{manual}

## 已跑验证

{verification}

## 路线图逐项核查

| 阶段 | 名称 | 状态 | 未完成或需人工项 |
| --- | --- | --- | --- |
{stage_rows}

## 阻塞项

{blockers}

## 证据文件

| 项 | 路径 | 状态 |
| --- | --- | --- |
{evidence_rows}

## 配置与数据哈希

| 项 | 路径 | 状态 | SHA-256 |
| --- | --- | --- | --- |
{hash_rows}
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the final SoulDrive competition closeout snapshot.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-json", type=Path, default=Path("eval/results/competition_closeout_current.json"))
    parser.add_argument("--output-md", type=Path, default=Path("docs/competition_closeout_current.md"))
    args = parser.parse_args(argv)

    root = args.root.resolve()
    report = build_report(root)
    output_json = args.output_json if args.output_json.is_absolute() else root / args.output_json
    output_md = args.output_md if args.output_md.is_absolute() else root / args.output_md
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output_md.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({
        "output_json": str(output_json),
        "output_md": str(output_md),
        "mvp_ready_for_demo": report["mvp_ready_for_demo"],
        "competition_ready": report["competition_ready"],
        "missing": report["evidence_missing"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
