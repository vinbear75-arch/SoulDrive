from scripts.build_souldrive_mmi_480 import build
from scripts.eval_souldrive_mmi_480 import main


def test_480_eval_writes_frozen_test_report(tmp_path, monkeypatch):
    dataset_dir = tmp_path / "dataset"
    build(dataset_dir)
    output = tmp_path / "result"
    monkeypatch.setattr("sys.argv", ["eval_souldrive_mmi_480.py", "--dataset", str(dataset_dir / "episodes.jsonl"), "--output-dir", str(output)])
    assert main() == 0
    assert (output / "metrics.json").is_file()
    assert (output / "predictions_test.jsonl").is_file()
