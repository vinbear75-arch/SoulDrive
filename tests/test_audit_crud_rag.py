import json
import subprocess
from pathlib import Path

from scripts.audit_crud_rag import audit


def test_audit_crud_rag_reads_contract_and_marks_missing_retrieval_gold(tmp_path, monkeypatch):
    repo = tmp_path / "crud"
    (repo / "data" / "crud_split").mkdir(parents=True)
    (repo / "data" / "crud").mkdir()
    for relative in ("README.md", "README.zh_CN.md", "data/crud/CRUD_Data.zip", "data/crud/merged.zip"):
        (repo / relative).write_bytes(b"fixture")
    (repo / "data" / "crud_split" / "split_merged.json").write_text(
        json.dumps({"questanswer_1doc": [{"ID": "q1", "news1": "资料", "questions": "问题", "answers": "答案"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: type("Run", (), {"stdout": "fixture-commit\n"})())

    payload = audit(repo)

    assert payload["commit"] == "fixture-commit"
    assert payload["tasks"]["questanswer_1doc"]["sample_count"] == 1
    assert payload["tasks"]["questanswer_1doc"]["has_gold_document_id"] is False
    assert "Recall@K" in payload["evaluation_boundary"]["not_allowed_without_added_gold"]
