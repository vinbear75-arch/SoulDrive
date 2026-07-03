import os
import json
import unittest

from core.enterprise_security import clamp_chat_top_k, is_loopback_client, max_chat_top_k, sanitize_payload


class EnterpriseSecurityTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("SOULDRIVE_MAX_CHAT_TOP_K", None)

    def test_loopback_client_detection_allows_only_local_hosts(self):
        self.assertTrue(is_loopback_client("127.0.0.1"))
        self.assertTrue(is_loopback_client("::1"))
        self.assertTrue(is_loopback_client("localhost"))
        self.assertTrue(is_loopback_client("testclient"))
        self.assertFalse(is_loopback_client("192.168.1.20"))
        self.assertFalse(is_loopback_client("example.com"))

    def test_chat_top_k_is_clamped_by_enterprise_limit(self):
        os.environ["SOULDRIVE_MAX_CHAT_TOP_K"] = "5"

        self.assertEqual(max_chat_top_k(), 5)
        self.assertEqual(clamp_chat_top_k(0), 1)
        self.assertEqual(clamp_chat_top_k(4), 4)
        self.assertEqual(clamp_chat_top_k(99), 5)

    def test_chat_top_k_defaults_to_six_evidence_items(self):
        self.assertEqual(clamp_chat_top_k(None), 6)
        self.assertEqual(clamp_chat_top_k("not-a-number"), 6)

    def test_chat_top_k_env_override_cannot_exceed_product_ceiling(self):
        os.environ["SOULDRIVE_MAX_CHAT_TOP_K"] = "999"

        self.assertEqual(max_chat_top_k(), 20)
        self.assertEqual(clamp_chat_top_k(99), 20)

    def test_sanitize_payload_redacts_local_paths_in_strings(self):
        sanitized = sanitize_payload({
            "error": r"failed to load C:\Private\Project\models\model.gguf",
        })

        encoded = json.dumps(sanitized, ensure_ascii=False)
        self.assertIn("[local path]", sanitized["error"])
        self.assertNotIn("D:\\PycharmProjects", encoded)


if __name__ == "__main__":
    unittest.main()
