"""Create a reviewed SoulDrive-PC-MMI snapshot from a review CSV."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = ROOT / "eval" / "fixtures" / "souldrive_pc_mmi_seed_v0.1"
DEFAULT_OUTPUT = ROOT / "eval" / "fixtures" / "souldrive_pc_mmi_seed_v0.2"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--accept-unreviewed", action="store_true")
    args = parser.parse_args()

    source = args.source_dir.resolve()
    output = args.output_dir.resolve()
    rows = list(csv.DictReader((source / "human_review.csv").open(encoding="utf-8-sig", newline="")))
    decisions = {}
    for row in rows:
        decision = row["decision"].strip() or ("accept" if args.accept_unreviewed else "")
        if decision not in {"accept", "modify", "reject"}:
            raise ValueError(f"{row['episode_id']}: invalid decision {decision!r}")
        if not decision:
            raise ValueError(f"{row['episode_id']}: missing decision")
        decisions[row["episode_id"]] = row | {"decision": decision}

    episodes = []
    for line in (source / "episodes.jsonl").read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        review = decisions.get(item["episode_id"])
        if review is None:
            raise ValueError(f"{item['episode_id']}: no review row")
        if review["decision"] == "reject":
            continue
        truth = item["ground_truth"]
        for field, key in (("corrected_intent", "intent"), ("corrected_target_id", "target_id"), ("corrected_action", "expected_action")):
            if review[field].strip():
                truth[key] = review[field].strip()
        item["provenance"]["review_status"] = "reviewed"
        episodes.append(item)

    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    episode_path = output / "episodes.jsonl"
    episode_path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in episodes), encoding="utf-8")
    with (output / "review_decisions.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(decisions.values())
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    manifest.update({
        "schema_version": "souldrive_pc_mmi_seed_manifest_v0.2",
        "dataset_id": "SoulDrive-PC-MMI-seed-v0.2",
        "episodes_sha256": hashlib.sha256(episode_path.read_bytes()).hexdigest(),
        "review_status": "reviewed",
        "review_source": "human_review.csv; blank decisions accepted by explicit operator authorization" if args.accept_unreviewed else "human_review.csv",
        "episode_count": len(episodes),
    })
    manifest["limitations"] = [item for item in manifest.get("limitations", []) if "require human review" not in item]
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
