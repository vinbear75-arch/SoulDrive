import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.paper_importer import safe_pdf_filename
from core.keyword_index import KEYWORD_TABLE
from core.secure_document_store import SecureDocumentStore
from core.secure_vector_store import SecureVectorStore
from core.security_context import get_workspace_keys
from core.workspace import WORKSPACE_MANIFEST, SoulDriveWorkspace
from scripts import eval_rag


DEFAULT_MANIFEST = PROJECT_ROOT / "eval" / "corpus_manifest_v0.2.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check whether a SoulDrive workspace contains the v0.2 RAG evaluation corpus."
    )
    parser.add_argument("--workspace", required=True, help="SoulDrive workspace root path.")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="Corpus manifest JSON path.")
    parser.add_argument("--passphrase", default=None, help="Optional workspace passphrase for encrypted document metadata.")
    parser.add_argument(
        "--json",
        "--json-output",
        dest="json_output",
        action="store_true",
        help="Print machine-readable JSON only. Use --json-output with conda run.",
    )
    args = parser.parse_args(argv)

    report = build_report(
        workspace_path=args.workspace,
        manifest_path=Path(args.manifest),
        passphrase=args.passphrase,
    )
    if args.json_output:
        print(format_json_report(report))
    else:
        print_human_report(report)
    return (
        0
        if report["missing_count"] == 0
        and report["missing_indexed_count"] == 0
        and report["workspace_ready"]
        and not report["indexed_documents_locked"]
        else 1
    )


def build_report(workspace_path: str, manifest_path: Path, passphrase: str | None = None) -> dict[str, Any]:
    workspace_root = Path(workspace_path)
    workspace_ready = (workspace_root / "config" / WORKSPACE_MANIFEST).exists()
    expected_documents = expected_document_names(manifest_path)
    present_documents: list[str] = []
    indexed_documents: list[str] = []
    secure_documents_locked = False
    indexed_documents_locked = False
    errors: list[str] = []

    if workspace_ready:
        workspace = SoulDriveWorkspace(str(workspace_root))
        present_documents.extend(plain_paper_names(workspace))
        try:
            eval_rag.prepare_workspace_keys(workspace.root_path, passphrase)
        except Exception as exc:
            errors.append(f"unlock failed: {type(exc).__name__}: {exc}")
        keys = get_workspace_keys(workspace.root_path)
        document_manifest = Path(workspace.document_manifest_path)
        if document_manifest.exists() and keys is None:
            secure_documents_locked = True
        elif document_manifest.exists() and keys is not None:
            present_documents.extend(secure_document_names(workspace, keys))
        if Path(workspace.secure_vector_store_path).exists() and keys is None:
            indexed_documents_locked = True
        else:
            indexed_documents.extend(indexed_source_names(workspace, keys))

    present_set = set(present_documents)
    indexed_set = set(indexed_documents)
    missing_documents = sorted(name for name in expected_documents if name not in present_set)
    missing_indexed_documents = sorted(name for name in expected_documents if name not in indexed_set)
    extra_documents = sorted(name for name in present_set if name not in set(expected_documents))
    extra_indexed_documents = sorted(name for name in indexed_set if name not in set(expected_documents))
    return {
        "workspace": str(workspace_root),
        "workspace_ready": workspace_ready,
        "manifest": str(manifest_path),
        "expected_count": len(expected_documents),
        "present_count": len(expected_documents) - len(missing_documents),
        "missing_count": len(missing_documents),
        "indexed_count": len(expected_documents) - len(missing_indexed_documents),
        "missing_indexed_count": len(missing_indexed_documents),
        "extra_count": len(extra_documents),
        "extra_indexed_count": len(extra_indexed_documents),
        "secure_documents_locked": secure_documents_locked,
        "indexed_documents_locked": indexed_documents_locked,
        "expected_documents": expected_documents,
        "present_documents": sorted(present_set),
        "indexed_documents": sorted(indexed_set),
        "missing_documents": missing_documents,
        "missing_indexed_documents": missing_indexed_documents,
        "extra_documents": extra_documents,
        "extra_indexed_documents": extra_indexed_documents,
        "errors": errors,
    }


def format_json_report(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=True, indent=2)


def expected_document_names(manifest_path: Path) -> list[str]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return sorted(safe_pdf_filename(document["local_filename"]) for document in manifest["documents"])


def plain_paper_names(workspace: SoulDriveWorkspace) -> list[str]:
    root = Path(workspace.papers_path)
    if not root.exists():
        return []
    return sorted(path.name for path in root.rglob("*.pdf") if path.is_file())


def secure_document_names(workspace: SoulDriveWorkspace, keys: Any) -> list[str]:
    store = SecureDocumentStore(workspace, keys)
    try:
        return sorted(safe_pdf_filename(document["name"]) for document in store.iter_documents())
    finally:
        store.close()


def indexed_source_names(workspace: SoulDriveWorkspace, keys: Any | None) -> list[str]:
    if keys is not None and Path(workspace.secure_vector_store_path).exists():
        return secure_indexed_source_names(workspace, keys)
    return plain_indexed_source_names(workspace)


def secure_indexed_source_names(workspace: SoulDriveWorkspace, keys: Any) -> list[str]:
    store = SecureVectorStore(workspace.secure_vector_store_path, keys)
    try:
        payload = store.get(include=["metadatas"])
        return sorted(
            {
                safe_pdf_filename(str(metadata.get("source_filename") or ""))
                for metadata in payload.get("metadatas", [])
                if metadata and metadata.get("source_filename")
            }
        )
    finally:
        store.close()


def plain_indexed_source_names(workspace: SoulDriveWorkspace) -> list[str]:
    keyword_index_path = Path(workspace.keyword_index_path)
    if not keyword_index_path.exists():
        return []

    conn = sqlite3.connect(str(keyword_index_path))
    try:
        available = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        table_name = next(
            (
                candidate
                for candidate in (KEYWORD_TABLE, "keyword_documents")
                if candidate in available
            ),
            None,
        )
        if table_name is None:
            return []
        rows = conn.execute(
            f"SELECT metadata_json FROM {table_name}"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    names = set()
    for (metadata_json,) in rows:
        try:
            metadata = json.loads(metadata_json)
        except (TypeError, json.JSONDecodeError):
            continue
        source_filename = metadata.get("source_filename")
        if source_filename:
            names.add(safe_pdf_filename(str(source_filename)))
    return sorted(names)


def print_human_report(report: dict[str, Any]) -> None:
    print(f"Workspace: {report['workspace']}")
    print(f"Workspace ready: {report['workspace_ready']}")
    print(f"Expected documents: {report['expected_count']}")
    print(f"Present documents: {report['present_count']}")
    print(f"Missing documents: {report['missing_count']}")
    print(f"Indexed documents: {report['indexed_count']}")
    print(f"Missing indexed documents: {report['missing_indexed_count']}")
    if report["secure_documents_locked"]:
        print("Secure document metadata is locked. Pass --passphrase to inspect encrypted imports.")
    if report["indexed_documents_locked"]:
        print("Secure vector index metadata is locked. Pass --passphrase to inspect indexed documents.")
    if report["missing_documents"]:
        print("\nMissing documents:")
        for name in report["missing_documents"]:
            print(f"- {name}")
    if report["missing_indexed_documents"]:
        print("\nMissing indexed documents:")
        for name in report["missing_indexed_documents"]:
            print(f"- {name}")
    if report["errors"]:
        print("\nErrors:")
        for error in report["errors"]:
            print(f"- {error}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
