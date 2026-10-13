import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.knowledge_base import _reranker_passage
from core.retrieval import SearchCandidate
from core.reranker import (
    LocalReranker,
    _normalize_reranker_scores,
    preferred_reranker_model_name,
    reranker_runtime_diagnostics,
)


class RerankerTests(unittest.TestCase):
    def tearDown(self):
        patch.stopall()

    def test_preferred_reranker_model_prefers_compact_cross_encoder_when_available(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_dir = Path(temp_dir)
            (model_dir / "mmarco-mMiniLMv2-L6-H384-v1").mkdir(parents=True)
            (model_dir / "bge-reranker-base").mkdir(parents=True)

            with patch.dict("os.environ", {"SOULDRIVE_MODEL_DIR": str(model_dir)}):
                self.assertEqual(preferred_reranker_model_name(), "mmarco-mMiniLMv2-L6-H384-v1")

    def test_preferred_reranker_model_prefers_qwen3_when_available(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_dir = Path(temp_dir)
            (model_dir / "Qwen3-Reranker-0.6B").mkdir(parents=True)
            (model_dir / "mmarco-mMiniLMv2-L6-H384-v1").mkdir(parents=True)

            with patch.dict("os.environ", {"SOULDRIVE_MODEL_DIR": str(model_dir)}):
                self.assertEqual(preferred_reranker_model_name(), "Qwen3-Reranker-0.6B")

    def test_preferred_reranker_model_prefers_qwen3_across_search_dirs(self):
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            (Path(first_dir) / "mmarco-mMiniLMv2-L6-H384-v1").mkdir(parents=True)
            (Path(second_dir) / "Qwen3-Reranker-0.6B").mkdir(parents=True)

            with patch(
                "core.reranker.model_search_dirs",
                return_value=[Path(first_dir), Path(second_dir)],
            ):
                self.assertEqual(preferred_reranker_model_name(), "Qwen3-Reranker-0.6B")

    def test_reranker_runtime_diagnostics_reports_disabled_when_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict("os.environ", {
                "SOULDRIVE_MODEL_DIR": temp_dir,
                "SOULDRIVE_APP_ROOT": temp_dir,
            }), patch("core.reranker.model_search_dirs", return_value=[Path(temp_dir)]):
                report = reranker_runtime_diagnostics()

        self.assertFalse(report["ready"])
        self.assertEqual(report["mode"], "disabled")

    def test_local_reranker_scores_pairs_when_model_exists(self):
        class FakeCrossEncoder:
            def __init__(self, model_path, max_length=None, device="cpu", local_files_only=True):
                self.model_path = model_path

            def predict(self, pairs):
                return [0.91 for _ in pairs]

        with tempfile.TemporaryDirectory() as temp_dir:
            model_dir = Path(temp_dir)
            reranker_dir = model_dir / "mmarco-mMiniLMv2-L6-H384-v1"
            reranker_dir.mkdir(parents=True)
            with patch.dict("os.environ", {"SOULDRIVE_MODEL_DIR": str(model_dir)}), patch(
                "core.reranker._load_cross_encoder",
                return_value=FakeCrossEncoder,
            ):
                reranker = LocalReranker()
                self.assertTrue(reranker.ready)
                scores = reranker.score("GraphRAG 是什么", ["GraphRAG local search improves QA"])

        self.assertEqual(scores, [0.91])

    def test_broken_optional_cross_encoder_degrades_to_disabled_reranker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_dir = Path(temp_dir)
            (model_dir / "mmarco-mMiniLMv2-L6-H384-v1").mkdir(parents=True)
            with patch.dict("os.environ", {"SOULDRIVE_MODEL_DIR": str(model_dir)}), patch(
                "core.reranker._load_cross_encoder",
                side_effect=ImportError("optional reranker unavailable"),
            ):
                reranker = LocalReranker()

        self.assertFalse(reranker.ready)
        self.assertEqual(reranker.score("query", ["passage"]), [0.0])

    def test_local_reranker_normalizes_raw_logits_for_hybrid_ranking(self):
        class FakeCrossEncoder:
            def __init__(self, model_path, max_length=None, device="cpu", local_files_only=True):
                self.model_path = model_path

            def predict(self, pairs):
                return [-2.0, 1.0, 4.0][: len(pairs)]

        with tempfile.TemporaryDirectory() as temp_dir:
            model_dir = Path(temp_dir)
            reranker_dir = model_dir / "mmarco-mMiniLMv2-L6-H384-v1"
            reranker_dir.mkdir(parents=True)
            with patch.dict("os.environ", {"SOULDRIVE_MODEL_DIR": str(model_dir)}), patch(
                "core.reranker._load_cross_encoder",
                return_value=FakeCrossEncoder,
            ):
                reranker = LocalReranker()
                scores = reranker.score("GraphRAG 是什么", ["a", "b", "c"])

        self.assertEqual(scores[0], 0.0)
        self.assertAlmostEqual(scores[1], 0.5)
        self.assertEqual(scores[2], 1.0)

    def test_normalize_reranker_scores_preserves_probability_outputs(self):
        self.assertEqual(_normalize_reranker_scores([0.1, 0.9]), [0.1, 0.9])
        self.assertEqual(_normalize_reranker_scores([float("nan"), float("inf")]), [0.0, 0.0])

    def test_local_reranker_uses_available_runtime_device(self):
        captured = {}

        class FakeCrossEncoder:
            def __init__(self, model_path, max_length=None, device="cpu", local_files_only=True):
                captured["device"] = device

            def predict(self, pairs):
                return [0.5 for _ in pairs]

        with tempfile.TemporaryDirectory() as temp_dir:
            model_dir = Path(temp_dir)
            reranker_dir = model_dir / "mmarco-mMiniLMv2-L6-H384-v1"
            reranker_dir.mkdir(parents=True)
            with patch.dict("os.environ", {"SOULDRIVE_MODEL_DIR": str(model_dir)}), patch(
                "core.reranker._load_cross_encoder",
                return_value=FakeCrossEncoder,
            ), patch("core.reranker._runtime_device", return_value="cuda"):
                reranker = LocalReranker()
                self.assertTrue(reranker.ready)

        self.assertEqual(captured["device"], "cuda")

    def test_qwen3_reranker_uses_domain_instruction_and_probability_scores(self):
        captured = {}

        class FakeCrossEncoder:
            def __init__(self, model_path, **options):
                captured["options"] = options

            def predict(self, pairs, activation_fn=None):
                captured["activation_fn"] = activation_fn
                return [0.88 for _ in pairs]

        with tempfile.TemporaryDirectory() as temp_dir:
            model_dir = Path(temp_dir)
            (model_dir / "Qwen3-Reranker-0.6B").mkdir(parents=True)
            with patch.dict("os.environ", {"SOULDRIVE_MODEL_DIR": str(model_dir)}), patch(
                "core.reranker._load_cross_encoder",
                return_value=FakeCrossEncoder,
            ):
                reranker = LocalReranker()
                scores = reranker.score("适用范围是什么", ["本办法适用于境内企业。"])

        self.assertEqual(captured["options"]["max_length"], 2048)
        self.assertEqual(captured["options"]["default_prompt_name"], "souldrive")
        self.assertIn("government document", captured["options"]["prompts"]["souldrive"])
        self.assertIsNotNone(captured["activation_fn"])
        self.assertEqual(scores, [0.88])

    def test_reranker_passage_uses_source_and_child_excerpt(self):
        candidate = SearchCandidate(
            id="child-1",
            content="完整父文档正文不应直接送入重排器。" * 200,
            metadata={
                "source_filename": "rag.pdf",
                "Header 4": "2.1.1 深层检索",
                "Header 2": "2 基本原理",
                "child_excerpt": "子块摘录是重排所需的最小证据。",
            },
        )

        passage = _reranker_passage(candidate)

        self.assertIn("rag.pdf", passage)
        self.assertIn("2.1.1 深层检索", passage)
        self.assertIn("子块摘录", passage)
        self.assertNotIn("完整父文档正文", passage)
        self.assertLessEqual(len(passage), 600 + len("rag.pdf") + len("2 基本原理") + 2)


if __name__ == "__main__":
    unittest.main()
