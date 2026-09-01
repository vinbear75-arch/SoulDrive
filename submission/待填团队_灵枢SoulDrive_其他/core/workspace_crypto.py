import base64
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


KEYSTORE_VERSION = 2
LEGACY_KEYSTORE_VERSION = 1
LEGACY_KDF_NAME = "pbkdf2-sha256"
KDF_NAME = "argon2id"
KEY_WRAP = "aes-256-gcm"
DATA_KEY_BYTES = 32
SALT_BYTES = 16
NONCE_BYTES = 12
PBKDF2_ITERATIONS = 600_000
ARGON2_ITERATIONS = 3
ARGON2_LANES = 4
ARGON2_MEMORY_COST = 64 * 1024


class WorkspaceCryptoError(Exception):
    pass


class KeystoreAlreadyInitializedError(WorkspaceCryptoError):
    pass


class KeystoreNotInitializedError(WorkspaceCryptoError):
    pass


class IncorrectPassphraseError(WorkspaceCryptoError):
    pass


@dataclass(frozen=True)
class WorkspaceKeys:
    workspace_data_key: bytes
    document_key: bytes
    metadata_key: bytes
    index_key: bytes
    graph_key: bytes
    audit_key: bytes


def is_keystore_initialized(workspace) -> bool:
    return Path(workspace.keystore_path).exists()


def initialize_keystore(workspace, passphrase: str, format_version: int = KEYSTORE_VERSION) -> dict:
    if not passphrase:
        raise ValueError("passphrase is required")

    path = Path(workspace.keystore_path)
    if path.exists():
        raise KeystoreAlreadyInitializedError("workspace keystore already initialized")

    workspace_data_key = os.urandom(DATA_KEY_BYTES)
    payload = _build_keystore_payload(passphrase, workspace_data_key, format_version)
    _write_payload(path, payload)
    return {"initialized": True, "version": payload["version"]}


def unlock_keystore(workspace, passphrase: str) -> WorkspaceKeys:
    if not passphrase:
        raise IncorrectPassphraseError("passphrase is required")

    path = Path(workspace.keystore_path)
    if not path.exists():
        raise KeystoreNotInitializedError("workspace keystore is not initialized")

    payload = json.loads(path.read_text(encoding="utf-8"))
    workspace_data_key = _unwrap_workspace_data_key(passphrase, payload)
    return _workspace_keys(workspace_data_key)


def migrate_keystore_if_needed(workspace, passphrase: str) -> dict:
    path = Path(workspace.keystore_path)
    if not path.exists():
        raise KeystoreNotInitializedError("workspace keystore is not initialized")

    payload = json.loads(path.read_text(encoding="utf-8"))
    if int(payload.get("version") or 0) == KEYSTORE_VERSION:
        return {"migrated": False, "version": KEYSTORE_VERSION}

    workspace_data_key = _unwrap_workspace_data_key(passphrase, payload)
    migrated_payload = _build_keystore_payload(passphrase, workspace_data_key, KEYSTORE_VERSION)
    _write_payload(path, migrated_payload)
    return {"migrated": True, "version": KEYSTORE_VERSION}


def rotate_passphrase(workspace, current_passphrase: str, new_passphrase: str) -> dict:
    if not new_passphrase:
        raise ValueError("new passphrase is required")

    path = Path(workspace.keystore_path)
    if not path.exists():
        raise KeystoreNotInitializedError("workspace keystore is not initialized")

    payload = json.loads(path.read_text(encoding="utf-8"))
    workspace_data_key = _unwrap_workspace_data_key(current_passphrase, payload)
    rotated_payload = _build_keystore_payload(new_passphrase, workspace_data_key, KEYSTORE_VERSION)
    _write_payload(path, rotated_payload)
    return {"rotated": True, "version": KEYSTORE_VERSION}


def _workspace_keys(workspace_data_key: bytes) -> WorkspaceKeys:
    return WorkspaceKeys(
        workspace_data_key=workspace_data_key,
        document_key=_derive_purpose_key(workspace_data_key, b"documents"),
        metadata_key=_derive_purpose_key(workspace_data_key, b"metadata"),
        index_key=_derive_purpose_key(workspace_data_key, b"indexes"),
        graph_key=_derive_purpose_key(workspace_data_key, b"graph"),
        audit_key=_derive_purpose_key(workspace_data_key, b"audit"),
    )


