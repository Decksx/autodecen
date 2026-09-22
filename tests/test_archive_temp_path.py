import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image

import api


class ArchiveTempPathTests(unittest.TestCase):
    def test_long_output_filename_does_not_lengthen_temporary_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'Book.cbz'
            original_page = io.BytesIO()
            Image.new('RGB', (8, 8), (255, 0, 0)).save(original_page, format='PNG')
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('001.png', original_page.getvalue())

            _, context = api.extract_cbz_images(str(source), str(root / 'stage'), set())
            processed_page = root / 'processed.png'
            Image.new('RGB', (8, 8), (0, 255, 0)).save(processed_page)
            long_name = 'A' * 220 + '.cbz'
            relative_path = str(Path('D' * 220) / long_name)
            output_root = root / 'Comix'
            output = output_root / relative_path
            self.assertGreater(len(str(output)), 260)
            self.assertGreater(len(str(output.parent)), 260)
            self.assertGreater(len(long_name + '.tmp-' + '0' * 32), 255)

            result = api.create_processed_cbz(
                [{'filename': '001.png', 'processed_path': str(processed_page)}],
                {
                    **context,
                    'original_name': long_name,
                    'relative_path': relative_path,
                    'source_path': str(source),
                    'output_mode': 'copy',
                    'output_root': str(output_root),
                    'completed_methods': ['black_bars'],
                },
                'long-filename-test',
            )

            self.assertEqual(Path(result['path']), output)
            self.assertTrue(source.is_file())
            with zipfile.ZipFile(api.filesystem_path(output)) as archive:
                self.assertIsNone(archive.testzip())
                with archive.open('001.png') as page, Image.open(page) as image:
                    self.assertGreater(image.getpixel((0, 0))[1], image.getpixel((0, 0))[0])
            self.assertEqual(
                list(Path(api.filesystem_path(output.parent)).glob('.camelia-*.tmp')),
                [],
            )


if __name__ == '__main__':
    unittest.main()
