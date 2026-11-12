"""Preview and atomically retire missing-file backlog records, never files.

Absence is proved only on readable roots and by an actual not-found stat
error. Permission/network failures are unknown, not evidence of deletion.
Preview tokens bind the queue snapshot and findings; apply repeats the checks.
"""
import errno
import hashlib
import json
import os
import re
from collections import Counter


_ROWS = """SELECT id,source_path,root_path,state,updated_at,file_size,mtime_ns,
           queued_at,queue_priority,selected_sequence,auto_detect FROM books ORDER BY id"""


def _ready(db):
    if db.execute("SELECT 1 FROM controls WHERE name='queue' AND value='running'").fetchone():
        raise ValueError("Pause the queue before pruning missing files")
    if db.execute("SELECT 1 FROM books WHERE state='processing' LIMIT 1").fetchone():
        raise ValueError("Wait for the current book to finish before pruning")
    if db.execute("SELECT 1 FROM scans WHERE status='running' LIMIT 1").fetchone():
        raise ValueError("Pause or stop the crawl before pruning missing files")


def _root_error(root):
    try:
        with os.scandir(root) as entries:
            next(entries, None)  # force a real directory read, not just attributes
    except OSError as error:
        return str(error)
    return None


def _missing(path):
    try:
        os.stat(path)
    except FileNotFoundError as error:
        # Windows network-path failures can also be FileNotFoundError. They
        # must never be mistaken for a missing book on an available library.
        if error.errno == errno.ENOENT and getattr(error, "winerror", None) in (None, 2, 3):
            return True, None
        return False, str(error)
    except OSError as error:
        return False, str(error)
    return False, None


def _preview(rows):
    roots = {row['root_path'] for row in rows}
    errors = {root: _root_error(root) for root in roots}
    missing, unknown = [], []
    for row in rows:
        if errors[row['root_path']] is not None:
            continue
        absent, error = _missing(row['source_path'])
        if absent:
            missing.append(row)
        elif error is not None:
            unknown.append(row['id'])
    # A drive that went away during a long preview invalidates its findings.
    for root in roots:
        if errors[root] is None:
            errors[root] = _root_error(root)
    missing = [row for row in missing if errors[row['root_path']] is None]
    unavailable = [{'root': root, 'error': error} for root, error in sorted(errors.items()) if error is not None]
    evidence = {'rows': rows, 'missing': [row['id'] for row in missing],
                'unknown': unknown, 'unavailable_roots': unavailable}
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {
        'missing_count': len(missing), 'by_state': dict(Counter(row['state'] for row in missing)),
        'kept_count': len(rows) - len(missing), 'unverified_count': len(unknown),
        'unavailable_roots': unavailable, 'snapshot_digest': digest,
        'samples': [{'source_path': row['source_path'], 'state': row['state']} for row in missing[:20]],
        'removed_count': 0,
    }, missing


def prune_missing_books(store, *, dry_run=True, expected_count=None, snapshot_digest=None):
    """Report all statuses, or apply a matching preview in one transaction.

    The caller holds the manager's thread lock and has settled its workers.
    Database guards also protect standalone callers and concurrent API edits.
    Existing events survive with detached book references; an audit event names
    each removed ID/path/state. Scan totals remain historical observations.
    """
    if type(dry_run) is not bool:
        raise ValueError("dry_run must be a boolean")
    if not dry_run and (type(expected_count) is not int or expected_count < 0
            or not isinstance(snapshot_digest, str) or re.fullmatch('[0-9a-f]{64}', snapshot_digest) is None):
        raise ValueError("Apply requires the preview count and snapshot digest")
    with store.connect() as db:
        _ready(db)
        rows = [dict(row) for row in db.execute(_ROWS)]
    report, missing = _preview(rows)
    if dry_run:
        return report
    if expected_count != report['missing_count'] or snapshot_digest != report['snapshot_digest']:
        raise ValueError("The queue or files changed; preview missing files again")
    with store._lock, store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        _ready(db)
        if [dict(row) for row in db.execute(_ROWS)] != rows:
            raise ValueError("The queue changed; preview missing files again")
        for root in {row['root_path'] for row in missing}:
            if _root_error(root) is not None:
                raise ValueError("A library became unavailable; preview missing files again")
        # Recheck every candidate before deleting any row, so a restored file
        # or uncertainty aborts the whole operation rather than partial pruning.
        if any(not _missing(row['source_path'])[0] for row in missing):
            raise ValueError("A file changed or cannot be checked; preview missing files again")
        for root in {row['root_path'] for row in missing}:
            if _root_error(root) is not None:
                raise ValueError("A library became unavailable; preview missing files again")
        for row in missing:
            db.execute('UPDATE events SET book_id=NULL WHERE book_id=?', (row['id'],))
            store._event(db, 'book_pruned_missing', json.dumps({
                'book_id': row['id'], 'source_path': row['source_path'], 'state': row['state'],
            }), scan_id=None)
            db.execute('DELETE FROM books WHERE id=?', (row['id'],))
    report['removed_count'] = len(missing)
    return report
