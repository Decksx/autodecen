"""Bound SQLite work for pruning against a large unrelated event history."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backlog_prune import prune_missing_books
from library_backlog import BacklogStore


class PruneEventScalingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.root = self.home / 'library'
        self.root.mkdir()
        self.database = str(self.home / 'backlog.sqlite3')
        self.store = BacklogStore(self.database)
        with self.store.connect() as db:
            for number in range(33):
                path = self.root / f'{number}.cbz'
                if number == 32:
                    path.write_bytes(b'kept file')
                db.execute('''INSERT INTO books
                    (source_path,root_path,file_size,mtime_ns,state,selected_sequence,discovered_at,updated_at,series_path)
                    VALUES(?,?,0,0,'queued','[]','fixture','fixture',?)''',
                    (str(path), str(self.root), str(self.root)))
            self.ids = [row[0] for row in db.execute('SELECT id FROM books ORDER BY id')]
            db.executemany('''INSERT INTO events(book_id,event_type,message,created_at)
                VALUES(?,'old_history','preserve history','fixture')''',
                [(self.ids[-1],)] * 12000 + [(book_id,) for book_id in self.ids[:-1]])

    @contextmanager
    def measure(self):
        calls = [0]
        native = self.store.connect
        def count():
            calls[0] += 1
            return 0
        @contextmanager
        def connect():
            with native() as db:
                db.set_progress_handler(count, 1000)
                try:
                    yield db
                finally:
                    db.set_progress_handler(None, 0)
        with patch.object(self.store, 'connect', connect):
            yield calls

    def test_prune_does_not_rescan_unrelated_history_for_each_missing_book(self):
        preview = prune_missing_books(self.store)
        self.assertEqual(preview['missing_count'], 32)
        with self.measure() as calls:
            result = prune_missing_books(self.store, dry_run=False,
                expected_count=32, snapshot_digest=preview['snapshot_digest'])
        # VM instructions, not host timing: indexed work is below 20k here;
        # repeated scans exceed 2 million. Keep generous headroom for SQLite.
        self.assertLess(calls[0] * 1000, 200000)
        self.assertEqual(result['removed_count'], 32)
        with self.store.connect() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT id FROM books')], [self.ids[-1]])
            self.assertEqual(db.execute('SELECT count(*) FROM events WHERE book_id=?',
                (self.ids[-1],)).fetchone()[0], 12000)
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE book_id IS NULL AND event_type='old_history'").fetchone()[0], 32)
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE event_type='book_pruned_missing'").fetchone()[0], 32)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_reopening_old_schema_bounds_lookup_without_changing_history(self):
        with self.store.connect() as db:
            # Emulate the pre-index database without depending on the index name.
            for index in db.execute('PRAGMA index_list(events)').fetchall():
                columns = [r[2] for r in db.execute(f'PRAGMA index_info("{index[1]}")')]
                if columns == ['book_id']:
                    db.execute(f'DROP INDEX "{index[1]}"')
            before = [tuple(row) for row in db.execute('SELECT * FROM events ORDER BY id')]
        self.store = BacklogStore(self.database)
        self.store = BacklogStore(self.database)  # repeat initialization is harmless
        with self.measure() as calls:
            with self.store.connect() as db:
                db.execute('UPDATE events SET book_id=NULL WHERE book_id=-1')
        self.assertLess(calls[0] * 1000, 1000)
        with self.store.connect() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM events ORDER BY id')], before)
            self.assertEqual(db.execute('SELECT count(*) FROM books').fetchone()[0], 33)


if __name__ == '__main__':
    unittest.main()
