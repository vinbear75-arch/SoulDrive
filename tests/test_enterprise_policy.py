import json
import tempfile
import unittest
from pathlib import Path

from core.enterprise_policy import ensure_policy_file, load_policy, production_policy, validate_policy_for_production


class EnterprisePolicyTests(unittest.TestCase):
    def test_repository_policy_is_production_hardened(self):
        policy = load_policy(Path(__file__).resolve().parent.parent / "config" / "enterprise-policy.json")
        report = validate_policy_for_production(policy)

        self.assertTrue(report["ready"], report["issues"])

    def test_ensure_policy_file_creates_enterprise_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "enterprise-policy.json"

            ensure_policy_file(path)
            policy = load_policy(path)

        self.assertTrue(policy.disable_network_update)
        self.assertTrue(policy.allow_lite_mode)
        self.assertEqual(policy.max_chat_top_k, 8)

    def test_load_policy_bounds_enterprise_limits(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "enterprise-policy.json"
            path.write_text(json.dumps({
                "organization": "ACME Lab",
                "allow_lite_mode": False,
                "require_signed_license": True,
                "max_chat_top_k": 999,
                "rate_limit_per_minute": -1,
            }), encoding="utf-8")

            policy = load_policy(path)

        self.assertEqual(policy.organization, "ACME Lab")
        self.assertFalse(policy.allow_lite_mode)
        self.assertTrue(policy.require_signed_license)
        self.assertEqual(policy.max_chat_top_k, 20)
        self.assertEqual(policy.rate_limit_per_minute, 1)

    def test_load_policy_falls_back_for_non_object_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "enterprise-policy.json"
            path.write_text("[]", encoding="utf-8")

            policy = load_policy(path)

        self.assertEqual(policy.organization, "SoulDrive Local")
        self.assertTrue(policy.allow_lite_mode)

    def test_production_policy_passes_hardened_validation(self):
        report = validate_policy_for_production(production_policy("ACME Lab"))

        self.assertTrue(report["ready"])
        self.assertEqual(report["issues"], [])

    def test_default_policy_reports_hardening_gaps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "enterprise-policy.json"
            ensure_policy_file(path)
            report = validate_policy_for_production(load_policy(path))

        self.assertFalse(report["ready"])
        self.assertIn("SIGNED_LICENSE_NOT_REQUIRED", report["issues"])
        self.assertIn("LITE_MODE_ALLOWED", report["issues"])


if __name__ == "__main__":
    unittest.main()
