import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.reranker import (
    LocalReranker,
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
                "sentence_transformers.CrossEncoder",
                FakeCrossEncoder,
            ):
                reranker = LocalReranker()
                self.assertTrue(reranker.ready)
                scores = reranker.score("GraphRAG 是什么", ["GraphRAG local search improves QA"])

        self.assertEqual(scores, [0.91])

    def test_local_reranker_uses_cpu_device(self):
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
                "sentence_transformers.CrossEncoder",
                FakeCrossEncoder,
            ):
                reranker = LocalReranker()
                self.assertTrue(reranker.ready)

        self.assertEqual(captured["device"], "cpu")

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
                "sentence_transformers.CrossEncoder",
                FakeCrossEncoder,
            ):
                reranker = LocalReranker()
                scores = reranker.score("适用范围是什么", ["本办法适用于境内企业。"])

        self.assertEqual(captured["options"]["max_length"], 2048)
        self.assertEqual(captured["options"]["default_prompt_name"], "souldrive")
        self.assertIn("government document", captured["options"]["prompts"]["souldrive"])
        self.assertIsNotNone(captured["activation_fn"])
        self.assertEqual(scores, [0.88])


if __name__ == "__main__":
    unittest.main()
