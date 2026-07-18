import json
import unittest
from pathlib import Path

from scripts import check_rag_eval_workspace, import_rag_eval_corpus


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
        self.assertIn('"--ignore-vuln", "PYSEC-2026-311"', script)
        self.assertIn('"--ignore-vuln", "CVE-2025-69872"', script)
        self.assertIn("Ensure-RustSecAdvisoryDb", script)
        self.assertIn('"--ignore", "RUSTSEC-2026-0194"', script)
        self.assertIn('"--ignore", "RUSTSEC-2026-0195"', script)
        self.assertIn("Rust advisory audit cannot run", script)

    def test_scripts_do_not_pin_local_anaconda_paths(self):
        for script_name in ("check.ps1", "audit.ps1", "package-sidecar.ps1"):
            with self.subTest(script=script_name):
                script = (ROOT / "scripts" / script_name).read_text(encoding="utf-8")

                self.assertNotIn("D:\\Anaconda", script)
                self.assertNotIn("envs\\souldrive\\python.exe", script)

    def test_package_sidecar_collects_offline_document_runtime_assets(self):
        script_path = ROOT / "scripts" / "package-sidecar.ps1"

        self.assertTrue(script_path.exists())
        script = script_path.read_text(encoding="utf-8")
        self.assertIn("--collect-all", script)
        self.assertIn("docling", script)
        self.assertIn("docling_parse", script)
        self.assertIn("rapidocr", script)
        self.assertIn('"--collect-all", "chromadb"', script)
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

        self.assertIn('"--hidden-import", "chromadb.telemetry.product.posthog"', script)
        self.assertIn('"--hidden-import", "chromadb.api.rust"', script)

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
        self.assertIn("--top-k", script)
        self.assertIn("conda", script)
        self.assertIn("souldrive", script)
        self.assertIn("scripts/eval_rag.py", script)
        self.assertIn("[int]$TopK = 6", script)

    def test_rag_harness_runner_combines_retrieval_and_ingestion_metrics(self):
        script_path = ROOT / "scripts" / "run-rag-harness.ps1"

        self.assertTrue(script_path.exists())
        script = script_path.read_text(encoding="utf-8")
        self.assertIn("eval/retrieval_quality_blind_v0.3.jsonl", script)
        self.assertIn("eval/ingestion_quality_v0.1.jsonl", script)
        self.assertIn("eval/results/rag_harness_candidate_v0.1.json", script)
        self.assertIn("scripts/eval_harness.py", script)
        self.assertIn('"run", "--no-capture-output", "-n", "souldrive"', script)

    def test_frontend_chat_uses_six_evidence_items_by_default(self):
        source = (ROOT / "souldrive-ui" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertIn("top_k: 6", source)

    def test_rag_eval_runner_avoids_conda_stdout_capture_on_windows(self):
        script = (ROOT / "scripts" / "run-rag-eval.ps1").read_text(encoding="utf-8")

        self.assertEqual(script.count("--no-capture-output"), 2)
        self.assertIn('"run", "--no-capture-output", "-n", "souldrive"', script)

    def test_rag_eval_runner_forces_utf8_console_output_on_windows(self):
        script = (ROOT / "scripts" / "run-rag-eval.ps1").read_text(encoding="utf-8")

        self.assertIn("[Console]::OutputEncoding", script)
        self.assertIn("UTF8Encoding", script)
        self.assertIn('PYTHONIOENCODING = "utf-8"', script)

    def test_rag_miss_analyzer_defaults_to_blind_v03_dataset(self):
        source = (ROOT / "scripts" / "analyze_rag_eval_misses.py").read_text(encoding="utf-8")

        self.assertIn("retrieval_quality_blind_v0.3.jsonl", source)
        self.assertIn("retrieval_baseline_v0.3.jsonl", source)

    def test_rag_eval_workspace_preflight_checks_manifest_documents(self):
        script_path = ROOT / "scripts" / "check_rag_eval_workspace.py"

        self.assertTrue(script_path.exists())
        script = script_path.read_text(encoding="utf-8")
        self.assertIn("corpus_manifest_v0.2.json", script)
        self.assertIn("SecureDocumentStore", script)
        self.assertIn("missing_documents", script)
        self.assertIn("present_documents", script)
        self.assertIn("expected_documents", script)

    def test_rag_eval_corpus_import_script_uses_manifest_and_optional_indexing(self):
        script_path = ROOT / "scripts" / "import_rag_eval_corpus.py"

        self.assertTrue(script_path.exists())
        script = script_path.read_text(encoding="utf-8")
        self.assertIn("corpus_manifest_v0.2.json", script)
        self.assertIn("import_paper_into_workspace", script)
        self.assertIn("SOULDRIVE_EVAL_CORPUS_DIR", script)
        self.assertIn("--dry-run", script)
        self.assertIn("--index", script)
        self.assertIn("--skip-graph", script)
        self.assertIn("DriveIndexer", script)

    def test_rag_eval_json_output_flags_do_not_collide_with_conda_run_json(self):
        check_script = (ROOT / "scripts" / "check_rag_eval_workspace.py").read_text(encoding="utf-8")
        import_script = (ROOT / "scripts" / "import_rag_eval_corpus.py").read_text(encoding="utf-8")

        self.assertIn("--json-output", check_script)
        self.assertIn("--json-output", import_script)

    def test_rag_eval_json_output_is_ascii_safe_for_conda_run(self):
        payload = {"name": "城市+AI", "replacement": "\ufffd"}

        import_json = import_rag_eval_corpus.format_json_report(payload)
        check_json = check_rag_eval_workspace.format_json_report(payload)

        self.assertEqual(json.loads(import_json), payload)
        self.assertEqual(json.loads(check_json), payload)
        self.assertTrue(all(ord(character) < 128 for character in import_json))
        self.assertTrue(all(ord(character) < 128 for character in check_json))


if __name__ == "__main__":
    unittest.main()
