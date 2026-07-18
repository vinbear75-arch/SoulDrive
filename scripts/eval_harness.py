import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import eval_ingestion, eval_rag


DEFAULT_RETRIEVAL_DATASET = PROJECT_ROOT / "eval" / "retrieval_quality_blind_v0.3.jsonl"
DEFAULT_INGESTION_DATASET = PROJECT_ROOT / "eval" / "ingestion_quality_v0.1.jsonl"


def configure_utf8_stdio() -> None:
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the unified SoulDrive RAG evaluation harness.")
    parser.add_argument("--workspace", default=None, help="SoulDrive workspace root for live retrieval and ingestion inspection.")
    parser.add_argument("--passphrase", default=None, help="Workspace passphrase for encrypted local indexes.")
    parser.add_argument("--top-k", type=int, default=6, help="Retrieval top-k.")
    parser.add_argument("--retrieval-dataset", default=str(DEFAULT_RETRIEVAL_DATASET), help="Retrieval dataset JSONL path.")
    parser.add_argument("--retrieval-results", default=None, help="Saved retrieval result JSONL path.")
    parser.add_argument("--ingestion-dataset", default=str(DEFAULT_INGESTION_DATASET), help="Ingestion dataset JSONL path.")
    parser.add_argument("--ingestion-chunks-jsonl", default=None, help="Saved indexed chunk JSONL path.")
    parser.add_argument("--save-report", default=None, help="Optional JSON report output path.")
    args = parser.parse_args(argv)

    workspace = eval_rag.resolve_workspace_arg(args.workspace) if args.workspace else None
    if workspace:
        eval_rag.prepare_workspace_keys(workspace, args.passphrase)

    report = build_harness_report(
        retrieval_dataset_path=Path(args.retrieval_dataset),
        retrieval_results_path=Path(args.retrieval_results) if args.retrieval_results else None,
        ingestion_dataset_path=Path(args.ingestion_dataset),
        ingestion_chunks_path=Path(args.ingestion_chunks_jsonl) if args.ingestion_chunks_jsonl else None,
        workspace=workspace,
        top_k=args.top_k,
    )
    if args.save_report:
        save_report(Path(args.save_report), report)
    print_report(report)
    return 0


def build_harness_report(
    *,
    retrieval_dataset_path: Path,
    retrieval_results_path: Path | None,
    ingestion_dataset_path: Path,
    ingestion_chunks_path: Path | None,
    workspace: str | None = None,
    top_k: int = 6,
) -> dict[str, Any]:
    retrieval_records = eval_rag.load_dataset(retrieval_dataset_path)
    if retrieval_results_path is not None:
        retrieval_results = eval_rag.load_results(retrieval_results_path, retrieval_records)
    elif workspace:
        retrieval_results = eval_rag.run_retrieval(retrieval_records, workspace=workspace, top_k=top_k)
    else:
        raise ValueError("retrieval_results_path or workspace is required")
    retrieval_report = eval_rag.summarize(retrieval_records, retrieval_results, top_k=top_k, mode="检索评测")

    ingestion_records = eval_ingestion.load_dataset(ingestion_dataset_path)
    if ingestion_chunks_path is not None:
        ingestion_chunks = eval_ingestion.load_chunks_jsonl(ingestion_chunks_path)
    elif workspace:
        ingestion_chunks = eval_ingestion.collect_workspace_chunks(workspace)
    else:
        raise ValueError("ingestion_chunks_path or workspace is required")
    ingestion_report = eval_ingestion.summarize(ingestion_records, ingestion_chunks)

    retrieval_score = retrieval_report.get("retrieval_all_hit_rate") or 0.0
    ingestion_score = ingestion_report.get("overall_ingestion_quality_rate") or 0.0
    harness_score = round((float(retrieval_score) + float(ingestion_score)) / 2, 6)
    return {
        "schema_version": "souldrive_rag_harness_v1",
        "retrieval_dataset": str(retrieval_dataset_path),
        "ingestion_dataset": str(ingestion_dataset_path),
        "top_k": top_k,
        "harness_score": harness_score,
        "retrieval": strip_rows(retrieval_report),
        "ingestion": strip_rows(ingestion_report),
    }


def strip_rows(report: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in report.items() if key != "rows"}


def save_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def print_report(report: dict[str, Any]) -> None:
    retrieval = report["retrieval"]
    ingestion = report["ingestion"]
    print("整体 Harness 指标")
    print(f"Harness 总分: {format_percent(report['harness_score'])}")
    print(f"检索全命中率@{report['top_k']}: {format_percent(retrieval.get('retrieval_all_hit_rate'))}")
    print(f"检索任一命中率@{report['top_k']}: {format_percent(retrieval.get('retrieval_any_hit_rate'))}")
    print(f"多文档全命中率@{report['top_k']}: {format_percent(retrieval.get('multi_doc_all_hit_rate'))}")
    print(f"清洗切片质量: {format_percent(ingestion.get('overall_ingestion_quality_rate'))}")
    print(f"页码元数据覆盖率: {format_percent(ingestion.get('page_metadata_rate'))}")
    print(f"动态切片策略覆盖率: {format_percent(ingestion.get('dynamic_chunk_rate'))}")
    print(f"表格保留覆盖率: {format_percent(ingestion.get('table_coverage_rate'))}")
    print(f"图片元数据覆盖率: {format_percent(ingestion.get('image_metadata_rate'))}")


def format_percent(value: float | None) -> str:
    if value is None:
        return "无样本"
    return f"{float(value) * 100:.1f}%"


if __name__ == "__main__":
    configure_utf8_stdio()
    raise SystemExit(main(sys.argv[1:]))
