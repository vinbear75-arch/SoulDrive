import tempfile
import unittest
from pathlib import Path

from core.task_journal import TaskJournal
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import derive_workspace_keys


class TaskJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.workspace = SoulDriveWorkspace.from_drive(self.temp_dir.name).ensure()
        self.keys = derive_workspace_keys(b"x" * 32)

    def test_resume_skips_completed_steps(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)
        task = journal.create_task("test.workflow", {"value": 2}, ["first", "second"])
        calls = []
        journal.claim_task(task["task_id"], "999999:first")
        journal.begin_step(task["task_id"], "first")
        journal.complete_step(task["task_id"], "first", {"value": 3})
        journal.interrupt_task(task["task_id"], "simulated interruption")

        result = journal.run_steps(
            task["task_id"],
            "999998:resume",
            {
                "first": lambda _inputs, _outputs: calls.append("first") or {"value": 99},
                "second": lambda _inputs, outputs: calls.append("second") or {"value": outputs["first"]["value"] + 1},
            },
            lambda outputs: {"answer": outputs["second"]["value"]},
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["answer"], 4)
        self.assertEqual(calls, ["second"])
        self.assertEqual(result["progress"], {"completed": 2, "total": 2})

    def test_checkpoint_payload_is_not_stored_in_plaintext(self):
        journal = TaskJournal(self.workspace, self.keys)
        self.addCleanup(journal.close)
        task = journal.create_task("test.workflow", {"secret_value": "private input"}, ["first"])
        raw = Path(self.workspace.task_journal_path).read_bytes()

        self.assertNotIn(b"private input", raw)
        self.assertEqual(journal.get_task(task["task_id"])["inputs"]["secret_value"], "private input")


if __name__ == "__main__":
    unittest.main()
