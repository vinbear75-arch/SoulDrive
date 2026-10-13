import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import eval_episodic_workflow_routing


class EpisodicWorkflowRoutingEvalTests(unittest.TestCase):
    def _record(self, *, mismatched_workflow: bool = False) -> dict:
        return {
            "id": "EPISODIC-CLI-001",
            "workflow_id": "experiment_compare",
            "query": "请回顾上次实验运行和数据集版本",
            "events": [
                {
                    "source_type": "experiment_run",
                    "source_id": "run-1",
                    "project_id": "project-a",
                    "workflow_id": "paper_review" if mismatched_workflow else "experiment_compare",
                    "event_time": "2026-09-11T10:00:00+08:00",
                    "title": "baseline",
                    "summary": "v1",
                    "status": "recorded",
                }
            ],
            "expected_event_ids": ["experiment_run:run-1"],
            "forbidden_event_ids": [],
            "metric": "experiment_temporal_scope",
        }

    def test_runtime_failure_returns_nonzero_and_writes_report(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "episodic.json"
            with patch.object(
                eval_episodic_workflow_routing,
                "load_records",
                return_value=[self._record(mismatched_workflow=True)],
            ), patch("builtins.print"):
                status = eval_episodic_workflow_routing.main(
                    ["--runtime", "--output", str(output)]
                )
            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(status, 1)
        self.assertEqual(report["summary"]["total"], 1)
        self.assertEqual(report["summary"]["passed"], 0)

    def test_runtime_evaluation_rejects_unreachable_expected_event(self):
        record = self._record()
        record["expected_event_ids"] = ["decision:missing"]
        with self.assertRaisesRegex(ValueError, "expected_event_ids"):
            eval_episodic_workflow_routing.evaluate([record])

    def test_frozen_runtime_dataset_passes(self):
        records = eval_episodic_workflow_routing.load_records(
            eval_episodic_workflow_routing.DEFAULT_DATASET
        )
        report = eval_episodic_workflow_routing.evaluate(records)
        self.assertEqual(report["summary"]["total"], 5)
        self.assertEqual(report["summary"]["passed"], 5)

    @unittest.skipUnless(os.name == "nt", "PowerShell gate integration is Windows-specific")
    def test_aggregate_check_stops_when_episodic_gate_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            script = root / "scripts" / "check.ps1"
            shutil.copyfile(eval_episodic_workflow_routing.ROOT / "scripts" / "check.ps1", script)
            shim = root / "python-fixture.cmd"
            shim.write_text(
                '@echo off\n'
                'echo %*>>"%~dp0calls.txt"\n'
                'if "%~1"=="scripts/eval_episodic_workflow_routing.py" exit /b 1\n'
                'exit /b 0\n',
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                ],
                env={**os.environ, "SOULDRIVE_PYTHON": str(shim)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            calls = (root / "calls.txt").read_text(encoding="utf-8")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("episodic workflow routing gate failed", result.stdout + result.stderr)
        self.assertIn("scripts/eval_episodic_workflow_routing.py", calls)
        self.assertNotIn("npm.cmd", calls)


if __name__ == "__main__":
    unittest.main()
