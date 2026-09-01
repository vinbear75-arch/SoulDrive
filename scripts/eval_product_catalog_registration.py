"""Measure product catalog registration on the frozen 30-asset demo pack."""

from __future__ import annotations

import argparse
import json
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
    started = time.perf_counter()
    created = 0
    try:
        for project_id in manifest["projects"]:
            catalog.create_project(project_id, project_id=project_id)
        for asset in manifest["assets"]:
            catalog.create_asset(asset["project_id"], asset["title"], asset["asset_type"], asset_id=asset["asset_id"])
            catalog.create_asset_version(
                asset["asset_id"], asset["sha256"], asset["relative_path"], version_id=asset["version_id"],
                valid_from=asset["valid_from"], imported_at=asset["valid_from"],
            )
            created += 1
        registered = catalog.conn.execute("SELECT COUNT(*) FROM asset_versions").fetchone()[0]
    finally:
        catalog.close()
    return {
        "schema_version": "souldrive_product_catalog_registration_metrics_v0.1",
        "sample_count": len(manifest["assets"]),
        "registered_count": registered,
        "registration_rate": registered / len(manifest["assets"]),
        "elapsed_ms": (time.perf_counter() - started) * 1000,
        "workspace": str(workspace_root),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.manifest.resolve(), args.workspace_root.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
