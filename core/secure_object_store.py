import base64
import json
import os
import tempfile
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


OBJECT_MAGIC = "SDOBJ1"
OBJECT_VERSION = 1
NONCE_BYTES = 12


class SecureObjectStoreError(Exception):
    pass


class SecureObjectStore:
    def __init__(self, root_path: Path, key: bytes, *, purpose: str):
        self.root_path = Path(root_path)
        self.key = key
        self.purpose = purpose
        self.root_path.mkdir(parents=True, exist_ok=True)

    def write_bytes(self, object_id: str, payload: bytes) -> Path:
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = AESGCM(self.key).encrypt(nonce, payload, self._aad(object_id))
        path = self.object_path(object_id)
        envelope = {
            "magic": OBJECT_MAGIC,
            "version": OBJECT_VERSION,
            "purpose": self.purpose,
            "object_id": object_id,
            "nonce": _b64(nonce),
            "ciphertext": _b64(ciphertext),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        _atomic_write_bytes(path, payload)
        return path

    def read_bytes(self, object_id: str) -> bytes:
        path = self.object_path(object_id)
        envelope = json.loads(path.read_text(encoding="utf-8"))
        self._validate_envelope(envelope, object_id)
        try:
            return AESGCM(self.key).decrypt(
                _unb64(envelope["nonce"]),
                _unb64(envelope["ciphertext"]),
                self._aad(object_id),
            )
        except (InvalidTag, ValueError, KeyError, TypeError) as exc:
            raise SecureObjectStoreError("secure object authentication failed") from exc

    def write_json(self, object_id: str, payload: dict) -> Path:
        return self.write_bytes(object_id, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def read_json(self, object_id: str) -> dict:
        return json.loads(self.read_bytes(object_id).decode("utf-8"))

    def delete(self, object_id: str) -> None:
        self.object_path(object_id).unlink(missing_ok=True)

    def object_path(self, object_id: str) -> Path:
        return self.root_path / object_id[:2] / f"{object_id}.sdoc"

    def _aad(self, object_id: str) -> bytes:
        return f"{OBJECT_MAGIC}:{OBJECT_VERSION}:{self.purpose}:{object_id}".encode("utf-8")

    def _validate_envelope(self, envelope: dict, object_id: str) -> None:
        if envelope.get("magic") != OBJECT_MAGIC:
            raise SecureObjectStoreError("unsupported object format")
        if int(envelope.get("version") or 0) != OBJECT_VERSION:
            raise SecureObjectStoreError("unsupported object version")
        if envelope.get("purpose") != self.purpose:
            raise SecureObjectStoreError("secure object purpose mismatch")
        if envelope.get("object_id") != object_id:
            raise SecureObjectStoreError("secure object id mismatch")


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Keep an authenticated object either old and readable or fully replaced."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
