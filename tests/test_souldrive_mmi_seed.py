import json
from pathlib import Path

from jsonschema import Draft202012Validator

from scripts.build_souldrive_mmi_seed import build_episodes
from scripts.eval_souldrive_mmi_seed import infer


ROOT = Path(__file__).resolve().parent.parent


def test_seed_covers_expected_business_intents_and_schema():
    episodes = build_episodes()
    assert len(episodes) == 30
    intents = {item["ground_truth"]["intent"] for item in episodes}
    expected = {
        "selection_summary", "rewrite_selection", "private_qa", "project_compare",
        "extract_tasks", "schedule_draft", "email_draft", "search_document",
        "switch_template", "confirm", "cancel", "clarify_target", "out_of_scope",
    }
    assert expected.issubset(intents)
    validator = Draft202012Validator(json.loads((ROOT / "eval/schemas/episode_v0.1.schema.json").read_text(encoding="utf-8")))
    assert not [error for item in episodes for error in validator.iter_errors(item)]


def test_b1_seed_baseline_produces_contract_valid_responses():
    response_schema = json.loads((ROOT / "eval/schemas/infer_response_v0.1.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(response_schema)
    episodes = build_episodes()
    predictions = [infer(item) for item in episodes]
    assert len(predictions) == 30
    assert not [error for item in predictions for error in validator.iter_errors(item)]
    assert all(item["latency_ms"]["total"] >= 0 for item in predictions)


def test_seed_review_artifact_has_one_editable_row_per_episode():
    import csv

    review_dir = ROOT / "eval/fixtures/souldrive_pc_mmi_seed_v0.1"
    with (review_dir / "human_review.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 30
    assert {row["decision"] for row in rows} == {""}
    assert (review_dir / "human_review_instructions.md").is_file()
