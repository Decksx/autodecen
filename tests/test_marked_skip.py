"""The one-time marked-book move (#6257): exact candidates, guards, atomic apply, no recovery side effect."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import marked_skip
from library_backlog import MARKED_SKIP_REASON, BacklogStore, inspect_cbz_eligibility
from test_library_backlog import make_cbz


class MarkedSkipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "comix"
        self.db_path = str(Path(self.temp.name) / "state" / "backlog.sqlite3")
        self.store = BacklogStore(self.db_path)
        self.scan = self.store.start_scan(str(self.root))

    def legacy(self, name, tags='["camelia:black_bars"]', title=None):
        """A row queued for leftover passes before the crawl policy, then carrying the marker."""
        archive = self.root / name
        make_cbz(archive, "<ComicInfo><Tags>camelia:black_bars</Tags></ComicInfo>")
        self.assertEqual(self.store.record_archive(self.scan, str(self.root), str(archive), archive.stat(),
                                                   inspect_cbz_eligibility), "queued")
        with self.store.connect() as db:
            db.execute("UPDATE books SET metadata_tags=?, metadata_title=? WHERE source_path=?",
                       (tags, title, os.path.abspath(archive)))
            return db.execute("SELECT id FROM books WHERE source_path=?", (os.path.abspath(archive),)).fetchone()[0]

    def idle(self):
        """Finish the fixture crawl so the apply-time idle guard can pass."""
        with self.store.connect() as db:
            db.execute("UPDATE scans SET status='completed'")

    def rows(self):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM books ORDER BY id")]

    def stages(self, book_id):
        with self.store.connect() as db:
            return {r["stage"]: r["state"] for r in db.execute("SELECT stage,state FROM book_stages WHERE book_id=?", (book_id,))}

    def test_preview_selects_only_marked_leftover_rows_and_writes_nothing(self):
        tagged = self.legacy("A.cbz", '["Adult", "uncensored", "camelia:black_bars"]')
        titled = self.legacy("B.cbz", '["camelia:black_bars"]', title="Series | Decensored")
        self.legacy("C.cbz")  # method tags only, no marker: stays queued
        manual = self.legacy("D.cbz", '["uncensored", "camelia:black_bars"]')
        with self.store.connect() as db:
            self.store._event(db, "manual_requeue", "operator choice", book_id=manual)
        before = self.rows()
        report = marked_skip.skip_marked_queued(marked_skip.ExistingStore(self.db_path))
        self.assertEqual(report["candidate_count"], 2)
        self.assertEqual([s["id"] for s in report["samples"]], [tagged, titled])
        self.assertEqual(self.rows(), before)

    def test_apply_moves_rows_atomically_and_keeps_stage_history(self):
        book = self.legacy("A.cbz", '["uncensored", "camelia:black_bars"]')
        with self.store.connect() as db:
            db.execute("UPDATE book_stages SET state='passed' WHERE book_id=? AND stage='transparent_black'", (book,))
        self.idle()
        store = marked_skip.ExistingStore(self.db_path)
        preview = marked_skip.skip_marked_queued(store)
        report = marked_skip.skip_marked_queued(store, dry_run=False, expected_count=1,
                                                snapshot_digest=preview["snapshot_digest"])
        self.assertEqual(report["skipped_count"], 1)
        row = [r for r in self.rows() if r["id"] == book][0]
        self.assertEqual((row["state"], row["eligibility_reason"], row["selected_sequence"], row["queued_at"]),
                         ("skipped", MARKED_SKIP_REASON, "[]", None))
        self.assertEqual(self.stages(book), {"transparent_black": "passed"})
        self.assertIsNone(self.store.claim_job())
        with self.store.connect() as db:
            events = [r[0] for r in db.execute("SELECT event_type FROM events WHERE event_type LIKE 'marked_policy_skip%'")]
        self.assertEqual(sorted(events), ["marked_policy_skip", "marked_policy_skip_batch"])

    def test_apply_refuses_running_queue_processing_book_and_running_scan(self):
        self.legacy("A.cbz", '["uncensored"]')
        store = marked_skip.ExistingStore(self.db_path)
        preview = marked_skip.skip_marked_queued(store)
        args = dict(dry_run=False, expected_count=1, snapshot_digest=preview["snapshot_digest"])
        for setup in ("UPDATE controls SET value='running' WHERE name='queue'",
                      "UPDATE scans SET status='running'"):
            with self.subTest(setup=setup):
                with self.store.connect() as db:
                    db.execute("UPDATE controls SET value='paused' WHERE name='queue'")
                    db.execute("UPDATE scans SET status='completed'")
                    db.execute(setup)
                before = self.rows()
                with self.assertRaises(ValueError):
                    marked_skip.skip_marked_queued(store, **args)
                self.assertEqual(self.rows(), before)
        with self.store.connect() as db:
            db.execute("UPDATE scans SET status='completed'")
        self.legacy("P.cbz")
        self.assertIsNotNone(self.store.claim_job())  # one book now processing
        with self.assertRaises(ValueError):
            marked_skip.skip_marked_queued(store, **args)

    def test_apply_refuses_a_stale_count_digest_or_changed_queue(self):
        self.legacy("A.cbz", '["uncensored"]')
        store = marked_skip.ExistingStore(self.db_path)
        preview = marked_skip.skip_marked_queued(store)
        for count, digest in ((2, preview["snapshot_digest"]), (1, "0" * 64), (None, None), (True, preview["snapshot_digest"])):
            with self.assertRaises(ValueError):
                marked_skip.skip_marked_queued(store, dry_run=False, expected_count=count, snapshot_digest=digest)
        self.legacy("B.cbz", '["decensored"]')  # queue changed after the preview
        before = self.rows()
        with self.assertRaises(ValueError):
            marked_skip.skip_marked_queued(store, dry_run=False, expected_count=1,
                                           snapshot_digest=preview["snapshot_digest"])
        self.assertEqual(self.rows(), before)

    def test_existing_store_never_runs_interrupted_work_recovery(self):
        """BacklogStore() would mark Camelia's in-flight book interrupted; the tool must not."""
        self.legacy("A.cbz", '["uncensored"]')
        self.legacy("P.cbz")
        job = self.store.claim_job()
        processing = [r["id"] for r in self.rows() if r["state"] == "processing"]
        self.assertEqual(processing, [job["id"]])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(marked_skip.main(["--db", self.db_path]), 0)
        self.assertEqual([r["id"] for r in self.rows() if r["state"] == "processing"], processing)

    def test_cli_preview_then_apply_backs_up_first(self):
        self.legacy("A.cbz", '["uncensored"]')
        self.idle()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(marked_skip.main(["--db", self.db_path]), 0)
        preview = json.loads(out.getvalue())
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(marked_skip.main(["--db", self.db_path, "--apply", "--expected-count", "2",
                                               "--snapshot-digest", preview["snapshot_digest"]]), 1)
        self.assertIn("Refused", err.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(marked_skip.main(["--db", self.db_path, "--apply", "--expected-count", "1",
                                               "--snapshot-digest", preview["snapshot_digest"]]), 0)
        backups = list(Path(self.db_path).parent.glob("library_backlog.pre-marked-skip-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        self.assertIn("backup", out.getvalue())
        self.assertEqual([r["state"] for r in self.rows()], ["skipped"])


if __name__ == "__main__":
    unittest.main()
