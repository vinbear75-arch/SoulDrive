import unittest

from scripts.build_current_product_baseline_datasets import (
    build_ingestion_records,
    build_rag_records,
)


class CurrentProductBaselineDatasetTests(unittest.TestCase):
    def test_builders_keep_one_traceable_record_per_asset(self):
        manifest = {
            "assets": [{
                "asset_id": "asset-1",
                "filename": "note.txt",
                "title": "实验笔记",
                "asset_type": "experiment_note",
                "project_id": "project-a",
                "text": "记录内容",
            }]
        }

        ingestion = build_ingestion_records(manifest)
        rag = build_rag_records(manifest)

        self.assertEqual(ingestion[0]["source_filename"], "note.txt")
        self.assertEqual(ingestion[0]["project_id"], "project-a")
        self.assertEqual(rag[0]["expected_sources"], ["note.txt"])
        self.assertEqual(rag[0]["project_id"], "project-a")


if __name__ == "__main__":
    unittest.main()
