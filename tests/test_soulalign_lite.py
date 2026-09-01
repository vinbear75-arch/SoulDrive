import json

from core.soulalign import (
    MODEL_SCHEMA_VERSION,
    build_features,
    clear_model_cache,
    decide,
    predict_lite_intent,
)
from scripts.train_soulalign_lite import train


def _checkpoint(path):
    path.write_text(json.dumps({
        "schema_version": MODEL_SCHEMA_VERSION,
        "checkpoint_id": "unit-test",
        "classes": ["selection_summary", "clarify_target"],
        "feature_names": ["char2:归纳", "target_count:0"],
        "weights": [[3.0, 0.0], [0.0, 0.0]],
        "intercepts": [0.0, 0.0],
        "minimum_confidence": 0.6,
    }), encoding="utf-8")


def test_lite_checkpoint_can_cover_unruled_paraphrase(tmp_path):
    checkpoint = tmp_path / "soulalign.json"
    _checkpoint(checkpoint)
    clear_model_cache()

    result = predict_lite_intent("归纳", [], model_path=checkpoint)

    assert result is not None
    assert result["intent"] == "selection_summary"
    assert result["confidence"] > 0.6


def test_rules_remain_the_safety_boundary_when_lite_checkpoint_exists(tmp_path):
    checkpoint = tmp_path / "soulalign.json"
    _checkpoint(checkpoint)
    clear_model_cache()

    result = decide("删除桌面上的所有文件", [], model_path=checkpoint)

    assert result["intent"] == "out_of_scope"
    assert result["intent_source"] == "rules"
    assert result["action"] == "refuse"


def test_lite_checkpoint_cannot_override_explicit_target_clarification(tmp_path):
    checkpoint = tmp_path / "soulalign.json"
    _checkpoint(checkpoint)
    clear_model_cache()

    result = decide("当前指代不清，请归纳", [], model_path=checkpoint)

    assert result["intent"] == "clarify_target"
    assert result["intent_source"] == "rules"
    assert result["action"] == "clarify"


def test_training_requires_baseline_and_excludes_sealed_test(tmp_path):
    rows = []
    for split, intent, text in (
        ("train", "selection_summary", "归纳选区"),
        ("train", "email_draft", "给老师写邮件"),
        ("validation", "selection_summary", "整理当前内容"),
        ("test", "email_draft", "发送本周进展"),
    ):
        rows.append({
            "schema_version": "souldrive_episode_v0.1",
            "episode_id": split,
            "split": split,
            "events": [{"event_id": "speech", "modality": "speech_text", "start_ms": 0, "payload": {"text": text}}],
            "ground_truth": {"intent": intent},
        })
    dataset = tmp_path / "episodes.jsonl"
    dataset.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    baseline = tmp_path / "baseline.json"
    baseline.write_text("{}", encoding="utf-8")
    output = tmp_path / "model.json"
    report = tmp_path / "report.json"

    result = train(dataset, baseline, output, report)

    assert result["test_count_excluded"] == 1
    assert json.loads(output.read_text(encoding="utf-8"))["training"]["test_used"] is False
    assert "char2:纳选" in build_features("归纳选区", [])
