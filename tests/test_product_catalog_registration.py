import json

from scripts.eval_product_catalog_registration import evaluate


def test_catalog_registration_registers_all_assets(tmp_path):
    manifest = {
        "projects": ["p"],
        "assets": [{
            "project_id": "p", "asset_id": "a", "version_id": "a-v1", "title": "A",
            "asset_type": "report", "sha256": "hash", "relative_path": "a.txt",
            "valid_from": "2026-08-19T00:00:00+08:00",
        }],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = evaluate(path, tmp_path / "workspace")
    assert result["registration_rate"] == 1.0
