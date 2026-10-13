import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image

from core.image_ocr import LocalImageOcrParser


class ImageOcrTests(unittest.TestCase):
    def test_ocr_confidence_rejects_non_finite_values(self):
        parser = LocalImageOcrParser(engine_factory=lambda: None)

        self.assertEqual(parser._confidence(float("nan")), 0.0)
        self.assertEqual(parser._confidence(float("inf")), 0.0)

    def test_parser_returns_ordered_text_with_normalized_regions_and_metrics(self):
        output = SimpleNamespace(
            txts=("第二行", "第一行"),
            scores=np.array((0.8, 0.9)),
            boxes=np.array(
                (
                    ((20, 50), (120, 50), (120, 70), (20, 70)),
                    ((10, 10), (110, 10), (110, 30), (10, 30)),
                )
            ),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "screen.png"
            Image.new("RGB", (200, 100), "white").save(image_path)
            parser = LocalImageOcrParser(engine_factory=lambda: lambda payload: output)

            chunks = parser.parse_and_chunk(str(image_path))

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].page_content, "第一行\n第二行")
        self.assertEqual(chunks[0].metadata["modality"], "image")
        self.assertEqual(chunks[0].metadata["vision_status"], "ocr_complete")
        regions = json.loads(chunks[0].metadata["ocr_regions_json"])
        self.assertEqual(regions[0]["text"], "第一行")
        self.assertEqual(regions[0]["bbox"], {"x": 0.05, "y": 0.1, "width": 0.5, "height": 0.2})
        self.assertEqual(parser.last_parse_stats["ocr_region_count"], 2)
        self.assertEqual(parser.last_parse_stats["ocr_mean_confidence"], 0.85)
        self.assertGreaterEqual(parser.last_parse_stats["ocr_ms"], 0)

    def test_parser_rejects_invalid_image_without_calling_ocr(self):
        calls = []
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "spoofed.png"
            image_path.write_bytes(b"not an image")
            parser = LocalImageOcrParser(engine_factory=lambda: calls.append(True))

            chunks = parser.parse_and_chunk(str(image_path))

        self.assertEqual(chunks, [])
        self.assertEqual(calls, [])
        self.assertEqual(parser.last_parse_stats["conversion_status"], "failed")
        self.assertIsNotNone(parser.last_error)


if __name__ == "__main__":
    unittest.main()
