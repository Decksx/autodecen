"""GUI 'Reset statistics' (David, hive #6698, option A): counters restart, no book record changes.

After a reset the outcome tiles count transitions into each outcome state,
logged by triggers with an auto-increment id. So an unchanged rescan (which
refreshes updated_at), same-second activity, and old books leaving a state all
behave correctly (codex-lead reviews of f573ea0 and c77ac4f).
"""
import sys
import tempfile
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
        self.add_scan(500, 500, 120)

    def add_scan(self, found, scanned, eligible, created=OLD):
        with self.store.connect() as db:
            db.execute("INSERT INTO scans(root_path,status,total_found,scanned_count,eligible_count,created_at,updated_at) "
                       "VALUES('X:\\comix','completed',?,?,?,?,?)", (found, scanned, eligible, created, created))

    def set_state(self, book_id, state, updated_at=None):
        with self.store.connect() as db:
            db.execute("UPDATE books SET state=?, updated_at=? WHERE id=?",
                       (state, updated_at or library_backlog.utc_now(), book_id))

    def books(self):
        with self.store.connect() as db:
            return [tuple(r) for r in db.execute("SELECT id, state, updated_at, source_path FROM books ORDER BY id")]

    def counts(self):
        return self.store.snapshot()["counts"]

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

    def test_an_unchanged_rescan_does_not_bring_old_outcomes_back(self):
        """A crawl's _reuse_unchanged refreshes updated_at without a state change."""
        self.store.reset_statistics()
        with self.store.connect() as db:
            db.execute("UPDATE books SET updated_at=?", (library_backlog.utc_now(),))
        for state in ("completed", "failed", "quarantined", "skipped"):
            self.assertEqual(self.counts()[state], 0, state)

    def test_a_real_unchanged_crawl_through_record_archive_does_not_recount(self):
        """Codex's probe: the public crawl path returns 'unchanged' and refreshes updated_at."""
        from test_library_backlog import make_cbz
        from library_backlog import inspect_cbz_eligibility
        root = Path(self.temp.name) / "comix"
        archive = root / "Real" / "Real.cbz"
        make_cbz(archive, "<ComicInfo><Tags>camelia:black_bars</Tags></ComicInfo>")
        scan = self.store.start_scan(str(root))
        self.assertEqual(self.store.record_archive(scan, str(root), str(archive), archive.stat(), inspect_cbz_eligibility), "queued")
        with self.store.connect() as db:
            db.execute("UPDATE books SET state='completed' WHERE source_path LIKE '%Real.cbz'")
        self.store.reset_statistics()
        self.assertEqual(self.counts()["completed"], 0)
        again = self.store.start_scan(str(root))
        self.assertEqual(self.store.record_archive(again, str(root), str(archive), archive.stat(), inspect_cbz_eligibility), "unchanged")
        self.assertEqual(self.counts()["completed"], 0)

    def test_same_second_activity_is_ordered_by_the_baseline_not_the_clock(self):
        """Pre-reset rows stamped with the reset's own second don't count; a change after does."""
        reset_at = self.store.reset_statistics()
        with self.store.connect() as db:
            db.execute("UPDATE books SET updated_at=? WHERE state IN ('completed','skipped')", (reset_at,))
        self.assertEqual(self.counts()["completed"], 0)
        self.set_state(1, "completed", updated_at=reset_at)  # same second, genuinely after the reset
        self.assertEqual((self.counts()["completed"], self.counts()["queued"]), (1, 1))

    def test_a_scan_started_in_the_reset_second_still_counts(self):
        reset_at = self.store.reset_statistics()
        self.add_scan(7, 3, 1, created=reset_at)
        self.assertEqual(self.store.snapshot()["scan_totals"], {"found": 7, "scanned": 3, "eligible": 1})

    def test_a_new_outcome_counts_even_after_an_old_book_left_that_state(self):
        """Codex review of c77ac4f: net inventory would show 0 here; a real new skip happened."""
        self.store.reset_statistics()
        self.set_state(8, "queued")    # an old skipped book is requeued
        self.set_state(1, "skipped")   # a queued book is newly skipped
        self.assertEqual(self.counts()["skipped"], 1)

    def test_a_retried_failure_counts_again(self):
        """An old failed book retried and failing again is a new failure after the reset."""
        self.store.reset_statistics()
        self.set_state(6, "queued")
        self.set_state(6, "failed")
        self.assertEqual((self.counts()["failed"], self.counts()["queued"]), (1, 2))

    def test_rewriting_a_state_to_the_same_value_logs_nothing(self):
        """Re-registration writes state=? even when unchanged; that's not a new outcome."""
        self.store.reset_statistics()
        with self.store.connect() as db:
            db.execute("UPDATE books SET state=state, updated_at=?", (library_backlog.utc_now(),))
        for state in ("completed", "failed", "quarantined", "skipped"):
            self.assertEqual(self.counts()[state], 0, state)

    def test_crawl_inserting_a_skipped_row_counts(self):
        self.store.reset_statistics()
        with self.store.connect() as db:
            db.execute("""INSERT INTO books(source_path,root_path,file_size,mtime_ns,state,selected_sequence,
                          discovered_at,updated_at,source_key) VALUES('X:\\comix\\N\\n.cbz','X:\\comix',1,1,'skipped','[]',?,?,
                          'x:\\comix\\n\\n.cbz')""", (OLD, OLD))
        self.assertEqual(self.counts()["skipped"], 1)

    def test_reset_prunes_the_log_up_to_its_boundary(self):
        self.store.reset_statistics()
        self.set_state(1, "completed")
        self.store.reset_statistics()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outcome_log").fetchone()[0], 0)
        self.set_state(2, "completed")
        self.assertEqual(self.counts()["completed"], 1)

    def test_leaving_an_outcome_state_never_goes_negative(self):
        self.store.reset_statistics()
        self.set_state(8, "queued")  # a skipped book requeued
        self.assertEqual((self.counts()["skipped"], self.counts()["queued"]), (0, 3))

    def test_a_second_reset_moves_the_baseline(self):
        self.store.reset_statistics()
        self.set_state(1, "skipped")
        self.assertEqual(self.counts()["skipped"], 1)
        self.store.reset_statistics()
        self.assertEqual(self.counts()["skipped"], 0)
        self.set_state(2, "skipped")
        self.assertEqual(self.counts()["skipped"], 1)

    def test_reset_records_an_event(self):
        self.store.reset_statistics()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE event_type='statistics_reset'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
