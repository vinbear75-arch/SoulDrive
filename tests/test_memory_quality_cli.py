import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import eval_memory_quality


class MemoryQualityCliTests(unittest.TestCase):
    def _record(self, *, leakage=False, query=True):
        events = [{
            "op": "remember", "alias": "mail_preference", "subtype": "user_preference",
            "title": "邮件发送时段", "content": "用户偏好早上发送邮件。",
        }]
        if query:
            events.append({
                "op": "query", "query": "邮件发送时段",
                "expect": (
                    {"forbidden_aliases": ["mail_preference"]}
                    if leakage else {"required_aliases": ["never_saved"]}
                ),
            })
        return [{"schema_version": eval_memory_quality.SCHEMA_VERSION, "id": "CLI-FIXTURE", "events": events}]

    def test_missing_memory_is_failure_even_when_report_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "failure.json"
            with patch.object(eval_memory_quality, "load_records", return_value=self._record()):
                with patch("builtins.print"):
                    status = eval_memory_quality.main(["--output", str(output)])
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["summary"]["passed_queries"], 0)
        self.assertEqual(report["results"][0]["query_results"][0]["missing_required_aliases"], ["never_saved"])
        self.assertEqual(status, 1)

    def test_forbidden_memory_leakage_returns_failure(self):
        with patch.object(eval_memory_quality, "load_records", return_value=self._record(leakage=True)):
            with patch("builtins.print") as printed:
                status = eval_memory_quality.main([])
        report = json.loads(printed.call_args.args[0])
        self.assertEqual(report["summary"]["stale_or_forbidden_leakage_rate"], 1.0)
        self.assertEqual(status, 1)

    def test_unscored_scenario_cannot_succeed_vacuously(self):
        with self.assertRaisesRegex(ValueError, "query|context"):
            eval_memory_quality.evaluate(self._record(query=False))

    def test_frozen_memory_contract_dataset_returns_success(self):
        with patch("builtins.print") as printed:
            status = eval_memory_quality.main([])
        report = json.loads(printed.call_args.args[0])
        self.assertGreater(report["summary"]["total_queries"], 0)
        self.assertEqual(report["summary"]["passed_queries"], report["summary"]["total_queries"])
        self.assertEqual(status, 0)

    @unittest.skipUnless(os.name == "nt", "PowerShell gate integration is Windows-specific")
    def test_aggregate_check_stops_when_memory_quality_gate_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            script = root / "scripts" / "check.ps1"
            shutil.copyfile(eval_memory_quality.ROOT / "scripts" / "check.ps1", script)
            shim = root / "python-fixture.cmd"
            shim.write_text(
                '@echo off\n'
                'echo %*>>"%~dp0calls.txt"\n'
                'if "%~1"=="scripts/eval_memory_quality.py" exit /b 1\n'
                'exit /b 0\n',
                encoding="utf-8",
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                env={**os.environ, "SOULDRIVE_PYTHON": str(shim)},
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            calls = (root / "calls.txt").read_text(encoding="utf-8")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("memory quality gate failed", result.stdout + result.stderr)
        self.assertIn("scripts/eval_memory_quality.py", calls)
        self.assertNotIn("scripts/eval_graphrag_route.py", calls)
        self.assertNotIn("scripts/eval_workflow_roles.py", calls)
