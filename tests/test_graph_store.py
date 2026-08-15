import tempfile
import unittest
from pathlib import Path

from core.graph_db import LocalGraphDB


class GraphStoreTests(unittest.TestCase):
    def test_sqlite_graph_store_requires_explicit_path(self):
        with self.assertRaisesRegex(ValueError, "db_path"):
            LocalGraphDB()

    def test_sqlite_graph_store_returns_context_for_matched_entity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = LocalGraphDB(db_path=str(Path(temp_dir) / "graph.sqlite"))
            try:
                db.add_entity("GraphRAG", "技术", "结合图谱和检索增强生成")
                db.add_entity("Local Search", "查询模式", "围绕实体邻域检索")
                db.add_relationship("GraphRAG", "Local Search", "支持")

                context = db.search_context("如何使用 GraphRAG 做论文问答？")
            finally:
                db.close()

        self.assertEqual(context[0], "已知逻辑关系: [GraphRAG] --(支持)--> [Local Search]")

    def test_sqlite_graph_store_recalls_entity_and_relationship_chunks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = LocalGraphDB(db_path=str(Path(temp_dir) / "graph.sqlite"))
            try:
                db.add_entity("零信任平台", "系统", "企业安全平台", chunk_ids=["chunk-1"])
                db.add_entity("身份认证", "能力", "统一认证能力", chunk_ids=["chunk-2"])
                db.add_relationship(
                    "零信任平台",
                    "身份认证",
                    "包含",
                    evidence_chunk_ids=["chunk-3"],
                )

                chunk_ids = db.recall_chunk_ids("零信任平台如何实现身份认证？")
            finally:
                db.close()

        self.assertEqual(chunk_ids, ["chunk-1", "chunk-2", "chunk-3"])

    def test_sqlite_graph_store_merges_and_removes_chunk_provenance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = LocalGraphDB(db_path=str(Path(temp_dir) / "graph.sqlite"))
            try:
                db.add_entity("数据中台", "系统", "旧描述", chunk_ids=["old-chunk"])
                db.add_entity("数据中台", "系统", "新描述", chunk_ids=["new-chunk"])
                db.add_entity("数据治理", "能力", "治理能力", chunk_ids=["new-chunk"])
                db.add_relationship(
                    "数据中台",
                    "数据治理",
                    "支持",
                    evidence_chunk_ids=["old-chunk", "new-chunk"],
                )

                db.remove_chunk_ids(["old-chunk"])
                remaining = db.recall_chunk_ids("数据中台")
                db.remove_chunk_ids(["new-chunk"])
                removed = db.recall_chunk_ids("数据中台")
            finally:
                db.close()

        self.assertEqual(remaining, ["new-chunk"])
        self.assertEqual(removed, [])


if __name__ == "__main__":
    unittest.main()
