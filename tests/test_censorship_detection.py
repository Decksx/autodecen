import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from censorship_detection import classify_detection, inspect_archive, reliable_region


class CensorshipDetectionTests(unittest.TestCase):
    def test_dedicated_detector_assigns_only_one_type_per_page_by_priority(self):
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
            {"black_bars"},
        )
        detection["scores"][0] = 0.5
        self.assertEqual(classify_detection(pixels, detection), {"transparent_black"})
        detection["scores"][2] = 0.5
        self.assertEqual(classify_detection(pixels, detection), {"white_bars"})
        detection["scores"][1] = 0.5
        self.assertEqual(classify_detection(pixels, detection), {"mosaic"})

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
            self.assertTrue(result["mosaic_skipped_due_to_bar"])
            self.assertEqual(source.read_bytes(), original)

    def test_bar_detection_suppresses_mosaic_for_the_whole_book(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "Book.cbz"
            image = io.BytesIO()
            Image.new("RGB", (4, 4), "white").save(image, format="PNG")
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("001.png", image.getvalue())
                archive.writestr("002.png", image.getvalue())

            findings = iter(({"mosaic"}, {"black_bars"}))
            result = inspect_archive(str(source), lambda _payload: next(findings))

            self.assertEqual(result["detected_methods"], ["black_bars"])
            self.assertEqual(result["pages_by_method"]["black_bars"], ["002.png"])
            self.assertEqual(result["pages_by_method"]["mosaic"], [])
            self.assertTrue(result["mosaic_skipped_due_to_bar"])

    def test_archive_audit_normalizes_multiple_page_findings_to_one_method(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "Book.cbz"
            image = io.BytesIO()
            Image.new("RGB", (4, 4), "white").save(image, format="PNG")
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("001.png", image.getvalue())

            result = inspect_archive(
                str(source),
                lambda _payload: {"white_bars", "transparent_black", "mosaic"},
            )

            self.assertEqual(result["detected_methods"], ["transparent_black"])
            self.assertEqual(result["pages_by_method"]["transparent_black"], ["001.png"])
            self.assertEqual(
                sum(len(names) for names in result["pages_by_method"].values()),
                1,
            )

    def test_corrupt_page_fails_instead_of_counting_as_no_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "Book.cbz"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("001.png", b"bad image")

            def fail(_payload):
                raise ValueError("cannot decode")

            with self.assertRaisesRegex(RuntimeError, "001.png"):
                inspect_archive(str(source), fail)

    def test_transient_missing_file_is_retried_without_skipping_the_page(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "Book.cbz"
            image = io.BytesIO()
            Image.new("RGB", (4, 4), "white").save(image, format="PNG")
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("014.jpg", image.getvalue())
            original = source.read_bytes()
            attempts = [0]

            def transient_failure(_payload):
                attempts[0] += 1
                if attempts[0] == 1:
                    raise FileNotFoundError(2, "No such file or directory")
                return {"black_bars"}

            with mock.patch("censorship_detection.time.sleep"):
                result = inspect_archive(str(source), transient_failure)

            self.assertEqual(attempts[0], 2)
            self.assertEqual(result["pages_by_method"]["black_bars"], ["014.jpg"])
            self.assertEqual(source.read_bytes(), original)

    def test_persistent_missing_file_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "Book.cbz"
            image = io.BytesIO()
            Image.new("RGB", (4, 4), "white").save(image, format="PNG")
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("014.jpg", image.getvalue())
            attempts = [0]

            def persistent_failure(_payload):
                attempts[0] += 1
                raise FileNotFoundError(2, "No such file or directory")

            with mock.patch("censorship_detection.time.sleep"), self.assertRaisesRegex(
                RuntimeError, r"014\.jpg: FileNotFoundError"
            ):
                inspect_archive(str(source), persistent_failure)
            self.assertEqual(attempts[0], 3)


if __name__ == "__main__":
    unittest.main()
