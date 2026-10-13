import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.workspace import SoulDriveWorkspace, is_souldrive_workspace, resolve_workspace


class WorkspaceTests(unittest.TestCase):
    def test_workspace_creates_drive_workspace_layout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir)
            workspace.ensure()

            self.assertTrue((Path(temp_dir) / "SoulDrive" / "data" / "papers").exists())
            self.assertTrue(workspace.chroma_path.endswith(str(Path("SoulDrive") / "index" / "chroma")))
            self.assertTrue(workspace.graph_db_path.endswith(str(Path("SoulDrive") / "index" / "knowledge_graph.sqlite")))
            self.assertTrue(workspace.audit_log_path.endswith(str(Path("SoulDrive") / "audit" / "audit_log.jsonl")))
            self.assertTrue(Path(workspace.manifest_path).exists())
            self.assertTrue(is_souldrive_workspace(temp_dir))

    def test_workspace_manifest_failure_leaves_no_partial_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir)
            with patch("core.atomic_file.os.fsync", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    workspace.ensure()

            manifest = Path(workspace.manifest_path)
            self.assertFalse(manifest.exists())
            self.assertEqual(list(manifest.parent.glob(".workspace.json.*.tmp")), [])

    def test_workspace_diagnose_reports_product_layout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            diagnostics = workspace.diagnose()

        self.assertTrue(diagnostics["ready"])
        self.assertEqual(diagnostics["root_name"], "SoulDrive")
        self.assertTrue(diagnostics["checks"]["manifest"])
        self.assertIn("free_bytes", diagnostics["disk"])
        self.assertIn("minimum_free_bytes", diagnostics["disk"])

    def test_default_workspace_requires_explicit_environment_override(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "SOULDRIVE_WORKSPACE"):
                SoulDriveWorkspace.default()

    def test_resolve_workspace_requires_active_drive_or_environment_override(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "removable SoulDrive workspace"):
                resolve_workspace(None)

    def test_resolve_workspace_uses_explicit_local_workspace_without_drive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            configured = str(Path(temp_dir) / "pc-workspace")
            with patch.dict(os.environ, {"SOULDRIVE_WORKSPACE": configured}, clear=True):
                workspace = resolve_workspace(None)

            self.assertEqual(workspace.root_path, configured)
            self.assertTrue(Path(workspace.manifest_path).exists())

    def test_resolve_workspace_keeps_removable_drive_precedence_when_provided(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            configured = str(Path(temp_dir) / "pc-workspace")
            removable_drive = str(Path(temp_dir) / "removable")
            Path(removable_drive).mkdir()
            with patch.dict(os.environ, {"SOULDRIVE_WORKSPACE": configured}, clear=True):
                workspace = resolve_workspace(removable_drive)

            self.assertEqual(workspace.root_path, str(Path(removable_drive) / "SoulDrive"))
            self.assertTrue(Path(workspace.manifest_path).exists())

    def test_default_workspace_can_be_explicitly_configured_for_tests(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            configured = str(Path(temp_dir) / "configured-workspace")
            with patch.dict(os.environ, {"SOULDRIVE_WORKSPACE": configured}, clear=True):
                workspace = SoulDriveWorkspace.default().ensure()

        self.assertEqual(workspace.root_path, configured)


if __name__ == "__main__":
    unittest.main()
