import json
import os
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stderr, redirect_stdout

from scripts import eval_rag


ROOT = Path(__file__).resolve().parent.parent
DATASET_PATH = ROOT / "eval" / "enterprise_zh_v0.2.jsonl"
BLIND_RETRIEVAL_V3_DATASET_PATH = ROOT / "eval" / "retrieval_quality_blind_v0.3.jsonl"
SCRIPT_PATH = ROOT / "scripts" / "eval_rag.py"
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

    def test_blind_retrieval_v03_limits_known_title_questions(self):
        records = _load_jsonl(BLIND_RETRIEVAL_V3_DATASET_PATH)

        self.assertEqual(len(records), 360)
        self.assertEqual(len({record["id"] for record in records}), 360)
        self.assertTrue(all(record["id"].startswith("SRQ3-") for record in records))

        known_title_records = [
            record
            for record in records
            if record.get("allows_title_reference")
        ]
        self.assertEqual(len(known_title_records), 60)
        self.assertEqual(len(records) - len(known_title_records), 300)

        for record in records:
            self.assertIn(record["type"], {"single_doc", "cross_doc", "negative", "safety"})
            self.assertIsInstance(record.get("allows_title_reference"), bool)
            self.assertIsInstance(record.get("doc_ids", []), list)
            self.assertGreaterEqual(len(record.get("focus", [])), 1)
            if record.get("should_refuse"):
                self.assertEqual(record["type"], "negative")
                self.assertEqual(record["expected_sources"], [])
                self.assertEqual(record["expected_keywords"], [])
            else:
                self.assertGreaterEqual(len(record["expected_sources"]), 1)
                self.assertGreaterEqual(len(record["expected_keywords"]), 3)
            if record.get("requires_multi_doc"):
                self.assertEqual(record["type"], "cross_doc")
                self.assertGreaterEqual(len(record["expected_sources"]), 2)

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
        self.assertIn("检索全命中率@3: 100.0%", output)
        self.assertIn("检索任一命中率@3: 100.0%", output)
        self.assertIn("首条命中率@1: 100.0%", output)
        self.assertIn("平均倒数排名(MRR): 1.000", output)
        self.assertIn("平均来源覆盖率@3: 100.0%", output)
        self.assertIn("引用合法率: 100.0%", output)
        self.assertIn("拒答正确率: 100.0%", output)
        self.assertIn("答案关键词覆盖率: 100.0%", output)

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
        self.assertEqual(captured["top_k"], 3)
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
                evidence=[{"id": "E1", "source_filename": "数据安全法.pdf"}],
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
        self.assertIn("检索全命中率@3", output)
        self.assertIn("MRR", output)
        self.assertIn("答案关键词覆盖率", output)
        self.assertIn("+50.0百分点", output)

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
        with patch.object(eval_rag, "find_unlocked_drive_workspace", return_value=None):
            self.assertEqual(eval_rag.resolve_workspace_arg(None), str(ROOT / "souldrive_db"))

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


def _load_dataset():
    records = []
    for line in DATASET_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def _load_jsonl(path: Path):
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


if __name__ == "__main__":
    unittest.main()
