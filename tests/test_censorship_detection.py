import io
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image

from censorship_detection import classify_detection, inspect_archive, reliable_region


class CensorshipDetectionTests(unittest.TestCase):
    def test_dedicated_detector_separates_bar_colors_and_mosaic(self):
        pixels = np.full((100, 100, 3), 128, dtype=np.uint8)
        masks = np.zeros((100, 100, 4), dtype=bool)
        for index, top in enumerate((0, 25, 50, 75)):
            masks[top:top + 20, 10:30, index] = True
        pixels[masks[:, :, 0]] = 0
        pixels[masks[:, :, 1]] = 255
        detection = {"masks": masks, "class_ids": [1, 1, 1, 2],
                     "scores": [0.99, 0.99, 0.99, 0.99]}
        self.assertEqual(
            classify_detection(pixels, detection),
            {"black_bars", "white_bars", "transparent_black", "mosaic"},
        )
        detection["scores"][2] = 0.5
        self.assertNotIn("transparent_black", classify_detection(pixels, detection))

    def test_contiguous_region_threshold_rejects_sparse_noise(self):
        mask = np.zeros((100, 100), dtype=np.uint8)
        mask[::10, ::10] = 1
        self.assertFalse(reliable_region(mask, minimum_pixels=20))
        mask[30:40, 30:40] = 1
        self.assertTrue(reliable_region(mask, minimum_pixels=20))

    def test_archive_audit_records_page_hits_per_method_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "Book.cbz"
            image = io.BytesIO()
            Image.new("RGB", (4, 4), "white").save(image, format="PNG")
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("001.png", image.getvalue())
                archive.writestr("002.png", image.getvalue())
                archive.writestr("ComicInfo.xml", "<ComicInfo />")
            original = source.read_bytes()

            calls = [0]

            def first_page_only(_payload):
                calls[0] += 1
                return {"black_bars"} if calls[0] == 1 else set()

            result = inspect_archive(str(source), first_page_only)
            self.assertEqual(result["page_count"], 2)
            self.assertEqual(result["detected_methods"], ["black_bars"])
            self.assertEqual(result["pages_by_method"]["black_bars"], ["001.png"])
            self.assertEqual(result["pages_by_method"]["mosaic"], [])
            self.assertEqual(source.read_bytes(), original)

    def test_corrupt_page_fails_instead_of_counting_as_no_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "Book.cbz"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("001.png", b"bad image")

            def fail(_payload):
                raise ValueError("cannot decode")

            with self.assertRaisesRegex(RuntimeError, "001.png"):
                inspect_archive(str(source), fail)


if __name__ == "__main__":
    unittest.main()
