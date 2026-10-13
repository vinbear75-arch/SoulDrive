import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.model_runtime import (
    LlamaRuntimeConfig,
    llama_runtime_config,
    load_llama_model,
    model_runtime_diagnostics,
    preferred_chat_model_filename,
    resolve_chat_model_path,
)


class ModelRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.cloud_model_config_patch = patch(
            "core.model_runtime.load_cloud_model_config",
            return_value=None,
        )
        self.cloud_model_config_patch.start()

    def tearDown(self):
        self.cloud_model_config_patch.stop()
        for name in (
            "SOULDRIVE_CHAT_MODEL",
            "SOULDRIVE_LLAMA_CHAT_CTX",
            "SOULDRIVE_LLAMA_MAX_TOKENS",
            "SOULDRIVE_MODEL_DIR",
            "SOULDRIVE_CHAT_PROVIDER",
            "SOULDRIVE_OLLAMA_MODEL",
            "SOULDRIVE_EMBEDDING_PROVIDER",
            "SOULDRIVE_OLLAMA_EMBEDDING_MODEL",
            "SOULDRIVE_LLAMA_SEED",
        ):
            os.environ.pop(name, None)

    def test_llama_runtime_config_is_bounded(self):
        with patch.dict(os.environ, {
            "SOULDRIVE_LLAMA_CHAT_CTX": "999999",
            "SOULDRIVE_LLAMA_MAX_TOKENS": "1",
        }):
            config = llama_runtime_config()

        self.assertEqual(config.chat_n_ctx, 32768)
        self.assertEqual(config.max_tokens, 128)

    def test_llama_runtime_config_defaults_to_demo_safe_context(self):
        config = llama_runtime_config()

        self.assertEqual(config.chat_n_ctx, 8192)
        self.assertEqual(config.graph_n_ctx, 8192)
        self.assertEqual(config.max_tokens, 900)
        self.assertEqual(config.temperature, 0.0)
        self.assertEqual(config.seed, 42)

    def test_llama_runtime_config_bounds_generation_seed(self):
        with patch.dict(os.environ, {"SOULDRIVE_LLAMA_SEED": "-5"}):
            config = llama_runtime_config()

        self.assertEqual(config.seed, 0)

    def test_preferred_chat_model_prefers_complete_7b_shard_for_experiment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_dir = Path(temp_dir)
            (model_dir / "qwen2.5-3b-instruct-q4_k_m.gguf").write_text("model", encoding="utf-8")
            (model_dir / "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf").write_text("model", encoding="utf-8")
            (model_dir / "qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf").write_text("model", encoding="utf-8")

            with patch.dict(os.environ, {"SOULDRIVE_MODEL_DIR": str(model_dir)}):
                model_filename = preferred_chat_model_filename()

        self.assertEqual(model_filename, "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf")

    def test_preferred_chat_model_ignores_incomplete_7b_shard(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_dir = Path(temp_dir)
            (model_dir / "qwen2.5-3b-instruct-q4_k_m.gguf").write_text("model", encoding="utf-8")
            (model_dir / "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf").write_text("model", encoding="utf-8")

            with patch.dict(os.environ, {"SOULDRIVE_MODEL_DIR": str(model_dir)}):
                model_filename = preferred_chat_model_filename()

        self.assertEqual(model_filename, "qwen2.5-3b-instruct-q4_k_m.gguf")

    def test_configured_chat_model_can_force_3b_when_7b_shard_is_available(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_dir = Path(temp_dir)
            (model_dir / "qwen2.5-3b-instruct-q4_k_m.gguf").write_text("model", encoding="utf-8")
            (model_dir / "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf").write_text("model", encoding="utf-8")
            (model_dir / "qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf").write_text("model", encoding="utf-8")

            with patch.dict(os.environ, {
                "SOULDRIVE_MODEL_DIR": str(model_dir),
                "SOULDRIVE_CHAT_MODEL": "qwen2.5-3b-instruct-q4_k_m.gguf",
            }):
                model_filename = preferred_chat_model_filename()

        self.assertEqual(model_filename, "qwen2.5-3b-instruct-q4_k_m.gguf")

    def test_preferred_chat_model_prioritizes_7b_across_search_roots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            configured_model_dir = root / "app_models"
            workspace_model_dir = root / "workspace" / "models"
            configured_model_dir.mkdir(parents=True)
            workspace_model_dir.mkdir(parents=True)
            (configured_model_dir / "qwen2.5-3b-instruct-q4_k_m.gguf").write_text("model", encoding="utf-8")
            (workspace_model_dir / "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf").write_text("model", encoding="utf-8")
            (workspace_model_dir / "qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf").write_text("model", encoding="utf-8")

            with patch.dict(os.environ, {"SOULDRIVE_MODEL_DIR": str(configured_model_dir)}):
                model_filename = preferred_chat_model_filename(str(root / "workspace"))

        self.assertEqual(model_filename, "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf")

    def test_preferred_chat_model_falls_back_to_3b(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_dir = Path(temp_dir)
            (model_dir / "qwen2.5-3b-instruct-q4_k_m.gguf").write_text("model", encoding="utf-8")

            with patch.dict(os.environ, {"SOULDRIVE_MODEL_DIR": str(model_dir)}):
                model_filename = preferred_chat_model_filename()

        self.assertEqual(model_filename, "qwen2.5-3b-instruct-q4_k_m.gguf")

    def test_resolve_chat_model_uses_configured_filename(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_dir = Path(temp_dir)
            model_file = model_dir / "custom.gguf"
            model_file.write_text("model", encoding="utf-8")

            with patch.dict(os.environ, {
                "SOULDRIVE_MODEL_DIR": str(model_dir),
                "SOULDRIVE_CHAT_MODEL": "custom.gguf",
            }):
                resolved = resolve_chat_model_path()

        self.assertEqual(resolved, str(model_file))

    def test_model_runtime_diagnostics_reports_missing_and_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {
                "SOULDRIVE_MODEL_DIR": temp_dir,
                "SOULDRIVE_CHAT_MODEL": "missing-chat-model.gguf",
            }):
                report = model_runtime_diagnostics()

        self.assertFalse(report["ready"])
        self.assertIn("config", report)
        self.assertFalse(report["chat_model"]["ready"])
        self.assertIn("chat_n_ctx", report["config"])

    def test_model_runtime_diagnostics_requires_all_configured_7b_shards(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_dir = Path(temp_dir)
            first_shard = "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf"
            second_shard = "qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf"
            (model_dir / first_shard).write_text("model", encoding="utf-8")

            with patch.dict(os.environ, {
                "SOULDRIVE_MODEL_DIR": str(model_dir),
                "SOULDRIVE_CHAT_MODEL": first_shard,
            }):
                report = model_runtime_diagnostics()

        self.assertFalse(report["chat_model"]["ready"])
        self.assertEqual(report["chat_model"]["selected"], first_shard)
        self.assertEqual(report["chat_model"]["missing_parts"], [second_shard])

    def test_model_runtime_diagnostics_reports_preferred_qwen3_embedding(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_dir = Path(temp_dir)
            (model_dir / "Qwen3-Embedding-0.6B").mkdir()
            (model_dir / "chat.gguf").write_text("model", encoding="utf-8")

            with patch.dict(os.environ, {
                "SOULDRIVE_MODEL_DIR": str(model_dir),
                "SOULDRIVE_CHAT_MODEL": "chat.gguf",
                "SOULDRIVE_EMBEDDING_MODEL": "Qwen3-Embedding-0.6B",
            }):
                report = model_runtime_diagnostics()

        self.assertTrue(report["ready"])
        self.assertEqual(report["embedding_model"]["name"], "Qwen3-Embedding-0.6B")

    def test_model_runtime_diagnostics_reports_ollama_models(self):
        with patch.dict(os.environ, {
            "SOULDRIVE_CHAT_PROVIDER": "ollama",
            "SOULDRIVE_OLLAMA_MODEL": "qwen3.5:9b",
            "SOULDRIVE_EMBEDDING_PROVIDER": "ollama",
            "SOULDRIVE_OLLAMA_EMBEDDING_MODEL": "qwen3-embedding:4b",
        }, clear=False):
            report = model_runtime_diagnostics()

        self.assertTrue(report["ready"])
        self.assertEqual(report["chat_model"]["selected"], "qwen3.5:9b")
        self.assertEqual(report["chat_model"]["provider"], "ollama")
        self.assertEqual(report["embedding_model"]["name"], "qwen3-embedding:4b")

    def test_load_llama_model_offloads_all_layers_when_supported(self):
        class FakeLlama:
            instances = []

            def __init__(self, model_path, n_ctx, n_gpu_layers, verbose):
                self.model_path = model_path
                self.n_ctx = n_ctx
                self.n_gpu_layers = n_gpu_layers
                self.verbose = verbose
                FakeLlama.instances.append(self)

        config = LlamaRuntimeConfig(model_filename="model.gguf")

        with patch.dict("sys.modules", {"llama_cpp": type("M", (), {"Llama": FakeLlama, "llama_supports_gpu_offload": lambda: True})}):
            result = load_llama_model("model.gguf", config, n_ctx=1024)

        self.assertIs(result.effective_config, config)
        self.assertEqual(FakeLlama.instances[-1].n_ctx, 1024)
        self.assertEqual(FakeLlama.instances[-1].n_gpu_layers, -1)
        self.assertEqual(result.gpu_layers, -1)


if __name__ == "__main__":
    unittest.main()
