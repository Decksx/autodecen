"""Durable comic-library discovery and decensor backlog orchestration.

The crawler checkpoints directories (rather than only a last filename), which
allows an interrupted recursive walk to resume without losing subtrees.  The
queue deliberately has one worker because Camelia's model pipeline is shared
and GPU intensive.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from xml.etree import ElementTree
from zoneinfo import ZoneInfo


LOGGER = logging.getLogger(__name__)
PROCESSED_MARKERS = ("uncensored", "decensored")
DEFAULT_ROOTS = (r"X:\comix", r"Z:\comics\Comix")
EXCLUDED_DIRECTORY_NAMES = frozenset({
    ".stversions", ".stfolder", "@eadir", "$recycle.bin", "system volume information",
})
DEFAULT_SEQUENCE = ("black_bars", "transparent_black", "white_bars", "mosaic")
METHOD_TAG_PREFIX = "camelia:"
PREVIOUS_DEFAULT_SEQUENCE = ("black_bars", "transparent_black", "white_bars")
ACTIVE_STATES = ("queued", "processing")
SCHEDULE_ROOT = DEFAULT_ROOTS[0]
SCHEDULE_TIMEZONE = ZoneInfo("America/Denver")


def excluded_library_path(path: str, root: str, *, directory: bool = False) -> bool:
    """Match ComicAutomation's sync/history exclusions and refuse paths outside root."""
    try:
        relative = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    except ValueError:  # Different Windows drives.
        return True
    if relative == os.curdir:
        return False
    parts = Path(relative).parts
    if not parts or parts[0] == os.pardir:
        return True
    checked = parts if directory else parts[:-1]
    return any(part.casefold() in EXCLUDED_DIRECTORY_NAMES for part in checked)


def processing_window_status(now: datetime | None = None) -> dict:
    """Local-time batch windows; DST follows America/Denver calendar time."""
    current = (now or datetime.now(timezone.utc)).astimezone(SCHEDULE_TIMEZONE)
    windows = []
    for offset in range(8):
        day = current.date() + timedelta(days=offset)
        windows.append((
            datetime.combine(day, clock_time(0), SCHEDULE_TIMEZONE),
            datetime.combine(day, clock_time(5), SCHEDULE_TIMEZONE),
        ))
        if day.weekday() < 5:
            windows.append((
                datetime.combine(day, clock_time(9), SCHEDULE_TIMEZONE),
                datetime.combine(day, clock_time(17), SCHEDULE_TIMEZONE),
            ))
    for start, end in windows:
        if start <= current < end:
            return {"in_window": True, "window_ends_at": end.isoformat(),
                    "next_window_at": None}
    next_start = min(start for start, _ in windows if start > current)
    return {"in_window": False, "window_ends_at": None,
            "next_window_at": next_start.isoformat()}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_elapsed_duration(seconds: float | int | None) -> str:
    """Format elapsed seconds compactly for logs and progress displays."""
    total_seconds = max(0, int(round(float(seconds or 0))))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if remaining_seconds or not parts:
        parts.append(f"{remaining_seconds}s")
    return " ".join(parts)


def elapsed_seconds(started_at: str | None, finished_at: str | None = None) -> float | None:
    """Return a non-negative wall-clock delta for persisted ISO timestamps."""
    if not started_at:
        return None
    try:
        start = datetime.fromisoformat(started_at)
        finish = datetime.fromisoformat(finished_at) if finished_at else datetime.now(timezone.utc)
        return max(0.0, (finish - start).total_seconds())
    except (TypeError, ValueError):
        return None


def contains_processed_marker(value: object) -> bool:
    text = str(value or "").casefold()
    return any(marker in text for marker in PROCESSED_MARKERS)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _comicinfo_member(archive: zipfile.ZipFile) -> zipfile.ZipInfo | None:
    for member in archive.infolist():
        if not member.is_dir() and member.filename.replace("\\", "/").rsplit("/", 1)[-1].casefold() == "comicinfo.xml":
            return member
    return None


def inspect_cbz_eligibility(path: str) -> dict:
    """Read only filename and ComicInfo.xml to classify one CBZ.

    Image members are never extracted. Malformed/missing ComicInfo metadata is
    logged in the result but does not make an otherwise unmarked book unsafe to
    queue.
    """
    result = {
        "eligible": True,
        "already_processed": False,
        "title": None,
        "tags": [],
        "applied_methods": [],
        "comicinfo_member": None,
        "parse_error": None,
        "reason": "No uncensored/decensored marker found",
    }
    filename_marked = contains_processed_marker(os.path.basename(path))
    if filename_marked:
        result.update(eligible=False, already_processed=True,
                      reason="Filename contains an uncensored/decensored marker")

    try:
        with zipfile.ZipFile(path) as archive:
            member = _comicinfo_member(archive)
            if member is None:
                if not filename_marked:
                    result["reason"] = "ComicInfo.xml is missing; filename is unmarked"
                return result
            result["comicinfo_member"] = member.filename
            try:
                root = ElementTree.fromstring(archive.read(member))
            except (ElementTree.ParseError, UnicodeError, ValueError) as exc:
                result["parse_error"] = f"{type(exc).__name__}: {exc}"
                if not filename_marked:
                    result["reason"] = "ComicInfo.xml could not be parsed; filename is unmarked"
                return result

            title_values: list[str] = []
            tag_values: list[str] = []
            for element in root.iter():
                name = _local_name(element.tag)
                text = (element.text or "").strip()
                if name in {"title", "series", "number", "alternateSeries".casefold()} and text:
                    title_values.append(text)
                if name in {"tags", "tag", "genre"} and text:
                    tag_values.extend(
                        item.strip() for item in text.replace(";", ",").split(",") if item.strip()
                    )
            result["title"] = " | ".join(title_values) or None
            result["tags"] = tag_values
            result["applied_methods"] = [
                method for method in DEFAULT_SEQUENCE
                if any(tag.casefold() == f"{METHOD_TAG_PREFIX}{method}" for tag in tag_values)
            ]
            metadata_text = " ".join([*title_values, *tag_values])
            if contains_processed_marker(metadata_text):
                result.update(
                    eligible=False,
                    already_processed=True,
                    reason="ComicInfo title/series/tags contain an uncensored/decensored marker",
                )
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        result["eligible"] = False
        result["parse_error"] = f"{type(exc).__name__}: {exc}"
        result["reason"] = "Archive could not be inspected"
    return result


def pending_decensor_methods(inspection: dict, selected_methods: Iterable[str],
                            reprocess: bool = False) -> list[str]:
    """Return ordered methods still needed; legacy generic markers are unknown."""
    selected = list(dict.fromkeys(selected_methods))
    if inspection.get("parse_error") and inspection.get("reason") == "Archive could not be inspected":
        return []
    if inspection.get("eligible") is False and not inspection.get("already_processed"):
        return []
    if reprocess:
        return selected
    applied = set(inspection.get("applied_methods") or ())
    if applied:
        return [method for method in selected if method not in applied]
    return [] if inspection.get("already_processed") else selected


