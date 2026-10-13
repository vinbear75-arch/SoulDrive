import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from core.graph_db import LocalGraphDB


class GraphStoreTests(unittest.TestCase):
    def test_shared_graph_connection_serializes_concurrent_index_writes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = LocalGraphDB(db_path=str(Path(temp_dir) / "graph.sqlite"))
            start = threading.Barrier(4)
            errors = []

            def worker(worker_id):
                try:
                    start.wait(timeout=5)
                    for index in range(12):
                        db.add_entity(
                            f"entity-{worker_id}-{index}",
                            "test",
                            "concurrent graph entity",
                            chunk_ids=[f"chunk-{worker_id}-{index}"],
                        )
                except BaseException as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(worker_id,)) for worker_id in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            entities = db.get_all_entities()
            db.close()

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(entities), 48)

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
                db.save_graphrag_index({"community_count": 1})

                db.remove_chunk_ids(["old-chunk"])
                remaining = db.recall_chunk_ids("数据中台")
                invalidated = db.load_graphrag_index()
                db.remove_chunk_ids(["new-chunk"])
                removed = db.recall_chunk_ids("数据中台")
            finally:
                db.close()

        self.assertEqual(remaining, ["new-chunk"])
        self.assertIsNone(invalidated)
        self.assertEqual(removed, [])

    def test_graph_snapshot_includes_document_project_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = LocalGraphDB(db_path=str(Path(temp_dir) / "graph.sqlite"))
            try:
                db.add_document(
                    source_filename="research.pdf",
                    title="Research",
                    summary="RAG evaluation notes",
                    keywords=["RAG"],
                    chunk_ids=["chunk-1"],
                    project_id="project-a",
                )
                snapshot = db.get_graph_snapshot()
            finally:
                db.close()

        self.assertEqual(snapshot["documents"][0]["project_id"], "project-a")

    def test_graph_recall_applies_project_filter_before_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = LocalGraphDB(db_path=str(Path(temp_dir) / "graph.sqlite"))
            try:
                db.add_document(
                    "other.pdf",
                    "Other",
                    "other",
                    chunk_ids=["other-1", "other-2"],
                    project_id="project-other",
                )
                db.add_document(
                    "current.pdf",
                    "Current",
                    "current",
                    chunk_ids=["current-1"],
                    project_id="project-current",
                )
                db.add_entity(
                    "目标实体",
                    "概念",
                    "目标",
                    chunk_ids=["other-1", "other-2", "current-1"],
                )
                project_chunks = db.recall_chunk_ids(
                    "目标实体",
                    limit=1,
                    project_id="project-current",
                )
            finally:
                db.close()

        self.assertEqual(project_chunks, ["current-1"])

    def test_graph_snapshot_keeps_entity_attributes_by_chunk(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = LocalGraphDB(db_path=str(Path(temp_dir) / "graph.sqlite"))
            try:
                db.add_entity("同名实体", "项目 A 类型", "项目 A 描述", chunk_ids=["a-1"])
                db.add_entity("同名实体", "项目 B 类型", "项目 B 描述", chunk_ids=["b-1"])
                entity = db.get_graph_snapshot()["entities"][0]
            finally:
                db.close()

        self.assertEqual(
            entity["source_attributes"],
            [
                {"chunk_id": "a-1", "type": "项目 A 类型", "description": "项目 A 描述"},
                {"chunk_id": "b-1", "type": "项目 B 类型", "description": "项目 B 描述"},
            ],
        )

    def test_graph_chunk_pruning_removes_entity_attribute_provenance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = LocalGraphDB(db_path=str(Path(temp_dir) / "graph.sqlite"))
            try:
                db.add_entity("同名实体", "项目 A 类型", "项目 A 描述", chunk_ids=["a-1"])
                db.add_entity("同名实体", "项目 B 类型", "项目 B 描述", chunk_ids=["b-1"])
                db.remove_chunk_ids(["a-1"])
                entity = db.get_graph_snapshot()["entities"][0]
            finally:
                db.close()

        self.assertEqual(entity["source_attributes"], [
            {"chunk_id": "b-1", "type": "项目 B 类型", "description": "项目 B 描述"},
        ])

    def test_remove_project_derives_chunk_ids_when_caller_omits_them(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = LocalGraphDB(db_path=str(Path(temp_dir) / "graph.sqlite"))
            try:
                db.add_document("project.pdf", "项目", "项目", chunk_ids=["project-1"], project_id="project-a")
                db.add_document("global.pdf", "全局", "全局", chunk_ids=["global-1"], project_id="")
                db.add_entity("共享实体", "概念", "共享", chunk_ids=["project-1", "global-1"])
                db.add_entity("关联实体", "概念", "关联", chunk_ids=["project-1", "global-1"])
                db.add_relationship(
                    "共享实体",
                    "关联实体",
                    "关联",
                    evidence_chunk_ids=["project-1", "global-1"],
                )

                db.remove_project("project-a")
                snapshot = db.get_graph_snapshot()
            finally:
                db.close()

        self.assertEqual([item["project_id"] for item in snapshot["documents"]], [""])
        self.assertTrue(all(item["chunk_ids"] == ["global-1"] for item in snapshot["entities"]))
        self.assertEqual(snapshot["relationships"][0]["chunk_ids"], ["global-1"])

    def test_graph_documents_keep_same_source_filename_per_project(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = LocalGraphDB(db_path=str(Path(temp_dir) / "graph.sqlite"))
            try:
                db.add_document(
                    "same.pdf",
                    "项目 A 报告",
                    "A",
                    chunk_ids=["a-1"],
                    project_id="project-a",
                )
                db.add_document(
                    "same.pdf",
                    "项目 B 报告",
                    "B",
                    chunk_ids=["b-1"],
                    project_id="project-b",
                )
                documents = db.get_graph_snapshot()["documents"]
            finally:
                db.close()

        self.assertEqual(
            {(item["project_id"], item["title"], tuple(item["chunk_ids"])) for item in documents},
            {
                ("project-a", "项目 A 报告", ("a-1",)),
                ("project-b", "项目 B 报告", ("b-1",)),
            },
        )
        self.assertEqual(len({item["document_id"] for item in documents}), 2)
        self.assertNotEqual(documents[0]["document_id"], documents[1]["document_id"])

    def test_legacy_graph_document_schema_migrates_to_project_scoped_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "graph.sqlite"
            connection = sqlite3.connect(db_path)
            connection.execute(
                """
                CREATE TABLE graph_documents (
                    source_filename TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    keywords_json TEXT NOT NULL,
                    chunk_ids_json TEXT NOT NULL,
                    project_id TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                """
                INSERT INTO graph_documents
                    (source_filename, title, summary, keywords_json, chunk_ids_json, project_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("same.pdf", "旧项目报告", "legacy", "[]", '["legacy-1"]', "project-a"),
            )
            connection.commit()
            connection.close()

            db = LocalGraphDB(db_path=str(db_path))
            try:
                db.add_document(
                    "same.pdf",
                    "新项目报告",
                    "new",
                    chunk_ids=["new-1"],
                    project_id="project-b",
                )
                documents = db.get_graph_snapshot()["documents"]
            finally:
                db.close()

        self.assertEqual(
            {(item["project_id"], item["title"], tuple(item["chunk_ids"])) for item in documents},
            {
                ("project-a", "旧项目报告", ("legacy-1",)),
                ("project-b", "新项目报告", ("new-1",)),
            },
        )


if __name__ == "__main__":
    unittest.main()
