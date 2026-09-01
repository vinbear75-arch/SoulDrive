import json
from pathlib import Path

from jsonschema import Draft202012Validator

from core.multimodal_media import validate_episode_media
from scripts.build_souldrive_mmi_media_480 import build


ROOT = Path(__file__).resolve().parents[1]


def test_media_builder_creates_valid_synthetic_audio_and_screens(tmp_path):
    manifest = build(tmp_path, audio_mode="tone", limit=3)
    rows = [json.loads(line) for line in (tmp_path / "episodes.jsonl").read_text(encoding="utf-8").splitlines()]
    schema = json.loads((ROOT / "eval" / "schemas" / "episode_v0.1.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)

    assert manifest["episode_count"] == 3
    assert manifest["media"]["audio"]["backend"] == "synthetic_tone_fallback"
    assert not [error for row in rows for error in validator.iter_errors(row)]
    assert not [error for row in rows for error in validate_episode_media(row, tmp_path, require_media=True)]
    for row in rows:
        modalities = {event["modality"] for event in row["events"]}
        assert {"speech_text", "audio", "screen"}.issubset(modalities)
        assert row["provenance"]["source"] == "SoulDrive-PC-MMI-480-v0.1_structured_labels"
        assert len(row["ground_truth"]["aligned_event_ids"]) >= 3


def test_media_prompt_template_names_are_split_scoped(tmp_path):
    source = ROOT / "eval" / "fixtures" / "souldrive_pc_mmi_480_v0.1"
    manifest = build(tmp_path, source=source, audio_mode="tone", limit=3)
    assert manifest["template_split_overlap"] is False
    assert "不是公开竞赛测试集" in (tmp_path / "dataset_card.md").read_text(encoding="utf-8")
