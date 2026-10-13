import json
import os
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stderr, redirect_stdout

from scripts import eval_rag
from scripts.audit_eval_leakage import audit_records


ROOT = Path(__file__).resolve().parent.parent
DATASET_PATH = ROOT / "eval" / "enterprise_zh_v0.2.jsonl"
STRICT_RETRIEVAL_DATASET_PATH = ROOT / "eval" / "retrieval_quality_strict_v0.1.jsonl"
STRICT_RETRIEVAL_V2_DATASET_PATH = ROOT / "eval" / "retrieval_quality_strict_v0.2.jsonl"
BLIND_RETRIEVAL_V3_DATASET_PATH = ROOT / "eval" / "retrieval_quality_blind_v0.3.jsonl"
CORPUS_MANIFEST_V2_PATH = ROOT / "eval" / "corpus_manifest_v0.2.json"
CORPUS_FILE_INVENTORY_V2_PATH = ROOT / "eval" / "corpus_file_inventory_v0.2.json"
SCRIPT_PATH = ROOT / "scripts" / "eval_rag.py"
LOCAL_EXISTING_DOCUMENTS = {
    "LazyLLM企业级RAG方案-私有化部署与权限安全.pdf",
    "“城市+AI”应用场景清单（第五批）.pdf",
    "人工智能安全治理蓝皮书.pdf",
    "基于大模型和RAG的知识库问答系统.pdf",
    "大模型基准测试体系研究报告.pdf",
    "大模型知识管理系统.pdf",
    "大模型私有部署与基础应用落地.pdf",
    "数据安全技术 数据分类分级规则.pdf",
    "数据要素发展报告.pdf",
    "生成式人工智能服务安全基本要求.pdf",
    "知识库问答场景中大语言模型私有化研究与应用实践.pdf",
    "综合交通运输大模型智能体创新应用.pdf",
}
ALLOWED_SOURCE_HINTS = {
    "大模型知识管理系统",
    "大模型基准测试体系研究报告",
    "数据分类分级规则",
    "生成式人工智能服务安全基本要求",
    "知识库问答场景中大语言模型私有化研究与应用实践",
    "大模型私有部署与基础应用落地",
    "城市+AI应用场景清单",
    "综合交通运输大模型智能体创新应用典型案例",
    "人工智能安全治理蓝皮书",
    "数据要素发展报告",
    "基于大模型和RAG的知识库问答系统",
    "企业级RAG方案",
    "MaxKB",
    "LazyLLM",
}


