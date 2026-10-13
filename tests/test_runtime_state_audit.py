import tempfile
import unittest
from unittest.mock import patch

import core.runtime_state as runtime_state


class RuntimeStateAuditTests(unittest.TestCase):
    class FailingAuditLogger:
        def append_event(self, *_args, **_kwargs):
            raise OSError("audit-secret-runtime-state")

    def test_unlock_persists_state_when_audit_sink_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = f"{temp_dir}\\runtime_state.json"
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                runtime_state,
                "_audit_logger_for_workspace_path",
                return_value=self.FailingAuditLogger(),
            ):
                state = runtime_state.unlock_runtime(
                    "LOCAL",
                    None,
                    None,
                    temp_dir,
                    reason="local workspace ready",
                )

            self.assertTrue(state["hardware_mounted"])
            self.assertTrue(state["locked"])
            self.assertEqual(state["workspace_path"], temp_dir)

    def test_lock_persists_state_when_audit_sink_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = f"{temp_dir}\\runtime_state.json"
            with patch.object(runtime_state, "STATE_PATH", state_path), patch.object(
                runtime_state,
                "_audit_logger_for_workspace_path",
                return_value=self.FailingAuditLogger(),
            ):
                runtime_state.unlock_runtime("LOCAL", None, None, temp_dir)
                state = runtime_state.lock_runtime("workspace removed")

            self.assertTrue(state["locked"])
            self.assertEqual(state["reason"], "workspace removed")
            self.assertIsNone(state["workspace_path"])


if __name__ == "__main__":
    unittest.main()
