import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import library_backlog


class MetadataInstallRetryTests(unittest.TestCase):
    def make_archive(self, path):
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('001.jpg', b'opaque unchanged page')
            archive.writestr('ComicInfo.xml', b'<ComicInfo><Title>Test Book</Title></ComicInfo>')

    def test_transient_lock_retries_atomic_install_without_changing_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'Book.cbz'
            self.make_archive(path)
            replace = library_backlog.os.replace
            calls = []
            def locked_once(source, target):
                calls.append((source, target))
                if len(calls) == 1:
                    raise PermissionError('temporary sharing violation')
                return replace(source, target)
            with mock.patch.object(library_backlog.os, 'replace', side_effect=locked_once), \
                    mock.patch.object(library_backlog.time, 'sleep'):
                library_backlog.add_uncensored_tag(str(path), ['black_bars'])
            self.assertEqual(len(calls), 2)
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(archive.read('001.jpg'), b'opaque unchanged page')
                self.assertIsNone(archive.testzip())

    def test_permanent_permission_error_preserves_original_and_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'Book.cbz'
            self.make_archive(path)
            original = path.read_bytes()
            with mock.patch.object(library_backlog.os, 'replace', side_effect=PermissionError('denied')) as replace, \
                    mock.patch.object(library_backlog.time, 'sleep'):
                with self.assertRaises(PermissionError):
                    library_backlog.add_uncensored_tag(str(path), ['black_bars'])
            self.assertEqual(replace.call_count, 5)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
