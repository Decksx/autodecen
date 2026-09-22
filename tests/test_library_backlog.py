from __future__ import annotations

import tempfile
import json
import time
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from library_backlog import (
    BacklogStore,
    LibraryBacklog,
    add_uncensored_tag,
    assert_overwrite_allowed,
    excluded_library_path,
    format_elapsed_duration,
    inspect_cbz_eligibility,
    pending_decensor_methods,
    processing_window_status,
)


def make_cbz(path: Path, comicinfo: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.jpg", b"not-read-by-metadata-inspection")
        archive.writestr("extras/note.txt", b"preserve me")
        if comicinfo is not None:
            archive.writestr("metadata/ComicInfo.xml", comicinfo.encode("utf-8"))


class LibraryBacklogTests(unittest.TestCase):
    def test_known_types_only_mode_is_opt_in_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state" / "backlog.sqlite3"
            store = BacklogStore(str(database))
            self.assertFalse(store.known_types_only())
            store.control_known_types_only(True)
            self.assertTrue(BacklogStore(str(database)).known_types_only())
            self.assertTrue(store.snapshot()["known_types_only"])

    def test_detection_mode_endpoint_persists_setting(self):
        import api

        with tempfile.TemporaryDirectory() as directory:
            store = BacklogStore(str(Path(directory) / "backlog.sqlite3"))
            with patch.object(api.library_backlog, "store", store):
                response = api.app.test_client().post(
                    "/api/library-backlog/known-types-only",
                    json={"enabled": True}, headers={"Origin": "http://localhost:3000"},
                )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(store.known_types_only())

    def test_detected_book_runs_only_matching_pass_and_persists_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            archive = root / "Book.cbz"
            make_cbz(archive)
            database = Path(directory) / "state" / "backlog.sqlite3"
            store = BacklogStore(str(database))
            scan_id = store.start_scan(str(root))
            store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)
            seen = []
            store.control_known_types_only(True)

            def detector(path, _job):
                seen.append(("detect", path))
                return {"page_count": 2, "pages_by_method": {
                    "black_bars": [], "transparent_black": [], "white_bars": [],
                    "mosaic": ["002.jpg"]}, "detected_methods": ["mosaic"]}

            def processor(path, sequence, _job):
                seen.append(("process", sequence))
                add_uncensored_tag(path, sequence)
                return {"path": path}

            manager = LibraryBacklog(store, processor, detector=detector)
            manager.resume_queue(retry_failed=False)
            deadline = time.time() + 3
            while time.time() < deadline and store.book_progress(str(archive))["state"] != "completed":
                time.sleep(0.02)
            manager.pause_queue()
            manager._queue_thread.join(timeout=2)

            self.assertEqual(seen, [("detect", str(archive)), ("process", ["mosaic"])])
            progress = BacklogStore(str(database)).book_progress(str(archive))
            self.assertEqual(progress["selected_sequence"], ["mosaic"])
            self.assertEqual(progress["detected_methods"], ["mosaic"])
            self.assertEqual(progress["detected_pages"]["mosaic"], ["002.jpg"])
            recent = store.snapshot()["recent_books"][0]
            self.assertIsNone(recent["detected_pages"])
            self.assertEqual(recent["detected_page_counts"]["mosaic"], 1)
            self.assertIsNotNone(progress["detection_checked_at"])
            self.assertEqual(progress["stages"][0]["state"], "not_selected")

    def test_no_detected_type_leaves_archive_unchanged_for_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            archive = root / "Book.cbz"
            make_cbz(archive)
            original = archive.read_bytes()
            store = BacklogStore(str(Path(directory) / "state" / "backlog.sqlite3"))
            scan_id = store.start_scan(str(root))
            store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)
            processed = []
            store.control_known_types_only(True)
            manager = LibraryBacklog(
                store, lambda *_: processed.append(True),
                detector=lambda *_: {"page_count": 1, "pages_by_method": {
                    method: [] for method in ("black_bars", "transparent_black", "white_bars", "mosaic")},
                    "detected_methods": []},
            )
            manager.resume_queue(retry_failed=False)
            deadline = time.time() + 3
            while time.time() < deadline and store.book_progress(str(archive))["state"] != "skipped":
                time.sleep(0.02)
            manager.pause_queue()
            manager._queue_thread.join(timeout=2)
            self.assertEqual(processed, [])
            self.assertEqual(archive.read_bytes(), original)
            progress = store.book_progress(str(archive))
            self.assertEqual(progress["detected_methods"], [])
            self.assertEqual(progress["selected_sequence"], [])

    def test_manual_queue_bypasses_automatic_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            archive = root / "Book.cbz"
            make_cbz(archive)
            store = BacklogStore(str(Path(directory) / "state" / "backlog.sqlite3"))
            scan_id = store.start_scan(str(root))
            store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)
            store.requeue_book(str(archive), ["black_bars"])
            store.control_known_types_only(True)
            calls = []

            def processor(path, sequence, _job):
                calls.append(sequence)
                add_uncensored_tag(path, sequence)
                return {"path": path}

            manager = LibraryBacklog(store, processor, detector=lambda *_: self.fail("detector called"))
            manager.resume_queue(retry_failed=False)
            deadline = time.time() + 3
            while time.time() < deadline and store.book_progress(str(archive))["state"] != "completed":
                time.sleep(0.02)
            manager.pause_queue()
            manager._queue_thread.join(timeout=2)
            self.assertEqual(calls, [["black_bars"]])
            self.assertEqual(store.book_progress(str(archive))["auto_detect"], 0)

    def test_detector_failure_pauses_without_treating_book_as_uncensored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            archive = root / "Book.cbz"
            make_cbz(archive)
            store = BacklogStore(str(Path(directory) / "state" / "backlog.sqlite3"))
            scan_id = store.start_scan(str(root))
            store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)
            store.control_known_types_only(True)
            processed = []

            def fail_detection(*_args):
                raise RuntimeError("model unavailable")

            manager = LibraryBacklog(store, lambda *_: processed.append(True), detector=fail_detection)
            manager.resume_queue(retry_failed=False)
            manager._queue_thread.join(timeout=3)
            progress = store.book_progress(str(archive))
            self.assertEqual(progress["state"], "failed")
            self.assertEqual(store.queue_control(), "paused")
            self.assertIsNone(progress["detected_methods"])
            self.assertEqual(processed, [])

    def test_saved_detection_is_reused_after_interrupted_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            archive = root / "Book.cbz"
            make_cbz(archive)
            database = Path(directory) / "state" / "backlog.sqlite3"
            store = BacklogStore(str(database))
            scan_id = store.start_scan(str(root))
            store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)
            store.control_known_types_only(True)
            job = store.claim_job()
            store.save_detection(job["id"], {
                "page_count": 1,
                "pages_by_method": {"black_bars": ["001.jpg"], "transparent_black": [],
                                    "white_bars": [], "mosaic": []},
                "detected_methods": ["black_bars"],
            }, 2.5)
            restarted = BacklogStore(str(database))
            calls = []

            def processor(path, sequence, _job):
                calls.append(sequence)
                add_uncensored_tag(path, sequence)
                return {"path": path}

            manager = LibraryBacklog(restarted, processor, detector=lambda *_: self.fail("audit repeated"))
            manager.resume_queue(retry_failed=False)
            deadline = time.time() + 3
            while time.time() < deadline and restarted.book_progress(str(archive))["state"] != "completed":
                time.sleep(0.02)
            manager.pause_queue()
            manager._queue_thread.join(timeout=2)
            self.assertEqual(calls, [["black_bars"]])
            self.assertEqual(restarted.book_progress(str(archive))["detected_methods"], ["black_bars"])

    def test_elapsed_duration_formatting(self):
        self.assertEqual(format_elapsed_duration(0), "0s")
        self.assertEqual(format_elapsed_duration(377), "6m 17s")
        self.assertEqual(format_elapsed_duration(7384), "2h 3m 4s")

    def test_crawl_excludes_sync_history_folders_even_from_old_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            visible = root / "Series" / "Book.cbz"
            history = root / ".stversions" / "Series" / "Old.cbz"
            make_cbz(visible)
            make_cbz(history)
            self.assertTrue(excluded_library_path(str(history), str(root)))
            self.assertFalse(excluded_library_path(str(visible), str(root)))
            store = BacklogStore(str(Path(directory) / "state" / "backlog.sqlite3"))
            manager = LibraryBacklog(store, lambda *_: None)
            scan_id = manager.start_scan(str(root), validate=False)
            deadline = time.time() + 3
            while time.time() < deadline and store.scan_status(scan_id) != "completed":
                time.sleep(0.02)
            self.assertEqual(store.scan_status(scan_id), "completed")
            self.assertIsNone(store.snapshot()["current_file"])
            with store.connect() as db:
                paths = [row[0] for row in db.execute("SELECT source_path FROM books")]
            self.assertEqual(paths, [str(visible)])

    def test_batch_windows_use_denver_weekdays_and_every_night(self):
        zone = ZoneInfo("America/Denver")
        self.assertTrue(processing_window_status(datetime(2026, 9, 16, 10, tzinfo=zone))["in_window"])
        self.assertFalse(processing_window_status(datetime(2026, 9, 19, 10, tzinfo=zone))["in_window"])
        self.assertTrue(processing_window_status(datetime(2026, 9, 20, 2, tzinfo=zone))["in_window"])
        after_hours = processing_window_status(datetime(2026, 9, 18, 18, tzinfo=zone))
        self.assertFalse(after_hours["in_window"])
        self.assertEqual(after_hours["next_window_at"], "2026-09-19T00:00:00-06:00")

    def test_schedule_setting_survives_restart_and_claims_only_selected_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first_root = base / "first"
            other_root = base / "other"
            first = first_root / "First.cbz"
            other = other_root / "Other.cbz"
            make_cbz(first)
            make_cbz(other)
            database = base / "state" / "backlog.sqlite3"
            store = BacklogStore(str(database))
            for root, archive in ((first_root, first), (other_root, other)):
                scan_id = store.start_scan(str(root))
                store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)
            store.control_schedule(True)
            recovered = BacklogStore(str(database))
            self.assertTrue(recovered.schedule_enabled())
            self.assertEqual(recovered.claim_job(root_path=str(other_root))["source_path"], str(other))
            self.assertEqual(recovered.claim_job(root_path=str(other_root)), None)
            self.assertEqual(recovered.claim_job(root_path=str(first_root))["source_path"], str(first))

    def test_scheduled_worker_waits_outside_window_then_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            archive = root / "Book.cbz"
            make_cbz(archive)
            store = BacklogStore(str(Path(directory) / "state" / "backlog.sqlite3"))
            scan_id = store.start_scan(str(root))
            store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)
            window_open = [False]

            def window_status():
                return {"in_window": window_open[0], "next_window_at": "test window"}

            def processor(path, sequence, _job):
                add_uncensored_tag(path, sequence)
                return {"path": path}

            manager = LibraryBacklog(store, processor, sync_callback=lambda: None)
            with patch("library_backlog.SCHEDULE_ROOT", str(root)), patch(
                "library_backlog.processing_window_status", side_effect=window_status
            ):
                manager.set_batch_schedule(True)
                time.sleep(0.1)
                self.assertEqual(store.snapshot()["counts"]["queued"], 1)
                window_open[0] = True
                deadline = time.time() + 3
                while time.time() < deadline and store.snapshot()["counts"]["completed"] != 1:
                    time.sleep(0.02)
                self.assertEqual(store.snapshot()["counts"]["completed"], 1)
                manager.pause_queue()
                manager._queue_thread.join(timeout=2)
                self.assertTrue(store.schedule_enabled())
                self.assertFalse(store.scheduled_autostart())

    def test_x_queue_refuses_to_start_without_database_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            archive = root / "Book.cbz"
            make_cbz(archive)
            store = BacklogStore(str(Path(directory) / "state" / "backlog.sqlite3"))
            scan_id = store.start_scan(str(root))
            store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)
            manager = LibraryBacklog(store, lambda *_: None)
            with patch("library_backlog.SCHEDULE_ROOT", str(root)):
                with self.assertRaisesRegex(ValueError, "handoff"):
                    manager.resume_queue()
                with self.assertRaisesRegex(ValueError, "handoff"):
                    manager.set_batch_schedule(True)
            self.assertEqual(store.snapshot()["counts"]["queued"], 1)
            self.assertFalse(store.schedule_enabled())

    def test_queue_refuses_without_required_backup_offload(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BacklogStore(str(Path(directory) / "backlog.sqlite3"))
            manager = LibraryBacklog(store, lambda *_: None,
                                     sync_callback=lambda: None, require_backup_offload=True)
            with self.assertRaisesRegex(ValueError, "backup offload"):
                manager.resume_queue()
            with self.assertRaisesRegex(ValueError, "backup offload"):
                manager.set_batch_schedule(True)
            self.assertFalse(store.schedule_enabled())

    def test_resume_rejects_unavailable_root_before_retrying_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            archive = root / "Book.cbz"
            make_cbz(archive)
            store = BacklogStore(str(Path(directory) / "state" / "backlog.sqlite3"))
            scan_id = store.start_scan(str(root))
            store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)
            manager = LibraryBacklog(store, lambda *_: None)

            with patch("library_backlog.os.path.isdir", return_value=False):
                with self.assertRaisesRegex(ValueError, "unavailable to Camelia"):
                    manager.resume_queue()

            progress = store.book_progress(str(archive))
            self.assertEqual(progress["attempts"], 0)
            self.assertEqual(progress["state"], "queued")
            self.assertEqual(store.queue_control(), "paused")
            self.assertIsNone(manager._queue_thread)

    def test_missing_book_does_not_stop_other_queued_books(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            missing = root / "Missing.cbz"
            available = root / "Available.cbz"
            make_cbz(missing)
            make_cbz(available)
            store = BacklogStore(str(Path(directory) / "state" / "backlog.sqlite3"))
            scan_id = store.start_scan(str(root))
            for archive in (missing, available):
                store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)
            missing.unlink()

            def processor(path, sequence, _job):
                add_uncensored_tag(path, sequence)
                return {"path": path}

            manager = LibraryBacklog(store, processor)
            manager.resume_queue(retry_failed=False)
            deadline = time.time() + 3
            while time.time() < deadline and store.book_progress(str(available))["state"] != "completed":
                time.sleep(0.02)
            manager.pause_queue()
            manager._queue_thread.join(timeout=2)

            self.assertEqual(store.book_progress(str(missing))["state"], "failed")
            self.assertEqual(store.book_progress(str(available))["state"], "completed")
            self.assertTrue(any(event["event_type"] == "job_failed" for event in store.snapshot()["events"]))

    def test_unavailable_root_after_claim_does_not_consume_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            archive = root / "Book.cbz"
            make_cbz(archive)
            store = BacklogStore(str(Path(directory) / "state" / "backlog.sqlite3"))
            scan_id = store.start_scan(str(root))
            store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)
            job = store.claim_job()
            manager = LibraryBacklog(store, lambda *_: None)

            with patch("library_backlog.os.path.isdir", return_value=False):
                paused = manager._handle_source_access_error(
                    job, FileNotFoundError("mapped drive unavailable"), "Source preflight failed"
                )

            self.assertTrue(paused)
            progress = store.book_progress(str(archive))
            self.assertEqual(progress["attempts"], 0)
            self.assertEqual(progress["state"], "queued")
            self.assertEqual(store.queue_control(), "paused")
            self.assertTrue(any(event["event_type"] == "job_deferred" for event in store.snapshot()["events"]))

    def test_x_source_registration_precedes_processor_and_failure_pauses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            archive = root / "Book.cbz"
            make_cbz(archive)
            store = BacklogStore(str(Path(directory) / "state" / "backlog.sqlite3"))
            scan_id = store.start_scan(str(root))
            store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)
            calls = []

            def prepare(_job):
                calls.append("register")
                raise RuntimeError("database unavailable")

            manager = LibraryBacklog(store, lambda *_: calls.append("process"),
                                     sync_callback=lambda: None, prepare_callback=prepare,
                                     require_source_registration=True)
            with patch("library_backlog.SCHEDULE_ROOT", str(root)):
                manager.resume_queue(retry_failed=False)
                deadline = time.time() + 3
                while time.time() < deadline and store.queue_control() != "paused":
                    time.sleep(0.02)
            self.assertEqual(calls, ["register"])
            self.assertEqual(store.snapshot()["counts"]["failed"], 1)
            manager._queue_thread.join(timeout=2)

    def test_x_source_registration_runs_before_model_and_history_job_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            archive = root / "Series" / "Book.cbz"
            history = root / ".stversions" / "History.cbz"
            make_cbz(archive)
            make_cbz(history)
            store = BacklogStore(str(Path(directory) / "state" / "backlog.sqlite3"))
            scan_id = store.start_scan(str(root))
            store.record_archive(scan_id, str(root), str(history), history.stat(), inspect_cbz_eligibility)
            store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)
            calls = []

            def processor(path, sequence, _job):
                calls.append("process")
                add_uncensored_tag(path, sequence)
                return {"path": path}

            manager = LibraryBacklog(store, processor, sync_callback=lambda: None,
                                     prepare_callback=lambda _job: calls.append("register"),
                                     require_source_registration=True)
            with patch("library_backlog.SCHEDULE_ROOT", str(root)):
                manager.resume_queue(retry_failed=False)
                deadline = time.time() + 3
                while time.time() < deadline and store.snapshot()["counts"]["completed"] != 1:
                    time.sleep(0.02)
                manager.pause_queue()
            manager._queue_thread.join(timeout=2)
            self.assertEqual(calls, ["register", "process"])
            self.assertEqual(store.snapshot()["counts"]["skipped"], 1)

    def test_handoff_failure_after_completion_pauses_without_reprocessing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            archive = root / "Book.cbz"
            make_cbz(archive)
            store = BacklogStore(str(Path(directory) / "state" / "backlog.sqlite3"))
            scan_id = store.start_scan(str(root))
            store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)
            calls = []

            def processor(path, sequence, _job):
                calls.append("process")
                add_uncensored_tag(path, sequence)
                return {"path": path}

            sync_calls = []

            def sync():
                sync_calls.append("sync")
                if len(sync_calls) == 2:
                    raise RuntimeError("database unavailable")

            manager = LibraryBacklog(store, processor, sync_callback=sync)
            manager.resume_queue(retry_failed=False)
            deadline = time.time() + 3
            while time.time() < deadline and store.queue_control() != "paused":
                time.sleep(0.02)
            snapshot = store.snapshot()
            self.assertEqual(snapshot["counts"]["completed"], 1)
            self.assertEqual(snapshot["counts"]["failed"], 0)
            self.assertEqual(calls, ["process"])
            self.assertTrue(any(event["event_type"] == "sync_failure" for event in snapshot["events"]))
            manager._queue_thread.join(timeout=2)

    def test_backup_offload_failure_after_completion_pauses_without_reprocessing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            archive = root / "Book.cbz"
            make_cbz(archive)
            store = BacklogStore(str(Path(directory) / "state" / "backlog.sqlite3"))
            scan_id = store.start_scan(str(root))
            store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)
            calls = []

            def processor(path, sequence, _job):
                calls.append("process")
                add_uncensored_tag(path, sequence)
                return {"path": path, "backup_path": str(Path(directory) / "original.cbz")}

            def offload(_job, result):
                if result is not None:
                    raise OSError("F: unavailable")

            manager = LibraryBacklog(store, processor, sync_callback=lambda: None,
                                     backup_callback=offload, require_backup_offload=True)
            manager.resume_queue(retry_failed=False)
            deadline = time.time() + 3
            while time.time() < deadline and store.queue_control() != "paused":
                time.sleep(0.02)
            snapshot = store.snapshot()
            self.assertEqual(snapshot["counts"]["completed"], 1)
            self.assertEqual(calls, ["process"])
            self.assertTrue(any(event["event_type"] == "backup_failure" for event in snapshot["events"]))
            manager._queue_thread.join(timeout=2)

    def test_changed_source_refuses_to_run_processor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            archive = root / "Book.cbz"
            make_cbz(archive)
            store = BacklogStore(str(Path(directory) / "state" / "backlog.sqlite3"))
            scan_id = store.start_scan(str(root))
            store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)
            make_cbz(archive, "<ComicInfo><Title>Changed</Title></ComicInfo>")
            calls = []
            manager = LibraryBacklog(store, lambda *_: calls.append("process"),
                                     sync_callback=lambda: None,
                                     prepare_callback=lambda _job: calls.append("register"),
                                     require_source_registration=True)
            with patch("library_backlog.SCHEDULE_ROOT", str(root)):
                manager.resume_queue(retry_failed=False)
            deadline = time.time() + 3
            while time.time() < deadline and store.snapshot()["counts"]["failed"] != 1:
                time.sleep(0.02)
            self.assertEqual(calls, [])
            self.assertIn("Source changed", next(event["message"] for event in store.snapshot()["events"]
                                                     if event["event_type"] == "job_failed"))
            manager.pause_queue()
            manager._queue_thread.join(timeout=2)

    def test_eligibility_uses_filename_title_and_tags_case_insensitively(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plain = root / "Plain.cbz"
            tagged = root / "Tagged.cbz"
            titled = root / "Titled.cbz"
            named = root / "Already DECENSORED.cbz"
            make_cbz(plain, "<ComicInfo><Title>Plain</Title><Tags>English, Color</Tags></ComicInfo>")
            make_cbz(tagged, "<ComicInfo><Tags>Color; UnCenSored</Tags></ComicInfo>")
            make_cbz(titled, "<ComicInfo><Title>Special decensored edition</Title></ComicInfo>")
            make_cbz(named)

            self.assertTrue(inspect_cbz_eligibility(str(plain))["eligible"])
            self.assertFalse(inspect_cbz_eligibility(str(tagged))["eligible"])
            self.assertFalse(inspect_cbz_eligibility(str(titled))["eligible"])
            self.assertFalse(inspect_cbz_eligibility(str(named))["eligible"])

    def test_tagging_preserves_metadata_is_idempotent_and_can_create_minimal_xml(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "Existing.cbz"
            missing = root / "Missing.cbz"
            make_cbz(existing, "<ComicInfo><Series>Example</Series><Tags>Color</Tags></ComicInfo>")
            make_cbz(missing)

            first = add_uncensored_tag(str(existing))
            second = add_uncensored_tag(str(existing))
            created = add_uncensored_tag(str(missing))

            self.assertTrue(first["changed"])
            self.assertFalse(first["created"])
            self.assertFalse(second["changed"])
            self.assertTrue(created["created"])
            for archive_path in (existing, missing):
                with zipfile.ZipFile(archive_path) as archive:
                    self.assertEqual(archive.read("extras/note.txt"), b"preserve me")
                    member = next(name for name in archive.namelist() if name.lower().endswith("comicinfo.xml"))
                    xml = ElementTree.fromstring(archive.read(member))
                    tags = next(element for element in xml.iter() if element.tag.lower().endswith("tags"))
                    self.assertEqual((tags.text or "").casefold().count("uncensored"), 1)

    def test_method_tags_allow_only_missing_methods_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "Already Uncensored.cbz"
            make_cbz(archive, "<ComicInfo><Title>Book</Title><Tags>Color</Tags></ComicInfo>")
            add_uncensored_tag(str(archive), ["black_bars"])
            add_uncensored_tag(str(archive), ["black_bars"])
            inspection = inspect_cbz_eligibility(str(archive))
            self.assertEqual(inspection["applied_methods"], ["black_bars"])
            self.assertEqual(pending_decensor_methods(inspection, ["black_bars", "mosaic"]), ["mosaic"])
            self.assertEqual(pending_decensor_methods(inspection, ["black_bars"]), [])
            self.assertEqual(pending_decensor_methods(inspection, ["black_bars"], True), ["black_bars"])
            with zipfile.ZipFile(archive) as cbz:
                tags = ElementTree.fromstring(cbz.read("metadata/ComicInfo.xml")).findtext("Tags")
            self.assertEqual(tags.casefold().count("camelia:black_bars"), 1)
            self.assertIn("Color", tags)

    def test_per_book_stage_markers_distinguish_finished_pass_from_applied_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            archive = root / "Book.cbz"
            make_cbz(archive)
            database = Path(directory) / "state" / "backlog.sqlite3"
            store = BacklogStore(str(database))
            scan_id = store.start_scan(str(root))
            store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)
            job = store.claim_job()
            sequence = json.loads(job["selected_sequence"])
            for stage in sequence:
                store.update_stage(job["id"], stage, "stage_pending", f"Pending {stage}")
            self.assertIsNone(store.snapshot()["current_stage"])

            first = sequence[0]
            store.update_stage(job["id"], first, "stage_start", f"Starting {first}")
            self.assertEqual(store.snapshot()["current_stage"], first)
            store.update_stage(
                job["id"], first, "stage_complete", f"Completed {first}", 377.0
            )
            progress = store.book_progress(str(archive))
            self.assertEqual(progress["stages"][0]["state"], "passed")
            self.assertIsNone(progress["stages"][0]["applied_at"])
            self.assertEqual(progress["stages"][0]["duration_seconds"], 377.0)

            for stage in sequence[1:]:
                store.update_stage(job["id"], stage, "stage_start", f"Starting {stage}")
                store.update_stage(job["id"], stage, "stage_complete", f"Completed {stage}")
            add_uncensored_tag(str(archive), sequence)
            store.finish_job(job["id"], str(archive))
            restarted = BacklogStore(str(database))
            progress = restarted.book_progress(str(archive))
            self.assertEqual(progress["state"], "completed")
            self.assertEqual(progress["stages"][0]["duration_seconds"], 377.0)
            self.assertTrue(all(stage["state"] == "completed" and stage["applied_at"]
                                for stage in progress["stages"]))
            self.assertEqual(restarted.snapshot()["recent_books"][0]["id"], job["id"])
            restarted.requeue_book(str(archive), [first], reprocess=True)
            progress = restarted.book_progress(str(archive))
            self.assertEqual(progress["stages"][0]["state"], "pending")
            self.assertIsNotNone(progress["stages"][0]["applied_at"])
            self.assertEqual(progress["stages"][1]["state"], "completed")

    def test_finalization_timing_and_total_duration_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            archive = root / "Book.cbz"
            make_cbz(archive)
            database = Path(directory) / "state" / "backlog.sqlite3"
            store = BacklogStore(str(database))
            scan_id = store.start_scan(str(root))
            store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)
            job = store.claim_job()
            sequence = json.loads(job["selected_sequence"])
            for stage in sequence:
                store.update_stage(job["id"], stage, "stage_start", f"Starting {stage}")
                store.update_stage(job["id"], stage, "stage_complete", f"Completed {stage}", 1.0)
            store.update_phase(job["id"], "cbz_rebuild", "phase_start", "Finalization start: CBZ rebuild")
            store.update_phase(
                job["id"], "cbz_rebuild", "phase_complete",
                "Finalization complete: CBZ rebuild (12s)", 12.0,
            )
            add_uncensored_tag(str(archive), sequence)
            store.finish_job(job["id"], str(archive), 42.0)

            progress = BacklogStore(str(database)).book_progress(str(archive))
            self.assertEqual(progress["duration_seconds"], 42.0)
            self.assertEqual(progress["finalization"][0]["phase"], "cbz_rebuild")
            self.assertEqual(progress["finalization"][0]["duration_seconds"], 12.0)

    def test_older_completed_backlog_gets_verified_stage_marker_backfill(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            archive = root / "Book.cbz"
            make_cbz(archive)
            database = Path(directory) / "state" / "backlog.sqlite3"
            store = BacklogStore(str(database))
            scan_id = store.start_scan(str(root))
            store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)
            job = store.claim_job()
            sequence = json.loads(job["selected_sequence"])
            add_uncensored_tag(str(archive), sequence)
            store.finish_job(job["id"], str(archive))
            with store.connect() as db:
                db.execute("DELETE FROM book_stages WHERE book_id=?", (job["id"],))
                db.execute("DELETE FROM controls WHERE name='stage_marker_backfill_v1'")
            restarted = BacklogStore(str(database))
            stages = restarted.book_progress(str(archive))["stages"]
            self.assertTrue(all(stage["state"] == "completed" and stage["applied_at"]
                                for stage in stages))

    def test_stage_failure_and_restart_keep_accurate_attempt_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            archive = root / "Book.cbz"
            make_cbz(archive)
            database = Path(directory) / "state" / "backlog.sqlite3"
            store = BacklogStore(str(database))
            scan_id = store.start_scan(str(root))
            store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)
            job = store.claim_job()
            sequence = json.loads(job["selected_sequence"])
            store.update_stage(job["id"], sequence[0], "stage_start", "Starting first")
            store.update_stage(job["id"], sequence[0], "stage_complete", "Completed first")
            store.update_stage(job["id"], sequence[1], "stage_start", "Starting second")
            store.update_stage(job["id"], sequence[1], "stage_failure", "Failed second: model error")
            store.fail_job(job["id"], "model error")
            restarted = BacklogStore(str(database))
            stages = restarted.book_progress(str(archive))["stages"]
            self.assertEqual([stage["state"] for stage in stages[:2]], ["passed", "failed"])
            self.assertFalse(any(stage["applied_at"] for stage in stages))
            self.assertIn("model error", stages[1]["last_error"])

            restarted.retry_failed()
            restarted.claim_job()
            restarted.update_stage(job["id"], sequence[0], "stage_pending", "Retry pending")
            self.assertEqual(restarted.book_progress(str(archive))["stages"][0]["state"], "pending")

    def test_interrupted_running_stage_is_not_reported_as_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            archive = root / "Book.cbz"
            make_cbz(archive)
            database = Path(directory) / "state" / "backlog.sqlite3"
            store = BacklogStore(str(database))
            scan_id = store.start_scan(str(root))
            store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)
            job = store.claim_job()
            store.update_stage(job["id"], "black_bars", "stage_start", "Starting black bars")
            restarted = BacklogStore(str(database))
            stage = restarted.book_progress(str(archive))["stages"][0]
            self.assertEqual(stage["state"], "interrupted")
            self.assertIsNone(stage["applied_at"])

    def test_book_progress_endpoint_exposes_stage_markers(self):
        import api

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            archive = root / "Book.cbz"
            make_cbz(archive)
            store = BacklogStore(str(Path(directory) / "state" / "backlog.sqlite3"))
            scan_id = store.start_scan(str(root))
            store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)
            with patch.object(api.library_backlog, "store", store):
                response = api.app.test_client().get(
                    "/api/library-backlog/book-progress",
                    query_string={"source_path": str(archive)},
                    headers={"Origin": "http://localhost:3000"},
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["source_path"], str(archive))
            self.assertEqual(len(response.json["stages"]), 4)

    def test_refused_crawl_start_is_saved_in_event_history(self):
        import api

        with tempfile.TemporaryDirectory() as directory:
            store = BacklogStore(str(Path(directory) / "state" / "backlog.sqlite3"))
            with patch.object(api.library_backlog, "store", store), patch.object(
                api.library_backlog, "start_scan", side_effect=ValueError("test root unavailable")
            ):
                response = api.app.test_client().post(
                    "/api/library-backlog/scan/start",
                    json={"root": r"Z:\missing"},
                    headers={"Origin": "http://localhost:3000"},
                )

            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json()["error"], "test root unavailable")
            events = store.snapshot()["events"]
            self.assertTrue(any(
                event["event_type"] == "scan_start_refused"
                and event["message"] == "test root unavailable"
                for event in events
            ))

    def test_legacy_generic_marker_requires_override(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "Legacy.cbz"
            make_cbz(archive, "<ComicInfo><Tags>uncensored</Tags></ComicInfo>")
            inspection = inspect_cbz_eligibility(str(archive))
            self.assertEqual(pending_decensor_methods(inspection, ["mosaic"]), [])
            self.assertEqual(pending_decensor_methods(inspection, ["mosaic"], True), ["mosaic"])

    def test_backlog_queues_only_methods_not_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            archive = root / "Book.cbz"
            make_cbz(archive, "<ComicInfo><Tags>uncensored, camelia:black_bars</Tags></ComicInfo>")
            store = BacklogStore(str(Path(directory) / "state" / "backlog.sqlite3"))
            scan_id = store.start_scan(str(root))
            self.assertEqual(store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility), "queued")
            job = store.claim_job()
            self.assertEqual(json.loads(job["selected_sequence"]), ["transparent_black", "white_bars", "mosaic"])
            progress = store.book_progress(str(archive))
            self.assertEqual(progress["stages"][0]["state"], "not_selected")
            self.assertTrue(progress["stages"][0]["recorded_tag"])

    def test_manual_backlog_override_can_requeue_legacy_book(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            archive = root / "Legacy.cbz"
            make_cbz(archive, "<ComicInfo><Tags>uncensored</Tags></ComicInfo>")
            store = BacklogStore(str(Path(directory) / "state" / "backlog.sqlite3"))
            scan_id = store.start_scan(str(root))
            self.assertEqual(store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility), "skipped")
            with self.assertRaisesRegex(ValueError, "enable reprocess"):
                store.requeue_book(str(archive), ["mosaic"])
            self.assertEqual(store.requeue_book(str(archive), ["mosaic"], True), ["mosaic"])
            self.assertEqual(json.loads(store.claim_job()["selected_sequence"]), ["mosaic"])

    def test_unchanged_rescan_does_not_reopen_archive_or_duplicate_same_scan_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "Book.cbz"
            make_cbz(archive, "<ComicInfo><Title>Book</Title></ComicInfo>")
            store = BacklogStore(str(root / "state" / "backlog.sqlite3"))
            calls = []

            def inspector(path):
                calls.append(path)
                return inspect_cbz_eligibility(path)

            stat = archive.stat()
            first_scan = store.start_scan(str(root))
            store.record_archive(first_scan, str(root), str(archive), stat, inspector)
            store.record_archive(first_scan, str(root), str(archive), stat, inspector)
            store.set_scan_status(first_scan, "completed", "done")
            second_scan = store.start_scan(str(root))
            store.record_archive(second_scan, str(root), str(archive), stat, inspector)

            self.assertEqual(len(calls), 1)
            with store.connect() as db:
                first = db.execute("SELECT total_found FROM scans WHERE id=?", (first_scan,)).fetchone()[0]
                second = db.execute("SELECT total_found,scanned_count FROM scans WHERE id=?", (second_scan,)).fetchone()
            self.assertEqual(first, 1)
            self.assertEqual(tuple(second), (1, 0))

    def test_restart_recovers_directory_checkpoint_and_processing_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "Book.cbz"
            make_cbz(archive)
            database = root / "state" / "backlog.sqlite3"
            store = BacklogStore(str(database))
            scan_id = store.start_scan(str(root))
            self.assertEqual(store.claim_directory(scan_id), str(root))
            store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)
            job = store.claim_job()
            self.assertEqual(job["state"], "processing")

            recovered = BacklogStore(str(database))
            self.assertEqual(recovered.scan_status(scan_id), "paused")
            with recovered.connect() as db:
                directory_state = db.execute("SELECT status FROM scan_directories WHERE scan_id=?", (scan_id,)).fetchone()[0]
                job_state = db.execute("SELECT state,last_error FROM books WHERE id=?", (job["id"],)).fetchone()
            self.assertEqual(directory_state, "pending")
            self.assertEqual(job_state[0], "queued")
            self.assertIn("interrupted", job_state[1].lower())

    def test_existing_default_queue_gets_mosaic_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "Book.cbz"
            make_cbz(archive)
            database = root / "state" / "backlog.sqlite3"
            store = BacklogStore(str(database))
            scan_id = store.start_scan(str(root))
            store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)
            with store.connect() as db:
                db.execute(
                    "UPDATE books SET selected_sequence=?, eligibility_version=1 WHERE source_path=?",
                    (json.dumps(["black_bars", "transparent_black", "white_bars"]), str(archive)),
                )
            recovered = BacklogStore(str(database))
            with recovered.connect() as db:
                sequence = db.execute("SELECT selected_sequence FROM books WHERE source_path=?", (str(archive),)).fetchone()[0]
            self.assertEqual(json.loads(sequence)[-1], "mosaic")

    def test_pause_and_resume_finishes_from_persisted_directory_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            make_cbz(root / "A" / "One.cbz")
            make_cbz(root / "B" / "Two.cbz")
            store = BacklogStore(str(Path(directory) / "state" / "backlog.sqlite3"))
            manager = LibraryBacklog(store, lambda *_: None)
            scan_id = manager.start_scan(str(root), validate=False)
            manager.pause_scan(scan_id)
            deadline = time.time() + 2
            while time.time() < deadline and any(thread.is_alive() for thread in manager._scan_threads.values()):
                time.sleep(0.01)
            self.assertEqual(store.scan_status(scan_id), "paused")

            manager.start_scan(str(root), validate=False)
            deadline = time.time() + 3
            while time.time() < deadline and store.scan_status(scan_id) != "completed":
                time.sleep(0.01)
            self.assertEqual(store.scan_status(scan_id), "completed")
            self.assertEqual(store.snapshot()["counts"]["queued"], 2)

    def test_unexpected_crawl_exception_is_persisted_as_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            root.mkdir()
            store = BacklogStore(str(Path(directory) / "state" / "backlog.sqlite3"))
            manager = LibraryBacklog(store, lambda *_: None)

            with patch.object(manager, "_crawl", side_effect=RuntimeError("test crash")):
                scan_id = manager.start_scan(str(root), validate=False)
                manager._scan_threads[scan_id].join(timeout=2)

            self.assertEqual(store.scan_status(scan_id), "failed")
            events = store.snapshot()["events"]
            self.assertTrue(any(
                event["event_type"] == "scan_failed" and "test crash" in event["message"]
                for event in events
            ))

    def test_thread_start_failure_does_not_leave_crawl_running(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            root.mkdir()
            store = BacklogStore(str(Path(directory) / "state" / "backlog.sqlite3"))
            manager = LibraryBacklog(store, lambda *_: None)

            with patch("library_backlog.threading.Thread.start", side_effect=RuntimeError("test start failure")):
                with self.assertRaisesRegex(RuntimeError, "could not start"):
                    manager.start_scan(str(root), validate=False)

            self.assertEqual(store.snapshot()["scan"]["status"], "failed")

    def test_overwrite_guard_refuses_unmarked_incoming_for_processed_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "destination.cbz"
            incoming = root / "incoming.cbz"
            accepted = root / "incoming uncensored.cbz"
            make_cbz(destination, "<ComicInfo><Tags>uncensored</Tags></ComicInfo>")
            make_cbz(incoming, "<ComicInfo><Tags>color</Tags></ComicInfo>")
            make_cbz(accepted, "<ComicInfo><Tags>color</Tags></ComicInfo>")
            with self.assertRaisesRegex(PermissionError, "Refusing to overwrite"):
                assert_overwrite_allowed(str(incoming), str(destination))
            assert_overwrite_allowed(str(accepted), str(destination))

    def test_successful_queue_job_requires_and_records_tagged_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comix"
            archive = root / "Book.cbz"
            make_cbz(archive)
            store = BacklogStore(str(Path(directory) / "state" / "backlog.sqlite3"))
            scan_id = store.start_scan(str(root))
            store.record_archive(scan_id, str(root), str(archive), archive.stat(), inspect_cbz_eligibility)

            def processor(path, sequence, job):
                self.assertEqual(sequence, ["black_bars", "transparent_black", "white_bars", "mosaic"])
                add_uncensored_tag(path, sequence)
                return {"path": path}

            manager = LibraryBacklog(store, processor)
            manager.resume_queue(retry_failed=False)
            deadline = time.time() + 3
            while time.time() < deadline and store.snapshot()["counts"]["completed"] != 1:
                time.sleep(0.01)
            snapshot = store.snapshot()
            self.assertEqual(snapshot["counts"]["completed"], 1)
            self.assertTrue(inspect_cbz_eligibility(str(archive))["already_processed"])
            self.assertTrue(any(event["event_type"] == "job_complete" for event in snapshot["events"]))
            manager.pause_queue()
            manager._queue_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
