import os
import unittest
from unittest.mock import patch

from core.api_security import is_authorized_token
from core.observability import RuntimeMetrics


class ApiSecurityTests(unittest.TestCase):
    def test_authorization_is_closed_when_no_token_is_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(is_authorized_token(None, configured_token=None))

    def test_authorization_can_be_opened_explicitly_for_development(self):
        with patch.dict(os.environ, {"SOULDRIVE_ALLOW_UNAUTHENTICATED_API": "1"}, clear=True):
            self.assertTrue(is_authorized_token(None, configured_token=None))

    def test_authorization_requires_matching_token_when_configured(self):
        self.assertTrue(is_authorized_token("secret", configured_token="secret"))
        self.assertFalse(is_authorized_token("wrong", configured_token="secret"))
        self.assertFalse(is_authorized_token(None, configured_token="secret"))

    def test_metrics_include_remote_rejection_counter(self):
        metrics = RuntimeMetrics()

        metrics.increment("rejected_remote_requests")
        snapshot = metrics.snapshot()

        self.assertEqual(snapshot["rejected_remote_requests"], 1)

    def test_metrics_include_model_load_status(self):
        metrics = RuntimeMetrics()

        metrics.increment("model_load_failures")
        metrics.record_model_load(1234)
        snapshot = metrics.snapshot()

        self.assertEqual(snapshot["model_load_failures"], 1)
        self.assertEqual(snapshot["last_model_load_ms"], 1234)

    def test_metrics_include_indexing_status(self):
        metrics = RuntimeMetrics()

        metrics.record_indexing_run(2345, 12, failed=True)
        snapshot = metrics.snapshot()

        self.assertEqual(snapshot["indexing_runs"], 1)
        self.assertEqual(snapshot["indexing_failures"], 1)
        self.assertEqual(snapshot["last_indexing_ms"], 2345)
        self.assertEqual(snapshot["last_indexed_chunk_count"], 12)

    def test_metrics_include_retrieval_status(self):
        metrics = RuntimeMetrics()

        metrics.record_retrieval_run(345, candidate_count=42, evidence_count=6)
        snapshot = metrics.snapshot()

        self.assertEqual(snapshot["retrieval_runs"], 1)
        self.assertEqual(snapshot["last_retrieval_ms"], 345)
        self.assertEqual(snapshot["last_retrieval_candidate_count"], 42)
        self.assertEqual(snapshot["last_retrieval_evidence_count"], 6)

    def test_metrics_last_error_redacts_paths_and_sensitive_fields(self):
        metrics = RuntimeMetrics()

        metrics.record_error(
            r"failed C:\Private\Project\models\model.gguf token=SECRET"
        )
        snapshot = metrics.snapshot()

        self.assertIn("[local path]", snapshot["last_error"])
        self.assertIn("token=[redacted]", snapshot["last_error"])
        self.assertNotIn("D:\\PycharmProjects", snapshot["last_error"])
        self.assertNotIn("SECRET", snapshot["last_error"])


if __name__ == "__main__":
    unittest.main()
