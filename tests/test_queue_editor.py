from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from library_backlog import BacklogStore, DEFAULT_SEQUENCE, inspect_cbz_eligibility


class QueueEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'comix'
        self.database = Path(self.temp.name) / 'backlog.sqlite3'
        self.store = BacklogStore(str(self.database))
        self.scan = self.store.start_scan(str(self.root))

    def add(self, name, pages=1, root=None, metadata=None):
        root = root or self.root
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, 'w') as cbz:
            for i in range(pages):
                cbz.writestr(f'{i:03}.jpg', b'page remains unchanged')
            if metadata:
                cbz.writestr('ComicInfo.xml', metadata)
        self.store.record_archive(self.scan, str(root), str(path), path.stat(), inspect_cbz_eligibility)
        return self.store.book_progress(str(path))['id'], path

    def test_promotion_overrides_size_tiers_and_preserves_batch_order(self):
        short, _ = self.add('Short/Book.cbz')
        large, _ = self.add('Large/Book.cbz', pages=351)
        other, _ = self.add('Other/Book.cbz')
        self.store.edit_queue_books([large, other], 'next')
        self.store = BacklogStore(str(self.database))
        self.assertEqual([b['id'] for b in self.store.list_queue_books()['items']], [large, other, short])
        for expected in (large, other, short):
            self.assertEqual(self.store.claim_job()['id'], expected)

    def test_reset_priority_restores_normal_short_book_order(self):
        large, _ = self.add('Large/Book.cbz', pages=351)
        short, _ = self.add('Short/Book.cbz')
        self.store.edit_queue_books([large], 'next')
        self.store.edit_queue_books([large], 'normal')
        self.assertEqual(self.store.claim_job()['id'], short)

    def test_held_books_survive_restart_and_rescan_without_file_changes(self):
        book, path = self.add('Held/Book.cbz')
        original = path.read_bytes()
        self.store.edit_queue_books([book], 'hold')
        self.store = BacklogStore(str(self.database))
        self.assertIsNone(self.store.claim_job())
        self.assertEqual(self.store.snapshot()['counts']['held'], 1)
        with zipfile.ZipFile(path, 'a') as cbz:
            cbz.writestr('extra.jpg', b'an added page')
        self.store.record_archive(self.scan, str(self.root), str(path), path.stat(), inspect_cbz_eligibility)
        self.assertEqual(self.store.list_queue_books(state='held')['total'], 1)
        self.store.edit_queue_books([book], 'release')
        self.assertEqual(self.store.claim_job()['id'], book)
        self.assertTrue(path.read_bytes().startswith(original[:20]))

    def test_mixed_state_bulk_edit_is_atomic_and_keeps_archives(self):
        first, path1 = self.add('First/Book.cbz')
        second, path2 = self.add('Second/Book.cbz')
        payloads = [p.read_bytes() for p in (path1, path2)]
        self.store.claim_job()
        with self.assertRaisesRegex(ValueError, 'Processing books are locked'):
            self.store.edit_queue_books([second, first], 'hold')
        self.assertEqual(self.store.book_progress(str(path2))['state'], 'queued')
        self.assertEqual([p.read_bytes() for p in (path1, path2)], payloads)

    def test_promoted_other_root_does_not_override_schedule_scope(self):
        allowed, _ = self.add('Allowed/Book.cbz')
        elsewhere = Path(self.temp.name) / 'other'
        promoted, _ = self.add('Elsewhere/Book.cbz', root=elsewhere)
        self.store.edit_queue_books([promoted], 'next')
        self.assertEqual(self.store.claim_job(root_path=str(self.root))['id'], allowed)
        self.assertIsNone(self.store.claim_job(exclude_root=str(elsewhere)))

    def test_search_pagination_and_literal_wildcards(self):
        literal, _ = self.add('Series/100%_Book.cbz', metadata='<ComicInfo><Title>Special title</Title></ComicInfo>')
        self.add('Series/100XXBook.cbz')
        self.assertEqual(self.store.list_queue_books(query='%_')['items'][0]['id'], literal)
        self.assertEqual(self.store.list_queue_books(query='%_')['total'], 1)
        page = self.store.list_queue_books(query='SPECIAL', limit=1)
        self.assertEqual(page['total'], 1)
        self.assertEqual(page['items'][0]['metadata_title'], 'Special title')
        self.assertEqual(self.store.list_queue_books(limit=1, offset=1)['total'], 2)
        self.assertEqual(len(self.store.list_queue_books(limit=1, offset=1)['items']), 1)
        self.assertEqual(self.store.list_queue_books(query="' OR 1=1 --")['total'], 0)

    def test_schema_migration_preserves_pending_books(self):
        book, _ = self.add('Book.cbz')
        with self.store.connect() as db:
            db.execute('ALTER TABLE books DROP COLUMN queue_priority')
        self.store = BacklogStore(str(self.database))
        item = self.store.list_queue_books()['items'][0]
        self.assertEqual(item['id'], book)
        self.assertEqual(item['queue_priority'], 0)

    def test_method_edits_can_restore_automatic_without_forcing_applied_methods(self):
        book, path = self.add('Book.cbz')
        self.store.requeue_book(str(path), ['black_bars'])
        self.assertEqual(self.store.book_progress(str(path))['auto_detect'], 0)
        self.store.requeue_book(str(path), [], automatic=True)
        progress = self.store.book_progress(str(path))
        self.assertEqual(progress['auto_detect'], 1)
        self.assertEqual(progress['selected_sequence'], list(DEFAULT_SEQUENCE))
        with self.assertRaisesRegex(ValueError, 'Automatic processing'):
            self.store.requeue_book(str(path), [], True, automatic=True)
        self.store.claim_job()
        with self.assertRaisesRegex(ValueError, 'processing'):
            self.store.requeue_book(str(path), ['white_bars'])

    def test_edit_rechecks_processing_after_archive_inspection(self):
        _, path = self.add('Book.cbz')
        inspector = inspect_cbz_eligibility
        def claim_during_inspection(source):
            self.store.claim_job()
            return inspector(source)
        with patch('library_backlog.inspect_cbz_eligibility', side_effect=claim_during_inspection):
            with self.assertRaisesRegex(ValueError, 'processing'):
                self.store.requeue_book(str(path), ['black_bars'])
        self.assertEqual(self.store.book_progress(str(path))['state'], 'processing')

    def test_api_endpoints_list_edit_and_enforce_local_access(self):
        import api
        book, path = self.add('Book.cbz')
        client = api.app.test_client()
        client.environ_base['HTTP_ORIGIN'] = 'http://localhost:3000'
        with patch.object(api.library_backlog, 'store', self.store):
            response = client.get('/api/library-backlog/books?limit=1')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['items'][0]['id'], book)
            self.assertEqual(client.post('/api/library-backlog/books/edit', json={'book_ids': [book], 'action': 'hold'}).status_code, 200)
            self.assertEqual(client.get('/api/library-backlog/books?state=held').json['total'], 1)
            self.assertEqual(client.post('/api/library-backlog/queue/book', json={'source_path': str(path), 'automatic': True}).status_code, 200)
            self.assertEqual(client.get('/api/library-backlog/books?limit=0').status_code, 400)
            self.assertEqual(client.post('/api/library-backlog/books/edit', json={'book_ids': [book, book], 'action': 'next'}).status_code, 409)
            self.assertEqual(client.post('/api/library-backlog/books/edit', json={'book_ids': [book], 'action': {}}).status_code, 409)
            self.assertEqual(client.get('/api/library-backlog/books', environ_overrides={'REMOTE_ADDR': '192.0.2.10'}).status_code, 403)
            self.assertEqual(client.post('/api/library-backlog/books/edit', json={'book_ids': [book], 'action': 'hold'}, environ_overrides={'REMOTE_ADDR': '192.0.2.10'}).status_code, 403)


if __name__ == '__main__':
    unittest.main()
