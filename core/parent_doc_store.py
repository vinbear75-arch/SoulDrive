import json
import os
import sqlite3
from typing import Any


class ParentDocumentStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self):
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS parent_documents (
                parent_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            )
        """)
        self.conn.commit()

    def upsert_parent(self, parent_id: str, content: str, metadata: dict[str, Any]):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO parent_documents (parent_id, content, metadata_json)
            VALUES (?, ?, ?)
        """, (parent_id, content, json.dumps(metadata, ensure_ascii=False)))
        self.conn.commit()

    def get_parent(self, parent_id: str):
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT content, metadata_json
            FROM parent_documents
            WHERE parent_id = ?
        """, (parent_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "content": row[0],
            "metadata": json.loads(row[1]),
        }

    def delete_by_document_hash(self, document_hash: str):
        cursor = self.conn.cursor()
        cursor.execute("""
            DELETE FROM parent_documents
            WHERE json_extract(metadata_json, '$.document_hash') = ?
        """, (document_hash,))
        self.conn.commit()

    def delete(self, parent_ids: list[str]):
        if not parent_ids:
            return
        placeholders = ",".join("?" for _ in parent_ids)
        self.conn.execute(
            f"DELETE FROM parent_documents WHERE parent_id IN ({placeholders})",
            tuple(parent_ids),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
