from pathlib import Path
import unittest


class PackageSidecarTests(unittest.TestCase):
    def test_package_script_collects_docling_pdf_resources(self):
        script_path = Path(__file__).resolve().parents[1] / "scripts" / "package-sidecar.ps1"
        content = script_path.read_text(encoding="utf-8")

        self.assertIn("docling_parse", content)
        self.assertIn("pdf_resources", content)

    def test_package_script_copies_docling_distribution_metadata(self):
        script_path = Path(__file__).resolve().parents[1] / "scripts" / "package-sidecar.ps1"
        content = script_path.read_text(encoding="utf-8")

        self.assertIn('"--copy-metadata", "docling"', content)
        self.assertIn('"--copy-metadata", "docling_slim"', content)

    def test_release_backend_startup_removes_development_unlock_environment(self):
        source_path = Path(__file__).resolve().parents[1] / "souldrive-ui" / "src-tauri" / "src" / "lib.rs"
        content = source_path.read_text(encoding="utf-8")

        self.assertIn('env_remove("SOULDRIVE_DEV_BYPASS_WORKSPACE_LOCK")', content)
        self.assertIn('env_remove("SOULDRIVE_DEV_WORKSPACE_PASSPHRASE")', content)
        production_source = content.rsplit("#[cfg(test)]", 1)[0]
        self.assertEqual(production_source.count("apply_development_workspace_environment(&mut command)"), 2)

    def test_default_launcher_does_not_enable_development_unlock_or_embed_credentials(self):
        root = Path(__file__).resolve().parents[1]
        content = (root / "启动SoulDrive.bat").read_text(encoding="utf-8")
        self.assertNotIn("SOULDRIVE_DEV_BYPASS_WORKSPACE_LOCK", content)
        self.assertNotIn("SOULDRIVE_DEV_WORKSPACE_PASSPHRASE", content)
        self.assertNotIn("SOULDRIVE_DEMO_WORKSPACE", content)
        self.assertNotIn("postgresql://", content)
        self.assertNotIn("D:\\SOFTWARES", content)

    def test_debug_backend_does_not_inject_a_fixed_development_passphrase(self):
        root = Path(__file__).resolve().parents[1]
        content = (root / "souldrive-ui/src-tauri/src/lib.rs").read_text(encoding="utf-8")
        production_source = content.rsplit("#[cfg(test)]", 1)[0]
        self.assertNotIn('.env("SOULDRIVE_DEV_BYPASS_WORKSPACE_LOCK", "1")', production_source)
        self.assertNotIn('.env("SOULDRIVE_DEV_WORKSPACE_PASSPHRASE", "88888888")', production_source)

    def test_default_desktop_startup_does_not_force_optional_shared_state_backends(self):
        root = Path(__file__).resolve().parents[1]
        content = (root / "souldrive-ui/src-tauri/src/lib.rs").read_text(encoding="utf-8")
        production_source = content.rsplit("#[cfg(test)]", 1)[0]
        self.assertNotIn('.env("SOULDRIVE_REDIS_ENABLED", "1")', production_source)
        self.assertNotIn('.env("SOULDRIVE_POSTGRES_ENABLED", "1")', production_source)
        self.assertNotIn("postgresql://souldrive:souldrive@127.0.0.1:5432/souldrive", production_source)
        self.assertEqual(production_source.count("apply_optional_state_backend_environment(&mut command)"), 2)


if __name__ == "__main__":
    unittest.main()
