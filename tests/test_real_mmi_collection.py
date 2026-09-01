import json
import wave
from pathlib import Path

from jsonschema import Draft202012Validator

from scripts.audit_real_mmi_collection import audit_collection
from scripts.build_souldrive_mmi_seed import build_episodes
from scripts.init_real_mmi_collection import SEED_TASKS, initialize
from scripts.ingest_real_mmi_episode import main as ingest_main


def _wav(path: Path) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)


def test_real_collection_init_and_ingest(tmp_path, monkeypatch):
    root = tmp_path / "collection"
    initialize(root)
    audio = tmp_path / "voice.wav"
    screen = tmp_path / "screen.png"
    _wav(audio)
    from PIL import Image
    Image.new("RGB", (32, 32), "white").save(screen)
    monkeypatch.setattr("sys.argv", ["ingest", "--collection-root", str(root), "--task-id", "PILOT-01", "--audio", str(audio), "--screen", str(screen), "--transcript", "总结我当前选中的实验结论", "--project-id", "demo", "--speaker-id", "speaker-01", "--application", "PowerPoint", "--target-kind", "text", "--target-bbox", "0.1,0.2,0.4,0.3"])
    assert ingest_main() == 0
    rows = [json.loads(line) for line in (root / "episodes.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["episode_id"] == "REAL-PILOT-01"
    assert rows[0]["events"][1]["payload_ref"].startswith("media/")
    assert rows[0]["speaker_id"] == "speaker-01"
    assert rows[0]["capture"]["screen_size"] == {"width": 32, "height": 32}
    assert rows[0]["capture"]["target_kind"] == "text"
    assert rows[0]["ground_truth"]["target_bbox"] == {"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.3}
    schema = json.loads((Path(__file__).resolve().parents[1] / "eval/schemas/episode_v0.1.schema.json").read_text(encoding="utf-8"))
    assert not list(Draft202012Validator(schema).iter_errors(rows[0]))


def test_ingest_keeps_selection_label_separate_from_confirmed_selected_text(tmp_path, monkeypatch):
    root = tmp_path / "collection"
    initialize(root)
    audio = tmp_path / "voice.wav"
    screen = tmp_path / "screen.png"
    _wav(audio)
    from PIL import Image
    Image.new("RGB", (32, 32), "white").save(screen)
    monkeypatch.setattr("sys.argv", [
        "ingest", "--collection-root", str(root), "--task-id", "PILOT-01",
        "--audio", str(audio), "--screen", str(screen),
        "--transcript", "总结我当前选中的实验结论", "--project-id", "demo",
        "--target-id", "selected", "--selection-text", "结果段落",
        "--selected-text", "本轮准确率为 85%。", "--selection-source", "windows_uia",
    ])

    assert ingest_main() == 0
    row = json.loads((root / "episodes.jsonl").read_text(encoding="utf-8").splitlines()[0])
    payload = row["events"][-1]["payload"]
    assert payload["text"] == "结果段落"
    assert payload["selected_text"] == "本轮准确率为 85%。"
    assert payload["selected_text_source"] == "windows_uia"


def test_seed_collection_has_thirty_distinct_task_cards(tmp_path):
    initialize(tmp_path, tasks=SEED_TASKS, dataset_id="SoulDrive-PC-MMI-Real-seed-v0.2")
    manifest = json.loads((tmp_path / "collection_manifest.json").read_text(encoding="utf-8"))
    tasks = json.loads((tmp_path / "tasks.json").read_text(encoding="utf-8"))

    assert manifest["dataset_id"] == "SoulDrive-PC-MMI-Real-seed-v0.2"
    assert manifest["splits"]["seed"] == 30
    assert len(tasks) == len({task["task_id"] for task in tasks}) == 30


def test_collection_audit_accepts_schema_valid_seed_without_claiming_real_capture(tmp_path):
    episodes = build_episodes()
    (tmp_path / "episodes.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in episodes),
        encoding="utf-8",
    )
    (tmp_path / "manifest.json").write_text(
        json.dumps({"dataset_id": "seed", "episode_count": len(episodes)}),
        encoding="utf-8",
    )

    report = audit_collection(tmp_path, minimum_episodes=30)

    assert report["valid"] is True
    assert report["requirements"]["schema_valid"] is True
    assert report["requirements"]["unique_task_coverage"] is True
    assert report["require_real_capture"] is False


def test_collection_audit_fails_closed_for_duplicate_and_incomplete_real_rows(tmp_path):
    episodes = build_episodes()[:1]
    episodes.append(dict(episodes[0]))
    (tmp_path / "episodes.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in episodes),
        encoding="utf-8",
    )
    (tmp_path / "manifest.json").write_text(
        json.dumps({"dataset_id": "seed", "episode_count": len(episodes)}),
        encoding="utf-8",
    )

    report = audit_collection(
        tmp_path,
        minimum_episodes=1,
        require_real_capture=True,
        minimum_speakers=1,
        minimum_resolutions=1,
        minimum_projects=1,
    )

    assert report["valid"] is False
    assert any("duplicate" in error for error in report["errors"])
    assert any("missing anonymized speaker_id" in error for error in report["errors"])
    assert report["requirements"]["schema_valid"] is True


def test_collection_audit_fails_closed_for_non_utf8_jsonl(tmp_path):
    (tmp_path / "episodes.jsonl").write_bytes(b"{\xff\n")

    report = audit_collection(tmp_path, minimum_episodes=0)

    assert report["valid"] is False
    assert report["requirements"]["schema_valid"] is True
    assert any("UTF-8" in error for error in report["errors"])


def test_collection_audit_reports_label_only_selection_without_treating_it_as_text(tmp_path):
    row = build_episodes()[0]
    row["events"] = [
        event for event in row["events"]
        if event["modality"] != "ui_selection"
    ] + [{
        "event_id": "selection",
        "modality": "ui_selection",
        "start_ms": 100,
        "end_ms": 200,
        "confidence": 1.0,
        "privacy_level": "project",
        "payload_ref": None,
        "payload": {"text": "结果段落", "target_id": "selected"},
    }]
    (tmp_path / "episodes.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps({"dataset_id": "seed", "episode_count": 1}), encoding="utf-8")

    report = audit_collection(tmp_path, minimum_episodes=0)

    assert report["selection_text_coverage"] == {
        "selection_event_count": 1,
        "with_selected_text": 0,
        "label_only": 1,
    }
