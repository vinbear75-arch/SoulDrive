import argparse
import hashlib
import importlib.metadata
import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import eval_rag


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_bytes(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
    ).stdout


def repository_snapshot(excluded_root: Path | None = None) -> dict[str, Any]:
    head = git_bytes("rev-parse", "HEAD").decode("utf-8", errors="replace").strip()
    branch = git_bytes("branch", "--show-current").decode("utf-8", errors="replace").strip()
    tracked_diff = git_bytes("diff", "--binary", "--no-ext-diff")
    untracked_output = git_bytes("ls-files", "--others", "--exclude-standard", "-z")
    untracked_paths = sorted(
        Path(item.decode("utf-8", errors="surrogateescape"))
        for item in untracked_output.split(b"\0")
        if item
    )
    excluded_root = excluded_root.resolve() if excluded_root is not None else None
    untracked_files = []
    for path in untracked_paths:
        absolute_path = (PROJECT_ROOT / path).resolve()
        if not absolute_path.is_file():
            continue
        if excluded_root is not None and absolute_path.is_relative_to(excluded_root):
            continue
        untracked_files.append({"path": path.as_posix(), "sha256": sha256_file(absolute_path)})
    state_payload = json.dumps(
        {
            "head": head,
            "tracked_diff_sha256": sha256_bytes(tracked_diff),
            "untracked_files": untracked_files,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "head": head,
        "branch": branch,
        "tracked_diff_sha256": sha256_bytes(tracked_diff),
        "untracked_files": untracked_files,
        "workspace_state_sha256": sha256_bytes(state_payload),
    }


def package_versions() -> dict[str, str]:
    versions = {}
    for name in ("cryptography", "docling", "fastapi", "langchain-core", "pytest", "rapidocr-onnxruntime"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not-installed"
    return versions


def gpu_summary() -> str:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return "unavailable"
    return result.stdout.strip()


def input_record(path: Path, provenance: str) -> dict[str, Any]:
    return {
        "path": path.relative_to(PROJECT_ROOT).as_posix(),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "provenance": provenance,
    }


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def capture(args: argparse.Namespace) -> Path:
    output_dir = args.output_dir.resolve()
    repository = repository_snapshot(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    retrieval_dataset = args.retrieval_dataset.resolve()
    retrieval_results = args.retrieval_results.resolve()
    ingestion_report_path = args.ingestion_report.resolve()

    records = eval_rag.load_dataset(retrieval_dataset)
    results = eval_rag.load_results(retrieval_results, records)
    retrieval_report = eval_rag.summarize(records, results, top_k=args.top_k, mode="B0 archived retrieval")
    ingestion_report = json.loads(ingestion_report_path.read_text(encoding="utf-8"))
    retrieval_metrics = {key: value for key, value in retrieval_report.items() if key != "rows"}
    metrics = {
        "schema_version": "souldrive_b0_metrics_v0.1",
        "retrieval": retrieval_metrics,
        "ingestion": ingestion_report,
        "tests": {
            "summary": args.test_summary,
            "environment_note": args.test_environment_note,
        },
        "provenance": {
            "retrieval": "archived predictions re-scored by current evaluation code",
            "ingestion": "archived aggregate report; no source corpus available for live replay",
        },
    }
    write_json(output_dir / "metrics.json", metrics)
    shutil.copyfile(retrieval_results, output_dir / "predictions.jsonl")
    write_jsonl(
        output_dir / "latency.jsonl",
        [
            {
                "id": result.id,
                "elapsed_ms": result.elapsed_ms,
                "retrieval_ms": result.retrieval_ms,
                "generation_ms": result.generation_ms,
                "provenance": "archived",
            }
            for result in results
        ],
    )
    write_jsonl(
        output_dir / "failures.jsonl",
        [row for row in retrieval_report["rows"] if not row["source_all_hit"]],
    )

    inputs = [
        input_record(retrieval_dataset, "frozen evaluation dataset"),
        input_record(retrieval_results, "archived per-sample predictions"),
        input_record(ingestion_report_path, "archived ingestion aggregate"),
    ]
    manifest = {
        "schema_version": "souldrive_run_manifest_v0.1",
        "run_id": args.run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "baseline_level": "B0_CURRENT",
        "status": "partial",
        "repository": repository,
        "environment": {
            "os": platform.platform(),
            "python": platform.python_version(),
            "conda_environment": "souldrive",
            "packages": package_versions(),
            "gpu": gpu_summary(),
        },
        "inputs": inputs,
        "components": {
            "retrieval": "archived predictions + current eval_rag summarizer",
            "ingestion": "archived aggregate report",
            "tests": args.test_summary,
        },
        "outputs": {
            "predictions": "predictions.jsonl",
            "metrics": "metrics.json",
            "latency": "latency.jsonl",
            "failures": "failures.jsonl",
            "summary": "summary.md",
        },
        "limitations": [
            "SOULDRIVE_WORKSPACE was not configured for a live retrieval replay.",
            "The original 36-document evaluation corpus was not available on this machine.",
            "Retrieval predictions and ingestion metrics are archived evidence, not a fresh end-to-end run.",
            "OCR and full answer-generation baselines require separate fixed datasets.",
        ],
    }
    write_json(output_dir / "run_manifest.json", manifest)
    summary = f"# SoulDrive B0 Current Baseline\n\n"
    summary += f"- Run: `{args.run_id}`\n"
    summary += "- Status: partial (archived retrieval/ingestion evidence)\n"
    summary += f"- Tests: {args.test_summary}\n"
    summary += f"- Retrieval samples: {retrieval_metrics['sample_count']}\n"
    summary += f"- Retrieval all-hit@{args.top_k}: {retrieval_metrics['retrieval_all_hit_rate']:.1%}\n"
    summary += f"- Retrieval any-hit@{args.top_k}: {retrieval_metrics['retrieval_any_hit_rate']:.1%}\n"
    summary += f"- MRR: {retrieval_metrics['mean_reciprocal_rank']:.3f}\n"
    summary += f"- Ingestion samples: {ingestion_report.get('sample_count')}\n"
    summary += f"- Ingestion quality: {float(ingestion_report.get('overall_ingestion_quality_rate') or 0):.1%}\n\n"
    summary += "## Limitations\n\n"
    summary += "".join(f"- {item}\n" for item in manifest["limitations"])
    (output_dir / "summary.md").write_text(summary, encoding="utf-8")
    return output_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture the pre-refactor SoulDrive B0 evidence bundle.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--retrieval-dataset", required=True, type=Path)
    parser.add_argument("--retrieval-results", required=True, type=Path)
    parser.add_argument("--ingestion-report", required=True, type=Path)
    parser.add_argument("--top-k", type=int, default=6)
    parser.add_argument("--test-summary", required=True)
    parser.add_argument("--test-environment-note", required=True)
    args = parser.parse_args()
    output = capture(args)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
