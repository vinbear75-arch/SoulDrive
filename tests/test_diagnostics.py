import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.runtime_state as runtime_state
from core.diagnostics import model_diagnostics, product_diagnostics
from core.license import LicenseStatus
from core.workspace import SoulDriveWorkspace


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.cloud_model_config_patch = patch(
            "core.model_runtime.load_cloud_model_config",
            return_value=None,
        )
        self.cloud_model_config_patch.start()

    def tearDown(self):
        self.cloud_model_config_patch.stop()

    def test_product_diagnostics_reports_mounted_workspace_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = _create_workspace_with_models(root / "drive")
            _create_minimal_project_root(root)
            state_path = str(root / "runtime_state.json")

            with patch.dict(os.environ, {
                "SOULDRIVE_APP_ROOT": str(root),
                "SOULDRIVE_MODEL_DIR": str(root / "models"),
            }), patch.object(runtime_state, "STATE_PATH", state_path):
                runtime_state.unlock_runtime("PRO", "SN-1", str(root / "drive"), workspace.root_path)
                report = product_diagnostics()

        self.assertIn("checks", report)
        self.assertTrue(report["checks"]["policy"])
        self.assertTrue(report["checks"]["workspace"])
        self.assertTrue(report["checks"]["audit_chain"])
        self.assertTrue(report["models"]["ready"])
        self.assertEqual(report["models"]["runtime"]["chat_model"]["selected"], "qwen2.5-3b-instruct-q4_k_m.gguf")
        self.assertEqual(report["models"]["runtime"]["reranker"]["selected"], "mmarco-mMiniLMv2-L6-H384-v1")
        self.assertNotIn("audit_path", report["audit"])
        self.assertNotIn("package", report)
        self.assertNotIn("release", report)
        self.assertFalse(report["license"]["valid"])

    def test_product_diagnostics_redacts_license_source_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = _create_workspace_with_models(root / "drive")
            license_path = root / "config" / "license.json"
            _create_minimal_project_root(root)
            state_path = str(root / "runtime_state.json")

            with patch.dict(os.environ, {
                "SOULDRIVE_APP_ROOT": str(root),
                "SOULDRIVE_MODEL_DIR": str(root / "models"),
            }), patch.object(runtime_state, "STATE_PATH", state_path), patch(
                "core.diagnostics.verify_license_for_workspace",
                return_value=LicenseStatus(
                    True,
                    "PRO",
                    "license verified",
                    source=str(license_path),
                    license_id="lic-1",
                ),
            ):
                runtime_state.unlock_runtime("PRO", "SN-1", str(root / "drive"), workspace.root_path)
                report = product_diagnostics()

        self.assertEqual(report["license"]["source"], "license.json")
        self.assertNotIn(str(root), json.dumps(report["license"], ensure_ascii=False))

    def test_product_diagnostics_does_not_require_license_in_pc_only_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = _create_workspace_with_models(root / "drive")
            _create_minimal_project_root(root)
            state_path = str(root / "runtime_state.json")

            with patch.dict(os.environ, {
                "SOULDRIVE_APP_ROOT": str(root),
                "SOULDRIVE_MODEL_DIR": str(root / "models"),
                "SOULDRIVE_PC_ONLY": "1",
            }), patch.object(runtime_state, "STATE_PATH", state_path):
                runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
                report = product_diagnostics()

        self.assertFalse(report["policy"]["require_signed_license"])
        self.assertTrue(report["checks"]["license"])
        self.assertTrue(report["runtime"]["local_pc_mode"])

    def test_product_diagnostics_reports_effective_request_body_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = _create_workspace_with_models(root / "drive")
            _create_minimal_project_root(root)
            state_path = str(root / "runtime_state.json")

            with patch.dict(os.environ, {
                "SOULDRIVE_APP_ROOT": str(root),
                "SOULDRIVE_MODEL_DIR": str(root / "models"),
                "SOULDRIVE_MAX_BODY_BYTES": str(16 * 1024 * 1024),
            }), patch.object(runtime_state, "STATE_PATH", state_path):
                runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
                report = product_diagnostics()

        self.assertEqual(report["policy"]["max_request_body_bytes"], 16 * 1024 * 1024)

    def test_product_diagnostics_requires_mounted_unlocked_workspace_for_ready(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_minimal_project_root(root)
            state_path = str(root / "runtime_state.json")

            with patch.dict(os.environ, {
                "SOULDRIVE_APP_ROOT": str(root),
                "SOULDRIVE_MODEL_DIR": str(root / "models"),
            }), patch.object(runtime_state, "STATE_PATH", state_path):
                report = product_diagnostics()

        self.assertFalse(report["checks"]["runtime_unlocked"])
        self.assertFalse(report["checks"]["workspace"])
        self.assertFalse(report["checks"]["audit_chain"])
        self.assertFalse(report["workspace"]["ready"])
        self.assertEqual(report["workspace"]["reason"], "waiting for removable SoulDrive workspace")
        self.assertFalse(report["ready"])

    def test_model_diagnostics_accepts_complete_7b_runtime_without_3b(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            _create_workspace_embedding_models(workspace)
            models_path = Path(workspace.models_path)
            (models_path / "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf").write_text("model", encoding="utf-8")
            (models_path / "qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf").write_text("model", encoding="utf-8")

            with patch("core.diagnostics.model_search_dirs", return_value=[models_path]):
                report = model_diagnostics(workspace.root_path)

        self.assertTrue(report["ready"])
        self.assertEqual(
            report["runtime"]["chat_model"]["selected"],
            "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf",
        )
        self.assertNotIn("qwen2.5-3b-instruct-q4_k_m.gguf", report["missing"])


def _create_minimal_project_root(root: Path):
    (root / "models" / "bge-small-zh-v1.5" / "1_Pooling").mkdir(parents=True)
    (root / "models" / "mmarco-mMiniLMv2-L6-H384-v1").mkdir(parents=True)
    (root / "models" / "qwen2.5-3b-instruct-q4_k_m.gguf").write_text("model", encoding="utf-8")
    for relative_path in (
        "bge-small-zh-v1.5/config.json",
        "bge-small-zh-v1.5/model.safetensors",
        "bge-small-zh-v1.5/tokenizer.json",
        "bge-small-zh-v1.5/vocab.txt",
        "bge-small-zh-v1.5/1_Pooling/config.json",
    ):
        (root / "models" / relative_path).write_text("{}", encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "enterprise-policy.json").write_text(json.dumps({
        "require_signed_license": False,
        "model_manifest_required": True,
    }), encoding="utf-8")


def _create_workspace_with_models(drive_root: Path):
    workspace = SoulDriveWorkspace.from_drive(str(drive_root)).ensure()
    _create_workspace_embedding_models(workspace)
    (Path(workspace.models_path) / "mmarco-mMiniLMv2-L6-H384-v1").mkdir(parents=True)
    (Path(workspace.models_path) / "qwen2.5-3b-instruct-q4_k_m.gguf").write_text("model", encoding="utf-8")
    return workspace


def _create_workspace_embedding_models(workspace: SoulDriveWorkspace):
    for relative_path in (
        "bge-small-zh-v1.5/config.json",
        "bge-small-zh-v1.5/model.safetensors",
        "bge-small-zh-v1.5/tokenizer.json",
        "bge-small-zh-v1.5/vocab.txt",
        "bge-small-zh-v1.5/1_Pooling/config.json",
    ):
        target = Path(workspace.models_path) / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
