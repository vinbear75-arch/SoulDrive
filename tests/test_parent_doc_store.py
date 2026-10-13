from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from pathlib import Path

from core.parent_doc_store import ParentDocumentStore


def _capture_exception(function, *args):
    try:
        function(*args)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


class ParentDocumentStoreTests(unittest.TestCase):
    def test_shared_connection_concurrent_writes_are_complete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ParentDocumentStore(str(Path(temp_dir) / "parent.sqlite"))
            try:
                def write_batch(batch_index):
                    start = batch_index * 20
                    for index_number in range(start, start + 20):
                        store.upsert_parent(
                            parent_id=f"concurrent-parent-{index_number}",
                            content=f"concurrent parent evidence {index_number}",
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
                self.assertEqual(
                    [
                        store.get_parent(f"concurrent-parent-{index_number}")["content"]
                        for index_number in range(80)
                    ],
                    [f"concurrent parent evidence {index_number}" for index_number in range(80)],
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
