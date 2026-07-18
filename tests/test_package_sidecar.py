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


if __name__ == "__main__":
    unittest.main()
