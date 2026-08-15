import tempfile
import unittest
from pathlib import Path

from core.keyword_index import KeywordIndex


class KeywordIndexTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
