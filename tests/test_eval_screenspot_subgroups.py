import json

import pytest

from scripts.eval_screenspot_subgroups import evaluate_screen_spot_subgroups


def _write(path, rows):
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_screenspot_subgroups_and_paired_deltas_are_explicit(tmp_path):
    baseline = tmp_path / "baseline.jsonl"
    candidate = tmp_path / "candidate.jsonl"
    rows = [
        {"img_filename": "desktop-a.png", "instruction": "open menu", "platform": "desktop", "data_type": "text", "correct": False, "latency_ms": 100},
        {"img_filename": "desktop-b.png", "instruction": "save", "platform": "windows", "data_type": "icon", "correct": True, "latency_ms": 200},
        {"img_filename": "web-a.png", "instruction": "submit", "platform": "web", "data_type": "widget", "correct": False, "latency_ms": 300},
        {"img_filename": "mobile-a.png", "instruction": "back", "platform": "ios", "data_type": "text", "correct": True, "latency_ms": 400},
    ]
    candidate_rows = [dict(row, correct=True) for row in rows]
    _write(baseline, rows)
    _write(candidate, candidate_rows)
    (tmp_path / "data_provenance.json").write_text(
        json.dumps({"dataset": "ScreenSpot", "candidate": "test"}),
        encoding="utf-8",
    )

    report = evaluate_screen_spot_subgroups(candidate, baseline_path=baseline)

    assert report["metrics"]["overall"]["sample_count"] == 4
    assert report["metrics"]["by_platform"]["desktop"]["sample_count"] == 2
    assert report["metrics"]["by_type"]["icon"]["sample_count"] == 2
    assert report["metrics"]["by_platform"]["desktop"]["latency_ms"]["p95"] == 200.0
    assert report["comparison"]["accuracy_delta"] == 0.5
    assert report["comparison"]["by_type"]["text"]["accuracy_delta"] == 0.5
    assert report["provenance"]["status"] == "declared"


def test_screenspot_pairing_fails_closed_on_different_samples(tmp_path):
    baseline = tmp_path / "baseline.jsonl"
    candidate = tmp_path / "candidate.jsonl"
    base_row = {"img_filename": "a.png", "instruction": "open", "platform": "desktop", "data_type": "text", "correct": True}
    _write(baseline, [base_row])
    _write(candidate, [dict(base_row, instruction="close")])

    with pytest.raises(ValueError, match="sample keys do not match"):
        evaluate_screen_spot_subgroups(candidate, baseline_path=baseline)
