import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from core.keyword_index import KeywordIndex
from scripts import eval_harness, eval_ingestion


ROOT = Path(__file__).resolve().parent.parent
INGESTION_DATASET_PATH = ROOT / "eval" / "ingestion_quality_v0.1.jsonl"
CORPUS_MANIFEST_PATH = ROOT / "eval" / "corpus_manifest_v0.2.json"
MODALITY_GROUND_TRUTH_PATH = ROOT / "eval" / "corpus_modality_ground_truth_v0.1.json"


class IngestionEvalTests(unittest.TestCase):
    def test_ingestion_dataset_covers_structured_enterprise_and_government_docs(self):
        records = _load_jsonl(INGESTION_DATASET_PATH)

        self.assertGreaterEqual(len(records), 216)
        self.assertTrue(any(record["document_class"] == "enterprise_technical_plan" for record in records))
        self.assertTrue(any(record["document_class"] == "government_formal_document" for record in records))
        self.assertTrue(any(record["document_class"] == "government_standard" for record in records))
        self.assertTrue(any(record["document_class"] == "rag_research_reference" for record in records))
        self.assertTrue(any("table" in record.get("required_modalities", []) for record in records))
        self.assertTrue(any("image" in record.get("required_modalities", []) for record in records))

        for record in records:
            self.assertTrue(record["id"].startswith("ING-"))
            self.assertTrue(record["doc_id"].startswith("SDOC-"))
            self.assertTrue(record["source_filename"].endswith(".pdf"))
            self.assertIn(record.get("language"), {"zh", "en"})
            self.assertIn(record.get("difficulty"), {"medium", "hard"})
            self.assertTrue(record.get("evaluation_scope"))
            self.assertGreaterEqual(record.get("min_chunk_count", 0), 1)
            self.assertIn("page", record.get("required_metadata", []))
            self.assertTrue(record.get("required_metadata"))
            self.assertIsInstance(record.get("focus", []), list)
            self.assertGreaterEqual(len(record.get("focus", [])), 3)
        covered_metadata = {
            item
            for record in records
            for item in record["required_metadata"]
        }
        self.assertTrue({
            "dynamic_chunk",
            "document_context",
            "token_safe",
            "formal_window",
            "retrieval_context",
            "source_provenance",
            "parent_linkage",
            "semantic_units",
            "index_schema",
            "content_integrity",
        }.issubset(covered_metadata))

    def test_ingestion_dataset_covers_full_manifest_with_six_samples_per_document(self):
        records = _load_jsonl(INGESTION_DATASET_PATH)
        manifest = json.loads(CORPUS_MANIFEST_PATH.read_text(encoding="utf-8"))
        manifest_doc_ids = {document["doc_id"] for document in manifest["documents"]}
        dataset_doc_ids = {record["doc_id"] for record in records}
        samples_per_document = {
            doc_id: sum(1 for record in records if record["doc_id"] == doc_id)
            for doc_id in dataset_doc_ids
        }

        self.assertEqual(dataset_doc_ids, manifest_doc_ids)
        self.assertEqual(len(records), 216)
        self.assertTrue(all(count >= 6 for count in samples_per_document.values()))

    def test_modality_requirements_match_independent_pdf_ground_truth(self):
        records = _load_jsonl(INGESTION_DATASET_PATH)
        ground_truth = json.loads(MODALITY_GROUND_TRUTH_PATH.read_text(encoding="utf-8"))
        expected = {
            document["doc_id"]: document["required_modalities"]
            for document in ground_truth["documents"]
        }

        self.assertEqual(ground_truth["schema_version"], "corpus_modality_ground_truth_v1")
        self.assertEqual(ground_truth["document_count"], 36)
        for record in records:
            if record["evaluation_scope"] in {
                "structure_and_sectioning",
                "architecture_figure_metadata",
                "english_research_layout",
                "formal_report_layout",
                "scenario_list_preservation",
                "table_and_clause_preservation",
                "text_only_layout",
            }:
                self.assertEqual(record["required_modalities"], expected[record["doc_id"]])

    def test_ingestion_dataset_quality_gate_has_breadth_and_depth(self):
        records = _load_jsonl(INGESTION_DATASET_PATH)

        quality = eval_ingestion.dataset_quality(records)

        self.assertTrue(quality["pass"], quality["checks"])
        self.assertEqual(quality["schema_version"], "ingestion_dataset_quality_v2")
        self.assertEqual(quality["sample_count"], 216)
        self.assertEqual(quality["document_count"], 36)
        self.assertEqual(quality["min_samples_per_document"], 6)
        self.assertGreaterEqual(quality["document_class_count"], 4)
        self.assertGreaterEqual(quality["language_count"], 2)
        self.assertGreaterEqual(quality["evaluation_scope_count"], 10)
        self.assertGreaterEqual(quality["hard_record_count"], 96)
        self.assertGreaterEqual(quality["focus_tag_count"], 50)
        self.assertGreaterEqual(quality["table_record_count"], 44)
        self.assertGreaterEqual(quality["image_record_count"], 30)
        self.assertGreaterEqual(quality["multimodal_record_count"], 20)
        self.assertGreaterEqual(quality["deep_chunk_record_count"], 28)

    def test_summarize_scores_required_metadata_modalities_and_overlap(self):
        records = [
            {
                "id": "ING-TABLE",
                "source_filename": "alpha.pdf",
                "document_class": "enterprise_technical_plan",
                "evaluation_scope": "structure_and_sectioning",
                "difficulty": "hard",
                "min_chunk_count": 2,
                "required_metadata": [
                    "page",
                    "section",
                    "dynamic_chunk",
                    "paragraph_overlap",
                    "formal_window",
                    "document_context",
                    "token_safe",
                ],
                "required_modalities": ["table"],
            },
            {
                "id": "ING-IMAGE",
                "source_filename": "beta.pdf",
                "document_class": "government_formal_document",
                "evaluation_scope": "formal_report_layout",
                "difficulty": "medium",
                "min_chunk_count": 1,
                "required_metadata": ["page", "dynamic_chunk"],
                "required_modalities": ["image"],
            },
        ]
        chunks = [
            {
                "content": "# 总体方案\n第一段。",
                "metadata": {
                    "source_filename": "alpha.pdf",
                    "page": 1,
                    "Header 1": "总体方案",
                    "chunk_strategy": "formal_structure_paragraph_window_v2",
                    "chunk_overlap_chars": 0,
                    "paragraph_window_size": 2,
                    "paragraph_overlap": 1,
                    "document_title": "Alpha 总体方案",
                    "document_profile": "project_plan",
                    "embedding_input_tokens": 80,
                    "embedding_token_limit": 512,
                    "table_count": 1,
                    "tables_json": '[{"markdown":"| 阶段 | 产物 |"}]',
                },
            },
            {
                "content": "第一段。\n\n第二段。",
                "metadata": {
                    "source_filename": "alpha.pdf",
                    "page_number": 1,
                    "Header 1": "总体方案",
                    "chunk_strategy": "formal_structure_paragraph_window_v2",
                    "chunk_overlap_chars": 12,
                    "paragraph_window_size": 2,
                    "paragraph_overlap": 1,
                    "document_title": "Alpha 总体方案",
                    "document_profile": "project_plan",
                    "embedding_input_tokens": 90,
                    "embedding_token_limit": 512,
                },
            },
            {
                "content": "[图像: 流程图]",
                "metadata": {
                    "source_filename": "beta.pdf",
                    "page": 2,
                    "chunk_strategy": "dynamic_structure_overlap_v1",
                    "image_count": 1,
                    "image_metadata_json": '[{"caption":"流程图"}]',
                },
            },
        ]

        report = eval_ingestion.summarize(records, chunks)

        self.assertEqual(report["sample_count"], 2)
        self.assertEqual(report["overall_ingestion_quality_rate"], 1.0)
        self.assertEqual(report["table_coverage_rate"], 1.0)
        self.assertEqual(report["image_metadata_rate"], 1.0)
        self.assertEqual(report["paragraph_overlap_rate"], 1.0)
        self.assertEqual(report["formal_window_rate"], 1.0)
        self.assertEqual(report["document_context_rate"], 1.0)
        self.assertEqual(report["token_safe_rate"], 1.0)
        self.assertEqual(
            report["chunk_char_distribution"]["max"],
            max(len(str(chunk["content"])) for chunk in chunks),
        )
        self.assertEqual(report["oversized_chunk_rate"], 0.0)
        self.assertEqual(report["missing_page_chunk_count"], 0)
        self.assertEqual(report["missing_section_chunk_count"], 1)
        self.assertEqual(
            report["scope_metrics"]["structure_and_sectioning"]["pass_rate"],
            1.0,
        )
        self.assertEqual(report["difficulty_metrics"]["hard"]["sample_count"], 1)
        self.assertFalse(report["dataset_quality"]["pass"])

    def test_paragraph_window_is_not_applicable_to_table_only_document(self):
        records = [{
            "id": "ING-TABLE-ONLY",
            "source_filename": "table-only.pdf",
            "document_class": "government_formal_document",
            "evaluation_scope": "paragraph_window_and_overlap",
            "difficulty": "hard",
            "min_chunk_count": 1,
            "required_metadata": [
                "page",
                "dynamic_chunk",
                "paragraph_overlap",
                "formal_window",
            ],
            "required_modalities": ["table"],
        }]
        chunks = [{
            "content": "| 指标 | 要求 |",
            "metadata": {
                "source_filename": "table-only.pdf",
                "page": 1,
                "content_kind": "table",
                "chunk_strategy": "formal_structure_paragraph_window_v2",
                "paragraph_window_size": 1,
                "paragraph_overlap": 0,
                "chunk_overlap_chars": 0,
                "table_count": 1,
            },
        }]

        report = eval_ingestion.summarize(records, chunks)

        self.assertEqual(report["overall_ingestion_quality_rate"], 1.0)
        self.assertEqual(report["paragraph_overlap_applicability_rate"], 0.0)
        self.assertIsNone(report["paragraph_overlap_rate"])
        self.assertEqual(report["formal_window_applicability_rate"], 0.0)
        self.assertIsNone(report["formal_window_rate"])
        self.assertFalse(report["rows"][0]["paragraph_overlap_applicable"])

    def test_eval_ingestion_cli_reads_chunk_jsonl(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset_path = root / "ingestion.jsonl"
            chunks_path = root / "chunks.jsonl"
            dataset_path.write_text(
                json.dumps(
                    {
                        "id": "ING-ONE",
                        "source_filename": "alpha.pdf",
                        "document_class": "enterprise_technical_plan",
                        "min_chunk_count": 1,
                        "required_metadata": ["page", "dynamic_chunk"],
                        "required_modalities": [],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            chunks_path.write_text(
                json.dumps(
                    {
                        "content": "alpha",
                        "metadata": {
                            "source_filename": "alpha.pdf",
                            "page": 1,
                            "chunk_strategy": "dynamic_structure_overlap_v1",
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            stdout = StringIO()
            with redirect_stdout(stdout):
                return_code = eval_ingestion.main(
                    [
                        "--dataset",
                        str(dataset_path),
                        "--chunks-jsonl",
                        str(chunks_path),
                    ]
                )

        self.assertEqual(return_code, 0)
        self.assertIn("清洗切片评测样本数: 1", stdout.getvalue())
        self.assertIn("综合清洗切片质量: 100.0%", stdout.getvalue())

    def test_collect_workspace_chunks_supports_v2_keyword_index(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "keyword.sqlite")
            index = KeywordIndex(db_path)
            index.upsert_document(
                "child-1",
                "企业技术方案正文",
                {"document_title": "企业技术方案", "page": 1},
            )
            index.close()

            chunks = eval_ingestion.collect_plain_keyword_chunks(db_path)

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].content, "企业技术方案正文")
        self.assertEqual(chunks[0].metadata["document_title"], "企业技术方案")

    def test_ingestion_eval_matches_document_title_when_legacy_index_has_no_source_filename(self):
        records = [{
            "id": "ING-LEGACY",
            "source_filename": "企业技术方案.pdf",
            "document_class": "enterprise_technical_plan",
            "min_chunk_count": 1,
            "required_metadata": ["page", "dynamic_chunk"],
            "required_modalities": [],
        }]
        chunks = [{
            "content": "企业技术方案正文",
            "metadata": {
                "document_title": "企业技术方案",
                "page": 1,
                "chunk_strategy": "dynamic_structure_overlap_v1",
            },
        }]

        report = eval_ingestion.summarize(records, chunks)

        self.assertEqual(report["document_coverage_rate"], 1.0)
        self.assertEqual(report["overall_ingestion_quality_rate"], 1.0)

    def test_ingestion_eval_rejects_legacy_source_for_corpus_document_id(self):
        records = [{
            "id": "ING-ID-LEGACY",
            "doc_id": "SDOC-001",
            "source_filename": "安全治理标准.pdf",
            "document_class": "government_standard",
            "min_chunk_count": 1,
            "required_metadata": ["page", "dynamic_chunk"],
            "required_modalities": [],
        }]
        chunks = [{
            "content": "旧版标准正文",
            "metadata": {
                "source_filename": "安全治理标准-旧版.pdf",
                "document_id": "asset-runtime-old",
                "page": 1,
                "chunk_strategy": "dynamic_structure_overlap_v1",
            },
        }]

        report = eval_ingestion.summarize(records, chunks)

        self.assertTrue(report["rows"][0]["document_identity_required"])
        self.assertEqual(report["rows"][0]["expected_document_id"], "SDOC-001")
        self.assertEqual(report["rows"][0]["matched_chunk_count"], 0)
        self.assertFalse(report["rows"][0]["pass"])

    def test_ingestion_eval_does_not_compare_corpus_id_to_runtime_asset_id(self):
        records = [{
            "id": "ING-ID-RUNTIME",
            "doc_id": "SDOC-001",
            "source_filename": "安全治理标准.pdf",
            "document_class": "government_standard",
            "min_chunk_count": 1,
            "required_metadata": ["page", "dynamic_chunk"],
            "required_modalities": [],
        }]
        chunks = [{
            "content": "当前标准正文",
            "metadata": {
                "source_filename": "安全治理标准.pdf",
                "document_id": "asset-runtime-current",
                "page": 1,
                "chunk_strategy": "dynamic_structure_overlap_v1",
            },
        }]

        report = eval_ingestion.summarize(records, chunks)

        self.assertEqual(report["rows"][0]["matched_chunk_count"], 1)
        self.assertTrue(report["rows"][0]["pass"])

    def test_eval_ingestion_cli_can_audit_dataset_only(self):
        stdout = StringIO()
        with redirect_stdout(stdout):
            return_code = eval_ingestion.main(
                [
                    "--dataset",
                    str(INGESTION_DATASET_PATH),
                    "--audit-dataset-only",
                ]
            )

        self.assertEqual(return_code, 0)
        self.assertIn("评测集质量门禁: 通过", stdout.getvalue())
        self.assertIn("样本=216", stdout.getvalue())
        self.assertIn("文档=36", stdout.getvalue())
        self.assertIn("每文档最少样本=6", stdout.getvalue())
        self.assertIn("多模态样本=", stdout.getvalue())

    def test_harness_combines_retrieval_and_ingestion_reports(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            retrieval_dataset = root / "retrieval.jsonl"
            retrieval_results = root / "retrieval_results.jsonl"
            ingestion_dataset = root / "ingestion.jsonl"
            ingestion_chunks = root / "chunks.jsonl"

            retrieval_dataset.write_text(
                json.dumps(
                    {
                        "id": "RET-1",
                        "question": "Where is alpha?",
                        "expected_sources": ["alpha"],
                        "type": "single_doc",
                        "rubric": ["retrieve alpha"],
                        "expected_keywords": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            retrieval_results.write_text(
                json.dumps({"id": "RET-1", "evidence": [{"source_filename": "alpha.pdf"}]})
                + "\n",
                encoding="utf-8",
            )
            ingestion_dataset.write_text(
                json.dumps(
                    {
                        "id": "ING-1",
                        "source_filename": "alpha.pdf",
                        "document_class": "enterprise_technical_plan",
                        "min_chunk_count": 1,
                        "required_metadata": ["page", "dynamic_chunk"],
                        "required_modalities": [],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            ingestion_chunks.write_text(
                json.dumps(
                    {
                        "content": "alpha",
                        "metadata": {
                            "source_filename": "alpha.pdf",
                            "page": 1,
                            "chunk_strategy": "dynamic_structure_overlap_v1",
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            report = eval_harness.build_harness_report(
                retrieval_dataset_path=retrieval_dataset,
                retrieval_results_path=retrieval_results,
                ingestion_dataset_path=ingestion_dataset,
                ingestion_chunks_path=ingestion_chunks,
                top_k=1,
            )

        self.assertEqual(report["retrieval"]["retrieval_all_hit_rate"], 1.0)
        self.assertEqual(report["ingestion"]["overall_ingestion_quality_rate"], 1.0)
        self.assertEqual(report["harness_score"], 1.0)


def _load_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    unittest.main()
