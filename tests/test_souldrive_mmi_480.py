import json
from pathlib import Path

from scripts.build_souldrive_mmi_480 import build
from scripts.eval_souldrive_mmi_seed import infer


def test_build_480_has_frozen_split_contract(tmp_path):
    build(tmp_path)
    rows = [json.loads(line) for line in (tmp_path / "episodes.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 480
    assert {row["split"] for row in rows} == {"train", "validation", "test"}
    assert {split: sum(row["split"] == split for row in rows) for split in ("train", "validation", "test")} == {"train": 320, "validation": 80, "test": 80}
    assert len({row["template_id"] for row in rows}) == 480
    prediction = infer(rows[0])
    assert prediction["aligned_event_ids"][0] == rows[0]["events"][0]["event_id"]
