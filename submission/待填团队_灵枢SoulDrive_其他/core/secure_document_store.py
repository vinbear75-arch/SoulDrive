import base64
import hashlib
import hmac
import json
import os
import time
import sqlite3
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.document_types import document_media_type, is_supported_document
from core.secure_object_store import SecureObjectStore


class SecureDocumentStore:
    def __init__(self, workspace, keys):
        self.workspace = workspace
        self.keys = keys
        self.object_store = SecureObjectStore(Path(workspace.documents_path), keys.document_key, purpose="documents")
        self.conn = sqlite3.connect(workspace.document_manifest_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self):
        cursor = self.conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                object_id TEXT PRIMARY KEY,
                content_digest TEXT NOT NULL UNIQUE,
                size_bytes INTEGER NOT NULL,
                modified_at REAL NOT NULL,
                metadata_nonce TEXT NOT NULL,
                metadata_ciphertext TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def import_document(self, source_path: str) -> dict:
        source = Path(source_path)
        display_name = source.name or "unknown"

        if not is_supported_document(source.name):
            return {"name": display_name, "status": "rejected", "error_code": "UNSUPPORTED_FILE_TYPE"}
        if not source.exists() or not source.is_file():
            return {"name": display_name, "status": "rejected", "error_code": "SOURCE_NOT_FOUND"}

        payload = source.read_bytes()
        content_digest = self._content_digest(payload)
        existing = self._row_by_digest(content_digest)
        if existing is not None:
            return {
                "name": existing["name"],
                "status": "already_present",
                "media_type": document_media_type(existing["name"]),
            }

        object_id = self._object_id(content_digest, display_name)
        self.object_store.write_bytes(object_id, payload)
        relative_path = self.object_store.object_path(object_id).relative_to(self.workspace.documents_path).as_posix()
        metadata = {
            "name": display_name,
            "relative_path": relative_path,
            "media_type": document_media_type(display_name),
        }
        nonce, ciphertext = self._encrypt_metadata(metadata)
        self.conn.execute(
            """
            INSERT INTO documents (
                object_id, content_digest, size_bytes, modified_at, metadata_nonce, metadata_ciphertext
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (object_id, content_digest, len(payload), source.stat().st_mtime, nonce, ciphertext),
        )
        self.conn.commit()
        return {
            "name": display_name,
            "status": "imported",
            "media_type": document_media_type(display_name),
        }

    def import_document_bytes(self, display_name: str, payload: bytes, *, modified_at: float | None = None) -> dict:
        display_name = Path(display_name).name or "unknown"

        if not is_supported_document(display_name):
            return {"name": display_name, "status": "rejected", "error_code": "UNSUPPORTED_FILE_TYPE"}

        content_digest = self._content_digest(payload)
        existing = self._row_by_digest(content_digest)
        if existing is not None:
            return {
                "name": existing["name"],
                "status": "already_present",
                "media_type": document_media_type(existing["name"]),
            }

        object_id = self._object_id(content_digest, display_name)
        self.object_store.write_bytes(object_id, payload)
        relative_path = self.object_store.object_path(object_id).relative_to(self.workspace.documents_path).as_posix()
        metadata = {
            "name": display_name,
            "relative_path": relative_path,
            "media_type": document_media_type(display_name),
        }
        nonce, ciphertext = self._encrypt_metadata(metadata)
        self.conn.execute(
            """
            INSERT INTO documents (
                object_id, content_digest, size_bytes, modified_at, metadata_nonce, metadata_ciphertext
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (object_id, content_digest, len(payload), float(modified_at or 0.0) or time.time(), nonce, ciphertext),
        )
        self.conn.commit()
        return {
            "name": display_name,
            "status": "imported",
            "media_type": document_media_type(display_name),
        }

    def list_documents(self) -> list[dict[str, Any]]:
        return [
            {
                "name": document["name"],
                "relative_path": document["relative_path"],
                "size_bytes": document["size_bytes"],
                "modified_at": document["modified_at"],
                "media_type": document["media_type"],
            }
            for document in self.iter_documents()
        ]

    def iter_documents(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT object_id, content_digest, size_bytes, modified_at, metadata_nonce, metadata_ciphertext
            FROM documents
            ORDER BY modified_at DESC, object_id ASC
            """
        ).fetchall()

        documents = []
        for row in rows:
            metadata = self._decrypt_metadata(row[4], row[5])
            documents.append(
                {
                    "object_id": row[0],
                    "content_digest": row[1],
                    "name": metadata["name"],
                    "relative_path": metadata["relative_path"],
                    "size_bytes": int(row[2]),
                    "modified_at": float(row[3]),
                    "media_type": metadata.get("media_type") or document_media_type(metadata["name"]),
                }
            )
        return documents

    def read_document_bytes(self, object_id: str) -> bytes:
        return self.object_store.read_bytes(object_id)

    def read_document_named(self, name: str) -> bytes | None:
        for document in self.iter_documents():
            if document["name"] == name:
                return self.read_document_bytes(document["object_id"])
        return None

    def delete_document(self, name: str) -> bytes | None:
        """Delete one managed document and return its plaintext only for index cleanup."""
        for document in self.iter_documents():
            if document["name"] != name:
                continue
            payload = self.read_document_bytes(document["object_id"])
            self.object_store.delete(document["object_id"])
            self.conn.execute("DELETE FROM documents WHERE object_id = ?", (document["object_id"],))
            self.conn.commit()
            return payload
        return None

    def close(self):
        self.conn.close()

    def _row_by_digest(self, content_digest: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT object_id, size_bytes, modified_at, metadata_nonce, metadata_ciphertext
            FROM documents
            WHERE content_digest = ?
            """,
            (content_digest,),
        ).fetchone()
        if row is None:
            return None
        metadata = self._decrypt_metadata(row[3], row[4])
        return {
            "object_id": row[0],
            "name": metadata["name"],
            "size_bytes": int(row[1]),
            "modified_at": float(row[2]),
        }

    def _content_digest(self, payload: bytes) -> str:
        return hmac.new(self.keys.metadata_key, payload, hashlib.sha256).hexdigest()

    def _object_id(self, content_digest: str, display_name: str) -> str:
        basis = f"{content_digest}:{display_name}".encode("utf-8")
        return hmac.new(self.keys.metadata_key, basis, hashlib.sha256).hexdigest()

    def _encrypt_metadata(self, payload: dict[str, Any]) -> tuple[str, str]:
        nonce = os.urandom(12)
        ciphertext = AESGCM(self.keys.metadata_key).encrypt(
            nonce,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            b"SoulDrive document metadata v1",
        )
        return _b64(nonce), _b64(ciphertext)

    def _decrypt_metadata(self, nonce: str, ciphertext: str) -> dict[str, Any]:
        try:
            payload = AESGCM(self.keys.metadata_key).decrypt(
                _unb64(nonce),
                _unb64(ciphertext),
                b"SoulDrive document metadata v1",
            )
        except InvalidTag as exc:
            raise ValueError("document metadata authentication failed") from exc
        return json.loads(payload.decode("utf-8"))


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))
