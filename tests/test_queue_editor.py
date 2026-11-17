from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from library_backlog import BacklogStore, DEFAULT_SEQUENCE, inspect_cbz_eligibility, retired_replica_path


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

    def retired(self, root=r'Z:\comics\Comix', *, state='held', pages=1, priority=0, source=None):
        # Database-only historical row: never create or open a replica archive.
        with self.store.connect() as db:
            sequence = db.execute('SELECT COUNT(*) FROM books').fetchone()[0]
            source = source or root + f'\\Historical{sequence}\\Book.cbz'
            cursor = db.execute("""INSERT INTO books(source_path,root_path,file_size,mtime_ns,state,
                selected_sequence,discovered_at,updated_at,queued_at,archive_page_count,series_path,
                queue_priority,attempts,detected_methods)
                VALUES(?,?,1,1,?,'["black_bars"]','earlier','earlier','0000',?,?,?,2,'["black_bars"]')""",
                (source, root, state, pages, os.path.dirname(source), priority))
            book = cursor.lastrowid
            db.execute("INSERT INTO book_stages(book_id,stage,state,updated_at) VALUES(?,'black_bars','completed','earlier')", (book,))
            self.store._event(db, 'fixture_history', 'retained', book_id=book)
        return book, source

    def test_retired_replica_spellings_and_boundaries_are_recognized_without_io(self):
        aliases = (r'Z:\comics', r'z:\COMICS\Comix', 'Z:/comics/Comix/Series',
                   r'Z:\comics\Comix\..\Manga', r'\\TOWER\media\comics\Comix',
                   r'\\tower\MEDIA\COMICS', r'\\?\Z:\comics\Comix',
                   r'\\?\UNC\TOWER\media\comics\Comix')
        for path in aliases:
            with self.subTest(retired=path):
                self.assertTrue(retired_replica_path(path))
        for path in (r'X:\Comix', r'Z:\comics-old\Comix', r'Z:\comics\..\other',
                     r'\\Tower\media\comics-old', r'C:\Temp\Comix'):
            with self.subTest(active=path):
                self.assertFalse(retired_replica_path(path))

    def test_release_rejects_a_mixed_batch_atomically_without_losing_history(self):
        active, path = self.add('Active/Book.cbz')
        self.store.edit_queue_books([active], 'hold')
        retired, _ = self.retired()
        original = path.read_bytes()
        for ids in ([active, retired], [retired, active]):
            with self.subTest(order=ids):
                with self.assertRaisesRegex(ValueError, 'Retired Tower replica'):
                    self.store.edit_queue_books(ids, 'release')
                with self.store.connect() as db:
                    states = {r['id']: r['state'] for r in db.execute('SELECT id,state FROM books')}
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE event_type='queue_release'").fetchone()[0], 0)
                    self.assertEqual(db.execute("SELECT state FROM book_stages WHERE book_id=?", (retired,)).fetchone()[0], 'completed')
                self.assertEqual(states, {active: 'held', retired: 'held'})
                self.assertEqual(path.read_bytes(), original)

    def test_manual_requeue_refuses_replica_before_archive_io(self):
        for root in (r'Z:\comics\Comix', r'\\TOWER\media\comics\Comix', r'\\?\UNC\TOWER\media\comics\Comix'):
            with self.subTest(root=root):
                book, source = self.retired(root)
                with patch('library_backlog.os.path.isfile', side_effect=AssertionError('replica file probe')), \
                        patch('library_backlog.inspect_cbz_eligibility', side_effect=AssertionError('replica ZIP opened')):
                    with self.assertRaisesRegex(ValueError, 'Retired Tower replica'):
                        self.store.requeue_book(source, ['black_bars'])
                with self.store.connect() as db:
                    self.assertEqual(db.execute('SELECT state FROM books WHERE id=?', (book,)).fetchone()[0], 'held')

    def test_manual_requeue_rechecks_replica_policy_after_inspection(self):
        book, path = self.add('Active/Book.cbz')
        self.store.edit_queue_books([book], 'hold')
        before = path.read_bytes()
        original = inspect_cbz_eligibility

        def inspector(source):
            value = original(source)
            with self.store.connect() as db:
                db.execute("UPDATE books SET root_path=? WHERE id=?", (r'Z:\comics\Comix', book))
            return value

        with patch('library_backlog.inspect_cbz_eligibility', side_effect=inspector):
            with self.assertRaisesRegex(ValueError, 'Retired Tower replica'):
                self.store.requeue_book(str(path), ['black_bars'])
        with self.store.connect() as db:
            row = db.execute('SELECT state,attempts FROM books WHERE id=?', (book,)).fetchone()
        self.assertEqual(tuple(row), ('held', 0))
        self.assertEqual(path.read_bytes(), before)

    def test_each_queue_tier_holds_replica_jobs_without_attempts_and_keeps_active_work(self):
        variants = [(tier, root, None) for tier in ('promoted', 'ordinary', 'unknown', 'deferred')
                    for root in (r'Z:\comics\Comix', r'\\TOWER\media\comics\Comix', r'\\?\UNC\TOWER\media\comics\Comix')]
        variants += [('promoted', str(self.root), r'Z:\comics\Comix\Mislabelled\Book.cbz'),
                     ('promoted', r'Z:\comics\Comix', str(self.root / 'Mislabelled' / 'Book.cbz'))]
        for serial, (tier, root, source) in enumerate(variants):
            with self.subTest(tier=tier, root=root, source=source):
                active, path = self.add(f'Active{serial}/Book.cbz', pages=351 if tier == 'deferred' else 1)
                if tier == 'unknown':
                    with self.store.connect() as db:
                        db.execute('UPDATE books SET archive_page_count=NULL WHERE id=?', (active,))
                pages = None if tier == 'unknown' else (351 if tier == 'deferred' else 1)
                retired, _ = self.retired(root, state='queued', pages=pages,
                                          priority=100 if tier == 'promoted' else 0, source=source)
                def classify(source):
                    self.assertEqual(source, str(path), 'replica ZIP opened')
                    return 1

                with patch('library_backlog.inspect_cbz_page_count', side_effect=classify):
                    claimed = self.store.claim_job()
                    self.assertIsNotNone(claimed, 'retired work starved the active job')
                    self.assertEqual(claimed['id'], active)
                    self.assertIsNone(self.store.claim_job())
                with self.store.connect() as db:
                    row = db.execute('SELECT state,attempts,detected_methods FROM books WHERE id=?', (retired,)).fetchone()
                    self.assertEqual(tuple(row), ('held', 2, '["black_bars"]'))
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE book_id=? AND event_type='retired_root_hold'", (retired,)).fetchone()[0], 1)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE book_id=? AND event_type='job_start'", (retired,)).fetchone()[0], 0)
                    self.assertEqual(db.execute('SELECT state FROM book_stages WHERE book_id=?', (retired,)).fetchone()[0], 'completed')
                self.assertTrue(path.is_file())

    def test_resume_preflight_does_not_probe_retired_roots(self):
        self.add('Active/Book.cbz')
        self.retired(state='queued')
        self.retired(r'\\TOWER\media\comics\Comix', state='failed')
        self.assertEqual(self.store.pending_roots(include_failed=True), [str(self.root)])

    def test_api_release_and_manual_requeue_refuse_old_replica_history(self):
        import api
        book, source = self.retired()
        client = api.app.test_client()
        client.environ_base['HTTP_ORIGIN'] = 'http://localhost:3000'
        with patch.object(api.library_backlog, 'store', self.store), \
                patch('library_backlog.os.path.isfile', side_effect=AssertionError('replica file probe')):
            released = client.post('/api/library-backlog/books/edit', json={'book_ids': [book], 'action': 'release'})
            requeued = client.post('/api/library-backlog/queue/book', json={'source_path': source, 'automatic': True})
        self.assertEqual(released.status_code, 409)
        self.assertEqual(requeued.status_code, 400)
        self.assertIn('Retired Tower replica', released.json['error'])
        self.assertIn('Retired Tower replica', requeued.json['error'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM books WHERE id=?', (book,)).fetchone()[0], 'held')

    def test_restart_recovered_replica_job_is_held_before_archive_classification(self):
        retired, _ = self.retired(state='processing', pages=None)
        reopened = BacklogStore(str(self.database))
        with patch('library_backlog.inspect_cbz_page_count', side_effect=AssertionError('replica ZIP opened')):
            self.assertIsNone(reopened.claim_job())
        with reopened.connect() as db:
            row = db.execute('SELECT state,attempts FROM books WHERE id=?', (retired,)).fetchone()
        self.assertEqual(tuple(row), ('held', 2))

    def test_book_removed_during_inspection_is_refused_without_recreating_it(self):
        book, path = self.add('Active/Book.cbz')
        original = inspect_cbz_eligibility

        def inspector(source):
            value = original(source)
            # Models a concurrent supported prune's FK detach and delete,
            # confined to this temporary DB. The real archive stays present.
            with self.store.connect() as db:
                db.execute('UPDATE events SET book_id=NULL WHERE book_id=?', (book,))
                db.execute('DELETE FROM books WHERE id=?', (book,))
            return value

        with patch('library_backlog.inspect_cbz_eligibility', side_effect=inspector):
            try:
                self.store.requeue_book(str(path), ['black_bars'])
            except Exception as error:
                self.assertIsInstance(error, ValueError)
                self.assertIn('no longer in the backlog', str(error))
            else:
                self.fail('removed record was requeued')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM books').fetchone()[0], 0)
        self.assertTrue(path.is_file())

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
