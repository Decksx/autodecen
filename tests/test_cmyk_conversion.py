import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageCms

import api


SEGMENTATION_PATH = Path(__file__).resolve().parents[1] / 'smp-segmentation' / 'run_segmentation.py'
spec = importlib.util.spec_from_file_location('run_segmentation_for_cmyk_test', SEGMENTATION_PATH)
segmentation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(segmentation)


class CmykConversionTests(unittest.TestCase):
    def test_unprofiled_cmyk_page_converts_to_rgb_png(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / '040.jpg'
            Image.new('CMYK', (8, 8), (20, 30, 40, 50)).save(source, format='JPEG')
            original_bytes = source.read_bytes()

            converted_path, is_temporary = segmentation.convert_to_png(str(source))
            try:
                self.assertTrue(is_temporary)
                with Image.open(converted_path) as converted:
                    self.assertEqual(converted.format, 'PNG')
                    self.assertEqual(converted.mode, 'RGB')
                    self.assertEqual(converted.size, (8, 8))
            finally:
                Path(converted_path).unlink()

            self.assertEqual(source.read_bytes(), original_bytes)

    def test_rebuilt_rgb_jpeg_has_srgb_profile_instead_of_source_cmyk_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            source = io.BytesIO()
            Image.new('CMYK', (8, 8), (20, 30, 40, 50)).save(
                source, format='JPEG', icc_profile=b'original-cmyk-profile'
            )
            processed_path = Path(directory) / 'processed.png'
            Image.new('RGB', (8, 8), (100, 120, 140)).save(processed_path)

            rebuilt_bytes = api._encode_processed_page(
                str(processed_path), source.getvalue(), 'jpg'
            )
            with Image.open(io.BytesIO(rebuilt_bytes)) as rebuilt:
                self.assertEqual(rebuilt.mode, 'RGB')
                profile = ImageCms.ImageCmsProfile(
                    io.BytesIO(rebuilt.info['icc_profile'])
                )
                self.assertEqual(profile.profile.xcolor_space, 'RGB ')


if __name__ == '__main__':
    unittest.main()
