import json

from scripts.freeze_crud_rag_subset import freeze


def test_freeze_creates_stable_derived_evidence_and_sealed_split(tmp_path):
    source = tmp_path / "split.json"
    rows = {
        "questanswer_1doc": [{"ID": str(index), "news1": f"资料 {index}", "questions": "问题", "answers": "答案"} for index in range(30)],
        "questanswer_2docs": [{"ID": f"b{index}", "news1": "资料一", "news2": "资料二", "questions": "问题", "answers": "答案"} for index in range(30)],
        "questanswer_3docs": [{"ID": f"c{index}", "news1": "资料一", "news2": "资料二", "news3": "资料三", "questions": "问题", "answers": "答案"} for index in range(30)],
        "event_summary": [{"ID": f"s{index}", "text": "新闻", "summary": "摘要"} for index in range(20)],
        "hallu_modified": [{"ID": f"h{index}", "newsBeginning": "原文", "realContinuation": "真实续写", "hallucinatedContinuation": "虚构续写", "allKeywords": {}} for index in range(20)],
    }
    source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    manifest = freeze(source, tmp_path / "out")
    samples = [json.loads(line) for line in (tmp_path / "out" / "samples.jsonl").read_text(encoding="utf-8").splitlines()]

    assert manifest["sample_count"] == 104
    assert set(item["split"] for item in samples) == {"calibration", "sealed_test"}
    assert all(item["derived_gold_document_ids"] for item in samples)
    assert manifest["retrieval_metric_policy"].startswith("derived_")
