import hashlib
import json
import shutil

import pytest

from scripts import eval_rag, validate_business_seed_review


def _review_copy(tmp_path):
    target = tmp_path / "review"
    shutil.copytree(validate_business_seed_review.DEFAULT_DIRECTORY, target)
    project_root = tmp_path / "project"
    schema_dir = project_root / "eval/schemas"
    schema_dir.mkdir(parents=True)
    shutil.copyfile(
        validate_business_seed_review.PROJECT_ROOT / "eval/schemas/business_seed_review_v0.1.schema.json",
        schema_dir / "business_seed_review_v0.1.schema.json",
    )
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for source in manifest["sources"]:
        source["path"] = f"sources/{source['id']}.txt"
        path = project_root / source["path"]
        path.parent.mkdir(exist_ok=True)
        content = f"synthetic source: {source['id']}".encode("utf-8")
        path.write_bytes(content)
        source["sha256"] = hashlib.sha256(content).hexdigest()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return target, project_root


def test_review_seeds_only_pass_structural_checks(tmp_path):
    directory, project_root = _review_copy(tmp_path)
    report = validate_business_seed_review.validate_seed_review(directory, project_root=project_root)

    assert report["mode"] == "structure_only"
    assert report["review_status"] == "pending_review"
    assert report["sample_count"] == 30
    assert report["task_counts"] == validate_business_seed_review.TASK_COUNTS
    assert report["source_fingerprints_verified"] == 7
    assert report["scoring_performed"] is False
    assert report["ready_for_freeze"] is False


@pytest.mark.parametrize("field,value", [("gold", {"answer": "unreviewed"}), ("review_status", "approved"), ("source_refs", ["unknown-source"])])
def test_review_preflight_rejects_gold_status_and_source_changes(tmp_path, field, value):
    directory, project_root = _review_copy(tmp_path)
    path = directory / "seeds.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0][field] = value
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")

    with pytest.raises(ValueError):
        validate_business_seed_review.validate_seed_review(directory, project_root=project_root)


def test_review_preflight_refuses_quality_scoring_permission(tmp_path):
    directory, project_root = _review_copy(tmp_path)
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["usage_policy"]["allow_quality_scoring"] = True
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="forbid scoring"):
        validate_business_seed_review.validate_seed_review(directory, project_root=project_root)


@pytest.mark.parametrize("mutation", ["empty", "duplicate", "source_hash"])
def test_review_preflight_rejects_missing_duplicate_and_changed_sources(tmp_path, mutation):
    directory, project_root = _review_copy(tmp_path)
    if mutation == "source_hash":
        path = directory / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["sources"][0]["sha256"] = "0" * 64
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    else:
        path = directory / "seeds.jsonl"
        rows = path.read_text(encoding="utf-8").splitlines()
        path.write_text("" if mutation == "empty" else "\n".join([rows[0], *rows[1:-1], rows[0]]), encoding="utf-8")

    with pytest.raises(ValueError):
        validate_business_seed_review.validate_seed_review(directory, project_root=project_root)


def test_review_preflight_cli_returns_failure_without_quality_results(tmp_path, capsys):
    assert validate_business_seed_review.main(["--dataset-dir", str(tmp_path)]) == 1
    assert "structure_only" not in capsys.readouterr().out


def test_review_rows_cannot_be_silently_loaded_as_rag_gold():
    with pytest.raises(ValueError, match="not approved.*gold"):
        eval_rag.load_dataset(validate_business_seed_review.DEFAULT_DIRECTORY / "seeds.jsonl")


def test_review_rows_cannot_be_reformatted_into_unreviewed_rag_gold(tmp_path):
    row = json.loads((validate_business_seed_review.DEFAULT_DIRECTORY / "seeds.jsonl").read_text(encoding="utf-8").splitlines()[0])
    row.update(question=row["prompt"], type="synthesis", expected_sources=[])
    path = tmp_path / "reformatted.jsonl"
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="not approved.*gold"):
        eval_rag.load_dataset(path)
