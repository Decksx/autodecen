"""GUI 'Reset statistics' (David, hive #6698, option A): counters restart, no book record changes."""
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import library_backlog
from library_backlog import BacklogStore

OLD = "2020-01-01T00:00:00+00:00"


class StatisticsResetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = BacklogStore(str(Path(self.temp.name) / "state" / "backlog.sqlite3"))
        with self.store.connect() as db:
            for i, state in enumerate(["queued", "queued", "held", "completed", "completed", "failed",
                                       "quarantined", "skipped", "skipped", "skipped"]):
                db.execute("""INSERT INTO books(source_path,root_path,file_size,mtime_ns,state,selected_sequence,
                              discovered_at,updated_at,source_key) VALUES(?,?,?,?,?,?,?,?,?)""",
                           (f"X:\\comix\\B{i}\\b{i}.cbz", "X:\\comix", 1, 1, state, "[]", OLD, OLD, f"x:\\comix\\b{i}\\b{i}.cbz"))
            db.execute("INSERT INTO scans(root_path,status,total_found,scanned_count,eligible_count,created_at,updated_at) "
                       "VALUES('X:\\comix','completed',500,500,120,?,?)", (OLD, OLD))

    def books(self):
        with self.store.connect() as db:
            return [tuple(r) for r in db.execute("SELECT id, state, updated_at, source_path FROM books ORDER BY id")]

    def test_before_any_reset_everything_counts(self):
        snap = self.store.snapshot()
        self.assertIsNone(snap["stats_reset_at"])
        self.assertEqual((snap["counts"]["completed"], snap["counts"]["skipped"], snap["counts"]["queued"]), (2, 3, 2))
        self.assertEqual(snap["scan_totals"], {"found": 500, "scanned": 500, "eligible": 120})

    def test_reset_zeroes_outcomes_and_old_scan_but_keeps_the_real_queue(self):
        before = self.books()
        reset_at = self.store.reset_statistics()
        snap = self.store.snapshot()
        self.assertEqual(snap["stats_reset_at"], reset_at)
        for state in ("completed", "failed", "quarantined", "skipped"):
            self.assertEqual(snap["counts"][state], 0, state)
        self.assertEqual((snap["counts"]["queued"], snap["counts"]["held"]), (2, 1))
        self.assertEqual(snap["scan_totals"], {"found": 0, "scanned": 0, "eligible": 0})
        self.assertEqual(self.books(), before)  # no book record was touched

    def test_changes_after_a_reset_are_counted(self):
        self.store.reset_statistics()
        time.sleep(1.1)  # utc_now has one-second resolution
        later = library_backlog.utc_now()
        with self.store.connect() as db:
            db.execute("UPDATE books SET state='completed', updated_at=? WHERE id=1", (later,))
            db.execute("INSERT INTO scans(root_path,status,total_found,scanned_count,eligible_count,created_at,updated_at) "
                       "VALUES('X:\\comix','running',7,3,1,?,?)", (later, later))
        snap = self.store.snapshot()
        self.assertEqual((snap["counts"]["completed"], snap["counts"]["queued"]), (1, 1))
        self.assertEqual(snap["scan_totals"], {"found": 7, "scanned": 3, "eligible": 1})

    def test_a_second_reset_moves_the_starting_point(self):
        first = self.store.reset_statistics()
        time.sleep(1.1)
        with self.store.connect() as db:
            db.execute("UPDATE books SET state='skipped', updated_at=? WHERE id=1", (library_backlog.utc_now(),))
        self.assertEqual(self.store.snapshot()["counts"]["skipped"], 1)
        time.sleep(1.1)
        second = self.store.reset_statistics()
        self.assertGreater(second, first)
        self.assertEqual(self.store.snapshot()["counts"]["skipped"], 0)

    def test_reset_records_an_event(self):
        self.store.reset_statistics()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE event_type='statistics_reset'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
