import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.eval_image_ocr import evaluate_records


class _FakeParser:
    last_parse_stats = {}

    def parse_and_chunk(self, image_path: str):
        _ = image_path
        self.last_parse_stats = {
            "ocr_region_count": 1,
            "ocr_mean_confidence": 0.92,
        }
        return [
            SimpleNamespace(
                page_content="SoulDrive 本地隐私",
                metadata={
                    "ocr_regions_json": json.dumps(
                        [
                            {
                                "text": "SoulDrive 本地隐私",
                                "confidence": 0.92,
                                "bbox": {"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.1},
                            }
                        ]
                    )
                },
            )
        ]


class ImageOcrEvalTests(unittest.TestCase):
    def test_evaluation_reports_accuracy_localization_and_latency_without_paths(self):
        records = [
            {
                "id": "IMG-01",
                "image_path": "private.png",
                "expected_terms": ["SoulDrive", "本地隐私"],
                "expected_regions": [
                    {
                        "text": "本地隐私",
                        "bbox": {"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.1},
                    }
                ],
            }
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            report = evaluate_records(records, Path(temp_dir), parser=_FakeParser())

        self.assertEqual(report["parse_success_rate"], 1.0)
        self.assertEqual(report["term_recall"], 1.0)
        self.assertEqual(report["localization_accuracy_at_iou"], 1.0)
        self.assertEqual(report["under_5s_rate"], 1.0)
        self.assertEqual(report["ocr_mean_confidence"], 0.92)
        self.assertNotIn("image_path", report["samples"][0])


if __name__ == "__main__":
    unittest.main()