def derive_workspace_keys(workspace_data_key: bytes) -> WorkspaceKeys:
    return _workspace_keys(workspace_data_key)


def _build_keystore_payload(passphrase: str, workspace_data_key: bytes, version: int) -> dict:
    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(NONCE_BYTES)

    if version == LEGACY_KEYSTORE_VERSION:
        password_key = _derive_password_key_v1(passphrase, salt, PBKDF2_ITERATIONS)
        encrypted = AESGCM(password_key).encrypt(nonce, workspace_data_key, _aad(LEGACY_KEYSTORE_VERSION))
        return {
            "version": LEGACY_KEYSTORE_VERSION,
            "kdf": LEGACY_KDF_NAME,
            "kdf_params": {
                "iterations": PBKDF2_ITERATIONS,
                "salt": _b64(salt),
            },
            "key_wrap": KEY_WRAP,
            "nonce": _b64(nonce),
            "encrypted_workspace_data_key": _b64(encrypted),
            "created_at": time.time(),
        }

    if version != KEYSTORE_VERSION:
        raise WorkspaceCryptoError("unsupported keystore format")

    password_key = _derive_password_key_v2(
        passphrase,
        salt=salt,
        iterations=ARGON2_ITERATIONS,
        lanes=ARGON2_LANES,
        memory_cost=ARGON2_MEMORY_COST,
    )
    encrypted = AESGCM(password_key).encrypt(nonce, workspace_data_key, _aad(KEYSTORE_VERSION))
    return {
        "version": KEYSTORE_VERSION,
        "kdf": KDF_NAME,
        "kdf_params": {
            "iterations": ARGON2_ITERATIONS,
            "lanes": ARGON2_LANES,
            "memory_cost": ARGON2_MEMORY_COST,
            "salt": _b64(salt),
        },
        "key_wrap": KEY_WRAP,
        "nonce": _b64(nonce),
        "encrypted_workspace_data_key": _b64(encrypted),
        "created_at": time.time(),
    }


def _unwrap_workspace_data_key(passphrase: str, payload: dict) -> bytes:
    version = int(payload.get("version") or 0)
    kdf_name = payload.get("kdf")
    if payload.get("key_wrap") != KEY_WRAP:
        raise WorkspaceCryptoError("unsupported keystore format")

    params = payload.get("kdf_params") or {}
    salt = _unb64(params["salt"])
    nonce = _unb64(payload["nonce"])
    encrypted = _unb64(payload["encrypted_workspace_data_key"])

    if version == LEGACY_KEYSTORE_VERSION and kdf_name == LEGACY_KDF_NAME:
        password_key = _derive_password_key_v1(passphrase, salt, int(params["iterations"]))
        aad = _aad(LEGACY_KEYSTORE_VERSION)
    elif version == KEYSTORE_VERSION and kdf_name == KDF_NAME:
        password_key = _derive_password_key_v2(
            passphrase,
            salt=salt,
            iterations=int(params["iterations"]),
            lanes=int(params["lanes"]),
            memory_cost=int(params["memory_cost"]),
        )
        aad = _aad(KEYSTORE_VERSION)
    else:
        raise WorkspaceCryptoError("unsupported keystore format")

    try:
        return AESGCM(password_key).decrypt(nonce, encrypted, aad)
    except InvalidTag as exc:
        raise IncorrectPassphraseError("incorrect passphrase") from exc


def _write_payload(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _derive_password_key_v1(passphrase: str, salt: bytes, iterations: int) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=DATA_KEY_BYTES,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(passphrase.encode("utf-8"))


def _derive_password_key_v2(
    passphrase: str,
    *,
    salt: bytes,
    iterations: int,
    lanes: int,
    memory_cost: int,
) -> bytes:
    kdf = Argon2id(
        salt=salt,
        length=DATA_KEY_BYTES,
        iterations=iterations,
        lanes=lanes,
        memory_cost=memory_cost,
    )
    return kdf.derive(passphrase.encode("utf-8"))


def _derive_purpose_key(workspace_data_key: bytes, purpose: bytes) -> bytes:
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=DATA_KEY_BYTES,
        salt=None,
        info=b"souldrive:" + purpose,
    )
    return hkdf.derive(workspace_data_key)


def _aad(version: int) -> bytes:
    return f"SoulDrive workspace data key v{version}".encode("ascii")


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))
