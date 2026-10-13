import base64
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.keyword_index import KeywordSearchResult
from core.keyword_index import lexical_tokens
from core.retrieval import bm25_scores


PARENT_AAD = b"SoulDrive secure parent v1"
CHILD_CONTENT_AAD = b"SoulDrive secure child content v1"
CHILD_METADATA_AAD = b"SoulDrive secure child metadata v1"
CHILD_EMBEDDING_AAD = b"SoulDrive secure child embedding v1"
KEYWORD_POSTING_AAD = b"SoulDrive secure keyword posting v1"
MAX_KEYWORD_TOKENS = 4096


def _synchronized(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


def _document_name_matches(metadata: dict[str, Any], document_names: list[str]) -> bool:
    if not document_names:
        return True
    expected = {
        " ".join(str(item or "").strip().split()).casefold()
        for item in document_names
        if " ".join(str(item or "").strip().split())
    }
    identities = set()
    for key in ("source_filename", "document_name", "catalog_file_ref"):
        value = " ".join(str((metadata or {}).get(key) or "").strip().split())
        if value:
            identities.add(value.casefold())
            identities.add(value.replace("\\", "/").rsplit("/", 1)[-1].casefold())
    return bool(identities & expected)


def _document_id_candidates(document_id: str) -> set[str]:
    normalized = str(document_id or "").strip()
    if not normalized:
        return set()
    if normalized.startswith("secure_"):
        return {normalized, normalized.removeprefix("secure_")}
    return {normalized, f"secure_{normalized}"}


def _document_id_matches(metadata: dict[str, Any], document_ids: list[str]) -> bool:
    if not document_ids:
        return True
    metadata_id = str((metadata or {}).get("document_id") or "").strip()
    if not metadata_id:
        return False
    expected = set()
    for document_id in document_ids:
        expected.update(_document_id_candidates(document_id))
    return bool(_document_id_candidates(metadata_id) & expected)


@dataclass(frozen=True)
class _ChildRecord:
    child_id: str
    content: str
    metadata: dict[str, Any]
    embedding: list[float]


@dataclass(frozen=True)
class SecureCandidate:
    chunk_id: str
    score: float = 0.0
    content: str | None = None
    metadata: dict[str, Any] | None = None


class SecureVectorStore:
    def __init__(self, db_path: str, keys):
        self.db_path = db_path
        self.keys = keys
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self.collection = _SecureCollectionAdapter(self)
        self._init_schema()

    def _init_schema(self):
        cursor = self.conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS parent_documents (
                parent_id TEXT PRIMARY KEY,
                document_hash TEXT,
                content_nonce TEXT NOT NULL,
                content_ciphertext TEXT NOT NULL,
                metadata_nonce TEXT NOT NULL,
                metadata_ciphertext TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS child_chunks (
                child_id TEXT PRIMARY KEY,
                parent_id TEXT NOT NULL,
                document_hash TEXT,
                content_nonce TEXT NOT NULL,
                content_ciphertext TEXT NOT NULL,
                metadata_nonce TEXT NOT NULL,
                metadata_ciphertext TEXT NOT NULL,
                embedding_nonce TEXT NOT NULL,
                embedding_ciphertext TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS keyword_postings (
                token_id TEXT PRIMARY KEY,
                posting_nonce TEXT NOT NULL,
                posting_ciphertext TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    @_synchronized
    def upsert_parent(self, parent_id: str, content: str, metadata: dict[str, Any]):
        document_hash = str(metadata.get("document_hash") or "")
        content_nonce, content_ciphertext = self._encrypt_bytes(
            self.keys.index_key,
            content.encode("utf-8"),
            PARENT_AAD + parent_id.encode("utf-8"),
        )
        metadata_nonce, metadata_ciphertext = self._encrypt_bytes(
            self.keys.metadata_key,
            json.dumps(metadata, ensure_ascii=False).encode("utf-8"),
            PARENT_AAD + b":metadata:" + parent_id.encode("utf-8"),
        )
        self.conn.execute(
            """
            INSERT OR REPLACE INTO parent_documents (
                parent_id, document_hash, content_nonce, content_ciphertext, metadata_nonce, metadata_ciphertext
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                parent_id,
                document_hash,
                content_nonce,
                content_ciphertext,
                metadata_nonce,
                metadata_ciphertext,
            ),
        )
        self.conn.commit()

    @_synchronized
    def get_parent(self, parent_id: str):
        row = self.conn.execute(
            """
            SELECT content_nonce, content_ciphertext, metadata_nonce, metadata_ciphertext
            FROM parent_documents
            WHERE parent_id = ?
            """,
            (parent_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "content": self._decrypt_text(
                self.keys.index_key,
                row[0],
                row[1],
                PARENT_AAD + parent_id.encode("utf-8"),
            ),
            "metadata": self._decrypt_json(
                self.keys.metadata_key,
                row[2],
                row[3],
                PARENT_AAD + b":metadata:" + parent_id.encode("utf-8"),
            ),
        }

    @_synchronized
    def get_adjacent_parent(self, parent_id: str, offset: int = 1):
        match = re.match(r"^(.*)_parent_(\d+)$", str(parent_id or ""))
        if not match:
            return None
        target_id = f"{match.group(1)}_parent_{int(match.group(2)) + int(offset)}"
        return self.get_parent(target_id)

    @_synchronized
    def add_chunks(self, ids: list[str], documents: list[str], metadatas: list[dict[str, Any]], embeddings: list[list[float]]):
        cursor = self.conn.cursor()
        for child_id, content, metadata, embedding in zip(ids, documents, metadatas, embeddings):
            parent_id = str(metadata.get("parent_id") or "")
            document_hash = str(metadata.get("document_hash") or "")
            content_nonce, content_ciphertext = self._encrypt_bytes(
                self.keys.index_key,
                content.encode("utf-8"),
                CHILD_CONTENT_AAD + child_id.encode("utf-8"),
            )
            metadata_nonce, metadata_ciphertext = self._encrypt_bytes(
                self.keys.metadata_key,
                json.dumps(metadata, ensure_ascii=False).encode("utf-8"),
                CHILD_METADATA_AAD + child_id.encode("utf-8"),
            )
            embedding_nonce, embedding_ciphertext = self._encrypt_bytes(
                self.keys.index_key,
                json.dumps(embedding).encode("utf-8"),
                CHILD_EMBEDDING_AAD + child_id.encode("utf-8"),
            )
            cursor.execute(
                """
                INSERT OR REPLACE INTO child_chunks (
                    child_id, parent_id, document_hash,
                    content_nonce, content_ciphertext,
                    metadata_nonce, metadata_ciphertext,
                    embedding_nonce, embedding_ciphertext
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    child_id,
                    parent_id,
                    document_hash,
                    content_nonce,
                    content_ciphertext,
                    metadata_nonce,
                    metadata_ciphertext,
                    embedding_nonce,
                    embedding_ciphertext,
                ),
            )
            self._upsert_keyword_postings(child_id, content)
        self.conn.commit()

    @_synchronized
    def query(self, query_embeddings: list[list[float]], n_results: int, include: list[str]):
        if not query_embeddings:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}

        query_embedding = query_embeddings[0]
        ranked = sorted(
            (
                (
                    self._cosine_similarity(query_embedding, record.embedding),
                    record,
                )
                for record in self._all_child_records()
            ),
            key=lambda item: item[0],
            reverse=True,
        )[:n_results]

        ids = [record.child_id for _, record in ranked]
        documents = [record.content for _, record in ranked] if "documents" in include else []
        metadatas = [record.metadata for _, record in ranked] if "metadatas" in include else []
        distances = [1.0 - score for score, _ in ranked] if "distances" in include else []
        return {
            "ids": [ids],
            "documents": [documents],
            "metadatas": [metadatas],
            "distances": [distances],
        }

    @_synchronized
    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM child_chunks").fetchone()[0])

    @_synchronized
    def dense_candidates(
        self,
        query_embedding: list[float],
        limit: int = 80,
        project_id: str | None = None,
        global_only: bool = False,
        document_names: list[str] | None = None,
        document_ids: list[str] | None = None,
    ) -> list[SecureCandidate]:
        rows = self.conn.execute(
            """
            SELECT child_id, embedding_nonce, embedding_ciphertext,
                   metadata_nonce, metadata_ciphertext
            FROM child_chunks
            ORDER BY child_id ASC
            """
        ).fetchall()
        ranked = []
        for row in rows:
            child_id = row[0]
            if project_id is not None or global_only or document_names or document_ids:
                metadata = self._decrypt_json(
                    self.keys.metadata_key,
                    row[3],
                    row[4],
                    CHILD_METADATA_AAD + child_id.encode("utf-8"),
                )
                metadata_project_id = str(metadata.get("project_id") or "").strip()
                if project_id is not None and metadata_project_id != str(project_id):
                    continue
                if global_only and metadata_project_id:
                    continue
                if document_names and not _document_name_matches(metadata, document_names):
                    continue
                if document_ids and not _document_id_matches(metadata, document_ids):
                    continue
            embedding = self._decrypt_json(
                self.keys.index_key,
                row[1],
                row[2],
                CHILD_EMBEDDING_AAD + child_id.encode("utf-8"),
            )
            ranked.append(SecureCandidate(chunk_id=child_id, score=self._cosine_similarity(query_embedding, embedding)))
        ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked[:limit]

    @_synchronized
    def load_candidates(self, chunk_ids: list[str]) -> list[SecureCandidate]:
        if not chunk_ids:
            return []
        placeholders = ",".join("?" for _ in chunk_ids)
        rows = self.conn.execute(
            f"""
            SELECT child_id, content_nonce, content_ciphertext, metadata_nonce, metadata_ciphertext
            FROM child_chunks
            WHERE child_id IN ({placeholders})
            """,
            tuple(chunk_ids),
        ).fetchall()
        by_id = {row[0]: row for row in rows}
        loaded = []
        for chunk_id in chunk_ids:
            row = by_id.get(chunk_id)
            if row is None:
                continue
            loaded.append(
                SecureCandidate(
                    chunk_id=chunk_id,
                    content=self._decrypt_text(
                        self.keys.index_key,
                        row[1],
                        row[2],
                        CHILD_CONTENT_AAD + chunk_id.encode("utf-8"),
                    ),
                    metadata=self._decrypt_json(
                        self.keys.metadata_key,
                        row[3],
                        row[4],
                        CHILD_METADATA_AAD + chunk_id.encode("utf-8"),
                    ),
                )
            )
        return loaded

    @_synchronized
    def keyword_candidates(
        self,
        query: str,
        limit: int = 80,
        project_id: str | None = None,
        global_only: bool = False,
        document_names: list[str] | None = None,
        document_ids: list[str] | None = None,
    ) -> list[SecureCandidate]:
        scores: dict[str, float] = {}
        total_documents = max(
            1,
            int(self.conn.execute("SELECT COUNT(*) FROM child_chunks").fetchone()[0]),
        )
        for token in dict.fromkeys(self._tokens(query)):
            token_id = self._token_id(token)
            row = self.conn.execute(
                """
                SELECT posting_nonce, posting_ciphertext
                FROM keyword_postings
                WHERE token_id = ?
                """,
                (token_id,),
            ).fetchone()
            if row is None:
                continue
            posting = self._decrypt_json(
                self.keys.metadata_key,
                row[0],
                row[1],
                KEYWORD_POSTING_AAD + token_id.encode("ascii"),
            )
            chunks = posting.get("chunks", {})
            document_frequency = len(chunks)
            inverse_frequency = math.log(
                1.0 + (total_documents - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            for chunk_id, count in chunks.items():
                scores[chunk_id] = scores.get(chunk_id, 0.0) + (
                    inverse_frequency * (1.0 + math.log(max(1.0, float(count))))
                )
        if project_id is not None or global_only or document_names or document_ids:
            metadata_by_id = self._metadata_for_child_ids(scores)
            if project_id is not None:
                scores = {
                    chunk_id: score
                    for chunk_id, score in scores.items()
                    if str((metadata_by_id.get(chunk_id) or {}).get("project_id") or "")
                    == str(project_id)
                }
            elif global_only:
                scores = {
                    chunk_id: score
                    for chunk_id, score in scores.items()
                    if not str((metadata_by_id.get(chunk_id) or {}).get("project_id") or "").strip()
                }
            if document_names:
                scores = {
                    chunk_id: score
                    for chunk_id, score in scores.items()
                    if _document_name_matches(metadata_by_id.get(chunk_id) or {}, document_names)
                }
            if document_ids:
                scores = {
                    chunk_id: score
                    for chunk_id, score in scores.items()
                    if _document_id_matches(metadata_by_id.get(chunk_id) or {}, document_ids)
                }
        ranked = [SecureCandidate(chunk_id=chunk_id, score=score) for chunk_id, score in scores.items()]
        ranked.sort(key=lambda item: (-item.score, item.chunk_id))
        return ranked[:limit]

    @_synchronized
    def get(self, where: dict[str, Any] | None = None, include: list[str] | None = None, ids: list[str] | None = None):
        include = include or []
        records = self._all_child_records()
        if ids is not None:
            id_set = set(ids)
            records = [record for record in records if record.child_id in id_set]
        if where:
            records = [record for record in records if self._matches_where(record.metadata, where)]

        payload = {"ids": [record.child_id for record in records]}
        if "documents" in include:
            payload["documents"] = [record.content for record in records]
        if "metadatas" in include:
            payload["metadatas"] = [record.metadata for record in records]
        return payload

    @_synchronized
    def delete(self, ids: list[str]):
        if not ids:
            return
        self._remove_keyword_postings(ids)
        placeholders = ",".join("?" for _ in ids)
        self.conn.execute(f"DELETE FROM child_chunks WHERE child_id IN ({placeholders})", tuple(ids))
        self.conn.commit()

    @_synchronized
    def delete_parent_ids(self, parent_ids: list[str]):
        if not parent_ids:
            return
        placeholders = ",".join("?" for _ in parent_ids)
        self.conn.execute(
            f"DELETE FROM parent_documents WHERE parent_id IN ({placeholders})",
            tuple(parent_ids),
        )
        self.conn.commit()

    @_synchronized
    def keyword_search(self, query: str, limit: int = 12) -> list[KeywordSearchResult]:
        records = self._all_child_records()
        if not records:
            return []
        scores = bm25_scores(query, [record.content for record in records])
        ranked = sorted(
            zip(records, scores),
            key=lambda item: item[1],
            reverse=True,
        )
        results = []
        for record, score in ranked:
            if score <= 0:
                continue
            results.append(
                KeywordSearchResult(
                    doc_id=record.child_id,
                    content=record.content,
                    metadata=record.metadata,
                    score=score,
                )
            )
            if len(results) >= limit:
                break
        return results

    @_synchronized
    def delete_by_document_hash(self, document_hash: str):
        rows = self.conn.execute(
            "SELECT child_id FROM child_chunks WHERE document_hash = ?",
            (document_hash,),
        ).fetchall()
        deleted_child_ids = [row[0] for row in rows]
        self._remove_keyword_postings(deleted_child_ids)
        self.conn.execute("DELETE FROM child_chunks WHERE document_hash = ?", (document_hash,))
        self.conn.execute("DELETE FROM parent_documents WHERE document_hash = ?", (document_hash,))
        self.conn.commit()

    @_synchronized
    def close(self):
        self.conn.close()

    def _all_child_records(self) -> list[_ChildRecord]:
        rows = self.conn.execute(
            """
            SELECT child_id, content_nonce, content_ciphertext, metadata_nonce, metadata_ciphertext, embedding_nonce, embedding_ciphertext
            FROM child_chunks
            ORDER BY child_id ASC
            """
        ).fetchall()
        records = []
        for row in rows:
            child_id = row[0]
            records.append(
                _ChildRecord(
                    child_id=child_id,
                    content=self._decrypt_text(
                        self.keys.index_key,
                        row[1],
                        row[2],
                        CHILD_CONTENT_AAD + child_id.encode("utf-8"),
                    ),
                    metadata=self._decrypt_json(
                        self.keys.metadata_key,
                        row[3],
                        row[4],
                        CHILD_METADATA_AAD + child_id.encode("utf-8"),
                    ),
                    embedding=self._decrypt_json(
                        self.keys.index_key,
                        row[5],
                        row[6],
                        CHILD_EMBEDDING_AAD + child_id.encode("utf-8"),
                    ),
                )
            )
        return records

    def _matches_where(self, metadata: dict[str, Any], where: dict[str, Any]) -> bool:
        for key, value in where.items():
            if metadata.get(key) != value:
                return False
        return True

    def _metadata_for_child_ids(self, chunk_ids) -> dict[str, dict[str, Any]]:
        ordered_ids = [str(chunk_id) for chunk_id in chunk_ids if str(chunk_id)]
        if not ordered_ids:
            return {}
        placeholders = ",".join("?" for _ in ordered_ids)
        rows = self.conn.execute(
            f"""
            SELECT child_id, metadata_nonce, metadata_ciphertext
            FROM child_chunks
            WHERE child_id IN ({placeholders})
            """,
            tuple(ordered_ids),
        ).fetchall()
        return {
            row[0]: self._decrypt_json(
                self.keys.metadata_key,
                row[1],
                row[2],
                CHILD_METADATA_AAD + row[0].encode("utf-8"),
            )
            for row in rows
        }

    def _upsert_keyword_postings(self, chunk_id: str, content: str) -> None:
        counts: dict[str, int] = {}
        for token in self._tokens(content):
            token_id = self._token_id(token)
            counts[token_id] = counts.get(token_id, 0) + 1
        for token_id, count in counts.items():
            existing = self._keyword_posting(token_id)
            chunks = dict(existing.get("chunks") or {})
            chunks[chunk_id] = count
            nonce, ciphertext = self._encrypt_bytes(
                self.keys.metadata_key,
                json.dumps({"chunks": chunks}, ensure_ascii=False).encode("utf-8"),
                KEYWORD_POSTING_AAD + token_id.encode("ascii"),
            )
            self.conn.execute(
                """
                INSERT OR REPLACE INTO keyword_postings (token_id, posting_nonce, posting_ciphertext)
                VALUES (?, ?, ?)
                """,
                (token_id, nonce, ciphertext),
            )

    def _keyword_posting(self, token_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT posting_nonce, posting_ciphertext
            FROM keyword_postings
            WHERE token_id = ?
            """,
            (token_id,),
        ).fetchone()
        if row is None:
            return {"chunks": {}}
        return self._decrypt_json(
            self.keys.metadata_key,
            row[0],
            row[1],
            KEYWORD_POSTING_AAD + token_id.encode("ascii"),
        )

    def _remove_keyword_postings(self, chunk_ids: list[str]) -> None:
        if not chunk_ids:
            return
        deleted = set(chunk_ids)
        rows = self.conn.execute(
            """
            SELECT token_id, posting_nonce, posting_ciphertext
            FROM keyword_postings
            ORDER BY token_id ASC
            """
        ).fetchall()
        for token_id, nonce, ciphertext in rows:
            posting = self._decrypt_json(
                self.keys.metadata_key,
                nonce,
                ciphertext,
                KEYWORD_POSTING_AAD + token_id.encode("ascii"),
            )
            chunks = dict(posting.get("chunks") or {})
            changed = False
            for chunk_id in list(chunks):
                if chunk_id in deleted:
                    del chunks[chunk_id]
                    changed = True
            if not changed:
                continue
            if chunks:
                new_nonce, new_ciphertext = self._encrypt_bytes(
                    self.keys.metadata_key,
                    json.dumps({"chunks": chunks}, ensure_ascii=False).encode("utf-8"),
                    KEYWORD_POSTING_AAD + token_id.encode("ascii"),
                )
                self.conn.execute(
                    """
                    UPDATE keyword_postings
                    SET posting_nonce = ?, posting_ciphertext = ?
                    WHERE token_id = ?
                    """,
                    (new_nonce, new_ciphertext, token_id),
                )
            else:
                self.conn.execute("DELETE FROM keyword_postings WHERE token_id = ?", (token_id,))

    def _tokens(self, text: str) -> list[str]:
        return lexical_tokens(text)[:MAX_KEYWORD_TOKENS]

    def _token_id(self, token: str) -> str:
        return hmac.new(self.keys.metadata_key, token.encode("utf-8"), hashlib.sha256).hexdigest()

    def _encrypt_bytes(self, key: bytes, payload: bytes, aad: bytes) -> tuple[str, str]:
        nonce = os.urandom(12)
        ciphertext = AESGCM(key).encrypt(nonce, payload, aad)
        return _b64(nonce), _b64(ciphertext)

    def _decrypt_text(self, key: bytes, nonce: str, ciphertext: str, aad: bytes) -> str:
        return self._decrypt_bytes(key, nonce, ciphertext, aad).decode("utf-8")

    def _decrypt_json(self, key: bytes, nonce: str, ciphertext: str, aad: bytes):
        return json.loads(self._decrypt_bytes(key, nonce, ciphertext, aad).decode("utf-8"))

    def _decrypt_bytes(self, key: bytes, nonce: str, ciphertext: str, aad: bytes) -> bytes:
        try:
            return AESGCM(key).decrypt(_unb64(nonce), _unb64(ciphertext), aad)
        except InvalidTag as exc:
            raise ValueError("secure vector store authentication failed") from exc

    def _cosine_similarity(self, left: list[float], right: list[float]) -> float:
        if not left or not right:
            return 0.0
        dot = sum(float(a) * float(b) for a, b in zip(left, right))
        left_norm = math.sqrt(sum(float(a) * float(a) for a in left))
        right_norm = math.sqrt(sum(float(b) * float(b) for b in right))
        if left_norm <= 0 or right_norm <= 0:
            return 0.0
        return dot / (left_norm * right_norm)


class _SecureCollectionAdapter:
    def __init__(self, store: SecureVectorStore):
        self.store = store

    def add(self, embeddings, documents, metadatas, ids):
        self.store.add_chunks(ids=ids, documents=documents, metadatas=metadatas, embeddings=embeddings)

    def upsert(self, embeddings, documents, metadatas, ids):
        self.store.add_chunks(ids=ids, documents=documents, metadatas=metadatas, embeddings=embeddings)

    def query(self, query_embeddings, n_results, include):
        return self.store.query(query_embeddings=query_embeddings, n_results=n_results, include=include)

    def count(self):
        return self.store.count()

    def get(self, where=None, include=None, ids=None):
        return self.store.get(where=where, include=include, ids=ids)

    def delete(self, ids):
        self.store.delete(ids)


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))
