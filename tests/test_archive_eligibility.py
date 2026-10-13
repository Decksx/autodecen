import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

from library_backlog import BacklogStore, LibraryBacklog, inspect_cbz_eligibility, pending_decensor_methods


class UnsupportedArchiveTests(unittest.TestCase):
    def test_animation_only_book_is_skipped_without_a_processing_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'Animation.cbz'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('001.gif', b'opaque animation payload')
                archive.writestr('ComicInfo.xml', b'<ComicInfo/>')
            before = path.read_bytes()
            inspection = inspect_cbz_eligibility(str(path))
            self.assertFalse(inspection['eligible'])
            self.assertEqual(inspection['page_count'], 0)
            self.assertIsNone(inspection['parse_error'])
            self.assertIn('no supported image pages', inspection['reason'])
            self.assertEqual(pending_decensor_methods(inspection, ['black_bars']), [])
            self.assertEqual(path.read_bytes(), before)

    def test_manual_reprocess_cannot_queue_an_unsupported_archive(self):
        inspection = {'page_count': 0, 'already_processed': True, 'eligible': False}
        self.assertEqual(pending_decensor_methods(inspection, ['mosaic'], reprocess=True), [])

    def test_supported_page_still_queues_normally(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'Book.cbz'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('001.jpg', b'not decoded during metadata inspection')
            inspection = inspect_cbz_eligibility(str(path))
            self.assertTrue(inspection['eligible'])
            self.assertEqual(pending_decensor_methods(inspection, ['black_bars']), ['black_bars'])

    def test_previously_queued_animation_is_skipped_before_detector(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'comix'
            root.mkdir()
            path = root / 'Animation.cbz'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('001.gif', b'opaque animation payload')
            store = BacklogStore(str(Path(directory) / 'backlog.sqlite3'))
            scan = store.start_scan(str(root))
            store.record_archive(scan, str(root), str(path), path.stat(), inspect_cbz_eligibility)
            with store.connect() as db:
                db.execute("UPDATE books SET state='queued', selected_sequence='[\"black_bars\"]'")
            calls = []
            backlog = LibraryBacklog(
                store, lambda *args: calls.append('processor'),
                detector=lambda *args: calls.append('detector'),
            )
            store.control_queue('running')
            worker = threading.Thread(target=backlog._process_queue, daemon=True)
            worker.start()
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    with store.connect() as db:
                        state = db.execute('SELECT state FROM books').fetchone()['state']
                    if state == 'skipped':
                        break
                    time.sleep(0.01)
                self.assertEqual(state, 'skipped')
                self.assertEqual(calls, [])
                self.assertTrue(path.exists())
            finally:
                backlog.pause_queue()
                worker.join(2)


if __name__ == '__main__':
    unittest.main()
