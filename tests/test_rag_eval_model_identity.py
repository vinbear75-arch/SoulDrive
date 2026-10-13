import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import eval_rag


class RagEvalModelIdentityTests(unittest.TestCase):
    def _evaluate(self, llm):
        record = eval_rag.EvalRecord(
            id="MODEL-IDENTITY", question="虚构文档的问题", expected_sources=[],
            type="lookup", rubric=[], expected_keywords=[], should_refuse=False,
            requires_multi_doc=False, graph_relevant=False,
        )
        rag = SimpleNamespace(
            llm=llm, runtime_config=SimpleNamespace(model_filename="default-not-loaded.gguf"),
            close=Mock(),
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch("core.knowledge_base.LocalKnowledgeBase") as knowledge_base:
                with patch("core.rag_engine.RAGEngine", return_value=rag):
                    with patch.object(eval_rag, "generate_answer_with_fallback", return_value=("回答[E1]", [{"id": "E1"}])):
                        with patch("builtins.print"):
                            result = eval_rag.run_answer_generation([record], directory, 6)[0]
        rag.close.assert_called_once()
        knowledge_base.return_value.close.assert_called_once()
        return result

    def test_ollama_identity_comes_from_loaded_client_not_gguf_default(self):
        self.assertEqual(self._evaluate(SimpleNamespace(model="actual-ollama-model:9b")).model_name, "actual-ollama-model:9b")

    def test_compatible_provider_identity_comes_from_loaded_client(self):
        self.assertEqual(self._evaluate(SimpleNamespace(model="actual-compatible-model")).model_name, "actual-compatible-model")

    def test_local_model_identity_uses_loaded_file_basename_without_machine_path(self):
        self.assertEqual(self._evaluate(SimpleNamespace(model_path="D:/fixture/models/actually-loaded.gguf")).model_name, "actually-loaded.gguf")

    def test_unknown_loaded_backend_is_not_labeled_with_a_default(self):
        self.assertIsNone(self._evaluate(SimpleNamespace()).model_name)
