import json

from scripts.build_product_b1_corpus import build
from scripts.eval_product_b1_ingestion import evaluate
from scripts.summarize_product_b1_indexing import summarize


def test_product_b1_corpus_is_auditable_and_catalogued(tmp_path):
    corpus = tmp_path / "corpus"
    workspace = tmp_path / "workspace"
    manifest = build(corpus, workspace)
    report = evaluate(corpus / "product_benchmark_manifest_v0.1.json", workspace)

    assert manifest["asset_count"] == 30
    assert {item["project_id"] for item in manifest["assets"]} == {"project_souldrive_agent", "project_vision_optimizer"}
    assert report["asset_file_integrity_rate"] == 1
    assert report["catalog_registration_rate"] == 1
    assert report["current_format_support_rate"] == 1
    assert (workspace / "data" / "papers" / "project_souldrive_agent" / "01_requirement.pdf").is_file()
    json.dumps(report, ensure_ascii=False)


def test_product_b1_indexing_summary_counts_failures():
    report = summarize({"indexing": {"status": "completed", "discovered_files": 2, "chunk_count": 1, "failure_summary": {"OCR": 1}, "observability": {"documents": [{"status": "indexed"}, {"status": "failed"}], "totals": {}}}})
    assert report["parse_success_rate"] == 0.5
    assert report["failed_files"] == 1
