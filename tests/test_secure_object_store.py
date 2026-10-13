import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from core.secure_object_store import SecureObjectStore, SecureObjectStoreError


class SecureObjectStoreTests(unittest.TestCase):
    def test_binary_roundtrip_uses_authenticated_encryption(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SecureObjectStore(Path(temp_dir), b"k" * 32, purpose="documents")
            store.write_bytes("doc-1", b"secret pdf bytes")

            restored = store.read_bytes("doc-1")

        self.assertEqual(restored, b"secret pdf bytes")

    def test_json_roundtrip_preserves_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SecureObjectStore(Path(temp_dir), b"m" * 32, purpose="metadata")
            payload = {"name": "paper.pdf", "size": 123}
            store.write_json("doc-2", payload)

            restored = store.read_json("doc-2")

        self.assertEqual(restored, payload)

    def test_tampered_ciphertext_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SecureObjectStore(Path(temp_dir), b"x" * 32, purpose="documents")
            path = store.write_bytes("doc-3", b"top secret")
            envelope = json.loads(path.read_text(encoding="utf-8"))
            envelope["ciphertext"] = envelope["ciphertext"][:-2] + "AA"
            path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")

            with self.assertRaises(SecureObjectStoreError):
                store.read_bytes("doc-3")

    def test_wrong_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            writer = SecureObjectStore(Path(temp_dir), b"z" * 32, purpose="documents")
            writer.write_bytes("doc-4", b"content")
            reader = SecureObjectStore(Path(temp_dir), b"y" * 32, purpose="documents")

            with self.assertRaises(SecureObjectStoreError):
                reader.read_bytes("doc-4")

    def test_atomic_write_keeps_existing_authenticated_object_when_flush_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SecureObjectStore(Path(temp_dir), b"q" * 32, purpose="documents")
            path = store.write_bytes("doc-5", b"original")
            original = path.read_bytes()
            with patch("core.secure_object_store.os.fsync", side_effect=OSError("object disk full")):
                with self.assertRaisesRegex(OSError, "object disk full"):
                    store.write_bytes("doc-5", b"replacement")

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])
            self.assertEqual(store.read_bytes("doc-5"), b"original")

    def test_atomic_write_keeps_existing_authenticated_object_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SecureObjectStore(Path(temp_dir), b"r" * 32, purpose="documents")
            path = store.write_bytes("doc-6", b"original")
            original = path.read_bytes()
            with patch("core.secure_object_store.os.replace", side_effect=OSError("object replace denied")):
                with self.assertRaisesRegex(OSError, "object replace denied"):
                    store.write_bytes("doc-6", b"replacement")

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])
            self.assertEqual(store.read_bytes("doc-6"), b"original")


if __name__ == "__main__":
    unittest.main()
