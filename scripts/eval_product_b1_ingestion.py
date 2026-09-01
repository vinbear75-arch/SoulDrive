"""Audit the B1 corpus against currently implemented file support and catalog facts."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.project_catalog import ProjectCatalog
from core.workspace import SoulDriveWorkspace


def evaluate(manifest_path: Path, workspace_root: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    workspace = SoulDriveWorkspace(str(workspace_root)).ensure()
    catalog = ProjectCatalog(workspace)
    rows = []
    try:
        for item in manifest["assets"]:
            started = time.perf_counter()
            path = manifest_path.parent / item["relative_path"]
            exists = path.is_file()
            digest = __import__("hashlib").sha256(path.read_bytes()).hexdigest() if exists else ""
            catalog_row = catalog.conn.execute("SELECT 1 FROM asset_versions WHERE version_id = ?", (item["version_id"],)).fetchone()
            rows.append({
                "asset_id": item["asset_id"], "filename": item["filename"], "asset_type": item["asset_type"],
                "supported_by_current_indexer": item["supported_by_current_indexer"], "file_exists": exists,
                "content_hash_matches": digest == item["sha256"], "catalog_registered": bool(catalog_row),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            })
    finally:
        catalog.close()
    supported = [row for row in rows if row["supported_by_current_indexer"]]
    elapsed = sorted(row["elapsed_ms"] for row in rows)
    return {
        "schema_version": "souldrive_product_b1_ingestion_metrics_v0.1",
        "sample_count": len(rows),
        "current_format_support_rate": len(supported) / len(rows),
        "asset_file_integrity_rate": sum(row["file_exists"] and row["content_hash_matches"] for row in rows) / len(rows),
        "catalog_registration_rate": sum(row["catalog_registered"] for row in rows) / len(rows),
        "supported_format_catalog_rate": sum(row["catalog_registered"] for row in supported) / len(supported),
        "latency_ms": {"p50": statistics.median(elapsed), "p95": elapsed[max(0, int((len(elapsed) - 1) * 0.95))]},
        "unsupported_extensions": sorted({Path(row["filename"]).suffix.lower() for row in rows if not row["supported_by_current_indexer"]}),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate B1 catalog and current format support.")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--workspace-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = evaluate(args.manifest.resolve(), args.workspace_root.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("sample_count", "current_format_support_rate", "asset_file_integrity_rate", "catalog_registration_rate", "unsupported_extensions")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
