import json

from scripts.apply_souldrive_mmi_review import main


def test_review_snapshot_marks_all_seed_examples_reviewed(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["apply_souldrive_mmi_review.py", "--output-dir", str(tmp_path), "--accept-unreviewed"])
    assert main() == 0
    episodes = [json.loads(line) for line in (tmp_path / "episodes.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(episodes) == 30
    assert {item["provenance"]["review_status"] for item in episodes} == {"reviewed"}
