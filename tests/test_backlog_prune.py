"""Real temporary archives/SQLite, unavailable roots and atomic prune races."""
import errno
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import zipfile

import backlog_prune as prune
from library_backlog import BacklogStore, LibraryBacklog, inspect_cbz_eligibility


class BacklogPruneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / 'library'
        self.root.mkdir()
        self.store = BacklogStore(str(self.home / 'queue.sqlite3'))
        self.scan = self.store.start_scan(str(self.root))
        self.store.set_scan_status(self.scan, 'paused', 'fixture paused')
        self.backlog = LibraryBacklog(self.store, lambda *args: None)

    def book(self, name, state='queued', missing=True, root=None):
        root = root or self.root
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('001.png', b'archive bytes must remain untouched')
        self.store.record_archive(self.scan, str(root), str(path), path.stat(), inspect_cbz_eligibility)
        row = self.store.book_progress(str(path))
        with self.store.connect() as db:
            db.execute('UPDATE books SET state=? WHERE id=?', (state, row['id']))
        if missing:
            path.unlink()
        return row['id'], path

    def apply(self, report):
        return self.backlog.prune_missing_books(dry_run=False,
            expected_count=report['missing_count'], snapshot_digest=report['snapshot_digest'])

    def rows(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM books ORDER BY id')]

    def test_preview_and_apply_cover_every_nonactive_category_without_changing_files_or_scan_history(self):
        states = ['queued', 'held', 'completed', 'failed', 'quarantined', 'skipped', 'eligible', 'discovered']
        for state in states:
            self.book(state + '.cbz', state)
        kept, path = self.book('kept.cbz', missing=False)
        payload, before = path.read_bytes(), self.rows()
        report = self.backlog.prune_missing_books()
        self.assertEqual(report['missing_count'], 8)
        self.assertEqual(report['by_state'], dict.fromkeys(states, 1))
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.apply(report)['removed_count'], 8)
        self.assertEqual([r['id'] for r in self.rows()], [kept])
        self.assertEqual(path.read_bytes(), payload)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM book_stages').fetchone()[0], 4)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events WHERE event_type=?', ('book_pruned_missing',)).fetchone()[0], 8)
            self.assertEqual(db.execute('SELECT total_found FROM scans').fetchone()[0], 9)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_unavailable_root_is_kept_while_other_library_missing_rows_are_pruned(self):
        gone, _ = self.book('gone.cbz')
        offline = self.home / 'offline'
        protected, _ = self.book('keep.cbz', root=offline)
        offline.rmdir()
        report = self.backlog.prune_missing_books()
        self.assertEqual(report['missing_count'], 1)
        self.assertEqual(report['unavailable_roots'][0]['root'], str(offline))
        self.apply(report)
        self.assertEqual([r['id'] for r in self.rows()], [protected])

    def test_permission_and_network_errors_are_not_absence(self):
        _, path = self.book('unknown.cbz')
        native_stat = os.stat
        for error in (PermissionError(errno.EACCES, 'unreadable'), PermissionError(''), FileNotFoundError(errno.ENOENT, 'network gone')):
            if isinstance(error, FileNotFoundError):
                error.winerror = 53
            def stat(target, *args, **kwargs):
                if os.fspath(target) == str(path):
                    raise error
                return native_stat(target, *args, **kwargs)
            with self.subTest(error=type(error).__name__), patch.object(prune.os, 'stat', side_effect=stat):
                report = self.backlog.prune_missing_books()
            self.assertEqual(report['missing_count'], 0)
            self.assertEqual(report['unverified_count'], 1)

    def test_root_permission_denial_is_reported_as_unavailable(self):
        self.book('unknown.cbz')
        for message in ('root not readable', ''):
            with self.subTest(message=message), patch.object(prune.os, 'scandir', side_effect=PermissionError(message)):
                report = self.backlog.prune_missing_books()
            self.assertEqual(report['missing_count'], 0)
            self.assertEqual(len(report['unavailable_roots']), 1)

    def test_restored_file_refuses_the_whole_preview(self):
        self.book('other.cbz')
        _, path = self.book('restored.cbz')
        report = self.backlog.prune_missing_books()
        path.write_bytes(b'restored')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.apply(report)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(path.read_bytes(), b'restored')

    def test_changed_queue_requires_another_preview(self):
        book, _ = self.book('gone.cbz')
        report = self.backlog.prune_missing_books()
        self.store.edit_queue_books([book], 'hold')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.apply(report)
        self.assertEqual(len(self.rows()), 1)

    def test_active_queue_scan_or_processing_book_is_refused(self):
        book, _ = self.book('gone.cbz')
        self.store.control_queue('running')
        with self.assertRaisesRegex(ValueError, 'Pause the queue'):
            self.backlog.prune_missing_books()
        self.store.control_queue('paused')
        self.store.set_scan_status(self.scan, 'running', 'fixture resumed')
        with self.assertRaisesRegex(ValueError, 'crawl'):
            self.backlog.prune_missing_books()
        self.store.set_scan_status(self.scan, 'paused', 'fixture paused')
        with self.store.connect() as db:
            db.execute("UPDATE books SET state='processing' WHERE id=?", (book,))
        with self.assertRaisesRegex(ValueError, 'current book'):
            self.backlog.prune_missing_books()

    def test_worker_must_settle_even_after_pause(self):
        self.backlog._queue_thread = Mock(is_alive=lambda: True)
        with self.assertRaisesRegex(ValueError, 'active work'):
            self.backlog.prune_missing_books()
        self.backlog._queue_thread = None
        self.backlog._scan_threads[1] = Mock(is_alive=lambda: True)
        with self.assertRaisesRegex(ValueError, 'active work'):
            self.backlog.prune_missing_books()

    def test_preview_count_and_digest_are_required_and_cannot_be_forged_by_count(self):
        self.book('gone.cbz')
        report = self.backlog.prune_missing_books()
        for options in ({}, {'expected_count': True, 'snapshot_digest': report['snapshot_digest']},
                        {'expected_count': 1, 'snapshot_digest': 'x'},
                        {'expected_count': 1, 'snapshot_digest': '0' * 64},
                        {'expected_count': 0, 'snapshot_digest': report['snapshot_digest']}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.backlog.prune_missing_books(dry_run=False, **options)
        self.assertEqual(len(self.rows()), 1)

    def test_queue_change_after_filesystem_walk_is_rechecked_under_write_lock(self):
        book, _ = self.book('gone.cbz')
        report = self.backlog.prune_missing_books()
        original = prune._preview
        def race(rows):
            result = original(rows)
            self.store.edit_queue_books([book], 'hold')
            return result
        with patch.object(prune, '_preview', side_effect=race), self.assertRaisesRegex(ValueError, 'queue changed'):
            self.apply(report)
        self.assertEqual(len(self.rows()), 1)

    def test_queue_resumed_after_walk_is_refused_under_the_write_lock(self):
        self.book('gone.cbz')
        report = self.backlog.prune_missing_books()
        original = prune._preview
        def resume(rows):
            result = original(rows)
            self.store.control_queue('running')
            return result
        with patch.object(prune, '_preview', side_effect=resume), self.assertRaisesRegex(ValueError, 'Pause the queue'):
            self.apply(report)
        self.assertEqual(len(self.rows()), 1)

    def test_file_restored_after_walk_prevents_any_deletion(self):
        self.book('first.cbz')
        _, path = self.book('restored.cbz')
        report = self.backlog.prune_missing_books()
        original = prune._preview
        def restore(rows):
            result = original(rows)
            path.write_bytes(b'restored after walk')
            return result
        with patch.object(prune, '_preview', side_effect=restore), self.assertRaisesRegex(ValueError, 'file changed'):
            self.apply(report)
        self.assertEqual(len(self.rows()), 2)

    def test_transient_root_outage_before_final_stats_is_not_forgotten(self):
        self.book('gone.cbz')
        report = self.backlog.prune_missing_books()
        original_missing, original_root = prune._missing, prune._root_error
        phase = {'walk_done': False, 'final_stat': False}
        original_preview = prune._preview
        def preview(rows):
            result = original_preview(rows)
            phase['walk_done'] = True
            return result
        def root_error(root):
            if phase['walk_done'] and not phase['final_stat']:
                return 'transient root outage'
            return original_root(root)
        def missing(path):
            if phase['walk_done']:
                phase['final_stat'] = True
            return original_missing(path)
        with patch.object(prune, '_preview', side_effect=preview), patch.object(prune, '_root_error', side_effect=root_error), patch.object(prune, '_missing', side_effect=missing):
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                self.apply(report)
        self.assertEqual(len(self.rows()), 1)

    def test_root_disappearing_during_final_stat_aborts_without_partial_delete(self):
        self.book('gone.cbz')
        report = self.backlog.prune_missing_books()
        original = prune._missing
        calls = []
        def disconnect(path):
            calls.append(path)
            if len(calls) == 2:  # apply walk, then transaction's last stat
                self.root.rename(self.home / 'disconnected')
            return original(path)
        with patch.object(prune, '_missing', side_effect=disconnect), self.assertRaisesRegex(ValueError, 'unavailable'):
            self.apply(report)
        self.assertEqual(len(self.rows()), 1)

    def test_mid_transaction_failure_rolls_back_rows_and_event_detachment(self):
        self.book('first.cbz')
        self.book('second.cbz')
        report = self.backlog.prune_missing_books()
        original, calls = self.store._event, []
        def fail(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                raise RuntimeError('injected audit write failure')
            return original(*args, **kwargs)
        with patch.object(self.store, '_event', side_effect=fail), self.assertRaises(RuntimeError):
            self.apply(report)
        self.assertEqual(len(self.rows()), 2)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE event_type='book_pruned_missing'").fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events WHERE book_id IS NOT NULL').fetchone()[0], 4)

    def test_database_write_lock_fences_claims_during_final_file_checks(self):
        self.book('gone.cbz')
        report = self.backlog.prune_missing_books()
        other = BacklogStore(self.store.database_path)
        entered, finished = threading.Event(), threading.Event()
        results, calls = [], []
        def claim():
            entered.set()
            results.append(other.claim_job())
            finished.set()
        thread = threading.Thread(target=claim)
        original = prune._missing
        def check(path):
            calls.append(path)
            if len(calls) == 2:
                thread.start()
                self.assertTrue(entered.wait(2))
                self.assertFalse(finished.wait(0.5), 'another writer claimed a prune candidate')
            return original(path)
        try:
            with patch.object(prune, '_missing', side_effect=check):
                self.apply(report)
        finally:
            if thread.ident is not None:
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [None])

    def test_local_api_preview_confirmation_and_remote_refusal(self):
        import api
        self.book('gone.cbz')
        client = api.app.test_client()
        client.environ_base['HTTP_ORIGIN'] = 'http://localhost:3000'
        endpoint = '/api/library-backlog/books/prune-missing'
        with patch.object(api, 'library_backlog', self.backlog):
            self.assertEqual(client.post(endpoint, json={'dry_run': 'yes'}).status_code, 400)
            self.assertEqual(client.post(endpoint, json={}, environ_overrides={'REMOTE_ADDR': '192.0.2.10'}).status_code, 403)
            report = client.post(endpoint, json={'dry_run': True}).json
            self.assertEqual(report['missing_count'], 1)
            self.assertEqual(client.post(endpoint, json={'dry_run': False}).status_code, 409)
            result = client.post(endpoint, json={'dry_run': False, 'expected_count': 1,
                                                'snapshot_digest': report['snapshot_digest']})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json['removed_count'], 1)
