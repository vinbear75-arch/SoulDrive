import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import (
    IncorrectPassphraseError,
    initialize_keystore,
    is_keystore_initialized,
    migrate_keystore_if_needed,
    rotate_passphrase,
    unlock_keystore,
)


class WorkspaceCryptoTests(unittest.TestCase):
    def test_initialize_keystore_writes_wrapped_key_without_plaintext_passphrase(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            result = initialize_keystore(workspace, "correct horse battery staple")
            payload = json.loads(Path(workspace.keystore_path).read_text(encoding="utf-8"))
            initialized = is_keystore_initialized(workspace)

        self.assertTrue(result["initialized"])
        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload["kdf"], "argon2id")
        self.assertEqual(payload["key_wrap"], "aes-256-gcm")
        self.assertNotIn("correct horse battery staple", json.dumps(payload))
        self.assertGreater(len(base64.b64decode(payload["encrypted_workspace_data_key"])), 32)
        self.assertTrue(initialized)

    def test_unlock_keystore_rejects_wrong_passphrase(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "right-passphrase")

            with self.assertRaises(IncorrectPassphraseError):
                unlock_keystore(workspace, "wrong-passphrase")

    def test_unlock_keystore_returns_stable_purpose_keys_for_correct_passphrase(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "right-passphrase")

            first = unlock_keystore(workspace, "right-passphrase")
            second = unlock_keystore(workspace, "right-passphrase")

        self.assertEqual(first.document_key, second.document_key)
        self.assertEqual(first.index_key, second.index_key)
        self.assertEqual(first.graph_key, second.graph_key)
        self.assertEqual(first.audit_key, second.audit_key)
        self.assertEqual(len(first.workspace_data_key), 32)
        self.assertEqual(len(first.document_key), 32)

    def test_unlock_keystore_keeps_v1_payload_compatible_until_migrated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "legacy-passphrase", format_version=1)

            payload_before = json.loads(Path(workspace.keystore_path).read_text(encoding="utf-8"))
            keys = unlock_keystore(workspace, "legacy-passphrase")
            payload_after = json.loads(Path(workspace.keystore_path).read_text(encoding="utf-8"))

        self.assertEqual(payload_before["version"], 1)
        self.assertEqual(payload_after["version"], 1)
        self.assertEqual(len(keys.workspace_data_key), 32)

    def test_migrate_keystore_rewrites_v1_payload_to_v2_without_changing_workspace_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "legacy-passphrase", format_version=1)
            legacy_keys = unlock_keystore(workspace, "legacy-passphrase")

            migration = migrate_keystore_if_needed(workspace, "legacy-passphrase")
            migrated_payload = json.loads(Path(workspace.keystore_path).read_text(encoding="utf-8"))
            migrated_keys = unlock_keystore(workspace, "legacy-passphrase")

        self.assertTrue(migration["migrated"])
        self.assertEqual(migrated_payload["version"], 2)
        self.assertEqual(migrated_payload["kdf"], "argon2id")
        self.assertEqual(legacy_keys.workspace_data_key, migrated_keys.workspace_data_key)

    def test_rotate_passphrase_rewraps_workspace_key_without_reencrypting_data_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "old-passphrase")
            previous_keys = unlock_keystore(workspace, "old-passphrase")

            rotation = rotate_passphrase(workspace, "old-passphrase", "new-passphrase")
            rotated_payload = json.loads(Path(workspace.keystore_path).read_text(encoding="utf-8"))
            rotated_keys = unlock_keystore(workspace, "new-passphrase")

            with self.assertRaises(IncorrectPassphraseError):
                unlock_keystore(workspace, "old-passphrase")

        self.assertTrue(rotation["rotated"])
        self.assertEqual(rotated_payload["version"], 2)
        self.assertEqual(previous_keys.workspace_data_key, rotated_keys.workspace_data_key)
        self.assertEqual(previous_keys.document_key, rotated_keys.document_key)

    def test_keystore_rotation_keeps_old_payload_when_flush_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "old-passphrase")
            keystore = Path(workspace.keystore_path)
            original = keystore.read_bytes()
            with patch("core.workspace_crypto.os.fsync", side_effect=OSError("keystore disk full")):
                with self.assertRaisesRegex(OSError, "keystore disk full"):
                    rotate_passphrase(workspace, "old-passphrase", "new-passphrase")

            self.assertEqual(keystore.read_bytes(), original)
            self.assertEqual(len(unlock_keystore(workspace, "old-passphrase").workspace_data_key), 32)
            self.assertEqual(list(keystore.parent.glob(f".{keystore.name}.*.tmp")), [])

    def test_keystore_rotation_keeps_old_payload_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            initialize_keystore(workspace, "old-passphrase")
            keystore = Path(workspace.keystore_path)
            original = keystore.read_bytes()
            with patch("core.workspace_crypto.os.replace", side_effect=OSError("keystore replace denied")):
                with self.assertRaisesRegex(OSError, "keystore replace denied"):
                    rotate_passphrase(workspace, "old-passphrase", "new-passphrase")

            self.assertEqual(keystore.read_bytes(), original)
            self.assertEqual(len(unlock_keystore(workspace, "old-passphrase").workspace_data_key), 32)
            self.assertEqual(list(keystore.parent.glob(f".{keystore.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