def archive_is_processed(path: str) -> bool:
    return bool(inspect_cbz_eligibility(path)["already_processed"])


def _clone_zip_info(member: zipfile.ZipInfo) -> zipfile.ZipInfo:
    return copy.copy(member)


def add_uncensored_tag(path: str, applied_methods: Iterable[str] = ()) -> dict:
    """Idempotently add ``uncensored`` and completed method tags to ComicInfo.xml.

    If metadata is absent, a minimal ComicInfo document is created. This is
    safer than treating a processed archive as unmarked, and changes no image
    or existing auxiliary member.
    """
    applied_methods = tuple(dict.fromkeys(applied_methods))
    invalid = [method for method in applied_methods if method not in DEFAULT_SEQUENCE]
    if invalid:
        raise ValueError(f"Unknown completed decensor method(s): {', '.join(invalid)}")
    source = os.path.abspath(path)
    temporary = f"{source}.metadata-{uuid.uuid4().hex}.tmp"
    created = False
    changed = False
    member_name = "ComicInfo.xml"
    try:
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            metadata_member = _comicinfo_member(archive)
            metadata_index = members.index(metadata_member) if metadata_member else None
            if metadata_member:
                member_name = metadata_member.filename
                payload = archive.read(metadata_member)
                try:
                    parser = ElementTree.XMLParser(
                        target=ElementTree.TreeBuilder(insert_comments=True)
                    )
                    root = ElementTree.fromstring(payload, parser=parser)
                except ElementTree.ParseError as exc:
                    raise ValueError(
                        "Refusing to replace malformed ComicInfo.xml after processing"
                    ) from exc
            else:
                root = ElementTree.Element("ComicInfo")
                created = True

            tags_element = next(
                (element for element in root.iter() if _local_name(element.tag) == "tags"),
                None,
            )
            if tags_element is None:
                tags_element = ElementTree.SubElement(root, "Tags")
            tags = [item.strip() for item in (tags_element.text or "").replace(";", ",").split(",") if item.strip()]
            additions = ["uncensored", *(f"{METHOD_TAG_PREFIX}{method}" for method in applied_methods)]
            for tag in additions:
                if any(item.casefold() == tag.casefold() for item in tags):
                    continue
                tags.append(tag)
                changed = True
            if changed:
                tags_element.text = ", ".join(tags)
            recorded_methods = [
                method for method in DEFAULT_SEQUENCE
                if any(tag.casefold() == f"{METHOD_TAG_PREFIX}{method}" for tag in tags)
            ]

            if not changed and not created:
                return {"changed": False, "created": False, "member": member_name,
                        "applied_methods": recorded_methods}

            xml_payload = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
            with zipfile.ZipFile(temporary, "w") as target:
                target.comment = archive.comment
                for index, member in enumerate(members):
                    if index == metadata_index:
                        target.writestr(_clone_zip_info(member), xml_payload)
                    elif member.is_dir():
                        target.writestr(_clone_zip_info(member), b"")
                    else:
                        target.writestr(_clone_zip_info(member), archive.read(member))
                if metadata_member is None:
                    target.writestr(member_name, xml_payload)

        with zipfile.ZipFile(temporary) as check:
            if check.testzip() is not None:
                raise RuntimeError("Tagged archive failed CRC verification")
        os.replace(temporary, source)
        return {"changed": True, "created": created, "member": member_name,
                "applied_methods": recorded_methods}
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def assert_overwrite_allowed(incoming_path: str, destination_path: str) -> None:
    """Enforce the processed-destination overwrite invariant."""
    if not os.path.exists(destination_path):
        return
    if archive_is_processed(destination_path) and not archive_is_processed(incoming_path):
        message = (
            "Refusing to overwrite an uncensored/decensored destination with "
            f"an unmarked incoming archive: {destination_path}"
        )
        LOGGER.warning(message)
        raise PermissionError(message)


