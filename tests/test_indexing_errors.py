import unittest

from core.indexing_errors import classify_indexing_error, indexing_failure


class IndexingErrorsTests(unittest.TestCase):
    def test_classify_common_indexing_errors(self):
        self.assertEqual(classify_indexing_error("no parseable chunks")["error_code"], "NO_PARSEABLE_CHUNKS")
        self.assertEqual(
            classify_indexing_error(
                "Docling partial_success conversion; failed pages: [40, 41]"
            )["error_code"],
            "PARTIAL_DOCUMENT_PARSE",
        )
        self.assertEqual(classify_indexing_error("PDF password required")["error_code"], "ENCRYPTED_DOCUMENT")
        self.assertEqual(classify_indexing_error("Access is denied")["error_code"], "FILE_ACCESS_DENIED")
        self.assertEqual(classify_indexing_error("out of memory")["error_code"], "RESOURCE_EXHAUSTED")
        self.assertEqual(classify_indexing_error("std::bad_alloc")["error_code"], "RESOURCE_EXHAUSTED")
        self.assertEqual(classify_indexing_error("insufficient disk space")["error_code"], "INSUFFICIENT_DISK_SPACE")

    def test_indexing_failure_includes_stable_error_code(self):
        failure = indexing_failure("paper.pdf", "no parseable chunks")

        self.assertEqual(failure["source_filename"], "paper.pdf")
        self.assertEqual(failure["error_code"], "NO_PARSEABLE_CHUNKS")
        self.assertEqual(failure["category"], "parser")


if __name__ == "__main__":
    unittest.main()
