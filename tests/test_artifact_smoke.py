import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.smoke_artifact_flows import run


class ArtifactSmokeTests(unittest.TestCase):
    def test_smoke_confirms_actual_outline_and_reopens_completed_generation(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {"SOULDRIVE_PRESENTATION_RENDERER": "com"}
        ), patch("socket.create_connection", side_effect=AssertionError("network forbidden")), patch(
            "core.presentation_service.subprocess.run",
            side_effect=AssertionError("native renderer forbidden"),
        ):
            output_path = Path(temp_dir) / "smoke.json"
            report = run(output_path)

            self.assertEqual(json.loads(output_path.read_text(encoding="utf-8")), report)
            self.assertTrue(output_path.with_suffix(".pptx").is_file())
            self.assertEqual([report[key]["status"] for key in ("ppt", "email", "skill")], ["pass"] * 3)
            self.assertEqual(report["ppt"]["outline_review_phase"], "outline_review")
            self.assertTrue(report["ppt"]["no_pptx_before_confirmation"])
            self.assertTrue(report["ppt"]["confirmation_replayed_same_task"])
            self.assertTrue(report["ppt"]["reopened_result_unchanged"])
            self.assertEqual(report["ppt"]["reopened_task_status"], "completed")
            self.assertTrue(report["ppt"]["renderer"].lower().endswith("openxml"))
            self.assertFalse(report["ppt"]["wps_open_requested"])
            self.assertFalse(report["email"]["authorization_code_exposed"])
            self.assertFalse(report["skill"]["scripts_auto_executed"])
            self.assertEqual(report["skill"]["approval_status"], "pending_review")
            self.assertFalse(report["skill"]["active_before_activation"])
            self.assertEqual(report["skill"]["activation_status"], "active")
            self.assertEqual(os.environ["SOULDRIVE_PRESENTATION_RENDERER"], "com")


if __name__ == "__main__":
    unittest.main()
