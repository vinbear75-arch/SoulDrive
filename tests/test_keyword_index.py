from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from pathlib import Path

from core.keyword_index import KeywordIndex


def _capture_exception(function, *args):
    try:
        function(*args)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


class KeywordIndexTests(unittest.TestCase):
    def test_shared_connection_concurrent_writes_are_complete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            index = KeywordIndex(str(Path(temp_dir) / "keyword.sqlite"))
            try:
                def write_batch(batch_index):
                    start = batch_index * 20
                    for index_number in range(start, start + 20):
                        index.upsert_document(
                            doc_id=f"concurrent-doc-{index_number}",
                            content=f"concurrent retrieval evidence {index_number}",
                            metadata={"project_id": "concurrency"},
                        )

                with ThreadPoolExecutor(max_workers=4) as pool:
                    errors = [
                        result
                        for result in pool.map(
                            lambda batch: _capture_exception(write_batch, batch),
                            range(4),
                        )
                        if result is not None
                    ]
                self.assertEqual(errors, [])
                self.assertTrue(
                    index.contains_ids([f"concurrent-doc-{index_number}" for index_number in range(80)])
                )
            finally:
                index.close()

    def test_keyword_index_returns_matching_child_documents(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            index = KeywordIndex(str(Path(temp_dir) / "keyword.sqlite"))
            try:
                index.upsert_document(
                    doc_id="doc-1-child-0",
                    content="GraphRAG local search improves paper question answering.",
                    metadata={"source_filename": "graph.pdf"},
                )
                index.upsert_document(
                    doc_id="doc-2-child-0",
                    content="Vision transformer image classification baseline.",
                    metadata={"source_filename": "vision.pdf"},
                )

                results = index.search("GraphRAG paper search", limit=5)
            finally:
                index.close()

            self.assertEqual(results[0].doc_id, "doc-1-child-0")
            self.assertEqual(results[0].metadata["source_filename"], "graph.pdf")

    def test_keyword_index_uses_chinese_ngrams_and_returns_raw_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            index = KeywordIndex(str(Path(temp_dir) / "keyword.sqlite"))
            try:
                expected_content = "数据分类分级规则明确了重要数据识别要求。"
                index.upsert_document(
                    doc_id="policy-child",
                    content=expected_content,
                    metadata={"source_filename": "数据分类分级规则.pdf"},
                )
                index.upsert_document(
                    doc_id="deployment-child",
                    content="数据中心采用分级部署方式建设计算资源。",
                    metadata={"source_filename": "部署方案.pdf"},
                )

                results = index.search("重要数据分类分级规则", limit=2)
            finally:
                index.close()

            self.assertEqual(results[0].doc_id, "policy-child")
            self.assertEqual(results[0].content, expected_content)

    def test_keyword_index_uses_stable_doc_id_tiebreaker_for_equal_scores(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            index = KeywordIndex(str(Path(temp_dir) / "keyword.sqlite"))
            try:
                for doc_id in ("doc-b", "doc-a"):
                    index.upsert_document(
                        doc_id=doc_id,
                        content="same retrieval signal",
                        metadata={"source_filename": f"{doc_id}.pdf"},
                    )
                results = index.search("retrieval signal", limit=2)
            finally:
                index.close()

        self.assertEqual([item.doc_id for item in results], ["doc-a", "doc-b"])

    def test_keyword_index_reports_missing_projection_ids(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            index = KeywordIndex(str(Path(temp_dir) / "keyword.sqlite"))
            try:
                index.upsert_document(
                    doc_id="present-child",
                    content="retrieval projection",
                    metadata={},
                )
                self.assertTrue(index.contains_ids(["present-child"]))
                self.assertFalse(index.contains_ids(["present-child", "missing-child"]))
            finally:
                index.close()


if __name__ == "__main__":
    unittest.main()
