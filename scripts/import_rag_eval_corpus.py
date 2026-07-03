import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.indexer import DriveIndexer
from core.paper_importer import import_paper_into_workspace, safe_pdf_filename
from core.security_context import set_workspace_keys
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import unlock_keystore


DEFAULT_MANIFEST = PROJECT_ROOT / "eval" / "corpus_manifest_v0.2.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import the v0.2 RAG evaluation corpus into a SoulDrive workspace.")
    parser.add_argument("--workspace", required=True, help="SoulDrive workspace root path.")
    parser.add_argument("--corpus-dir", default=str(default_corpus_dir()), help="Directory containing the corpus PDF files.")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="Corpus manifest JSON path.")
    parser.add_argument("--passphrase", default=None, help="Optional workspace passphrase for encrypted document imports.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be imported without copying files.")
    parser.add_argument("--index", action="store_true", help="Run DriveIndexer.sync_workspace after imports finish.")
    parser.add_argument(
        "--skip-graph",
        action="store_true",
        help="Skip graph extraction during indexing; use this for retrieval-only baseline runs.",
    )
    parser.add_argument(
        "--json",
        "--json-output",
        dest="json_output",
        action="store_true",
        help="Print machine-readable JSON only. Use --json-output with conda run.",
    )
    args = parser.parse_args(argv)

    report = import_corpus(
        workspace_path=args.workspace,
        corpus_dir=Path(args.corpus_dir),
        manifest_path=Path(args.manifest),
        passphrase=args.passphrase,
        dry_run=args.dry_run,
        sync_index=args.index,
        skip_graph=args.skip_graph,
    )
    if args.json_output:
        print(format_json_report(report))
    else:
        print_human_report(report)
    return 0 if report["missing_count"] == 0 and report["rejected_count"] == 0 else 1


def default_corpus_dir() -> Path:
    return Path(os.environ.get("SOULDRIVE_EVAL_CORPUS_DIR") or (Path.home() / "Desktop" / "souldrive_docs"))


def import_corpus(
    *,
    workspace_path: str,
    corpus_dir: Path,
    manifest_path: Path,
    passphrase: str | None,
    dry_run: bool,
    sync_index: bool,
    skip_graph: bool = False,
) -> dict[str, Any]:
    workspace = SoulDriveWorkspace(workspace_path).ensure()
    if passphrase:
        set_workspace_keys(workspace.root_path, unlock_keystore(workspace, passphrase))

    documents = expected_documents(manifest_path)
    results = []
    for document in documents:
        source_path = corpus_dir / document["local_filename"]
        safe_name = safe_pdf_filename(document["local_filename"])
        if not source_path.exists():
            results.append(
                {
                    "doc_id": document["doc_id"],
                    "name": safe_name,
                    "source_path": str(source_path),
                    "status": "missing",
                }
            )
            continue
        if dry_run:
            results.append(
                {
                    "doc_id": document["doc_id"],
                    "name": safe_name,
                    "source_path": str(source_path),
                    "status": "dry_run",
                }
            )
            continue
        result = import_paper_into_workspace(workspace, str(source_path))
        results.append(
            {
                "doc_id": document["doc_id"],
                "name": safe_pdf_filename(str(result.get("name") or safe_name)),
                "source_path": str(source_path),
                "status": str(result.get("status") or "unknown"),
                "error_code": result.get("error_code"),
            }
        )

    index_status = "skipped"
    if sync_index and not dry_run and all(item["status"] != "missing" for item in results):
        DriveIndexer().sync_workspace(workspace, auth_level="BASIC" if skip_graph else "PRO")
        index_status = "completed"

    return summarize_results(workspace.root_path, corpus_dir, manifest_path, results, dry_run=dry_run, index_status=index_status)


def format_json_report(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=True, indent=2)


def expected_documents(manifest_path: Path) -> list[dict[str, str]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return [
        {
            "doc_id": str(document["doc_id"]),
            "local_filename": safe_pdf_filename(str(document["local_filename"])),
        }
        for document in manifest["documents"]
    ]


def summarize_results(
    workspace_path: str,
    corpus_dir: Path,
    manifest_path: Path,
    results: list[dict[str, Any]],
    *,
    dry_run: bool,
    index_status: str,
) -> dict[str, Any]:
    return {
        "workspace": workspace_path,
        "corpus_dir": str(corpus_dir),
        "manifest": str(manifest_path),
        "expected_count": len(results),
        "imported_count": count_status(results, "imported"),
        "already_present_count": count_status(results, "already_present"),
        "dry_run_count": count_status(results, "dry_run"),
        "missing_count": count_status(results, "missing"),
        "rejected_count": sum(1 for item in results if item["status"] == "rejected" or item.get("error_code")),
        "dry_run": dry_run,
        "index_status": index_status,
        "results": results,
    }


def count_status(results: list[dict[str, Any]], status: str) -> int:
    return sum(1 for item in results if item["status"] == status)


def print_human_report(report: dict[str, Any]) -> None:
    print(f"Workspace: {report['workspace']}")
    print(f"Corpus dir: {report['corpus_dir']}")
    print(f"Expected documents: {report['expected_count']}")
    print(f"Imported: {report['imported_count']}")
    print(f"Already present: {report['already_present_count']}")
    print(f"Dry run: {report['dry_run_count']}")
    print(f"Missing: {report['missing_count']}")
    print(f"Rejected: {report['rejected_count']}")
    print(f"Index status: {report['index_status']}")
    missing = [item for item in report["results"] if item["status"] == "missing"]
    if missing:
        print("\nMissing documents:")
        for item in missing:
            print(f"- {item['name']}: {item['source_path']}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