class EvalAssetsTests(unittest.TestCase):
    def test_enterprise_eval_dataset_has_readable_required_fields(self):
        records = _load_dataset()

        self.assertGreaterEqual(len(records), 80)
        ids = {record["id"] for record in records}
        self.assertIn("KM-01", ids)
        self.assertIn("NEG-01", ids)
        self.assertIn("LAZY-01", ids)
        self.assertIn("XSEC-01", ids)

        for record in records:
            self.assertIsInstance(record["id"], str)
            self.assertIsInstance(record["question"], str)
            self.assertIsInstance(record["expected_sources"], list)
            self.assertIsInstance(record["rubric"], list)
            self.assertIsInstance(record.get("expected_keywords", []), list)
            self.assertIsInstance(record.get("should_refuse", False), bool)
            self.assertIsInstance(record.get("requires_multi_doc", False), bool)
            self.assertIsInstance(record.get("graph_relevant", False), bool)
            self.assertIn(record["type"], {"single_doc", "cross_doc", "negative", "safety"})

    def test_enterprise_eval_dataset_targets_enterprise_private_rag_documents(self):
        records = _load_dataset()
        covered_sources = set()

        for record in records:
            for source in record["expected_sources"]:
                self.assertIn(source, ALLOWED_SOURCE_HINTS)
                covered_sources.add(source)

        self.assertGreaterEqual(len(covered_sources), 10)
        self.assertTrue(any(record.get("requires_multi_doc") for record in records))
        self.assertTrue(any(record.get("graph_relevant") for record in records))
        self.assertTrue(any(record.get("should_refuse") for record in records))

    def test_strict_retrieval_eval_dataset_is_clear_and_demanding(self):
        records = _load_jsonl(STRICT_RETRIEVAL_DATASET_PATH)

        self.assertGreaterEqual(len(records), 60)
        ids = [record["id"] for record in records]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(item.startswith("SRQ-") for item in ids))

        covered_sources = {
            source
            for record in records
            for source in record["expected_sources"]
        }
        cross_doc_records = [record for record in records if record.get("requires_multi_doc")]
        graph_records = [record for record in records if record.get("graph_relevant")]
        refusal_records = [record for record in records if record.get("should_refuse")]

        self.assertGreaterEqual(len(covered_sources), 12)
        self.assertGreaterEqual(len(cross_doc_records), 18)
        self.assertGreaterEqual(len(graph_records), 12)
        self.assertGreaterEqual(len(refusal_records), 8)

        for record in records:
            self.assertIn(record["type"], {"single_doc", "cross_doc", "negative", "safety"})
            self.assertGreaterEqual(len(record["question"].strip()), 12)
            self.assertGreaterEqual(len(record["rubric"]), 3)
            self.assertIsInstance(record.get("expected_keywords", []), list)
            if record.get("should_refuse"):
                self.assertEqual(record["expected_sources"], [])
                self.assertEqual(record["expected_keywords"], [])
            else:
                self.assertGreaterEqual(len(record["expected_sources"]), 1)
                self.assertGreaterEqual(len(record["expected_keywords"]), 3)
                for source in record["expected_sources"]:
                    self.assertIn(source, ALLOWED_SOURCE_HINTS)
            if record.get("requires_multi_doc"):
                self.assertEqual(record["type"], "cross_doc")
                self.assertGreaterEqual(len(record["expected_sources"]), 2)

    def test_strict_retrieval_v02_dataset_scales_to_full_corpus(self):
        manifest = _load_json(CORPUS_MANIFEST_V2_PATH)
        records = _load_jsonl(STRICT_RETRIEVAL_V2_DATASET_PATH)
        source_hints = {
            Path(document["local_filename"]).stem
            for document in manifest["documents"]
        }

        self.assertGreaterEqual(len(records), 300)
        self.assertLessEqual(len(records), 500)

        ids = [record["id"] for record in records]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(item.startswith("SRQ2-") for item in ids))

        covered_sources = {
            source
            for record in records
            for source in record["expected_sources"]
        }
        self.assertEqual(covered_sources, source_hints)

        cross_doc_records = [record for record in records if record.get("requires_multi_doc")]
        graph_records = [record for record in records if record.get("graph_relevant")]
        refusal_records = [record for record in records if record.get("should_refuse")]
        safety_records = [record for record in records if record["type"] == "safety"]
        rerank_records = [
            record
            for record in records
            if "rerank" in record.get("focus", []) or "rerank" in record.get("expected_keywords", [])
        ]

        self.assertGreaterEqual(len(cross_doc_records), 90)
        self.assertGreaterEqual(len(graph_records), 60)
        self.assertGreaterEqual(len(refusal_records), 36)
        self.assertGreaterEqual(len(safety_records), 60)
        self.assertGreaterEqual(len(rerank_records), 36)

        for record in records:
            self.assertIn(record["type"], {"single_doc", "cross_doc", "negative", "safety"})
            self.assertGreaterEqual(len(record["question"].strip()), 16)
            self.assertGreaterEqual(len(record["rubric"]), 3)
            self.assertIsInstance(record.get("expected_keywords", []), list)
            self.assertIsInstance(record.get("focus", []), list)
            self.assertGreaterEqual(len(record.get("focus", [])), 1)
            if record.get("should_refuse"):
                self.assertEqual(record["type"], "negative")
                self.assertEqual(record["expected_sources"], [])
                self.assertEqual(record["expected_keywords"], [])
            else:
                self.assertGreaterEqual(len(record["expected_sources"]), 1)
                self.assertGreaterEqual(len(record["expected_keywords"]), 3)
                for source in record["expected_sources"]:
                    self.assertIn(source, source_hints)
            if record.get("requires_multi_doc"):
                self.assertEqual(record["type"], "cross_doc")
                self.assertGreaterEqual(len(record["expected_sources"]), 2)

    def test_blind_retrieval_v03_limits_known_title_questions(self):
        manifest = _load_json(CORPUS_MANIFEST_V2_PATH)
        records = _load_jsonl(BLIND_RETRIEVAL_V3_DATASET_PATH)
        source_hints = {
            Path(document["local_filename"]).stem
            for document in manifest["documents"]
        }
        source_titles = {
            document["doc_id"]: [
                document["title"],
                Path(document["local_filename"]).stem,
            ]
            for document in manifest["documents"]
        }

        self.assertEqual(len(records), 360)
        self.assertEqual(
            {
                source
                for record in records
                for source in record["expected_sources"]
            },
            source_hints,
        )

        known_title_records = [
            record
            for record in records
            if record.get("allows_title_reference")
        ]
        self.assertLessEqual(len(known_title_records), 60)
        self.assertGreaterEqual(len(records) - len(known_title_records), 300)

        for record in records:
            self.assertIsInstance(record.get("allows_title_reference"), bool)
            title_leaks = _full_source_mentions(record, source_titles)
            if record.get("allows_title_reference"):
                self.assertTrue(title_leaks)
            else:
                self.assertEqual(title_leaks, [])

    def test_blind_retrieval_v03_questions_are_readable(self):
        records = _load_jsonl(BLIND_RETRIEVAL_V3_DATASET_PATH)

        self.assertEqual(len(records), 360)
        for record in records:
            question = record["question"].strip()
            searchable_chars = [
                char
                for char in question
                if "\u4e00" <= char <= "\u9fff" or char.isascii() and char.isalnum()
            ]

            self.assertNotIn("??", question)
            self.assertGreaterEqual(len(searchable_chars), 12)

    def test_blind_retrieval_v03_is_a_sealed_nonleaking_gate(self):
        records = _load_jsonl(BLIND_RETRIEVAL_V3_DATASET_PATH)

        self.assertEqual({record.get("split") for record in records}, {"sealed_test"})
        self.assertTrue(audit_records(records)["pass"])

    def test_corpus_manifest_v02_covers_local_and_candidate_documents(self):
        manifest = _load_json(CORPUS_MANIFEST_V2_PATH)
        documents = manifest["documents"]

        self.assertEqual(manifest["version"], "v0.2")
        self.assertGreaterEqual(len(documents), 35)

        ids = [document["doc_id"] for document in documents]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(doc_id.startswith("SDOC-") for doc_id in ids))

        local_documents = [
            document
            for document in documents
            if document["origin"] == "local_existing"
        ]
        candidate_documents = [
            document
            for document in documents
            if document["origin"] == "download_candidate"
        ]
        local_filenames = {document["local_filename"] for document in local_documents}

        self.assertEqual(local_filenames, LOCAL_EXISTING_DOCUMENTS)
        self.assertEqual(len(local_documents), 12)
        self.assertGreaterEqual(len(candidate_documents), 20)

        total_question_budget = sum(document["question_budget"] for document in documents)
        self.assertGreaterEqual(total_question_budget, 300)
        self.assertLessEqual(total_question_budget, 500)

        covered_tags = {
            tag
            for document in documents
            for tag in document["topic_tags"]
        }
        self.assertGreaterEqual(
            covered_tags,
            {
                "rag",
                "rerank",
                "evaluation",
                "ai_safety",
                "data_security",
                "data_elements",
                "private_deployment",
                "industry_scenario",
            },
        )

        for document in documents:
            self.assertIn(document["origin"], {"local_existing", "download_candidate"})
            self.assertIn(document["language"], {"zh", "en"})
            self.assertGreaterEqual(document["question_budget"], 6)
            self.assertGreaterEqual(len(document["topic_tags"]), 2)
            self.assertTrue(document["title"].strip())
            self.assertTrue(document["local_filename"].endswith(".pdf"))
            if document["origin"] == "download_candidate":
                self.assertTrue(document["source_url"].startswith("https://"))
                self.assertEqual(document["status"], "to_download")
            else:
                self.assertEqual(document["status"], "present")

    def test_corpus_file_inventory_v02_matches_manifest_documents(self):
        manifest = _load_json(CORPUS_MANIFEST_V2_PATH)
        inventory = _load_json(CORPUS_FILE_INVENTORY_V2_PATH)

        manifest_ids = {document["doc_id"] for document in manifest["documents"]}
        inventory_files = inventory["files"]
        inventory_ids = {item["doc_id"] for item in inventory_files}

        self.assertEqual(inventory["manifest_version"], manifest["version"])
        self.assertEqual(inventory_ids, manifest_ids)
        self.assertEqual(inventory["summary"]["document_count"], len(manifest_ids))
        self.assertEqual(inventory["summary"]["missing_count"], 0)
        self.assertEqual(inventory["summary"]["bad_pdf_header_count"], 0)
        self.assertGreater(inventory["summary"]["total_size_bytes"], 0)
        self.assertGreater(inventory["summary"]["total_pages"], 0)
        self.assertGreater(inventory["summary"]["total_text_chars"], 100000)

        for item in inventory_files:
            self.assertTrue(item["local_filename"].endswith(".pdf"))
            self.assertGreater(item["size_bytes"], 0)
            self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(item["pdf_header_ok"])
            self.assertGreater(item["page_count"], 0)
            self.assertGreater(item["text_chars"], 100)

    def test_eval_script_prints_plain_chinese_metrics_from_fixture(self):
        fixture = [
            {
                "id": "PASS-01",
                "question": "企业知识库为什么需要审计日志？",
                "expected_sources": ["数据安全法"],
                "type": "single_doc",
                "rubric": ["追溯", "安全保护义务"],
                "expected_keywords": ["审计", "追溯"],
            },
            {
                "id": "NEG-01",
                "question": "SoulDrive 的创始人是谁？",
                "expected_sources": [],
                "type": "negative",
                "rubric": ["应拒答"],
                "should_refuse": True,
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            dataset_path = Path(temp_dir) / "fixture.jsonl"
            dataset_path.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in fixture),
                encoding="utf-8",
            )
            result_path = Path(temp_dir) / "retrieval_results.jsonl"
            result_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "id": "PASS-01",
                                "answer": "企业知识库需要审计日志以支持追溯 [E1]。",
                                "evidence": [
                                    {
                                        "id": "E1",
                                        "source_filename": "数据安全法.pdf",
                                        "score": 0.24,
                                    }
                                ],
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "id": "NEG-01",
                                "answer": "根据本地知识库，未找到足够可靠的相关证据。",
                                "evidence": [],
                            },
                            ensure_ascii=False,
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                return_code = eval_rag.main(
                    [
                        "--dataset",
                        str(dataset_path),
                        "--from-results",
                        str(result_path),
                    ]
                )

        output = stdout.getvalue()
        self.assertEqual(return_code, 0)
        self.assertIn("评测样本数: 2", output)
        self.assertIn("检索全命中率@6: 100.0%", output)
        self.assertIn("检索任一命中率@6: 100.0%", output)
        self.assertIn("首条命中率@1: 100.0%", output)
        self.assertIn("平均倒数排名(MRR): 1.000", output)
        self.assertIn("平均来源覆盖率@6: 100.0%", output)
        self.assertIn("引用合法率: 100.0%", output)
        self.assertIn("拒答正确率: 100.0%", output)
        self.assertIn("答案关键词覆盖率: 100.0%", output)

    def test_eval_results_fail_fast_when_dataset_ids_do_not_match(self):
        records = [
            eval_rag.EvalRecord(
                id="DEV-01",
                question="dev question",
                expected_sources=["dev.pdf"],
                type="single_doc",
                rubric=[],
                expected_keywords=[],
                should_refuse=False,
                requires_multi_doc=False,
                graph_relevant=False,
            )
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = Path(temp_dir) / "results.jsonl"
            result_path.write_text(
                json.dumps({"id": "SEALED-01", "evidence": []}) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "ID 不匹配"):
                eval_rag.load_results(result_path, records)

    def test_project_scoped_results_require_matching_project_id(self):
        records = [
            eval_rag.EvalRecord(
                id="SCOPED-01",
                question="项目内问题",
                expected_sources=["project.pdf"],
                type="single_doc",
                rubric=[],
                expected_keywords=[],
                should_refuse=False,
                requires_multi_doc=False,
                graph_relevant=False,
                project_id="project-a",
            )
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = Path(temp_dir) / "results.jsonl"
            result_path.write_text(
                json.dumps({"id": "SCOPED-01", "evidence": []}) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "project_id"):
                eval_rag.load_results(result_path, records)

    def test_workflow_scoped_results_require_matching_workflow_id(self):
        records = [
            eval_rag.EvalRecord(
                id="WORKFLOW-01",
                question="论文方法是什么？",
                expected_sources=["paper.pdf"],
                type="single_doc",
                rubric=[],
                expected_keywords=[],
                should_refuse=False,
                requires_multi_doc=False,
                graph_relevant=False,
                workflow_id="paper_review",
            )
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = Path(temp_dir) / "results.jsonl"
            result_path.write_text(
                json.dumps(
                    {
                        "id": "WORKFLOW-01",
                        "evidence": [],
                        "workflow_id": "experiment_compare",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "workflow_id"):
                eval_rag.load_results(result_path, records)

    def test_eval_results_reject_dataset_fingerprint_mismatch(self):
        records = [
            eval_rag.EvalRecord(
                id="FINGERPRINT-01",
                question="评测标签是否稳定？",
                expected_sources=["eval.pdf"],
                type="single_doc",
                rubric=["稳定"],
                expected_keywords=["稳定"],
                should_refuse=False,
                requires_multi_doc=False,
                graph_relevant=False,
            )
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = Path(temp_dir) / "results.jsonl"
            result_path.write_text(
                json.dumps(
                    {
                        "id": "FINGERPRINT-01",
                        "evidence": [],
                        "dataset_fingerprint": "sha256:not-the-current-dataset",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "dataset_fingerprint"):
                eval_rag.load_results(result_path, records)

    def test_eval_results_round_trip_dataset_fingerprint(self):
        record = eval_rag.EvalRecord(
            id="FINGERPRINT-ROUNDTRIP-01",
            question="评测版本是什么？",
            expected_sources=["eval.pdf"],
            type="single_doc",
            rubric=["版本"],
            expected_keywords=["版本"],
            should_refuse=False,
            requires_multi_doc=False,
            graph_relevant=False,
        )
        result = eval_rag.EvalResult(
            id=record.id,
            question=record.question,
            expected_sources=record.expected_sources,
            answer="评测版本已绑定。",
            evidence=[],
            elapsed_ms=10,
            dataset_fingerprint=eval_rag.evaluation_dataset_fingerprint([record]),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = Path(temp_dir) / "results.jsonl"
            eval_rag.save_results(result_path, [result])
            loaded = eval_rag.load_results(result_path, [record])

        self.assertEqual(loaded[0].dataset_fingerprint, result.dataset_fingerprint)

    def test_eval_run_manifest_records_dataset_and_execution_lineage(self):
        record = eval_rag.EvalRecord(
            id="MANIFEST-01",
            question="本次评测使用什么数据？",
            expected_sources=["eval.pdf"],
            type="single_doc",
            rubric=["数据"],
            expected_keywords=["数据"],
            should_refuse=False,
            requires_multi_doc=False,
            graph_relevant=False,
        )
        result = eval_rag.EvalResult(
            id=record.id,
            question=record.question,
            expected_sources=record.expected_sources,
            answer="已绑定评测数据。",
            evidence=[],
            elapsed_ms=10,
            model_name="test-model",
            dataset_fingerprint=eval_rag.evaluation_dataset_fingerprint([record]),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            dataset_path = Path(temp_dir) / "dataset.jsonl"
            dataset_path.write_text(
                json.dumps(
                    {
                        "id": record.id,
                        "question": record.question,
                        "expected_sources": record.expected_sources,
                        "type": record.type,
                        "rubric": record.rubric,
                        "expected_keywords": record.expected_keywords,
                        "should_refuse": record.should_refuse,
                        "requires_multi_doc": record.requires_multi_doc,
                        "graph_relevant": record.graph_relevant,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            manifest_path = Path(temp_dir) / "run_manifest.json"
            workspace_root = Path(temp_dir) / "SoulDrive"
            for relative_path, payload in (
                ("index/keyword_index.sqlite", b"keyword-index-v1"),
                ("index/knowledge_graph.sqlite", b"graph-index-v1"),
            ):
                artifact_path = workspace_root / relative_path
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                artifact_path.write_bytes(payload)

            manifest = eval_rag.save_run_manifest(
                manifest_path,
                dataset_path=dataset_path,
                records=[record],
                results=[result],
                mode="答案生成评测",
                top_k=6,
                baseline_level="CANDIDATE",
                run_id="manifest-test-01",
                retrieval_options={"rerank_candidate_limit": 12},
                source_results_path=None,
                output_results_path=None,
                workspace_path=str(workspace_root),
                summary={"overall_reliability_rate": 1.0},
            )
            self.assertTrue(manifest_path.exists())

        self.assertEqual(manifest["schema_version"], "souldrive_run_manifest_v0.1")
        self.assertEqual(manifest["run_id"], "manifest-test-01")
        self.assertEqual(manifest["baseline_level"], "CANDIDATE")
        self.assertEqual(
            manifest["components"]["dataset_fingerprint"],
            result.dataset_fingerprint,
        )
        self.assertEqual(manifest["components"]["retrieval_options"]["rerank_candidate_limit"], 12)
        self.assertEqual(manifest["components"]["model_names"], ["test-model"])
        self.assertEqual(manifest["components"]["workspace_index_snapshot"]["status"], "captured")
        self.assertEqual(
            len(manifest["components"]["workspace_index_snapshot"]["artifacts"]),
            2,
        )
        self.assertTrue(
            manifest["components"]["workspace_index_snapshot"]["artifact_fingerprint"].startswith(
                "sha256:"
            )
        )
        self.assertEqual(manifest["outputs"]["manifest"], "run_manifest.json")

    def test_eval_dataset_rejects_unknown_workflow_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            dataset_path = Path(temp_dir) / "dataset.jsonl"
            dataset_path.write_text(
                json.dumps(
                    {
                        "id": "WORKFLOW-UNKNOWN-01",
                        "question": "问题",
                        "expected_sources": [],
                        "type": "negative",
                        "rubric": [],
                        "expected_keywords": [],
                        "should_refuse": True,
                        "requires_multi_doc": False,
                        "graph_relevant": False,
                        "workflow_id": "unbounded_agent",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "workflow_id"):
                eval_rag.load_dataset(dataset_path)

    def test_live_retrieval_propagates_record_project_scope(self):
        records = [
            eval_rag.EvalRecord(
                id="SCOPED-01",
                question="项目内问题",
                expected_sources=["project.pdf"],
                type="single_doc",
                rubric=[],
                expected_keywords=[],
                should_refuse=False,
                requires_multi_doc=False,
                graph_relevant=False,
                project_id="project-a",
            )
        ]
        captured = []

        class FakeKnowledgeBase:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def search_with_evidence(self, query, *, top_k, project_id=None, global_only=False):
                captured.append((query, top_k, project_id, global_only))
                return {"evidence": []}

            def close(self):
                pass

        with patch("core.knowledge_base.LocalKnowledgeBase", FakeKnowledgeBase):
            results = eval_rag.run_retrieval(records, workspace="unused", top_k=3)

        self.assertEqual(captured, [("项目内问题", 3, "project-a", False)])
        self.assertEqual(results[0].project_id, "project-a")

    def test_live_retrieval_propagates_workflow_focus_variants(self):
        records = [
            eval_rag.EvalRecord(
                id="WORKFLOW-RETRIEVAL-01",
                question="总结这篇论文",
                expected_sources=["paper.pdf"],
                type="single_doc",
                rubric=[],
                expected_keywords=[],
                should_refuse=False,
                requires_multi_doc=False,
                graph_relevant=False,
                workflow_id="paper_review",
            )
        ]
        captured = []

        class FakeKnowledgeBase:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def search_with_evidence(
                self,
                query,
                *,
                top_k,
                extra_query_variants=None,
                global_only=False,
            ):
                captured.append((query, top_k, extra_query_variants, global_only))
                return {"evidence": []}

            def close(self):
                pass

        with patch("core.knowledge_base.LocalKnowledgeBase", FakeKnowledgeBase):
            results = eval_rag.run_retrieval(records, workspace="unused", top_k=3)

        self.assertEqual(captured[0][0], "总结这篇论文")
        self.assertEqual(captured[0][1], 3)
        self.assertTrue(captured[0][2])
        self.assertTrue(any("研究问题" in item for item in captured[0][2]))
        self.assertTrue(captured[0][3])
        self.assertEqual(results[0].workflow_id, "paper_review")
        self.assertEqual(
            results[0].dataset_fingerprint,
            eval_rag.evaluation_dataset_fingerprint(records),
        )

    def test_live_retrieval_enables_graph_store_for_graph_relevant_records(self):
        records = [
            eval_rag.EvalRecord(
                id="GRAPH-01",
                question="图谱关系问题",
                expected_sources=["graph.pdf"],
                type="graph",
                rubric=[],
                expected_keywords=[],
                should_refuse=False,
                requires_multi_doc=False,
                graph_relevant=True,
            )
        ]
        graph_store = object()
        captured = []

        class FakeKnowledgeBase:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def search_with_evidence(
                self,
                query,
                *,
                top_k,
                project_id=None,
                graph_db=None,
                global_only=False,
            ):
                captured.append((query, top_k, project_id, graph_db, global_only))
                return {"evidence": []}

            def close(self):
                pass

        with patch("core.knowledge_base.LocalKnowledgeBase", FakeKnowledgeBase), patch.object(
            eval_rag,
            "_open_retrieval_graph_store",
            return_value=graph_store,
        ) as open_graph:
            results = eval_rag.run_retrieval(records, workspace="unused", top_k=3)

        open_graph.assert_called_once_with("unused")
        self.assertEqual(captured, [("图谱关系问题", 3, None, graph_store, True)])
        self.assertEqual(results[0].project_id, None)

    def test_graph_eval_keeps_route_execution_separate_from_source_hit(self):
        record = eval_rag.EvalRecord(
            id="GRAPH-ROUTE-01",
            question="普通事实问题",
            expected_sources=["answer.pdf"],
            type="single_doc",
            rubric=[],
            expected_keywords=[],
            should_refuse=False,
            requires_multi_doc=False,
            graph_relevant=True,
        )

        class FakeKnowledgeBase:
            def __init__(self, **kwargs):
                pass

            def search_with_evidence(self, query, *, top_k, graph_db=None, global_only=False):
                _ = global_only
                return {
                    "evidence": [{"source_filename": "answer.pdf"}],
                    "observability": {
                        "schema_version": "retrieval_observability_v1",
                        "graph_route": {"use_graph": False, "mode": "hybrid"},
                        "graph_recall_enabled": False,
                        "graph_context_count": 0,
                    },
                }

            def close(self):
                pass

        with patch("core.knowledge_base.LocalKnowledgeBase", FakeKnowledgeBase), patch.object(
            eval_rag,
            "_open_retrieval_graph_store",
            return_value=object(),
        ):
            results = eval_rag.run_retrieval([record], workspace="unused", top_k=3)

        self.assertFalse(results[0].retrieval_observability["graph_route"]["use_graph"])
        report = eval_rag.summarize([record], results, top_k=3)
        self.assertEqual(report["graph_source_hit_rate"], 1.0)
        self.assertEqual(report["graph_route_selected_rate"], 0.0)
        self.assertEqual(report["graph_stage_executed_rate"], 0.0)
        self.assertEqual(report["graph_context_nonempty_rate"], 0.0)

    def test_eval_results_round_trip_retrieval_observability(self):
        record = eval_rag.EvalRecord(
            id="OBS-ROUNDTRIP-01",
            question="图谱路由是否执行？",
            expected_sources=["graph.pdf"],
            type="graph",
            rubric=[],
            expected_keywords=[],
            should_refuse=False,
            requires_multi_doc=False,
            graph_relevant=True,
        )
        result = eval_rag.EvalResult(
            id=record.id,
            question=record.question,
            expected_sources=record.expected_sources,
            answer="",
            evidence=[],
            elapsed_ms=12,
            retrieval_observability={
                "graph_route": {"use_graph": True, "mode": "local"},
                "graph_recall_enabled": True,
                "graph_context_count": 2,
            },
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = Path(temp_dir) / "results.jsonl"
            eval_rag.save_results(result_path, [result])
            loaded = eval_rag.load_results(result_path, [record])

        self.assertEqual(
            loaded[0].retrieval_observability,
            result.retrieval_observability,
        )

    def test_answer_fallback_propagates_workflow_context(self):
        captured = {}

        class FakeKB:
            def search_with_evidence(self, **kwargs):
                raise AssertionError("generation should not fall back")

        class FakeRag:
            kb = FakeKB()
            graph_db = object()

            def generate_response_stream(self, *args, **kwargs):
                captured["interaction_context"] = kwargs.get("interaction_context")
                return iter(
                    [
                        "## 研究问题\n问题说明 [E1]\n\n"
                        "## 核心方法\n方法说明 [E1]\n\n"
                        "## 实验结果\n结果说明 [E1]\n\n"
                        "## 局限与复现\n局限说明 [E1]\n\n"
                        "## 汇报提纲\n提纲说明 [E1]\n\n"
                        "```souldrive-evidence\n"
                        '[{"id":"E1","source_filename":"paper.pdf",'
                        '"context":"论文证据"}]\n'
                        "```"
                    ]
                )

        answer, evidence = eval_rag.generate_answer_with_fallback(
            FakeRag(),
            "总结这篇论文",
            project_id="project-a",
            workflow_id="paper_review",
            top_k=3,
        )

        self.assertEqual(
            captured["interaction_context"],
            {
                "active_context": {"project_id": "project-a"},
                "workflow_id": "paper_review",
            },
        )
        self.assertIn("## 汇报提纲", answer)
        self.assertEqual(evidence[0]["id"], "E1")

    def test_answer_fallback_can_capture_retrieval_trace(self):
        captured = {}

        class FakeKB:
            def search_with_evidence(self, **kwargs):
                raise AssertionError("generation should not fall back")

        class FakeRag:
            kb = FakeKB()
            graph_db = object()

            def generate_response_stream(self, *args, **kwargs):
                kwargs["on_retrieval_completed"]({
                    "schema_version": "agentic_retrieval_trace_v1",
                    "status": "pass",
                    "agentic_decision": "generate",
                    "stop_reason": "accepted_for_generation",
                    "evidence_count": 1,
                })
                return iter([
                    "结论来自证据。[E1]\n\n"
                    "```souldrive-evidence\n"
                    '[{"id":"E1","source_filename":"paper.pdf",'
                    '"context":"论文证据"}]\n'
                    "```"
                ])

        answer, evidence = eval_rag.generate_answer_with_fallback(
            FakeRag(),
            "总结这篇论文",
            project_id=None,
            workflow_id=None,
            top_k=3,
            on_retrieval_completed=captured.update,
        )

        self.assertIn("[E1]", answer)
        self.assertEqual(evidence[0]["id"], "E1")
        self.assertEqual(captured["status"], "pass")
        self.assertEqual(captured["evidence_count"], 1)

    def test_workflow_output_contract_is_scored_separately(self):
        record = eval_rag.EvalRecord(
            id="WORKFLOW-CONTRACT-01",
            question="总结这篇论文",
            expected_sources=["paper.pdf"],
            type="single_doc",
            rubric=[],
            expected_keywords=[],
            should_refuse=False,
            requires_multi_doc=False,
            graph_relevant=False,
            workflow_id="paper_review",
        )
        result = eval_rag.EvalResult(
            id=record.id,
            question=record.question,
            expected_sources=record.expected_sources,
            answer=(
                "## 研究问题\n问题 [E1]\n\n"
                "## 核心方法\n方法 [E1]\n\n"
                "## 实验结果\n结果 [E1]\n\n"
                "## 局限与复现\n局限 [E1]\n\n"
                "## 汇报提纲\n提纲 [E1]"
            ),
            evidence=[
                {
                    "id": "E1",
                    "source_filename": "paper.pdf",
                    "context": "论文证据。",
                }
            ],
            elapsed_ms=100,
            workflow_id="paper_review",
        )

        row = eval_rag.score_record(record, result, top_k=3)
        report = eval_rag.summarize([record], [result], top_k=3, mode="答案生成评测")

        self.assertTrue(row["workflow_contract_valid"])
        self.assertEqual(report["workflow_sample_count"], 1)
        self.assertEqual(report["workflow_output_contract_pass_rate"], 1.0)
        self.assertEqual(report["workflow_breakdown"]["paper_review"]["sample_count"], 1)

    def test_project_scoped_score_rejects_foreign_project_evidence(self):
        record = eval_rag.EvalRecord(
            id="SCOPED-02",
            question="项目内问题",
            expected_sources=["project.pdf"],
            type="single_doc",
            rubric=[],
            expected_keywords=[],
            should_refuse=False,
            requires_multi_doc=False,
            graph_relevant=False,
            project_id="project-a",
        )
        result = eval_rag.EvalResult(
            id=record.id,
            question=record.question,
            expected_sources=record.expected_sources,
            answer="项目证据 [E1]。",
            evidence=[
                {
                    "id": "E1",
                    "source_filename": "project.pdf",
                    "project_id": "project-b",
                    "context": "项目证据。",
                }
            ],
            elapsed_ms=10,
            project_id="project-a",
        )

        row = eval_rag.score_record(record, result, top_k=3)

        self.assertTrue(row["wrong_project"])
        self.assertFalse(row["scope_ok"])
        self.assertFalse(row["answerable_pass"])

    def test_multi_doc_answer_requires_every_expected_source(self):
        record = eval_rag.EvalRecord(
            id="MULTI-PARTIAL-01",
            question="请比较 alpha 与 beta。",
            expected_sources=["alpha", "beta"],
            type="cross_doc",
            rubric=["必须同时使用两个来源"],
            expected_keywords=["alpha"],
            should_refuse=False,
            requires_multi_doc=True,
            graph_relevant=False,
        )
        result = eval_rag.EvalResult(
            id=record.id,
            question=record.question,
            expected_sources=record.expected_sources,
            answer="alpha 方案说明。[E1]",
            evidence=[{
                "id": "E1",
                "source_filename": "alpha.pdf",
                "context": "alpha 方案说明。",
            }],
            elapsed_ms=100,
        )

        row = eval_rag.score_record(record, result, top_k=3)

        self.assertTrue(row["source_any_hit"])
        self.assertFalse(row["source_all_hit"])
        self.assertFalse(row["answerable_pass"])

    def test_multi_doc_answer_must_cite_every_expected_source(self):
        record = eval_rag.EvalRecord(
            id="MULTI-CITATION-01",
            question="请比较 alpha 与 beta。",
            expected_sources=["alpha", "beta"],
            type="cross_doc",
            rubric=["答案必须分别引用两个来源"],
            expected_keywords=["alpha", "beta"],
            should_refuse=False,
            requires_multi_doc=True,
            graph_relevant=False,
        )
        result = eval_rag.EvalResult(
            id=record.id,
            question=record.question,
            expected_sources=record.expected_sources,
            answer="alpha 与 beta 均有记录。[E1]",
            evidence=[
                {
                    "id": "E1",
                    "source_filename": "alpha.pdf",
                    "context": "alpha 与 beta 均有记录。",
                },
                {
                    "id": "E2",
                    "source_filename": "beta.pdf",
                    "context": "alpha 与 beta 均有记录。",
                },
            ],
            elapsed_ms=100,
        )

        row = eval_rag.score_record(record, result, top_k=3)

        self.assertTrue(row["source_all_hit"])
        self.assertFalse(row["expected_source_citation_all"])
        self.assertFalse(row["answerable_pass"])

        report = eval_rag.summarize([record], [result], top_k=3)

        self.assertEqual(report["answer_expected_source_all_hit_rate"], 0.0)
        self.assertEqual(report["answer_expected_source_coverage"], 0.5)

    def test_context_precision_is_position_sensitive_and_excludes_unanswerable_rows(self):
        record = eval_rag.EvalRecord(
            id="CONTEXT-PRECISION-01",
            question="请比较 alpha 与 beta。",
            expected_sources=["alpha", "beta"],
            type="cross_doc",
            rubric=["期望来源应尽量排在前面"],
            expected_keywords=[],
            should_refuse=False,
            requires_multi_doc=True,
            graph_relevant=False,
        )
        refusal = eval_rag.EvalRecord(
            id="CONTEXT-PRECISION-NEG",
            question="资料中没有答案的问题。",
            expected_sources=[],
            type="negative",
            rubric=["应拒答"],
            expected_keywords=[],
            should_refuse=True,
            requires_multi_doc=False,
            graph_relevant=False,
        )
        results = [
            eval_rag.EvalResult(
                id=record.id,
                question=record.question,
                expected_sources=record.expected_sources,
                answer="",
                evidence=[
                    {"id": "N1", "source_filename": "noise.pdf"},
                    {"id": "E1", "source_filename": "alpha.pdf"},
                    {"id": "E2", "source_filename": "beta.pdf"},
                ],
                elapsed_ms=10,
            ),
            eval_rag.EvalResult(
                id=refusal.id,
                question=refusal.question,
                expected_sources=[],
                answer="未找到足够可靠的相关证据，因此不能回答。",
                evidence=[],
                elapsed_ms=10,
            ),
        ]

        report = eval_rag.summarize([record, refusal], results, top_k=3)

        row = report["rows"][0]
        self.assertAlmostEqual(row["context_precision"], (0.5 + (2 / 3)) / 2)
        self.assertEqual(report["context_precision_sample_count"], 1)
        self.assertAlmostEqual(report["average_context_precision"], (0.5 + (2 / 3)) / 2)

    def test_context_precision_is_perfect_when_relevant_contexts_are_first(self):
        record = eval_rag.EvalRecord(
            id="CONTEXT-PRECISION-02",
            question="alpha 的结论是什么？",
            expected_sources=["alpha"],
            type="single_doc",
            rubric=[],
            expected_keywords=[],
            should_refuse=False,
            requires_multi_doc=False,
            graph_relevant=False,
        )
        result = eval_rag.EvalResult(
            id=record.id,
            question=record.question,
            expected_sources=record.expected_sources,
            answer="",
            evidence=[
                {"id": "E1", "source_filename": "alpha.pdf"},
                {"id": "N1", "source_filename": "noise.pdf"},
            ],
            elapsed_ms=10,
        )

        row = eval_rag.score_record(record, result, top_k=2)

        self.assertEqual(row["context_precision"], 1.0)

    def test_miss_analyzer_groups_unhit_sources_and_partial_multi_doc_records(self):
        from scripts import analyze_rag_eval_misses

        records = [
            {
                "id": "MISS-01",
                "question": "Where is alpha evidence?",
                "expected_sources": ["alpha"],
                "type": "single_doc",
                "rubric": ["must retrieve alpha"],
                "expected_keywords": ["alpha"],
            },
            {
                "id": "PARTIAL-01",
                "question": "Compare alpha and beta evidence.",
                "expected_sources": ["alpha", "beta"],
                "type": "cross_doc",
                "rubric": ["must retrieve both"],
                "expected_keywords": ["alpha", "beta"],
                "requires_multi_doc": True,
            },
            {
                "id": "HIT-01",
                "question": "Where is gamma evidence?",
                "expected_sources": ["gamma"],
                "type": "single_doc",
                "rubric": ["must retrieve gamma"],
                "expected_keywords": ["gamma"],
            },
        ]
        results = [
            {
                "id": "MISS-01",
                "evidence": [
                    {"source_filename": "beta.pdf"},
                    {"source_filename": "gamma.pdf"},
                ],
            },
            {
                "id": "PARTIAL-01",
                "evidence": [
                    {"source_filename": "alpha.pdf"},
                    {"source_filename": "gamma.pdf"},
                ],
            },
            {
                "id": "HIT-01",
                "evidence": [
                    {"source_filename": "gamma.pdf"},
                ],
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            dataset_path = Path(temp_dir) / "dataset.jsonl"
            results_path = Path(temp_dir) / "results.jsonl"
            dataset_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
                encoding="utf-8",
            )
            results_path.write_text(
                "\n".join(json.dumps(result, ensure_ascii=False) for result in results) + "\n",
                encoding="utf-8",
            )

            report = analyze_rag_eval_misses.analyze_result_files(dataset_path, results_path, top_k=3)

        self.assertEqual(report["sample_count"], 3)
        self.assertEqual(report["miss_count"], 2)
        self.assertEqual(report["misses_by_type"], {"cross_doc": 1, "single_doc": 1})
        self.assertEqual(report["missed_sources"], {"alpha": 1, "beta": 1})
        self.assertEqual(report["partial_multi_doc_misses"][0]["id"], "PARTIAL-01")
        self.assertEqual(report["partial_multi_doc_misses"][0]["missed_sources"], ["beta"])
        sample_by_id = {sample["id"]: sample for sample in report["miss_samples"]}
        self.assertEqual(sample_by_id["MISS-01"]["top_sources"], ["beta.pdf", "gamma.pdf"])
        self.assertEqual(sample_by_id["PARTIAL-01"]["hit_sources"], ["alpha"])

    def test_miss_analyzer_uses_the_same_corpus_identity_rule_as_scoring(self):
        from scripts import analyze_rag_eval_misses

        record = {
            "id": "MISS-ID-01",
            "question": "只允许当前语料来源。",
            "expected_sources": ["安全治理标准"],
            "doc_ids": ["SDOC-001"],
            "type": "single_doc",
            "rubric": [],
            "expected_keywords": [],
        }
        result = {
            "id": "MISS-ID-01",
            "evidence": [
                {
                    "source_filename": "安全治理标准-旧版.pdf",
                    "document_id": "asset-runtime-old",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            dataset_path = Path(temp_dir) / "dataset.jsonl"
            results_path = Path(temp_dir) / "results.jsonl"
            dataset_path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
            results_path.write_text(json.dumps(result, ensure_ascii=False) + "\n", encoding="utf-8")

            report = analyze_rag_eval_misses.analyze_result_files(dataset_path, results_path, top_k=3)

        self.assertEqual(report["miss_count"], 1)
        sample = report["miss_samples"][0]
        self.assertTrue(sample["document_identity_required"])
        self.assertEqual(sample["expected_document_ids"], ["SDOC-001"])
        self.assertEqual(sample["top_document_ids"], ["asset-runtime-old"])

    def test_eval_script_can_run_optional_answer_generation_with_model_override(self):
        fixture = [
            {
                "id": "ANS-01",
                "question": "企业知识库为什么需要审计日志？",
                "expected_sources": ["数据安全法"],
                "type": "single_doc",
                "rubric": ["追溯"],
                "expected_keywords": ["审计", "追溯"],
            }
        ]
        captured = {}

        def fake_run_answer_generation(records, workspace, top_k):
            captured["workspace"] = workspace
            captured["top_k"] = top_k
            captured["chat_model"] = os.environ.get("SOULDRIVE_CHAT_MODEL")
            return [
                eval_rag.EvalResult(
                    id=records[0].id,
                    question=records[0].question,
                    expected_sources=records[0].expected_sources,
                    answer="企业知识库需要审计日志来支持审计和追溯 [E1]。",
                    evidence=[
                        {
                            "id": "E1",
                            "source_filename": "数据安全法.pdf",
                            "score": 0.2,
                            "context": "企业知识库需要审计日志来支持审计和追溯。",
                        }
                    ],
                    elapsed_ms=1200,
                    retrieval_ms=200,
                    generation_ms=1000,
                    model_name=os.environ.get("SOULDRIVE_CHAT_MODEL"),
                )
            ]

        with tempfile.TemporaryDirectory() as temp_dir:
            dataset_path = Path(temp_dir) / "fixture.jsonl"
            dataset_path.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in fixture),
                encoding="utf-8",
            )
            result_path = Path(temp_dir) / "answer_results.jsonl"

            stdout = io.StringIO()
            with patch.object(eval_rag, "resolve_workspace_arg", return_value=str(Path(temp_dir) / "SoulDrive")), patch.object(
                eval_rag, "prepare_workspace_keys", return_value=True
            ), patch.object(eval_rag, "workspace_requires_key", return_value=False), patch.object(
                eval_rag, "run_answer_generation", side_effect=fake_run_answer_generation
            ), redirect_stdout(stdout):
                return_code = eval_rag.main(
                    [
                        "--dataset",
                        str(dataset_path),
                        "--workspace",
                        str(Path(temp_dir) / "SoulDrive"),
                        "--include-answers",
                        "--chat-model",
                        "qwen-test.gguf",
                        "--save-results",
                        str(result_path),
                    ]
                )
            saved = json.loads(result_path.read_text(encoding="utf-8").splitlines()[0])

        output = stdout.getvalue()
        self.assertEqual(return_code, 0)
        self.assertEqual(captured["chat_model"], "qwen-test.gguf")
        self.assertEqual(captured["top_k"], 6)
        self.assertIn("评测模式: 答案生成评测", output)
        self.assertIn("生成模型: qwen-test.gguf", output)
        self.assertIn("综合可靠率: 100.0%", output)
        self.assertEqual(saved["model_name"], "qwen-test.gguf")
        self.assertEqual(saved["generation_ms"], 1000)

    def test_answer_quality_summary_marks_reliable_refusal_and_leakage(self):
        records = [
            eval_rag.EvalRecord(
                id="GOOD-01",
                question="企业知识库为什么需要审计日志？",
                expected_sources=["数据安全法"],
                type="single_doc",
                rubric=["追溯"],
                expected_keywords=["审计", "追溯"],
                should_refuse=False,
                requires_multi_doc=False,
                graph_relevant=False,
            ),
            eval_rag.EvalRecord(
                id="NEG-OK",
                question="SoulDrive 创始人是谁？",
                expected_sources=[],
                type="negative",
                rubric=["应拒答"],
                expected_keywords=[],
                should_refuse=True,
                requires_multi_doc=False,
                graph_relevant=False,
            ),
            eval_rag.EvalRecord(
                id="NEG-BAD",
                question="SoulDrive 是否已经中标某银行项目？",
                expected_sources=[],
                type="negative",
                rubric=["应拒答"],
                expected_keywords=[],
                should_refuse=True,
                requires_multi_doc=False,
                graph_relevant=False,
            ),
        ]
        results = [
            eval_rag.EvalResult(
                id="GOOD-01",
                question=records[0].question,
                expected_sources=records[0].expected_sources,
                answer="企业知识库需要审计日志来支持审计和追溯 [E1]。",
                evidence=[{
                    "id": "E1",
                    "source_filename": "数据安全法.pdf",
                    "context": "企业知识库需要审计日志来支持审计和追溯。",
                }],
                elapsed_ms=1000,
            ),
            eval_rag.EvalResult(
                id="NEG-OK",
                question=records[1].question,
                expected_sources=[],
                answer="未找到足够可靠的相关证据，因此不能回答。",
                evidence=[],
                elapsed_ms=900,
            ),
            eval_rag.EvalResult(
                id="NEG-BAD",
                question=records[2].question,
                expected_sources=[],
                answer="SoulDrive 已经中标某银行项目。",
                evidence=[],
                elapsed_ms=800,
            ),
        ]

        report = eval_rag.summarize(records, results, top_k=3)

        self.assertEqual(report["answer_sample_count"], 3)
        self.assertEqual(report["answerable_pass_rate"], 1.0)
        self.assertEqual(report["refusal_leak_rate"], 0.5)
        self.assertEqual(report["overall_reliability_rate"], 2 / 3)
        self.assertEqual(report["invalid_citation_rate"], 1 / 3)
        self.assertEqual(report["citation_support_valid_rate"], 1.0)
        self.assertEqual(report["claim_support_checked_count"], 1)
        self.assertEqual(report["claim_support_unsupported_claim_count"], 0)
        self.assertEqual(
            report["retrieval_confidence_intervals_95"][
                "retrieval_all_hit_rate"
            ]["total"],
            1,
        )

    def test_answer_quality_summary_rejects_citation_without_body_support(self):
        record = eval_rag.EvalRecord(
            id="UNSUPPORTED-01",
            question="当前系统的准确率是多少？",
            expected_sources=["metrics"],
            type="single_doc",
            rubric=["数字必须有证据"],
            expected_keywords=["准确率"],
            should_refuse=False,
            requires_multi_doc=False,
            graph_relevant=False,
        )
        result = eval_rag.EvalResult(
            id=record.id,
            question=record.question,
            expected_sources=record.expected_sources,
            answer="当前系统的准确率为 99%。[E1]",
            evidence=[{
                "id": "E1",
                "source_filename": "metrics.pdf",
                "context": "当前系统记录了运行时间，但没有准确率数字。",
            }],
            elapsed_ms=100,
        )

        report = eval_rag.summarize([record], [result], top_k=3)

        self.assertEqual(report["citation_valid_rate"], 1.0)
        self.assertEqual(report["citation_support_valid_rate"], 0.0)
        self.assertEqual(report["claim_support_checked_count"], 1)
        self.assertEqual(report["claim_support_unsupported_claim_count"], 1)
        self.assertEqual(report["answerable_pass_rate"], 0.0)

    def test_answer_quality_counts_empty_generated_answers_in_denominators(self):
        records = [
            eval_rag.EvalRecord(
                id="EMPTY-ANSWER",
                question="系统有什么优势？",
                expected_sources=["system"],
                type="single_doc",
                rubric=["必须回答"],
                expected_keywords=["优势"],
                should_refuse=False,
                requires_multi_doc=False,
                graph_relevant=False,
            ),
            eval_rag.EvalRecord(
                id="EMPTY-REFUSAL",
                question="系统是否获得某项未公开大奖？",
                expected_sources=[],
                type="negative",
                rubric=["应拒答"],
                expected_keywords=[],
                should_refuse=True,
                requires_multi_doc=False,
                graph_relevant=False,
            ),
        ]
        results = [
            eval_rag.EvalResult(
                id=record.id,
                question=record.question,
                expected_sources=record.expected_sources,
                answer="",
                evidence=[],
                elapsed_ms=100,
                model_name="qwen-test",
            )
            for record in records
        ]

        report = eval_rag.summarize(records, results, top_k=3, mode="答案生成评测")

        self.assertEqual(report["answer_sample_count"], 2)
        self.assertEqual(report["answerable_pass_rate"], 0.0)
        self.assertEqual(report["refusal_accuracy"], 0.0)
        self.assertEqual(report["overall_reliability_rate"], 0.0)
        self.assertEqual(report["answer_keyword_coverage"], 0.0)

    def test_evidence_gap_language_counts_as_a_refusal(self):
        self.assertTrue(eval_rag.answer_refuses("资料中未包含该产品的价格信息，无法依据现有文档回答。"))

    def test_eval_script_prints_comparison_report_from_saved_answer_results(self):
        fixture = [
            {
                "id": "ANS-01",
                "question": "企业知识库为什么需要审计日志？",
                "expected_sources": ["数据安全法"],
                "type": "single_doc",
                "rubric": ["追溯"],
                "expected_keywords": ["审计", "追溯"],
            }
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            dataset_path = Path(temp_dir) / "fixture.jsonl"
            dataset_path.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in fixture),
                encoding="utf-8",
            )
            baseline_path = Path(temp_dir) / "3b.jsonl"
            candidate_path = Path(temp_dir) / "7b.jsonl"
            baseline_path.write_text(
                json.dumps(
                    {
                        "id": "ANS-01",
                        "answer": "企业知识库需要审计日志 [E1]。",
                        "evidence": [{"id": "E1", "source_filename": "数据安全法.pdf"}],
                        "elapsed_ms": 900,
                        "model_name": "3b",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            candidate_path.write_text(
                json.dumps(
                    {
                        "id": "ANS-01",
                        "answer": "企业知识库需要审计日志来支持审计和追溯 [E1]。",
                        "evidence": [{"id": "E1", "source_filename": "数据安全法.pdf"}],
                        "elapsed_ms": 1200,
                        "model_name": "7b",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                return_code = eval_rag.main(
                    [
                        "--dataset",
                        str(dataset_path),
                        "--compare-results",
                        str(baseline_path),
                        str(candidate_path),
                        "--baseline-label",
                        "3B",
                        "--candidate-label",
                        "7B",
                    ]
                )

        output = stdout.getvalue()
        self.assertEqual(return_code, 0)
        self.assertIn("对比报告: 3B -> 7B", output)
        self.assertIn("答案关键词覆盖率", output)
        self.assertIn("答案必需来源引用全命中率", output)
        self.assertIn("+50.0百分点", output)

    def test_paired_retrieval_comparison_uses_exact_mcnemar_test(self):
        baseline_rows = [
            {"id": f"Q{index}", "source_all_hit": False}
            for index in range(6)
        ]
        candidate_rows = [
            {"id": f"Q{index}", "source_all_hit": True}
            for index in range(6)
        ]

        report = eval_rag.paired_binary_comparison(
            baseline_rows,
            candidate_rows,
            "source_all_hit",
        )

        self.assertEqual(report["sample_count"], 6)
        self.assertEqual(report["improved"], 6)
        self.assertEqual(report["regressed"], 0)
        self.assertEqual(report["unchanged"], 0)
        self.assertEqual(report["p_value"], 0.03125)

    def test_eval_script_comparison_report_includes_retrieval_metrics(self):
        fixture = [
            {
                "id": "RET-01",
                "question": "Where is alpha?",
                "expected_sources": ["alpha"],
                "type": "single_doc",
                "rubric": ["retrieve alpha"],
                "expected_keywords": [],
            },
            {
                "id": "RET-02",
                "question": "Where is beta?",
                "expected_sources": ["beta"],
                "type": "single_doc",
                "rubric": ["retrieve beta"],
                "expected_keywords": [],
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            dataset_path = Path(temp_dir) / "fixture.jsonl"
            dataset_path.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in fixture),
                encoding="utf-8",
            )
            baseline_path = Path(temp_dir) / "baseline.jsonl"
            candidate_path = Path(temp_dir) / "candidate.jsonl"
            baseline_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "id": "RET-01",
                                "evidence": [{"source_filename": "alpha.pdf"}],
                                "elapsed_ms": 100,
                            }
                        ),
                        json.dumps(
                            {
                                "id": "RET-02",
                                "evidence": [{"source_filename": "gamma.pdf"}],
                                "elapsed_ms": 100,
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            candidate_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "id": "RET-01",
                                "evidence": [{"source_filename": "alpha.pdf"}],
                                "elapsed_ms": 80,
                            }
                        ),
                        json.dumps(
                            {
                                "id": "RET-02",
                                "evidence": [{"source_filename": "beta.pdf"}],
                                "elapsed_ms": 90,
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                return_code = eval_rag.main(
                    [
                        "--dataset",
                        str(dataset_path),
                        "--compare-results",
                        str(baseline_path),
                        str(candidate_path),
                        "--baseline-label",
                        "baseline",
                        "--candidate-label",
                        "candidate",
                    ]
                )

        output = stdout.getvalue()
        self.assertEqual(return_code, 0)
        self.assertIn("MRR", output)
        self.assertIn("0.500 -> 1.000", output)
        self.assertIn("+50.0", output)

    def test_eval_script_can_force_utf8_stdio_on_windows(self):
        class FakeStream:
            def __init__(self):
                self.reconfigure_calls = []

            def reconfigure(self, **kwargs):
                self.reconfigure_calls.append(kwargs)

        stdout = FakeStream()
        stderr = FakeStream()

        with patch.object(eval_rag.os, "name", "nt"), patch.object(eval_rag.sys, "stdout", stdout), patch.object(
            eval_rag.sys, "stderr", stderr
        ):
            eval_rag.configure_utf8_stdio()

        self.assertEqual(stdout.reconfigure_calls, [{"encoding": "utf-8"}])
        self.assertEqual(stderr.reconfigure_calls, [{"encoding": "utf-8"}])

    def test_source_matching_accepts_local_filename_aliases(self):
        self.assertTrue(eval_rag.source_matches("“城市+AI”应用场景清单（第五批）.pdf", "城市+AI应用场景清单"))
        self.assertTrue(eval_rag.source_matches("综合交通运输大模型智能体创新应用.pdf", "综合交通运输大模型智能体创新应用典型案例"))
        self.assertTrue(eval_rag.source_matches("基于大模型和RAG的知识库问答系统.pdf", "MaxKB"))

    def test_corpus_doc_id_ground_truth_does_not_accept_same_named_wrong_document(self):
        record = eval_rag.EvalRecord(
            id="DOC-ID-01",
            question="只允许命中文档身份一致的证据。",
            expected_sources=["安全治理标准"],
            type="single_doc",
            rubric=[],
            expected_keywords=[],
            should_refuse=False,
            requires_multi_doc=False,
            graph_relevant=False,
            expected_document_ids=["doc-expected"],
        )
        result = eval_rag.EvalResult(
            id=record.id,
            question=record.question,
            expected_sources=record.expected_sources,
            answer="错误版本的证据 [E1]。",
            evidence=[
                {
                    "id": "E1",
                    "source_filename": "安全治理标准-旧版.pdf",
                    "document_id": "doc-other",
                    "context": "错误版本。",
                }
            ],
            elapsed_ms=10,
        )

        row = eval_rag.score_record(record, result, top_k=3)

        self.assertTrue(row["document_identity_required"])
        self.assertFalse(row["source_any_hit"])
        self.assertFalse(row["answerable_pass"])

    def test_corpus_doc_id_does_not_compare_runtime_asset_id(self):
        record = eval_rag.EvalRecord(
            id="DOC-ID-02",
            question="语料库身份应通过来源标签匹配。",
            expected_sources=["安全治理标准"],
            type="single_doc",
            rubric=[],
            expected_keywords=[],
            should_refuse=False,
            requires_multi_doc=False,
            graph_relevant=False,
            expected_document_ids=["SDOC-001"],
        )
        result = eval_rag.EvalResult(
            id=record.id,
            question=record.question,
            expected_sources=record.expected_sources,
            answer="正确版本的证据 [E1]。",
            evidence=[
                {
                    "id": "E1",
                    "source_filename": "安全治理标准.pdf",
                    "document_id": "asset-runtime-123",
                    "context": "正确版本。",
                }
            ],
            elapsed_ms=10,
        )

        row = eval_rag.score_record(record, result, top_k=3)

        self.assertTrue(row["document_identity_required"])
        self.assertTrue(row["source_any_hit"])
        self.assertTrue(row["answerable_pass"])

    def test_explicit_corpus_identity_in_metadata_overrides_source_display_name(self):
        record = eval_rag.EvalRecord(
            id="DOC-ID-03",
            question="显式评测身份应优先。",
            expected_sources=["安全治理标准"],
            type="single_doc",
            rubric=[],
            expected_keywords=[],
            should_refuse=False,
            requires_multi_doc=False,
            graph_relevant=False,
            expected_document_ids=["SDOC-001"],
        )
        result = eval_rag.EvalResult(
            id=record.id,
            question=record.question,
            expected_sources=record.expected_sources,
            answer="已绑定评测身份 [E1]。",
            evidence=[
                {
                    "id": "E1",
                    "source_filename": "规范化显示标题.pdf",
                    "document_id": "asset-runtime-123",
                    "metadata": {"evaluation_doc_id": "SDOC-001"},
                    "context": "带显式语料标签。",
                }
            ],
            elapsed_ms=10,
        )

        row = eval_rag.score_record(record, result, top_k=3)

        self.assertTrue(row["source_any_hit"])

    def test_eval_dataset_preserves_document_identity_ids(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            dataset_path = Path(temp_dir) / "identity.jsonl"
            dataset_path.write_text(
                json.dumps(
                    {
                        "id": "DOC-ID-LOAD-01",
                        "question": "身份字段应保留。",
                        "expected_sources": ["安全治理标准"],
                        "doc_ids": ["doc-expected"],
                        "type": "single_doc",
                        "rubric": [],
                        "expected_keywords": [],
                        "should_refuse": False,
                        "requires_multi_doc": False,
                        "graph_relevant": False,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            records = eval_rag.load_dataset(dataset_path)

        self.assertEqual(records[0].expected_document_ids, ["doc-expected"])

    def test_all_known_local_document_names_match_dataset_sources(self):
        local_names = [
            "LazyLLM企业级RAG方案-私有化部署与权限安全.pdf",
            "基于大模型和RAG的知识库问答系统.pdf",
            "数据要素发展报告.pdf",
            "人工智能安全治理蓝皮书.pdf",
            "综合交通运输大模型智能体创新应用.pdf",
            "“城市+AI”应用场景清单（第五批）.pdf",
            "大模型私有部署与基础应用落地.pdf",
            "知识库问答场景中大语言模型私有化研究与应用实践.pdf",
            "生成式人工智能服务安全基本要求.pdf",
            "数据安全技术 数据分类分级规则.pdf",
            "大模型基准测试体系研究报告.pdf",
            "大模型知识管理系统.pdf",
        ]
        expected_sources = {
            source
            for record in _load_dataset()
            for source in record["expected_sources"]
        }

        unmatched = [
            source
            for source in expected_sources
            if not any(eval_rag.source_matches(name, source) for name in local_names)
        ]

        self.assertEqual(unmatched, [])

    def test_eval_script_reports_missing_passphrase_for_encrypted_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "SoulDrive"
            (workspace / "config").mkdir(parents=True)
            (workspace / "index").mkdir()
            (workspace / "config" / "workspace.json").write_text("{}", encoding="utf-8")
            (workspace / "index" / "secure_vectors.sqlite").write_bytes(b"")

            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                eval_rag.main(["--dataset", str(DATASET_PATH), "--workspace", str(workspace)])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--passphrase", stderr.getvalue())

    def test_eval_script_can_resolve_workspace_from_runtime_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "SoulDrive"
            (workspace / "config").mkdir(parents=True)
            (workspace / "config" / "workspace.json").write_text("{}", encoding="utf-8")

            original_get_runtime_state = eval_rag.get_runtime_state
            try:
                eval_rag.get_runtime_state = lambda: {
                    "workspace_path": str(workspace),
                    "locked": False,
                    "software_unlocked": True,
                }

                self.assertEqual(eval_rag.resolve_workspace_arg(None), str(workspace))
            finally:
                eval_rag.get_runtime_state = original_get_runtime_state

    def test_eval_script_can_resolve_project_local_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            workspace = project_root / "souldrive_db"
            (workspace / "config").mkdir(parents=True)
            (workspace / "config" / "workspace.json").write_text("{}", encoding="utf-8")

            with patch.object(eval_rag, "PROJECT_ROOT", project_root), patch.object(
                eval_rag, "find_unlocked_drive_workspace", return_value=None
            ):
                self.assertEqual(eval_rag.resolve_workspace_arg(None), str(workspace))

    def test_eval_script_prefers_unlocked_drive_workspace_over_project_local_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            drive_root = Path(temp_dir)
            workspace = drive_root / "SoulDrive"
            (workspace / "config").mkdir(parents=True)
            (workspace / "runtime").mkdir()
            (workspace / "config" / "workspace.json").write_text("{}", encoding="utf-8")
            (workspace / "runtime" / "runtime_state.json").write_text(
                json.dumps({"locked": False, "workspace_path": str(workspace)}),
                encoding="utf-8",
            )

            fake_drive = type("Drive", (), {"mountpoint": str(drive_root) + os.sep})()
            with patch.object(eval_rag, "iter_filesystem_drives", return_value=[fake_drive]):
                self.assertEqual(eval_rag.resolve_workspace_arg(None), str(workspace))

    def test_eval_script_prepares_encrypted_workspace_from_passphrase(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            from core.security_context import clear_workspace_keys, get_workspace_keys
            from core.workspace import SoulDriveWorkspace
            from core.workspace_crypto import initialize_keystore

            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "secret-passphrase")

            try:
                self.assertTrue(eval_rag.prepare_workspace_keys(workspace.root_path, "secret-passphrase"))
                self.assertIsNotNone(get_workspace_keys(workspace.root_path))
            finally:
                clear_workspace_keys(workspace.root_path)

    def test_eval_script_auto_selects_ollama_when_llama_cpp_is_missing(self):
        with patch.object(eval_rag.importlib.util, "find_spec", return_value=None), patch.dict(
            os.environ,
            {
                "SOULDRIVE_CHAT_PROVIDER": "",
                "SOULDRIVE_OLLAMA_MODEL": "",
                "SOULDRIVE_OLLAMA_URL": "",
            },
            clear=False,
        ):
            eval_rag.configure_answer_generation_backend()
            self.assertEqual(os.environ.get("SOULDRIVE_CHAT_PROVIDER"), "ollama")
            self.assertEqual(os.environ.get("SOULDRIVE_OLLAMA_MODEL"), "qwen3.5:9b")
            self.assertEqual(os.environ.get("SOULDRIVE_OLLAMA_URL"), "http://127.0.0.1:11434")

    def test_eval_script_falls_back_to_extractable_answer_when_generation_fails(self):
        class FakeKB:
            def search_with_evidence(self, **kwargs):
                return {
                    "evidence": [
                        {
                            "id": "E1",
                            "section": "核心流程",
                            "snippet": "RAG 的核心流程一般包括检索、重排和生成三个阶段。",
                            "context": "RAG 的核心流程一般包括检索、重排和生成三个阶段。",
                        }
                    ]
                }

        class FakeRag:
            kb = FakeKB()
            graph_db = object()

            def generate_response_stream(self, *args, **kwargs):
                raise ModuleNotFoundError("llama_cpp")

        answer, evidence = eval_rag.generate_answer_with_fallback(
            FakeRag(),
            "RAG 的核心流程是什么？",
            project_id=None,
            top_k=3,
        )

        self.assertTrue(answer)
        self.assertEqual(evidence[0]["id"], "E1")
        self.assertIn("[E1]", answer)

    def test_eval_script_does_not_replace_completed_refusal_with_fallback(self):
        class FakeKB:
            def search_with_evidence(self, **kwargs):
                return {
                    "evidence": [{
                        "id": "E1",
                        "snippet": "作者联系方式和论文内容。",
                        "context": "作者联系方式和论文内容。",
                    }]
                }

        class FakeRag:
            kb = FakeKB()
            graph_db = object()

            def generate_response_stream(self, *args, **kwargs):
                return iter(["为保护个人隐私，无法提供或推断私人住址、电话、身份证号码等敏感个人信息。"])

        answer, evidence = eval_rag.generate_answer_with_fallback(
            FakeRag(),
            "请列出论文作者的私人手机号。",
            project_id=None,
            top_k=3,
        )

        self.assertIn("无法提供或推断", answer)
        self.assertEqual(evidence, [])


def _load_dataset():
    return _load_jsonl(DATASET_PATH)


def _load_jsonl(path: Path):
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _full_source_mentions(record: dict, source_titles: dict[str, list[str]]) -> list[str]:
    question = eval_rag.normalize_source_name(record["question"])
    mentions = []
    for doc_id in record.get("doc_ids", []):
        for source in source_titles.get(doc_id, []):
            normalized = eval_rag.normalize_source_name(source)
            if len(normalized) >= 8 and normalized in question:
                mentions.append(source)
    return mentions


if __name__ == "__main__":
    unittest.main()
