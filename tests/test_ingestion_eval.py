import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts import eval_harness, eval_ingestion


ROOT = Path(__file__).resolve().parent.parent
INGESTION_DATASET_PATH = ROOT / "eval" / "ingestion_quality_v0.1.jsonl"


class IngestionEvalTests(unittest.TestCase):
    def test_ingestion_dataset_covers_structured_enterprise_and_government_docs(self):
        records = _load_jsonl(INGESTION_DATASET_PATH)

        self.assertGreaterEqual(len(records), 12)
        self.assertTrue(any(record["document_class"] == "enterprise_technical_plan" for record in records))
        self.assertTrue(any(record["document_class"] == "government_formal_document" for record in records))
        self.assertTrue(any("table" in record.get("required_modalities", []) for record in records))
        self.assertTrue(any("image" in record.get("required_modalities", []) for record in records))

        for record in records:
            self.assertTrue(record["id"].startswith("ING-"))
            self.assertTrue(record["source_filename"].endswith(".pdf"))
            self.assertGreaterEqual(record.get("min_chunk_count", 0), 1)
            self.assertIn("page", record.get("required_metadata", []))
            self.assertIn("dynamic_chunk", record.get("required_metadata", []))
            self.assertIsInstance(record.get("focus", []), list)

    def test_summarize_scores_required_metadata_modalities_and_overlap(self):
        records = [
            {
                "id": "ING-TABLE",
                "source_filename": "alpha.pdf",
                "document_class": "enterprise_technical_plan",
                "min_chunk_count": 2,
                "required_metadata": ["page", "section", "dynamic_chunk", "paragraph_overlap"],
                "required_modalities": ["table"],
            },
            {
                "id": "ING-IMAGE",
                "source_filename": "beta.pdf",
                "document_class": "government_formal_document",
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
                    "chunk_strategy": "dynamic_structure_overlap_v1",
                    "chunk_overlap_chars": 0,
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
                    "chunk_strategy": "dynamic_structure_overlap_v1",
                    "chunk_overlap_chars": 12,
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
