import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class ScriptTests(unittest.TestCase):
    def test_check_script_uses_windows_npm_entrypoint_without_audit(self):
        script = (ROOT / "scripts" / "check.ps1").read_text(encoding="utf-8")

        self.assertIn("npm.cmd run build", script)
        self.assertNotIn("npm run build", script)
        self.assertNotIn("npm audit", script)

    def test_audit_script_uses_runtime_dependency_audit(self):
        script = (ROOT / "scripts" / "audit.ps1").read_text(encoding="utf-8")

        self.assertIn("npm.cmd audit --omit=dev", script)

    def test_package_sidecar_collects_offline_document_runtime_assets(self):
        script_path = ROOT / "scripts" / "package-sidecar.ps1"

        self.assertTrue(script_path.exists())
        script = script_path.read_text(encoding="utf-8")
        self.assertIn("--collect-all", script)
        self.assertIn("docling", script)
        self.assertIn("rapidocr", script)
        self.assertIn("--collect-all chromadb", script)
        self.assertIn("souldrive-ui\\src-tauri\\sidecars", script)

    def test_package_sidecar_prunes_only_nonessential_cuda_and_video_runtime_bloat(self):
        script = (ROOT / "scripts" / "package-sidecar.ps1").read_text(encoding="utf-8")

        self.assertIn("Remove-PortableSidecarBloat", script)
        self.assertNotIn('"_internal\\llama_cpp\\lib\\ggml-cuda.dll"', script)
        self.assertIn('"_internal\\llama_cpp\\lib\\ggml-cuda.lib"', script)
        self.assertIn('"_internal\\cublasLt64_12.dll"', script)
        self.assertIn("opencv_videoio_ffmpeg*.dll", script)
        self.assertNotIn("--exclude-module torchvision", script)

    def test_package_sidecar_includes_chromadb_dynamic_runtime_imports(self):
        script = (ROOT / "scripts" / "package-sidecar.ps1").read_text(encoding="utf-8")

        self.assertIn("--hidden-import chromadb.telemetry.product.posthog", script)
        self.assertIn("--hidden-import chromadb.api.rust", script)

    def test_tauri_dev_prefers_source_backend_over_packaged_sidecar(self):
        source = (ROOT / "souldrive-ui" / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")

        self.assertIn("prefer_source_backend", source)
        self.assertIn("SOULDRIVE_DEV_USE_PACKAGED_SIDECAR", source)
        self.assertIn('"conda"', source)
        self.assertIn('"souldrive"', source)

    def test_rag_eval_runner_targets_blind_v03_dataset_and_results(self):
        script_path = ROOT / "scripts" / "run-rag-eval.ps1"

        self.assertTrue(script_path.exists())
        script = script_path.read_text(encoding="utf-8")
        self.assertIn("eval/retrieval_quality_blind_v0.3.jsonl", script)
        self.assertIn("eval/results/retrieval_baseline_v0.3.jsonl", script)
        self.assertIn("--workspace", script)
        self.assertIn("--save-results", script)
        self.assertIn("scripts/eval_rag.py", script)
        self.assertIn("scripts/check_rag_eval_workspace.py", script)
        self.assertIn("corpus_manifest_v0.2.json", script)

    def test_rag_eval_runner_forces_utf8_console_output_on_windows(self):
        script = (ROOT / "scripts" / "run-rag-eval.ps1").read_text(encoding="utf-8")

        self.assertIn("[Console]::OutputEncoding", script)
        self.assertIn("UTF8Encoding", script)
        self.assertIn('PYTHONIOENCODING = "utf-8"', script)

    def test_rag_miss_analyzer_defaults_to_blind_v03_dataset(self):
        script_path = ROOT / "scripts" / "analyze_rag_eval_misses.py"

        self.assertTrue(script_path.exists())
        script = script_path.read_text(encoding="utf-8")
        self.assertIn("retrieval_quality_blind_v0.3.jsonl", script)
        self.assertIn("retrieval_baseline_v0.3.jsonl", script)

    def test_rag_eval_workspace_preflight_checks_manifest_documents(self):
        script_path = ROOT / "scripts" / "check_rag_eval_workspace.py"
        manifest_path = ROOT / "eval" / "corpus_manifest_v0.2.json"

        self.assertTrue(script_path.exists())
        self.assertTrue(manifest_path.exists())
        script = script_path.read_text(encoding="utf-8")
        self.assertIn("corpus_manifest_v0.2.json", script)
        self.assertIn("SecureDocumentStore", script)
        self.assertIn("missing_documents", script)
        self.assertIn("missing_indexed_documents", script)

    def test_rag_eval_corpus_import_script_uses_manifest_and_optional_indexing(self):
        script_path = ROOT / "scripts" / "import_rag_eval_corpus.py"
        inventory_path = ROOT / "eval" / "corpus_file_inventory_v0.2.json"

        self.assertTrue(script_path.exists())
        self.assertTrue(inventory_path.exists())
        script = script_path.read_text(encoding="utf-8")
        self.assertIn("corpus_manifest_v0.2.json", script)
        self.assertIn("import_paper_into_workspace", script)
        self.assertIn("--dry-run", script)
        self.assertIn("--index", script)
        self.assertIn("--skip-graph", script)


if __name__ == "__main__":
    unittest.main()
