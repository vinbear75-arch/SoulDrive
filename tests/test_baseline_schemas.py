import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_baseline_schemas_are_valid_json_with_expected_versions():
    expected = {
        "episode_v0.1.schema.json": "SoulDrive multimodal episode v0.1",
        "infer_response_v0.1.schema.json": "SoulDrive inference response v0.1",
        "run_manifest_v0.1.schema.json": "SoulDrive evaluation run manifest v0.1",
    }
    for filename, title in expected.items():
        payload = json.loads((ROOT / "eval" / "schemas" / filename).read_text(encoding="utf-8"))
        assert payload["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert payload["title"] == title


def test_episode_schema_keeps_labels_optional_for_inference_inputs():
    payload = json.loads(
        (ROOT / "eval" / "schemas" / "episode_v0.1.schema.json").read_text(encoding="utf-8")
    )
    assert "ground_truth" not in payload["required"]
    assert {"schema_version", "episode_id", "split", "events"}.issubset(payload["required"])
