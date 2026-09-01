"""Turn the runtime indexing state into a compact, auditable B1 report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def summarize(state: dict) -> dict:
    indexing = state.get("indexing") or {}
    documents = list((indexing.get("observability") or {}).get("documents") or [])
    succeeded = sum(item.get("status") == "indexed" for item in documents)
    failures = [item for item in documents if item.get("status") != "indexed"]
    return {
        "schema_version": "souldrive_product_b1_runtime_metrics_v0.1",
        "status": indexing.get("status"),
        "discovered_files": indexing.get("discovered_files", 0),
        "processed_files": indexing.get("processed_files", 0),
        "succeeded_files": succeeded,
        "failed_files": len(failures),
        "parse_success_rate": succeeded / len(documents) if documents else 0.0,
        "chunk_count": indexing.get("chunk_count", 0),
        "failure_summary": indexing.get("failure_summary", {}),
        "totals": indexing.get("observability", {}).get("totals", {}),
        "failures": [
            {"source_filename": item.get("source_filename"), "reason": item.get("reason")}
            for item in (indexing.get("failures") or [])
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize a SoulDrive indexing runtime state.")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = summarize(json.loads(args.state.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "discovered_files", "succeeded_files", "failed_files", "parse_success_rate", "chunk_count", "failure_summary")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