class BacklogStore:
    """Small transactional repository for scan and queue state."""

    def __init__(self, database_path: str):
        self.database_path = os.path.abspath(database_path)
        os.makedirs(os.path.dirname(self.database_path), exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()
        self.recover_interrupted_work()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS scans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    root_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_path TEXT,
                    total_found INTEGER NOT NULL DEFAULT 0,
                    scanned_count INTEGER NOT NULL DEFAULT 0,
                    eligible_count INTEGER NOT NULL DEFAULT 0,
                    skipped_count INTEGER NOT NULL DEFAULT 0,
                    parse_error_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS scan_directories (
                    scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
                    path TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT,
                    PRIMARY KEY (scan_id, path)
                );
                CREATE TABLE IF NOT EXISTS books (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_path TEXT NOT NULL UNIQUE,
                    root_path TEXT NOT NULL,
                    last_scan_id INTEGER REFERENCES scans(id),
                    file_size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    metadata_title TEXT,
                    metadata_tags TEXT,
                    eligibility_reason TEXT,
                    eligibility_version INTEGER NOT NULL DEFAULT 1,
                    state TEXT NOT NULL,
                    selected_sequence TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 2,
                    current_stage TEXT,
                    last_error TEXT,
                    output_path TEXT,
                    discovered_at TEXT NOT NULL,
                    queued_at TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    duration_seconds REAL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS books_state_idx ON books(state, queued_at, id);
                CREATE TABLE IF NOT EXISTS book_stages (
                    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    stage TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','running','passed','completed','failed','interrupted')),
                    attempt INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT,
                    passed_at TEXT,
                    applied_at TEXT,
                    failed_at TEXT,
                    duration_seconds REAL,
                    last_error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(book_id, stage)
                );
                CREATE TABLE IF NOT EXISTS book_phases (
                    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    phase TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('running','completed','failed','interrupted')),
                    attempt INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT,
                    completed_at TEXT,
                    duration_seconds REAL,
                    last_error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(book_id, phase)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id INTEGER REFERENCES scans(id),
                    book_id INTEGER REFERENCES books(id),
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS controls (
                    name TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(books)")}
            if "eligibility_version" not in columns:
                db.execute("ALTER TABLE books ADD COLUMN eligibility_version INTEGER NOT NULL DEFAULT 1")
            if "duration_seconds" not in columns:
                db.execute("ALTER TABLE books ADD COLUMN duration_seconds REAL")
            stage_columns = {row["name"] for row in db.execute("PRAGMA table_info(book_stages)")}
            if "duration_seconds" not in stage_columns:
                db.execute("ALTER TABLE book_stages ADD COLUMN duration_seconds REAL")
            db.execute(
                "INSERT OR IGNORE INTO controls(name, value, updated_at) VALUES('queue', 'paused', ?)",
                (utc_now(),),
            )
            db.execute(
                "INSERT OR IGNORE INTO controls(name, value, updated_at) VALUES('batch_schedule', 'disabled', ?)",
                (utc_now(),),
            )
            db.execute(
                "INSERT OR IGNORE INTO controls(name, value, updated_at) VALUES('scheduled_autostart', 'disabled', ?)",
                (utc_now(),),
            )
            db.execute(
                "INSERT OR IGNORE INTO controls(name, value, updated_at) VALUES('instance_uuid', ?, ?)",
                (uuid.uuid4().hex, utc_now()),
            )
            if not db.execute("SELECT 1 FROM controls WHERE name='stage_marker_backfill_v1'").fetchone():
                # Earlier releases only marked a book complete after all
                # selected method tags were verified on the installed CBZ.
                for book in db.execute(
                    "SELECT id, selected_sequence, attempts, completed_at FROM books WHERE state='completed'"
                ).fetchall():
                    for stage in json.loads(book["selected_sequence"]):
                        db.execute(
                            """INSERT OR IGNORE INTO book_stages
                               (book_id,stage,state,attempt,applied_at,updated_at)
                               VALUES(?,?,'completed',?,?,?)""",
                            (book["id"], stage, book["attempts"],
                             book["completed_at"], utc_now()),
                        )
                db.execute(
                    "INSERT INTO controls(name,value,updated_at) VALUES('stage_marker_backfill_v1','done',?)",
                    (utc_now(),),
                )

    def recover_interrupted_work(self) -> None:
        now = utc_now()
        with self.connect() as db:
            scans = db.execute("SELECT id FROM scans WHERE status = 'running'").fetchall()
            for row in scans:
                db.execute("UPDATE scans SET status='paused', updated_at=? WHERE id=?", (now, row["id"]))
                db.execute("UPDATE scan_directories SET status='pending' WHERE scan_id=? AND status='scanning'", (row["id"],))
                self._event(db, "recovery", "Interrupted crawl recovered in paused state", scan_id=row["id"])
            jobs = db.execute("SELECT id FROM books WHERE state='processing'").fetchall()
            for row in jobs:
                db.execute(
                    """UPDATE book_stages SET state='interrupted', updated_at=?
                       WHERE book_id=? AND state IN ('pending','running','passed')""",
                    (now, row["id"]),
                )
                db.execute(
                    """UPDATE book_phases SET state='interrupted', updated_at=?
                       WHERE book_id=? AND state='running'""",
                    (now, row["id"]),
                )
                db.execute(
                    "UPDATE books SET state='queued', current_stage=NULL, last_error=?, updated_at=? WHERE id=?",
                    ("Recovered after interrupted processing; safe retry queued", now, row["id"]),
                )
                self._event(db, "recovery", "Interrupted job returned to the queue", book_id=row["id"])
            # Adopt the newly supported mosaic stage only for jobs retaining
            # the prior default. Never rewrite custom sequences or completed
            # work. Recovered processing jobs are now safe to update as well.
            upgraded = db.execute(
                """UPDATE books SET selected_sequence=?, updated_at=?
                   WHERE state IN ('queued', 'failed') AND eligibility_version=1 AND selected_sequence=?""",
                (json.dumps(DEFAULT_SEQUENCE), now, json.dumps(PREVIOUS_DEFAULT_SEQUENCE)),
            )
            if upgraded.rowcount:
                self._event(db, "sequence_upgrade", f"Added mosaic to {upgraded.rowcount} pending default job(s)")
            db.execute("UPDATE controls SET value='paused', updated_at=? WHERE name='queue'", (now,))

    @staticmethod
    def _event(db, event_type: str, message: str, scan_id=None, book_id=None) -> None:
        db.execute(
            "INSERT INTO events(scan_id, book_id, event_type, message, created_at) VALUES(?,?,?,?,?)",
            (scan_id, book_id, event_type, message, utc_now()),
        )

    def start_scan(self, root_path: str) -> int:
        root = os.path.abspath(root_path)
        now = utc_now()
        with self.connect() as db:
            active = db.execute(
                "SELECT id FROM scans WHERE root_path=? AND status IN ('running','paused') ORDER BY id DESC LIMIT 1",
                (root,),
            ).fetchone()
            if active:
                scan_id = active["id"]
                db.execute("UPDATE scans SET status='running', updated_at=? WHERE id=?", (now, scan_id))
                self._event(db, "scan_resume", f"Crawl resumed: {root}", scan_id=scan_id)
                return scan_id
            cursor = db.execute(
                "INSERT INTO scans(root_path,status,created_at,updated_at) VALUES(?, 'running', ?, ?)",
                (root, now, now),
            )
            scan_id = cursor.lastrowid
            db.execute(
                "INSERT INTO scan_directories(scan_id,path,status) VALUES(?,?,'pending')",
                (scan_id, root),
            )
            self._event(db, "scan_start", f"Crawl started: {root}", scan_id=scan_id)
            return scan_id

    def set_scan_status(self, scan_id: int, status: str, message: str) -> None:
        if status not in {"running", "paused", "stopped", "completed", "failed"}:
            raise ValueError(f"Invalid scan status: {status}")
        with self.connect() as db:
            db.execute(
                "UPDATE scans SET status=?, updated_at=?, completed_at=CASE WHEN ? IN ('completed','stopped','failed') THEN ? ELSE completed_at END WHERE id=?",
                (status, utc_now(), status, utc_now(), scan_id),
            )
            self._event(db, f"scan_{status}", message, scan_id=scan_id)

    def scan_status(self, scan_id: int) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT status FROM scans WHERE id=?", (scan_id,)).fetchone()
            return row["status"] if row else None

    def claim_directory(self, scan_id: int) -> str | None:
        with self._lock, self.connect() as db:
            row = db.execute(
                "SELECT path FROM scan_directories WHERE scan_id=? AND status='pending' ORDER BY path LIMIT 1",
                (scan_id,),
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE scan_directories SET status='scanning', error=NULL WHERE scan_id=? AND path=?",
                (scan_id, row["path"]),
            )
            db.execute("UPDATE scans SET current_path=?, updated_at=? WHERE id=?", (row["path"], utc_now(), scan_id))
            return row["path"]

    def finish_directory(self, scan_id: int, directory: str, children: Iterable[str], error: str | None = None) -> None:
        with self.connect() as db:
            for child in children:
                db.execute(
                    "INSERT OR IGNORE INTO scan_directories(scan_id,path,status) VALUES(?,?,'pending')",
                    (scan_id, child),
                )
            db.execute(
                "UPDATE scan_directories SET status=?, error=? WHERE scan_id=? AND path=?",
                ("failed" if error else "done", error, scan_id, directory),
            )
            if error:
                self._event(db, "scan_error", f"Could not read {directory}: {error}", scan_id=scan_id)

    def record_archive(self, scan_id: int, root: str, path: str, stat, inspector: Callable[[str], dict]) -> str:
        normalized = os.path.abspath(path)
        now = utc_now()
        with self.connect() as db:
            prior = db.execute("SELECT * FROM books WHERE source_path=?", (normalized,)).fetchone()
            unchanged = (prior and prior["eligibility_version"] >= 2
                         and prior["file_size"] == stat.st_size and prior["mtime_ns"] == stat.st_mtime_ns)
            if unchanged:
                db.execute("UPDATE books SET last_scan_id=?, updated_at=? WHERE id=?", (scan_id, now, prior["id"]))
                if prior["last_scan_id"] == scan_id:
                    return "unchanged"
                state = prior["state"]
                self._increment_scan(db, scan_id, state, unchanged=True)
                self._event(db, "scan_unchanged", f"Unchanged archive reused: {normalized}", scan_id=scan_id, book_id=prior["id"])
                return "unchanged"

        # ZIP I/O happens outside the write transaction.
        inspection = inspector(normalized)
        sequence = pending_decensor_methods(inspection, DEFAULT_SEQUENCE)
        state = "queued" if sequence else "skipped"
        reason = inspection.get("reason")
        if inspection.get("applied_methods"):
            reason = (f"Pending methods: {', '.join(sequence)}" if sequence else
                      "All decensor methods already recorded")
        with self.connect() as db:
            prior = db.execute("SELECT id, state FROM books WHERE source_path=?", (normalized,)).fetchone()
            values = (
                root, scan_id, stat.st_size, stat.st_mtime_ns,
                inspection.get("title"), json.dumps(inspection.get("tags", [])),
                reason, state, json.dumps(sequence),
                now if state == "queued" else None, inspection.get("parse_error"), now,
                normalized,
            )
            if prior:
                # The bytes changed; pass markers from the old archive can no
                # longer certify this replacement. Event history remains.
                db.execute("DELETE FROM book_stages WHERE book_id=?", (prior["id"],))
                db.execute(
                    """UPDATE books SET root_path=?, last_scan_id=?, file_size=?, mtime_ns=?,
                       metadata_title=?, metadata_tags=?, eligibility_reason=?, state=?,
                       selected_sequence=?, attempts=0, current_stage=NULL, queued_at=?,
                       last_error=?, completed_at=NULL, updated_at=?, eligibility_version=2 WHERE source_path=?""",
                    values,
                )
                book_id = prior["id"]
            else:
                cursor = db.execute(
                    """INSERT INTO books(source_path,root_path,last_scan_id,file_size,mtime_ns,
                       metadata_title,metadata_tags,eligibility_reason,state,selected_sequence,
                       eligibility_version,
                       queued_at,last_error,discovered_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        normalized, root, scan_id, stat.st_size, stat.st_mtime_ns,
                        inspection.get("title"), json.dumps(inspection.get("tags", [])),
                        reason, state, json.dumps(sequence), 2,
                        now if state == "queued" else None, inspection.get("parse_error"), now, now,
                    ),
                )
                book_id = cursor.lastrowid
                self._event(db, "discovered", f"Discovered {normalized}", scan_id=scan_id, book_id=book_id)
            for stage in sequence:
                db.execute(
                    """INSERT INTO book_stages(book_id,stage,state,updated_at)
                       VALUES(?,?,'pending',?)""",
                    (book_id, stage, now),
                )
            decision = "Eligible and queued" if state == "queued" else f"Skipped: {reason}"
            if inspection.get("parse_error"):
                decision += f"; metadata warning: {inspection['parse_error']}"
            self._event(db, "scan_decision", decision, scan_id=scan_id, book_id=book_id)
            self._increment_scan(db, scan_id, state, parse_error=bool(inspection.get("parse_error")))
        return state

    @staticmethod
    def _increment_scan(db, scan_id: int, state: str, unchanged=False, parse_error=False) -> None:
        eligible = int(state in {"eligible", "queued", "processing", "completed", "failed"})
        skipped = int(state == "skipped")
        db.execute(
            """UPDATE scans SET total_found=total_found+1, scanned_count=scanned_count+?,
               eligible_count=eligible_count+?, skipped_count=skipped_count+?,
               parse_error_count=parse_error_count+?, updated_at=? WHERE id=?""",
            (0 if unchanged else 1, eligible, skipped, int(parse_error), utc_now(), scan_id),
        )

    def control_queue(self, status: str) -> None:
        if status not in {"running", "paused", "stopped"}:
            raise ValueError(f"Invalid queue status: {status}")
        with self.connect() as db:
            db.execute("UPDATE controls SET value=?, updated_at=? WHERE name='queue'", (status, utc_now()))
            self._event(db, f"queue_{status}", f"Processing queue {status}")

    def queue_control(self) -> str:
        with self.connect() as db:
            return db.execute("SELECT value FROM controls WHERE name='queue'").fetchone()["value"]

    def schedule_enabled(self) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT value FROM controls WHERE name='batch_schedule'").fetchone()
            return bool(row and row["value"] == "enabled")

    def control_schedule(self, enabled: bool) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE controls SET value=?, updated_at=? WHERE name='batch_schedule'",
                ("enabled" if enabled else "disabled", utc_now()),
            )
            self._event(db, "schedule_enabled" if enabled else "schedule_disabled",
                        f"X:\\comix processing window {'enabled' if enabled else 'disabled'}")

    def scheduled_autostart(self) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT value FROM controls WHERE name='scheduled_autostart'").fetchone()
            return bool(row and row["value"] == "enabled")

    def has_queued_root(self, root_path: str, include_failed: bool = False) -> bool:
        with self.connect() as db:
            return db.execute(
                """SELECT 1 FROM books WHERE root_path=? COLLATE NOCASE
                   AND (state='queued' OR (? AND state='failed' AND attempts <= max_retries))
                   LIMIT 1""",
                (root_path, int(include_failed)),
            ).fetchone() is not None

    def control_scheduled_autostart(self, enabled: bool) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE controls SET value=?, updated_at=? WHERE name='scheduled_autostart'",
                ("enabled" if enabled else "disabled", utc_now()),
            )

    def record_schedule_event(self, event_type: str, message: str) -> None:
        self.record_event(event_type, message)

    def record_event(self, event_type: str, message: str) -> None:
        with self.connect() as db:
            self._event(db, event_type, message)

    def claim_job(self, root_path: str | None = None,
                  exclude_root: str | None = None) -> dict | None:
        with self._lock, self.connect() as db:
            row = db.execute(
                """SELECT * FROM books WHERE state='queued' AND attempts <= max_retries
                   AND (? IS NULL OR root_path = ? COLLATE NOCASE)
                   AND (? IS NULL OR root_path <> ? COLLATE NOCASE)
                   ORDER BY queued_at, id LIMIT 1""",
                (root_path, root_path, exclude_root, exclude_root),
            ).fetchone()
            if not row:
                return None
            now = utc_now()
            db.execute(
                "UPDATE books SET state='processing', attempts=attempts+1, started_at=?, completed_at=NULL, duration_seconds=NULL, updated_at=?, current_stage='starting' WHERE id=? AND state='queued'",
                (now, now, row["id"]),
            )
            self._event(db, "job_start", f"Processing started: {row['source_path']}", book_id=row["id"])
            return dict(db.execute("SELECT * FROM books WHERE id=?", (row["id"],)).fetchone())

    def update_stage(self, book_id: int, stage: str, event: str, message: str,
                     duration_seconds: float | None = None) -> None:
        states = {"stage_pending": "pending", "stage_start": "running",
                  "stage_complete": "passed", "stage_failure": "failed"}
        if event not in states:
            raise ValueError(f"Unknown stage event: {event}")
        now = utc_now()
        with self.connect() as db:
            book = db.execute(
                "SELECT selected_sequence,attempts,state FROM books WHERE id=?", (book_id,)
            ).fetchone()
            if book is None or book["state"] != "processing" or stage not in json.loads(book["selected_sequence"]):
                raise ValueError(f"Stage {stage} is not selected for the processing book {book_id}")
            db.execute(
                """INSERT OR IGNORE INTO book_stages(book_id,stage,state,attempt,updated_at)
                   VALUES(?,?,'pending',?,?)""",
                (book_id, stage, book["attempts"], now),
            )
            if event == "stage_pending":
                db.execute(
                    """UPDATE book_stages SET state='pending', attempt=?, started_at=NULL,
                       passed_at=NULL, failed_at=NULL, duration_seconds=NULL,
                       last_error=NULL, updated_at=?
                       WHERE book_id=? AND stage=?""",
                    (book["attempts"], now, book_id, stage),
                )
            elif event == "stage_start":
                db.execute(
                    """UPDATE book_stages SET state='running', attempt=?, started_at=?,
                       passed_at=NULL, failed_at=NULL, duration_seconds=NULL,
                       last_error=NULL, updated_at=?
                       WHERE book_id=? AND stage=?""",
                    (book["attempts"], now, now, book_id, stage),
                )
            elif event == "stage_complete":
                if duration_seconds is None:
                    started = db.execute(
                        "SELECT started_at FROM book_stages WHERE book_id=? AND stage=?",
                        (book_id, stage),
                    ).fetchone()
                    duration_seconds = elapsed_seconds(started["started_at"], now) if started else None
                db.execute(
                    """UPDATE book_stages SET state='passed', passed_at=?, duration_seconds=?, updated_at=?
                       WHERE book_id=? AND stage=?""",
                    (now, duration_seconds, now, book_id, stage),
                )
            else:
                if duration_seconds is None:
                    started = db.execute(
                        "SELECT started_at FROM book_stages WHERE book_id=? AND stage=?",
                        (book_id, stage),
                    ).fetchone()
                    duration_seconds = elapsed_seconds(started["started_at"], now) if started else None
                db.execute(
                    """UPDATE book_stages SET state='failed', failed_at=?, duration_seconds=?,
                       last_error=?, updated_at=?
                       WHERE book_id=? AND stage=?""",
                    (now, duration_seconds, message, now, book_id, stage),
                )
            db.execute(
                "UPDATE books SET current_stage=?, updated_at=? WHERE id=?",
                (stage if event in {"stage_start", "stage_failure"} else None, now, book_id),
            )
            self._event(db, event, message, book_id=book_id)

    def update_phase(self, book_id: int, phase: str, event: str, message: str,
                     duration_seconds: float | None = None) -> None:
        states = {"phase_start": "running", "phase_complete": "completed", "phase_failure": "failed"}
        if event not in states:
            raise ValueError(f"Unknown finalization event: {event}")
        now = utc_now()
        with self.connect() as db:
            book = db.execute("SELECT attempts,state FROM books WHERE id=?", (book_id,)).fetchone()
            if book is None or book["state"] != "processing":
                raise ValueError(f"Finalization phase {phase} does not belong to processing book {book_id}")
            if event == "phase_start":
                db.execute(
                    """INSERT INTO book_phases(book_id,phase,state,attempt,started_at,updated_at)
                       VALUES(?,?,'running',?,?,?)
                       ON CONFLICT(book_id,phase) DO UPDATE SET
                         state='running', attempt=excluded.attempt,
                         started_at=excluded.started_at, completed_at=NULL,
                         duration_seconds=NULL, last_error=NULL, updated_at=excluded.updated_at""",
                    (book_id, phase, book["attempts"], now, now),
                )
            else:
                existing = db.execute(
                    "SELECT started_at FROM book_phases WHERE book_id=? AND phase=?",
                    (book_id, phase),
                ).fetchone()
                if duration_seconds is None:
                    duration_seconds = elapsed_seconds(existing["started_at"], now) if existing else None
                db.execute(
                    """INSERT INTO book_phases
                       (book_id,phase,state,attempt,started_at,completed_at,duration_seconds,last_error,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(book_id,phase) DO UPDATE SET
                         state=excluded.state, attempt=excluded.attempt,
                         completed_at=excluded.completed_at,
                         duration_seconds=excluded.duration_seconds,
                         last_error=excluded.last_error, updated_at=excluded.updated_at""",
                    (book_id, phase, states[event], book["attempts"], now, now,
                     duration_seconds, message if event == "phase_failure" else None, now),
                )
            self._event(db, event, message, book_id=book_id)

    def finish_job(self, book_id: int, output_path: str,
                   duration_seconds: float | None = None) -> None:
        output_stat = os.stat(output_path)
        now = utc_now()
        with self.connect() as db:
            book = db.execute(
                "SELECT selected_sequence,attempts,started_at FROM books WHERE id=?", (book_id,)
            ).fetchone()
            if duration_seconds is None:
                duration_seconds = elapsed_seconds(book["started_at"], now)
            for stage in json.loads(book["selected_sequence"]):
                row = db.execute(
                    "SELECT state FROM book_stages WHERE book_id=? AND stage=?", (book_id, stage)
                ).fetchone()
                if row is None or row["state"] != "passed":
                    self._event(db, "stage_marker_reconciled",
                                f"Pass marker recovered from verified completed archive: {stage}",
                                book_id=book_id)
                db.execute(
                    """INSERT INTO book_stages(book_id,stage,state,attempt,applied_at,updated_at)
                       VALUES(?,?,'completed',?,?,?)
                       ON CONFLICT(book_id,stage) DO UPDATE SET
                         state='completed', attempt=excluded.attempt,
                         applied_at=excluded.applied_at, failed_at=NULL,
                         last_error=NULL, updated_at=excluded.updated_at""",
                    (book_id, stage, book["attempts"], now, now),
                )
            db.execute(
                """UPDATE books SET state='completed', current_stage=NULL, output_path=?,
                   file_size=?, mtime_ns=?, last_error=NULL, completed_at=?,
                   duration_seconds=?, updated_at=?
                   WHERE id=?""",
                (
                    output_path,
                    output_stat.st_size,
                    output_stat.st_mtime_ns,
                    now,
                    duration_seconds,
                    now,
                    book_id,
                ),
            )
            duration = f" ({format_elapsed_duration(duration_seconds)})" if duration_seconds is not None else ""
            self._event(db, "job_complete", f"Completed and tagged: {output_path}{duration}", book_id=book_id)

    def fail_job(self, book_id: int, error: str) -> None:
        with self.connect() as db:
            row = db.execute("SELECT attempts,max_retries,current_stage FROM books WHERE id=?", (book_id,)).fetchone()
            if row["current_stage"]:
                db.execute(
                    """UPDATE book_stages SET state='failed', failed_at=?, last_error=?, updated_at=?
                       WHERE book_id=? AND stage=? AND state='running'""",
                    (utc_now(), error, utc_now(), book_id, row["current_stage"]),
                )
            # A failed run is explicit. The user can resume/retry it later; this
            # avoids a hot loop repeatedly invoking an expensive model failure.
            db.execute(
                "UPDATE books SET state='failed', current_stage=NULL, last_error=?, updated_at=? WHERE id=?",
                (error, utc_now(), book_id),
            )
            self._event(db, "job_failed", f"Processing failed: {error} (attempt {row['attempts']})", book_id=book_id)

    def skip_job(self, book_id: int, reason: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE books SET state='skipped', current_stage=NULL, last_error=?, updated_at=? WHERE id=?",
                (reason, utc_now(), book_id),
            )
            self._event(db, "job_skipped", reason, book_id=book_id)

    def retry_failed(self) -> int:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE books SET state='queued', queued_at=?, updated_at=? WHERE state='failed' AND attempts <= max_retries",
                (utc_now(), utc_now()),
            )
            if cursor.rowcount:
                self._event(db, "queue_retry", f"Requeued {cursor.rowcount} failed job(s)")
            return cursor.rowcount

    def requeue_book(self, source_path: str, selected_methods: Iterable[str],
                     reprocess: bool = False) -> list[str]:
        """Queue one known book for missing methods or an explicit repeat run."""
        source = os.path.abspath(source_path)
        with self.connect() as db:
            row = db.execute("SELECT id,state FROM books WHERE source_path=?", (source,)).fetchone()
        if row is None:
            raise ValueError("Book is not in the library backlog; crawl its root first")
        if row["state"] == "processing":
            raise ValueError("Cannot change a book while it is processing")
        with self.connect() as db:
            root_row = db.execute("SELECT root_path FROM books WHERE id=?", (row["id"],)).fetchone()
        if excluded_library_path(source, root_row["root_path"]):
            raise ValueError("Sync/version-history directories cannot be queued for processing")
        if not os.path.isfile(source):
            raise ValueError(f"Book is no longer available: {source}")
        inspection = inspect_cbz_eligibility(source)
        if inspection["reason"] == "Archive could not be inspected":
            raise ValueError(f"Cannot inspect CBZ: {inspection['parse_error']}")
        sequence = pending_decensor_methods(inspection, selected_methods, reprocess)
        if not sequence:
            raise ValueError("Selected methods are already recorded; enable reprocess to repeat them")
        stat = os.stat(source)
        now = utc_now()
        with self.connect() as db:
            current = db.execute("SELECT state FROM books WHERE id=?", (row["id"],)).fetchone()
            if current["state"] == "processing":
                raise ValueError("Cannot change a book while it is processing")
            db.execute(
                """UPDATE books SET state='queued', selected_sequence=?, attempts=0,
                   queued_at=?, started_at=NULL, completed_at=NULL, current_stage=NULL,
                   duration_seconds=NULL, last_error=NULL, output_path=NULL,
                   file_size=?, mtime_ns=?, updated_at=?
                   WHERE id=?""",
                (json.dumps(sequence), now, stat.st_size, stat.st_mtime_ns, now, row["id"]),
            )
            for stage in sequence:
                db.execute(
                    """INSERT INTO book_stages(book_id,stage,state,attempt,updated_at)
                       VALUES(?,?,'pending',0,?)
                       ON CONFLICT(book_id,stage) DO UPDATE SET
                          state='pending', attempt=0, started_at=NULL,
                          passed_at=NULL, failed_at=NULL, duration_seconds=NULL, last_error=NULL,
                         updated_at=excluded.updated_at""",
                    (row["id"], stage, now),
                )
            db.execute("DELETE FROM book_phases WHERE book_id=?", (row["id"],))
            self._event(db, "manual_requeue",
                        f"Queued {source} for {' -> '.join(sequence)} (reprocess={reprocess})",
                        book_id=row["id"])
        return sequence

    @staticmethod
    def _progress_rows(db: sqlite3.Connection, books: list[sqlite3.Row]) -> list[dict]:
        if not books:
            return []
        ids = [book["id"] for book in books]
        placeholders = ",".join("?" for _ in ids)
        stages = {
            (row["book_id"], row["stage"]): dict(row)
            for row in db.execute(
                f"SELECT * FROM book_stages WHERE book_id IN ({placeholders})", ids
            )
        }
        result = []
        for book in books:
            selected = json.loads(book["selected_sequence"])
            observed_tags = {str(tag).casefold() for tag in json.loads(book["metadata_tags"] or "[]")}
            item = {key: book[key] for key in (
                "id", "source_path", "state", "attempts", "current_stage",
                "last_error", "started_at", "completed_at", "duration_seconds", "updated_at",
            )}
            item["selected_sequence"] = selected
            item["stages"] = [
                {
                    "stage": stage,
                    "recorded_tag": f"{METHOD_TAG_PREFIX}{stage}" in observed_tags,
                    "state": stages.get((book["id"], stage), {}).get(
                        "state", "pending" if stage in selected else "not_selected"
                    ),
                    "applied_at": stages.get((book["id"], stage), {}).get("applied_at"),
                    "started_at": stages.get((book["id"], stage), {}).get("started_at"),
                    "passed_at": stages.get((book["id"], stage), {}).get("passed_at"),
                    "duration_seconds": stages.get((book["id"], stage), {}).get("duration_seconds"),
                    "last_error": stages.get((book["id"], stage), {}).get("last_error"),
                }
                for stage in DEFAULT_SEQUENCE
            ]
            item["finalization"] = [
                dict(row) for row in db.execute(
                    """SELECT phase,state,started_at,completed_at,duration_seconds,last_error
                       FROM book_phases WHERE book_id=? ORDER BY rowid""",
                    (book["id"],),
                ).fetchall()
            ]
            result.append(item)
        return result

    def book_progress(self, source_path: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM books WHERE source_path=? COLLATE NOCASE", (os.path.abspath(source_path),)
            ).fetchone()
            return self._progress_rows(db, [row])[0] if row else None

    def snapshot(self, event_limit: int = 100) -> dict:
        with self.connect() as db:
            scan = db.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
            counts = {row["state"]: row["count"] for row in db.execute("SELECT state,COUNT(*) AS count FROM books GROUP BY state")}
            current = db.execute("SELECT source_path,current_stage FROM books WHERE state='processing' ORDER BY started_at LIMIT 1").fetchone()
            recent = db.execute(
                """SELECT * FROM books WHERE state IN ('processing','completed','failed')
                   ORDER BY CASE state WHEN 'processing' THEN 0 ELSE 1 END,
                            updated_at DESC, id DESC LIMIT 12"""
            ).fetchall()
            events = [dict(row) for row in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (event_limit,)).fetchall()]
            return {
                "allowed_roots": list(DEFAULT_ROOTS),
                "scan": dict(scan) if scan else None,
                "queue_status": self.queue_control(),
                "batch_schedule": {
                    "enabled": self.schedule_enabled(),
                    "auto_resume": self.scheduled_autostart(),
                    "root": SCHEDULE_ROOT,
                    "timezone": "America/Denver",
                    "windows": "Every day 00:00–05:00; Monday–Friday 09:00–17:00",
                    **processing_window_status(),
                },
                "counts": {name: counts.get(name, 0) for name in ("discovered", "eligible", "queued", "processing", "completed", "failed", "skipped")},
                "current_file": current["source_path"] if current else (scan["current_path"] if scan else None),
                "current_stage": current["current_stage"] if current else None,
                "recent_books": self._progress_rows(db, recent),
                "events": list(reversed(events)),
            }


class LibraryBacklog:
    def __init__(self, store: BacklogStore, processor: Callable,
                 sync_callback: Callable[[], object] | None = None,
                 backup_callback: Callable[[dict | None, dict | None], object] | None = None,
                 require_backup_offload: bool = False,
                 prepare_callback: Callable[[dict], object] | None = None,
                 require_source_registration: bool = False):
        self.store = store
        self.processor = processor
        self.sync_callback = sync_callback
        self.backup_callback = backup_callback
        self.require_backup_offload = require_backup_offload
        self.prepare_callback = prepare_callback
        self.require_source_registration = require_source_registration
        self._scan_threads: dict[int, threading.Thread] = {}
        self._queue_thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()

    @staticmethod
    def validate_root(root: str, allowed_roots: Iterable[str] = DEFAULT_ROOTS) -> str:
        candidate = os.path.normcase(os.path.abspath(root))
        permitted = {os.path.normcase(os.path.abspath(item)) for item in allowed_roots}
        if candidate not in permitted:
            raise ValueError(f"Library root must be one of: {', '.join(allowed_roots)}")
        if not os.path.isdir(root):
            detail = ""
            if os.name == "nt" and os.path.splitdrive(root)[0]:
                detail = (
                    " Mapped drives are visible only in the Windows logon session that launched "
                    "Camelia; reconnect the drive there and restart Camelia."
                )
            raise ValueError(f"Library root is not available: {root}.{detail}")
        return os.path.abspath(root)

    def start_scan(self, root: str, *, validate=True) -> int:
        root = self.validate_root(root) if validate else os.path.abspath(root)
        scan_id = self.store.start_scan(root)
        with self._thread_lock:
            thread = self._scan_threads.get(scan_id)
            if not thread or not thread.is_alive():
                thread = threading.Thread(target=self._crawl_guarded, args=(scan_id, root), daemon=True, name=f"library-scan-{scan_id}")
                self._scan_threads[scan_id] = thread
                try:
                    thread.start()
                except Exception as exc:
                    self._scan_threads.pop(scan_id, None)
                    message = f"Crawl worker could not start for {root}: {type(exc).__name__}: {exc}"
                    LOGGER.exception(message)
                    self.store.set_scan_status(scan_id, "failed", message)
                    raise RuntimeError(message) from exc
        return scan_id

    def pause_scan(self, scan_id: int) -> None:
        self.store.set_scan_status(scan_id, "paused", "Crawl paused at a directory checkpoint")

    def stop_scan(self, scan_id: int) -> None:
        self.store.set_scan_status(scan_id, "stopped", "Crawl stopped; completed directory checkpoints were retained")

    def _crawl_guarded(self, scan_id: int, root: str) -> None:
        """Persist and log exceptions that escape the background worker."""
        try:
            self._crawl(scan_id, root)
        except Exception as exc:
            message = f"Crawl failed for {root}: {type(exc).__name__}: {exc}"
            LOGGER.exception(message)
            try:
                self.store.set_scan_status(scan_id, "failed", message)
            except Exception:
                LOGGER.exception("Could not persist failure state for crawl %s", scan_id)

    def _crawl(self, scan_id: int, root: str) -> None:
        while self.store.scan_status(scan_id) == "running":
            directory = self.store.claim_directory(scan_id)
            if directory is None:
                self.store.set_scan_status(scan_id, "completed", f"Crawl completed: {root}")
                return
            if excluded_library_path(directory, root, directory=True):
                self.store.record_schedule_event("scan_excluded", f"Excluded library history directory: {directory}")
                self.store.finish_directory(scan_id, directory, [])
                continue
            children: list[str] = []
            error = None
            try:
                with os.scandir(directory) as entries:
                    ordered = sorted(entries, key=lambda entry: entry.name.casefold())
                for entry in ordered:
                    if self.store.scan_status(scan_id) != "running":
                        # Revisit the whole small directory on resume. Unchanged
                        # file fingerprints prevent duplicate ZIP reads.
                        self.store.finish_directory(scan_id, directory, [], error="interrupted")
                        with self.store.connect() as db:
                            db.execute("UPDATE scan_directories SET status='pending', error=NULL WHERE scan_id=? AND path=?", (scan_id, directory))
                        return
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if excluded_library_path(entry.path, root, directory=True):
                                self.store.record_schedule_event("scan_excluded", f"Excluded library history directory: {entry.path}")
                            else:
                                children.append(entry.path)
                        elif entry.is_file(follow_symlinks=False) and entry.name.casefold().endswith(".cbz"):
                            self.store.record_archive(scan_id, root, entry.path, entry.stat(follow_symlinks=False), inspect_cbz_eligibility)
                    except OSError as exc:
                        LOGGER.warning("Could not inspect %s: %s", entry.path, exc)
                        with self.store.connect() as db:
                            self.store._event(db, "scan_error", f"Could not inspect {entry.path}: {exc}", scan_id=scan_id)
            except OSError as exc:
                error = f"{type(exc).__name__}: {exc}"
            self.store.finish_directory(scan_id, directory, children, error=error)

    def resume_queue(self, retry_failed=True) -> int:
        if self.require_backup_offload and self.backup_callback is None:
            raise ValueError("Backlog processing requires verified F: backup offload configuration")
        if (self.require_source_registration and self.prepare_callback is None
                and self.store.has_queued_root(SCHEDULE_ROOT, retry_failed)):
            raise ValueError("X:\\comix jobs require ComicAutomation source registration preflight")
        if self.sync_callback is None and self.store.has_queued_root(SCHEDULE_ROOT, retry_failed):
            raise ValueError("X:\\comix jobs require ComicAutomation database handoff configuration")
        if self.store.schedule_enabled() and self.sync_callback is None:
            raise ValueError("Scheduled X:\\comix processing requires ComicAutomation database handoff")
        retried = self.store.retry_failed() if retry_failed else 0
        if self.store.schedule_enabled():
            self.store.control_scheduled_autostart(True)
        self.store.control_queue("running")
        with self._thread_lock:
            if not self._queue_thread or not self._queue_thread.is_alive():
                self._queue_thread = threading.Thread(target=self._process_queue, daemon=True, name="library-backlog")
                self._queue_thread.start()
        return retried

    def set_batch_schedule(self, enabled: bool) -> None:
        """Opt-in scheduled processing; disabling never frees a running job."""
        if enabled and self.require_backup_offload and self.backup_callback is None:
            raise ValueError("Configure verified F: backup offload before enabling batches")
        if enabled and self.require_source_registration and self.prepare_callback is None:
            raise ValueError("Configure ComicAutomation source registration before enabling batches")
        if enabled and self.sync_callback is None:
            raise ValueError("Configure ComicAutomation database handoff before enabling X:\\comix batches")
        self.store.control_schedule(enabled)
        if enabled:
            self.resume_queue(retry_failed=False)
        else:
            self.pause_queue()

    def pause_queue(self) -> None:
        self.store.control_scheduled_autostart(False)
        self.store.control_queue("paused")

    def stop_queue(self) -> None:
        self.store.control_scheduled_autostart(False)
        self.store.control_queue("stopped")

    def _process_queue(self) -> None:
        if self.backup_callback is not None:
            try:
                self.backup_callback(None, None)
            except Exception as exc:
                self.store.record_schedule_event("backup_failure", f"Pending original backup offload failed: {exc}")
                self.pause_queue()
                return
        if self.sync_callback is not None:
            try:
                self.sync_callback()
            except Exception as exc:
                self.store.record_schedule_event("sync_failure", f"ComicAutomation handoff failed before processing: {exc}")
                self.pause_queue()
                return
        waiting_for_window = False
        while self.store.queue_control() == "running":
            scheduled = self.store.schedule_enabled()
            if scheduled:
                window = processing_window_status()
                if not window["in_window"]:
                    if not waiting_for_window:
                        self.store.record_schedule_event(
                            "schedule_wait",
                            f"Waiting for next America/Denver window at {window['next_window_at']}",
                        )
                        waiting_for_window = True
                    time.sleep(1)
                    continue
                if waiting_for_window:
                    self.store.record_schedule_event(
                        "schedule_window_open", "Processing window opened",
                    )
                    waiting_for_window = False
            job = self.store.claim_job(
                root_path=SCHEDULE_ROOT if scheduled else None,
                exclude_root=SCHEDULE_ROOT if self.sync_callback is None else None,
            )
            if job is None:
                # Stay alive while a crawl is still discovering work. This
                # also lets a running queue consume books added by a later
                # scan without another manual resume click.
                time.sleep(0.5)
                continue
            book_id = job["id"]
            book_started = time.perf_counter()
            sequence = json.loads(job["selected_sequence"])
            if excluded_library_path(job["source_path"], job["root_path"]):
                self.store.skip_job(book_id, "Refused sync/version-history path outside the active library")
                continue
            try:
                current = os.stat(job["source_path"])
                if (current.st_size, current.st_mtime_ns) != (job["file_size"], job["mtime_ns"]):
                    raise RuntimeError("Source changed since discovery; recrawl or review an interrupted replacement before retrying")
            except (OSError, RuntimeError) as exc:
                self.store.fail_job(book_id, f"Source preflight failed: {exc}")
                self.pause_queue()
                return
            if self.backup_callback is not None:
                try:
                    self.backup_callback(job, None)
                except Exception as exc:
                    self.store.fail_job(book_id, f"Backup preflight failed: {exc}")
                    self.pause_queue()
                    return
            if self.prepare_callback is not None and job["root_path"].casefold() == SCHEDULE_ROOT.casefold():
                try:
                    self.prepare_callback(job)
                except Exception as exc:
                    self.store.fail_job(book_id, f"ComicAutomation source registration failed: {exc}")
                    self.pause_queue()
                    return
            try:
                current = os.stat(job["source_path"])
                if (current.st_size, current.st_mtime_ns) != (job["file_size"], job["mtime_ns"]):
                    raise RuntimeError("Source changed since discovery; recrawl or review an interrupted replacement before retrying")
            except (OSError, RuntimeError) as exc:
                self.store.fail_job(book_id, f"Source changed during preflight: {exc}")
                self.pause_queue()
                return
            try:
                for index, stage in enumerate(sequence, start=1):
                    self.store.update_stage(book_id, stage, "stage_pending", f"Stage {index}/{len(sequence)} queued: {stage}")
                result = self.processor(job["source_path"], sequence, job)
                output_path = result["path"] if isinstance(result, dict) else str(result)
                inspection = inspect_cbz_eligibility(output_path)
                if not inspection["already_processed"]:
                    raise RuntimeError("Processed archive is missing the uncensored metadata marker")
                if not set(sequence).issubset(inspection["applied_methods"]):
                    raise RuntimeError("Processed archive is missing one or more completed method tags")
                self.store.finish_job(
                    book_id, output_path, time.perf_counter() - book_started
                )
            except Exception as exc:  # worker boundary: persist every failure
                LOGGER.exception("Backlog job failed for %s", job["source_path"])
                self.store.fail_job(book_id, f"{type(exc).__name__}: {exc}")
            else:
                if self.backup_callback is not None:
                    try:
                        self.backup_callback(job, result if isinstance(result, dict) else None)
                    except Exception as exc:
                        self.store.record_schedule_event(
                            "backup_failure", f"Original remains on C: after {output_path}: {exc}"
                        )
                        self.pause_queue()
                        return
                if self.sync_callback is not None:
                    try:
                        self.sync_callback()
                    except Exception as exc:
                        self.store.record_schedule_event(
                            "sync_failure", f"ComicAutomation handoff failed after {output_path}: {exc}"
                        )
                        self.pause_queue()
                        return
        # A quick pause/resume can race the old worker's final loop check.
        # Replace it only if the queue was resumed while this thread exited.
        with self._thread_lock:
            if self._queue_thread is threading.current_thread() and self.store.queue_control() == "running":
                self._queue_thread = threading.Thread(
                    target=self._process_queue, daemon=True, name="library-backlog"
                )
                self._queue_thread.start()
