"""One-time, guarded move of already-marked queued books to ``skipped``.

David's policy (hive #6257, 2026-11-19): a book already marked
uncensored/decensored is not processed for leftover passes. The crawl now
records new such books as skipped (``MARKED_SKIP_REASON``); this tool applies
the same decision to rows that were queued before that change.

A candidate is a ``queued`` row whose crawl reason is ``Pending methods: ...``
(so its queue entry exists only for leftover passes) and whose stored metadata
or filename carries the marker. Rows an operator queued explicitly
(``manual_requeue`` event) are never touched: manual extra passes stay a
deliberate choice.

Preview is read-only. Apply needs the preview's count and snapshot digest,
the queue paused with nothing processing or crawling, and repeats every check
inside one ``BEGIN IMMEDIATE`` transaction. The CLI backs up the database and
verifies the copy before applying. No archive file is read or changed.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
from datetime import datetime

from library_backlog import MARKED_SKIP_REASON, BacklogStore, contains_processed_marker, utc_now

_ROWS = """SELECT b.id, b.source_path, b.state, b.updated_at, b.selected_sequence, b.eligibility_reason,
                  b.metadata_title, b.metadata_tags
           FROM books b
           WHERE b.state='queued' AND b.eligibility_reason LIKE 'Pending methods:%'
             AND NOT EXISTS (SELECT 1 FROM events e WHERE e.book_id=b.id AND e.event_type='manual_requeue')
           ORDER BY b.id"""


def _ready(db):
    """Same idle conditions as backlog_prune, with this tool's own wording."""
    if db.execute("SELECT 1 FROM controls WHERE name='queue' AND value='running'").fetchone():
        raise ValueError("Pause the queue before skipping marked books")
    if db.execute("SELECT 1 FROM books WHERE state='processing' LIMIT 1").fetchone():
        raise ValueError("Wait for the current book to finish before skipping marked books")
    if db.execute("SELECT 1 FROM scans WHERE status='running' LIMIT 1").fetchone():
        raise ValueError("Pause or stop the crawl before skipping marked books")


class ExistingStore:
    """The live backlog without BacklogStore's constructor side effects.

    ``BacklogStore.__init__`` migrates the schema and runs
    ``recover_interrupted_work``, which would mark the book Camelia is
    processing right now as interrupted. Opening the database through this
    adapter reuses the same connection settings and event writer only.
    """
    connect = BacklogStore.connect
    _event = staticmethod(BacklogStore._event)

    def __init__(self, database_path: str):
        self.database_path = os.path.abspath(database_path)
        self._lock = threading.RLock()


def _marked(row) -> bool:
    return (contains_processed_marker(row["metadata_tags"]) or contains_processed_marker(row["metadata_title"])
            or contains_processed_marker(os.path.basename(row["source_path"])))


def _candidates(db) -> list[dict]:
    return [dict(row) for row in db.execute(_ROWS) if _marked(row)]


def _digest(rows) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def skip_marked_queued(store, *, dry_run=True, expected_count=None, snapshot_digest=None) -> dict:
    """Report the candidates, or apply a matching preview atomically."""
    if type(dry_run) is not bool:
        raise ValueError("dry_run must be a boolean")
    if not dry_run and (type(expected_count) is not int or expected_count < 0
            or not isinstance(snapshot_digest, str) or re.fullmatch("[0-9a-f]{64}", snapshot_digest) is None):
        raise ValueError("Apply requires the preview count and snapshot digest")
    with store.connect() as db:
        if not dry_run:
            _ready(db)
        rows = _candidates(db)
    report = {"candidate_count": len(rows), "snapshot_digest": _digest(rows), "skipped_count": 0,
              "samples": [{"id": row["id"], "source_path": row["source_path"],
                           "selected_sequence": json.loads(row["selected_sequence"])} for row in rows[:20]]}
    if dry_run:
        return report
    if expected_count != report["candidate_count"] or snapshot_digest != report["snapshot_digest"]:
        raise ValueError("The queue changed; preview again")
    with store._lock, store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        # Repeated under the write lock: a resumed queue, a claimed book or a
        # new crawl between preview and apply must abort the whole move.
        _ready(db)
        if _candidates(db) != rows:
            raise ValueError("The queue changed; preview again")
        now = utc_now()
        for row in rows:
            changed = db.execute(
                """UPDATE books SET state='skipped', eligibility_reason=?, selected_sequence='[]',
                   queued_at=NULL, updated_at=? WHERE id=? AND state='queued'""",
                (MARKED_SKIP_REASON, now, row["id"]))
            if changed.rowcount != 1:
                raise ValueError("The queue changed; preview again")
            # Same as narrowing a plan: only unstarted stages go; passed or
            # failed stage history stays as evidence.
            db.execute("DELETE FROM book_stages WHERE book_id=? AND state='pending'", (row["id"],))
            store._event(db, "marked_policy_skip",
                         f"Skipped by policy #6257 (was queued for {', '.join(json.loads(row['selected_sequence']))})",
                         book_id=row["id"])
        store._event(db, "marked_policy_skip_batch", f"Skipped {len(rows)} already-marked queued book(s) (#6257)")
    report["skipped_count"] = len(rows)
    return report


def backup_database(path: str) -> str:
    """Online SQLite backup next to the database; refuses an unverifiable copy."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = os.path.join(os.path.dirname(path), f"library_backlog.pre-marked-skip-{stamp}.sqlite3")
    if os.path.exists(target):
        raise ValueError(f"Backup target already exists: {target}")
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        copy = sqlite3.connect(target)
        try:
            source.backup(copy)
            if copy.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Backup failed its quick_check")
            if copy.execute("SELECT COUNT(*) FROM books").fetchone()[0] != source.execute(
                    "SELECT COUNT(*) FROM books").fetchone()[0]:
                raise ValueError("Backup row count differs from the source")
        finally:
            copy.close()
    finally:
        source.close()
    return target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                     "camelia-decensor", "state", "library_backlog.sqlite3"))
    parser.add_argument("--apply", action="store_true", help="apply a reviewed preview (default: preview only)")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--snapshot-digest")
    args = parser.parse_args(argv)
    if not os.path.isfile(args.db):
        print(f"No backlog database at {args.db}", file=sys.stderr)
        return 2
    store = ExistingStore(args.db)
    try:
        if args.apply:
            preview = skip_marked_queued(store)
            if (preview["candidate_count"], preview["snapshot_digest"]) != (args.expected_count, args.snapshot_digest):
                raise ValueError("The queue changed; preview again")
            with store.connect() as db:
                _ready(db)
            print("backup", backup_database(args.db))
            report = skip_marked_queued(store, dry_run=False, expected_count=args.expected_count,
                                        snapshot_digest=args.snapshot_digest)
        else:
            report = skip_marked_queued(store)
    except ValueError as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
