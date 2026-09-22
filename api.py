import os
import sys
import uuid
import shutil
import copy
import queue
import threading
import io
import tempfile
import subprocess
import time
import psutil
import re
import zipfile
import ipaddress
import json
import traceback
from collections import deque
from datetime import datetime
from contextlib import contextmanager
from urllib.parse import urlsplit
from flask import Flask, request, jsonify, send_file, Response, stream_with_context
from flask_cors import CORS
from werkzeug.utils import secure_filename
from PIL import Image, ImageCms
from library_backlog import (
    BacklogStore,
    DEFAULT_SEQUENCE,
    LibraryBacklog,
    add_uncensored_tag,
    assert_overwrite_allowed,
    format_elapsed_duration,
    inspect_cbz_eligibility,
    pending_decensor_methods,
)
from aletheia_integration import validate_aletheia_runtime

app = Flask(__name__, static_folder=None)
CORS(app)  # Enable CORS for all routes

WORKSPACE_ROOT = os.path.dirname(os.path.abspath(__file__))
CAMELIA_TEMP = os.path.join(WORKSPACE_ROOT, "camelia-decensor", "temp")
CAMELIA_OUTPUT = os.path.join(WORKSPACE_ROOT, "camelia-decensor", "output")
IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'avif'}
ARCHIVE_EXTENSIONS = {'cbz'}
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | ARCHIVE_EXTENSIONS
MAX_ARCHIVE_IMAGES = 5000
MAX_ARCHIVE_IMAGE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
MAX_BATCH_ARCHIVES = 1000
MODEL_TYPES = ('black_bars', 'transparent_black', 'white_bars', 'mosaic')

# Global dictionary to store process logs
process_logs = {}
process_status = {}
processing_lock = threading.Lock()
active_processes = {}
process_log_listeners = {}


def normalize_model_types(model_types):
    """Validate an ordered model sequence and remove repeated stages."""
    if isinstance(model_types, str):
        model_types = [model_types]

    selected = []
    for model_type in model_types or []:
        model_type = str(model_type).strip()
        if model_type not in MODEL_TYPES:
            raise ValueError(f"Invalid model type: {model_type}")
        if model_type not in selected:
            selected.append(model_type)
    if not selected:
        raise ValueError("Select at least one processing type")
    return selected


def job_log(session_id, step, message):
    """Write one consistently formatted, useful job log entry."""
    timestamp = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{timestamp}] [{step}] {message}"
    log_queue = process_logs.get(session_id)
    if log_queue is not None:
        log_queue.put(line)
    listener = process_log_listeners.get(session_id)
    if listener is not None:
        try:
            listener(step, message)
        except Exception:
            app.logger.exception("Processing log listener failed for %s", session_id)
    return line


@contextmanager
def timed_job_step(session_id, phase, label):
    """Log a named finalization phase with its elapsed duration."""
    started = time.perf_counter()
    job_log(session_id, f'FINALIZE:{phase}', f'Starting {label}')
    try:
        yield
    except Exception as exc:
        duration = time.perf_counter() - started
        job_log(
            session_id,
            f'FINALIZE:{phase}',
            f'Failed {label}: {type(exc).__name__}: {exc} ({format_elapsed_duration(duration)})',
        )
        raise
    else:
        duration = time.perf_counter() - started
        job_log(
            session_id,
            f'FINALIZE:{phase}',
            f'Completed {label} ({format_elapsed_duration(duration)})',
        )


def normalize_local_path(path):
    """Return a normal absolute path for display and path-policy decisions."""
    value = os.path.expanduser(str(path).strip().strip('"'))
    if value.startswith('\\\\?\\UNC\\'):
        value = '\\\\' + value[8:]
    elif value.startswith('\\\\?\\'):
        value = value[4:]
    return os.path.realpath(os.path.abspath(value))


def filesystem_path(path):
    """Use the Win32 extended-length form for filesystem calls on Windows."""
    normalized = normalize_local_path(path)
    if os.name != 'nt':
        return normalized
    if normalized.startswith('\\\\'):
        return '\\\\?\\UNC\\' + normalized[2:]
    return '\\\\?\\' + normalized


def copy_file_long_path(source, destination):
    """Copy a file through extended paths without depending on registry policy."""
    ensure_directory(os.path.dirname(destination))
    with open(filesystem_path(source), 'rb') as source_file:
        with open(filesystem_path(destination), 'wb') as destination_file:
            shutil.copyfileobj(source_file, destination_file, length=1024 * 1024)


def path_is_under_comix(path):
    """Return whether the destination's parent includes a `comix` directory."""
    parent = os.path.dirname(normalize_local_path(path))
    parts = [part.casefold() for part in re.split(r'[\\/]+', parent) if part]
    return 'comix' in parts


def parse_model_types(form):
    """Accept the new ordered model list while retaining the old single field."""
    raw_values = form.getlist('model_types')
    if len(raw_values) == 1:
        try:
            decoded = json.loads(raw_values[0])
            if isinstance(decoded, list):
                raw_values = decoded
        except (TypeError, json.JSONDecodeError):
            raw_values = [item for item in raw_values[0].split(',') if item]
    if not raw_values:
        raw_values = [form.get('model_type', 'transparent_black')]

    return normalize_model_types(raw_values)


@contextmanager
def interprocess_processing_lock():
    """Serialize Camelia pipelines across the web API and automation CLI."""
    lock_path = os.path.join(tempfile.gettempdir(), "camelia-processing.lock")
    with open(lock_path, "a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)

        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

def allowed_file(filename):
    """Check if the filename has an allowed extension."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def request_is_local():
    """Return True only for requests from a loopback-hosted browser UI."""
    try:
        if not ipaddress.ip_address(request.remote_addr).is_loopback:
            return False

        origin = request.headers.get('Origin', '')
        origin_host = urlsplit(origin).hostname
        if origin_host == 'localhost':
            return True
        return bool(origin_host) and ipaddress.ip_address(origin_host).is_loopback
    except (TypeError, ValueError):
        return False

def natural_sort_key(value):
    """Sort names containing page numbers in human-readable order."""
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r'(\d+)', value)
    ]

def find_local_cbz_archives(source_path):
    """Return CBZ files represented by a local file or directory path."""
    resolved_path = normalize_local_path(source_path)
    scan_path = filesystem_path(resolved_path)

    if os.path.isfile(scan_path):
        if os.path.splitext(resolved_path)[1].lower() != '.cbz':
            raise ValueError("The local source file must be a .cbz archive")
        return [resolved_path]

    if not os.path.isdir(scan_path):
        raise ValueError(f"Local file or directory not found: {resolved_path}")

    archives = []
    for current_dir, directory_names, file_names in os.walk(
        scan_path,
        followlinks=False
    ):
        directory_names.sort(key=natural_sort_key)
        for file_name in sorted(file_names, key=natural_sort_key):
            if not file_name.lower().endswith('.cbz'):
                continue
            if file_name.lower().endswith(' - ai decensored.cbz'):
                continue

            archive_fs_path = os.path.join(current_dir, file_name)
            if os.path.islink(archive_fs_path):
                continue

            archive_path = normalize_local_path(archive_fs_path)
            archives.append(archive_path)
            if len(archives) > MAX_BATCH_ARCHIVES:
                raise ValueError(
                    f"The selected directory contains more than "
                    f"{MAX_BATCH_ARCHIVES} CBZ archives"
                )

    if not archives:
        raise ValueError("The selected directory contains no CBZ archives")

    return archives

def unique_filename(filename, used_names):
    """Return a case-insensitively unique filename for the staging directory."""
    stem, extension = os.path.splitext(filename)
    candidate = filename
    suffix = 2

    while candidate.casefold() in used_names:
        candidate = f"{stem}_{suffix}{extension}"
        suffix += 1

    used_names.add(candidate.casefold())
    return candidate

def extract_cbz_images(archive_path, target_dir, used_names):
    """Stage CBZ images while retaining the source archive's full manifest."""
    ensure_directory(target_dir)

    try:
        with zipfile.ZipFile(filesystem_path(archive_path)) as archive:
            archive_members = archive.infolist()
            members = [
                (member_index, member)
                for member_index, member in enumerate(archive_members)
                if not member.is_dir()
                and member.filename.rsplit('.', 1)[-1].lower() in IMAGE_EXTENSIONS
            ]
            members.sort(key=lambda item: natural_sort_key(item[1].filename))

            if not members:
                raise ValueError("The CBZ archive contains no supported images")
            if len(members) > MAX_ARCHIVE_IMAGES:
                raise ValueError(
                    f"The CBZ archive contains more than {MAX_ARCHIVE_IMAGES} images"
                )

            total_size = sum(member.file_size for _, member in members)
            if total_size > MAX_ARCHIVE_TOTAL_BYTES:
                raise ValueError("The extracted CBZ images would exceed 8 GiB")

            extracted_paths = []
            page_member_indices = []
            for index, (member_index, member) in enumerate(members, start=1):
                if member.flag_bits & 0x1:
                    raise ValueError("Encrypted CBZ archives are not supported")
                if member.file_size > MAX_ARCHIVE_IMAGE_BYTES:
                    raise ValueError(
                        f"CBZ image is larger than 512 MiB: {member.filename}"
                    )

                member_name = member.filename.replace('\\', '/').rsplit('/', 1)[-1]
                safe_name = secure_filename(member_name)
                if not safe_name:
                    extension = member_name.rsplit('.', 1)[-1].lower()
                    safe_name = f"page_{index:04d}.{extension}"
                safe_name = unique_filename(safe_name, used_names)
                destination = os.path.join(target_dir, safe_name)

                # Write only to the flat staging directory. Never trust or
                # recreate paths stored inside the archive.
                with archive.open(member) as source, open(destination, 'xb') as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                try:
                    with Image.open(filesystem_path(destination)) as image:
                        image.verify()
                except Exception as exc:
                    try:
                        os.unlink(filesystem_path(destination))
                    except OSError:
                        pass
                    raise ValueError(
                        f"Invalid image member {member.filename!r} in CBZ {archive_path}: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                extracted_paths.append(destination)
                page_member_indices.append(member_index)

            return extracted_paths, {
                "archive_path": archive_path,
                "page_member_indices": page_member_indices,
            }
    except zipfile.BadZipFile as e:
        raise ValueError("The selected CBZ is not a valid ZIP archive") from e

def ensure_directory(directory):
    """Ensure that a directory exists."""
    os.makedirs(filesystem_path(directory), exist_ok=True)

def clear_directory_contents(directory, preserve_names=None):
    """Remove everything in a directory except explicitly preserved names."""
    preserve_names = set(preserve_names or {".keep"})
    ensure_directory(directory)

    for entry in os.scandir(directory):
        if entry.name in preserve_names:
            continue

        try:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)
        except Exception as e:
            app.logger.error(f"Error removing {entry.path}: {e}")

def clean_temp_dirs():
    """Clean processing intermediates and recreate the required directories."""
    ensure_directory(CAMELIA_TEMP)
    images_dir = os.path.join(CAMELIA_TEMP, "images")
    masks_dir = os.path.join(CAMELIA_TEMP, "masks")

    clear_directory_contents(images_dir)
    clear_directory_contents(masks_dir)

    for entry in os.scandir(CAMELIA_TEMP):
        if entry.name in {".keep", "images", "masks"}:
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)
        except Exception as e:
            app.logger.error(f"Error removing {entry.path}: {e}")

def clear_output_directory(directory, preserve_names=None):
    """Clear output contents while optionally preserving session directories."""
    existed = os.path.exists(directory)
    clear_directory_contents(directory, preserve_names=preserve_names)
    return existed

def retained_output_names(current_session_id=None):
    """Return output session directories still exposed by the running API."""
    # Completed uploaded archives live under this stable, human-readable
    # subtree instead of session-ID directories.
    retained = {".keep", "archives"}
    if current_session_id:
        retained.add(current_session_id)

    for config_key in app.config:
        if config_key.startswith("results_"):
            retained.add(config_key.removeprefix("results_"))
        elif config_key.startswith("archive_"):
            retained.add(config_key.removeprefix("archive_"))
            archive_result = app.config.get(config_key) or {}
            archive_path = archive_result.get("path")
            if archive_path:
                try:
                    relative_path = os.path.relpath(archive_path, CAMELIA_OUTPUT)
                    if relative_path != os.pardir and not relative_path.startswith(os.pardir + os.sep):
                        retained.add(relative_path.split(os.sep, 1)[0])
                except ValueError:
                    # Different Windows drives cannot be made relative; those
                    # outputs are outside CAMELIA_OUTPUT and cleanup cannot touch them.
                    pass

    return retained

def cleanup_job_files(model_type, upload_dir, session_id):
    """Remove all non-result files created for a completed or failed job."""
    model_dir = os.path.join(
        WORKSPACE_ROOT,
        "camelia-decensor",
        "input",
        model_type
    )

    clear_directory_contents(model_dir)
    clean_temp_dirs()
    clear_output_directory(
        CAMELIA_OUTPUT,
        preserve_names=retained_output_names(session_id)
    )

    if upload_dir and os.path.isdir(upload_dir):
        try:
            shutil.rmtree(upload_dir)
        except Exception as e:
            app.logger.error(f"Error removing upload directory {upload_dir}: {e}")

def _safe_uploaded_relative_path(relative_path, fallback_name):
    """Return a traversal-free relative archive path supplied by a browser."""
    normalized = str(relative_path or fallback_name).replace('\\', '/')
    parts = [part for part in normalized.split('/') if part not in {'', '.'}]
    if not parts or any(part == '..' for part in parts):
        raise ValueError("The uploaded archive has an invalid relative path")

    safe_parts = []
    reserved = {
        'CON', 'PRN', 'AUX', 'NUL',
        *(f'COM{number}' for number in range(1, 10)),
        *(f'LPT{number}' for number in range(1, 10)),
    }
    for part in parts:
        safe_part = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', part).rstrip(' .')
        if not safe_part:
            safe_part = 'unnamed'
        if os.path.splitext(safe_part)[0].upper() in reserved:
            safe_part = f'_{safe_part}'
        safe_parts.append(safe_part)
    return os.path.join(*safe_parts)


def _encode_processed_page(processed_path, original_bytes, extension):
    """Encode a processed page in its original format and retain image metadata."""
    extension = extension.casefold()
    with Image.open(io.BytesIO(original_bytes)) as original_image:
        original_info = dict(original_image.info)
        original_mode = original_image.mode

    with Image.open(processed_path) as processed_image:
        image = processed_image.copy()

    output = io.BytesIO()
    common = {}
    for key in ('exif', 'icc_profile', 'dpi'):
        if original_info.get(key) is not None:
            common[key] = original_info[key]

    if extension in {'jpg', 'jpeg'}:
        if image.mode not in {'RGB', 'L'}:
            background = Image.new('RGB', image.size, (255, 255, 255))
            if image.mode in {'RGBA', 'LA'}:
                background.paste(image, mask=image.getchannel('A'))
            else:
                background.paste(image.convert('RGB'))
            image = background
        if original_mode == 'CMYK':
            # Segmentation converts CMYK pixels to sRGB; the source ICC describes
            # CMYK ink values and would give the rebuilt RGB JPEG wrong colors.
            common.pop('icc_profile', None)
            if image.mode == 'RGB':
                common['icc_profile'] = ImageCms.ImageCmsProfile(
                    ImageCms.createProfile('sRGB')
                ).tobytes()
        image.save(output, format='JPEG', quality=95, subsampling=0, **common)
    elif extension == 'webp':
        image.save(output, format='WEBP', lossless=True, method=6, **common)
    elif extension == 'avif':
        if image.mode not in {'RGB', 'RGBA'}:
            image = image.convert('RGBA' if 'A' in image.getbands() else 'RGB')
        image.save(
            output,
            format='AVIF',
            quality=95,
            subsampling='4:4:4',
            **common,
        )
    else:
        image.save(output, format='PNG', **common)
    return output.getvalue()


def _zip_info_signature(member):
    """Return the stable ZIP metadata fields that a rebuild must preserve."""
    return (
        member.filename,
        member.date_time,
        member.compress_type,
        member.comment,
        member.extra,
        member.create_system,
        member.create_version,
        member.extract_version,
        member.flag_bits & ~0x08,
        member.volume,
        member.internal_attr,
        member.external_attr,
    )


def _copy_archive_with_processed_pages(source_archive, destination, results, page_indices):
    """Rebuild a CBZ in original member order, replacing only image payloads."""
    with zipfile.ZipFile(source_archive) as source:
        source_members = source.infolist()
        if len(page_indices) != len(results):
            raise RuntimeError(
                "The processed page count does not match the source CBZ page count"
            )
        replacements = dict(zip(page_indices, results))

        with zipfile.ZipFile(destination, mode='w') as target:
            target.comment = source.comment
            for index, member in enumerate(source_members):
                cloned_member = copy.copy(member)
                if member.is_dir():
                    payload = b''
                else:
                    original_bytes = source.read(member)
                    if index in replacements:
                        extension = member.filename.rsplit('.', 1)[-1]
                        payload = _encode_processed_page(
                            replacements[index]["processed_path"],
                            original_bytes,
                            extension,
                        )
                    else:
                        payload = original_bytes
                target.writestr(cloned_member, payload)


def _verify_rebuilt_archive(source_archive, rebuilt_archive, page_indices):
    """Verify structure, metadata, unchanged payloads, and processed images."""
    page_indices = set(page_indices)
    with zipfile.ZipFile(source_archive) as source, zipfile.ZipFile(rebuilt_archive) as rebuilt:
        source_members = source.infolist()
        rebuilt_members = rebuilt.infolist()
        if rebuilt.testzip() is not None:
            raise RuntimeError("Archive CRC verification failed")
        if len(source_members) != len(rebuilt_members):
            raise RuntimeError("Archive verification found a changed member count")
        if source.comment != rebuilt.comment:
            raise RuntimeError("Archive comment changed while rebuilding the archive")

        for index, (before, after) in enumerate(zip(source_members, rebuilt_members)):
            if _zip_info_signature(before) != _zip_info_signature(after):
                raise RuntimeError(
                    f"ZIP metadata changed for archive member {before.filename}"
                )
            if index in page_indices:
                with rebuilt.open(after) as page_file, Image.open(page_file) as page_image:
                    page_image.verify()
            elif not before.is_dir() and source.read(before) != rebuilt.read(after):
                raise RuntimeError(
                    f"Non-image archive member changed: {before.filename}"
                )


def planned_archive_destination(original_name, relative_path=None, source_path=None,
                                output_root=None, output_mode='suffix'):
    """Compute the exact archive destination used by filtering and writing."""
    original_name = os.path.basename(original_name)
    relative_path = _safe_uploaded_relative_path(relative_path, original_name)
    if source_path and output_mode == 'replace':
        return normalize_local_path(source_path)
    if output_root:
        return os.path.join(
            normalize_local_path(output_root),
            os.path.dirname(relative_path),
            original_name,
        )
    if source_path:
        original_stem = os.path.splitext(original_name)[0]
        return os.path.join(
            os.path.dirname(normalize_local_path(source_path)),
            f"{original_stem} - AI Decensored.cbz",
        )
    return os.path.join(
        CAMELIA_OUTPUT,
        'archives',
        os.path.dirname(relative_path),
        original_name,
    )


def resolve_output_root(output_location, custom_root):
    """Resolve the output dropdown choice to a concrete root or automatic mode."""
    if output_location == 'camelia':
        return os.path.join(CAMELIA_OUTPUT, 'archives')
    if output_location == 'custom':
        if not custom_root:
            raise ValueError('Enter a custom output directory')
        return normalize_local_path(custom_root)
    if output_location not in {'', 'automatic'}:
        raise ValueError(f"Invalid output location: {output_location}")
    # Backward compatibility: older clients sent only output_root.
    return normalize_local_path(custom_root) if custom_root else None


def create_processed_cbz(results, archive_context, session_id):
    """Create and verify a metadata-preserving CBZ using the requested policy."""
    original_name = os.path.basename(archive_context["original_name"])
    source_path = archive_context.get("source_path")
    output_mode = archive_context.get("output_mode", "suffix")
    output_root = archive_context.get("output_root")
    output_path = planned_archive_destination(
        original_name,
        relative_path=archive_context.get('relative_path'),
        source_path=source_path,
        output_root=output_root,
        output_mode=output_mode,
    )
    output_name = os.path.basename(output_path)

    if os.path.exists(filesystem_path(output_path)) and output_mode != "replace":
        raise FileExistsError(
            f"Refusing to overwrite existing archive: {output_path}"
        )
    if (
        source_path
        and output_mode != "replace"
        and os.path.normcase(os.path.realpath(output_path)) == os.path.normcase(source_path)
    ):
        raise ValueError("An output root cannot resolve to the source archive path")

    # The final filename may already approach Windows' 255-character limit.
    # Keep the temporary archive in the same directory for atomic installation
    # without appending anything to that filename.
    temporary_path = os.path.join(
        os.path.dirname(output_path), f".camelia-{uuid.uuid4().hex}.tmp"
    )
    page_indices = archive_context["page_member_indices"]
    source_archive = archive_context["archive_path"]

    try:
        ensure_directory(os.path.dirname(output_path))
        with timed_job_step(session_id, 'cbz_rebuild', 'CBZ rebuild'):
            _copy_archive_with_processed_pages(
                filesystem_path(source_archive),
                filesystem_path(temporary_path),
                results,
                page_indices,
            )
        with timed_job_step(
            session_id, 'rebuilt_archive_verification', 'rebuilt archive verification'
        ):
            _verify_rebuilt_archive(
                filesystem_path(source_archive),
                filesystem_path(temporary_path),
                page_indices,
            )

        with timed_job_step(session_id, 'comicinfo_tagging', 'ComicInfo.xml tagging'):
            tag_result = add_uncensored_tag(
                filesystem_path(temporary_path), archive_context.get("completed_methods", ())
            )
        job_log(
            session_id,
            'METADATA',
            (
                f"Recorded uncensored and method tags ({', '.join(tag_result['applied_methods'])}) in {tag_result['member']}"
                if tag_result['changed']
                else f"Uncensored and method tags ({', '.join(tag_result['applied_methods'])}) already present in {tag_result['member']}"
            ) + (" (created minimal ComicInfo.xml)" if tag_result['created'] else ""),
        )
        # This is the final shared installation boundary used by replace mode.
        assert_overwrite_allowed(
            filesystem_path(temporary_path),
            filesystem_path(output_path),
        )

        backup_path = None
        if output_mode == "replace":
            backup_root = archive_context.get("backup_dir")
            if not backup_root:
                raise ValueError("Replace mode requires a backup directory")
            with timed_job_step(session_id, 'original_backup', 'original archive backup'):
                backup_job_dir = os.path.join(backup_root, session_id)
                ensure_directory(backup_job_dir)
                backup_path = os.path.join(backup_job_dir, os.path.basename(source_path))
                copy_file_long_path(source_path, backup_path)
                with zipfile.ZipFile(filesystem_path(backup_path)) as backup:
                    if backup.testzip() is not None:
                        raise RuntimeError("The original archive backup failed verification")

        with timed_job_step(session_id, 'destination_install', 'destination install/replace'):
            if output_mode == 'copy':
                # Hard-link installation is atomic and fails if another process
                # created this name after our initial existence check. os.replace
                # would silently overwrite that archive.
                os.link(filesystem_path(temporary_path), filesystem_path(output_path))
                os.unlink(filesystem_path(temporary_path))
            else:
                os.replace(filesystem_path(temporary_path), filesystem_path(output_path))

        with timed_job_step(
            session_id, 'installed_archive_verification', 'installed archive verification'
        ):
            with zipfile.ZipFile(filesystem_path(output_path)) as installed:
                if installed.testzip() is not None:
                    raise RuntimeError("Installed archive CRC verification failed")
            installed_inspection = inspect_cbz_eligibility(filesystem_path(output_path))
            expected_methods = set(archive_context.get("completed_methods", ()))
            if not installed_inspection["already_processed"]:
                raise RuntimeError("Installed archive is missing the uncensored metadata marker")
            if not expected_methods.issubset(installed_inspection["applied_methods"]):
                raise RuntimeError("Installed archive is missing one or more completed method tags")

        original_deleted = False
        if source_path and output_mode == "suffix":
            os.unlink(filesystem_path(source_path))
            original_deleted = True

        for result in results:
            try:
                os.unlink(result["processed_path"])
            except OSError as e:
                app.logger.warning(
                    f"Could not remove archived page {result['processed_path']}: {e}"
                )

        session_output_dir = os.path.join(CAMELIA_OUTPUT, session_id)
        try:
            os.rmdir(session_output_dir)
        except OSError:
            # The general cleanup pass will handle any unexpected leftovers.
            pass

        return {
            "filename": output_name,
            "path": output_path,
            "downloadable": source_path is None and not output_root,
            "original_deleted": original_deleted,
            "backup_path": backup_path,
            "metadata_tag": tag_result,
        }
    finally:
        if os.path.exists(filesystem_path(temporary_path)):
            try:
                os.unlink(filesystem_path(temporary_path))
            except OSError:
                pass

def _validate_runtime_files(model_types):
    """Fail before a long job starts when required model assets are missing."""
    model_names = {
        'black_bars': 'best_black_bars_model.pth',
        'transparent_black': 'best_transparent_black_model.pth',
        'white_bars': 'best_white_bars_model.pth',
    }
    missing = []
    for model_type in model_types:
        if model_type == 'mosaic':
            continue
        model_path = os.path.join(
            WORKSPACE_ROOT, 'smp-segmentation', 'pretrained', model_names[model_type]
        )
        if not os.path.isfile(filesystem_path(model_path)):
            missing.append(model_path)
    if any(model_type != 'mosaic' for model_type in model_types):
        lama_checkpoint = os.path.join(WORKSPACE_ROOT, 'lama-inpainting', 'pretrained', 'best')
        for required_path in (
            os.path.join(lama_checkpoint, 'config.yaml'),
            os.path.join(lama_checkpoint, 'models', 'best.ckpt'),
        ):
            if not os.path.isfile(filesystem_path(required_path)):
                missing.append(required_path)
    if missing:
        raise FileNotFoundError(
            'Required model file(s) are missing:\n' + '\n'.join(f'  - {path}' for path in missing)
        )
    if 'mosaic' in model_types:
        validate_aletheia_runtime()


def _run_model_pass(image_paths, model_type, session_id, pass_number,
                    pass_count, job_work_dir):
    """Run one model in a short isolated workspace and return its output files."""
    pass_dir = os.path.join(job_work_dir, f'pass-{pass_number:02d}-{model_type}')
    input_root = os.path.join(pass_dir, 'input')
    model_input = os.path.join(input_root, model_type)
    temp_dir = os.path.join(pass_dir, 'temp')
    output_dir = os.path.join(pass_dir, 'output')
    ensure_directory(model_input)
    ensure_directory(temp_dir)
    ensure_directory(output_dir)

    used_names = set()
    staged_names = []
    for index, image_path in enumerate(image_paths, start=1):
        name = unique_filename(os.path.basename(image_path), used_names)
        staged_path = os.path.join(model_input, name)
        copy_file_long_path(image_path, staged_path)
        staged_names.append(name)
    job_log(
        session_id,
        'STAGE',
        f"Pass {pass_number}/{pass_count} ({model_type}): staged "
        f"{len(staged_names)} image(s) in {model_input}",
    )

    command = [
        sys.executable,
        '-u',
        os.path.join(WORKSPACE_ROOT, 'main.py'),
        '--model_type', model_type,
        '--input_dir', input_root,
        '--temp_dir', temp_dir,
        '--output_dir', output_dir,
    ]
    command_text = subprocess.list2cmdline(command)
    job_log(session_id, 'SUBPROCESS', f"Command: {command_text}")
    job_log(session_id, 'SUBPROCESS', f"Working directory: {WORKSPACE_ROOT}")

    env = os.environ.copy()
    env['PYTHONUNBUFFERED'] = '1'
    env['PYTHONIOENCODING'] = 'UTF-8'
    recent_output = deque(maxlen=8)
    process = None
    try:
        process = subprocess.Popen(
            command,
            cwd=WORKSPACE_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding='utf-8',
            errors='replace',
            env=env,
        )
        active_processes[session_id] = process
        for line in iter(process.stdout.readline, ''):
            line = line.rstrip()
            if line:
                recent_output.append(line)
                job_log(session_id, f'OUTPUT:{model_type}', line)
        process.stdout.close()
        return_code = process.wait()
    except Exception as exc:
        raise RuntimeError(
            f"Could not run {model_type} subprocess ({type(exc).__name__}: {exc}). "
            f"Command: {command_text}; cwd: {WORKSPACE_ROOT}"
        ) from exc
    finally:
        active_processes.pop(session_id, None)

    if return_code != 0:
        output_context = ' | '.join(recent_output) or '<no subprocess output>'
        raise RuntimeError(
            f"{model_type} subprocess exited with code {return_code}. "
            f"Final output: {output_context}"
        )

    results = []
    for name in sorted(os.listdir(output_dir), key=natural_sort_key):
        path = os.path.join(output_dir, name)
        if os.path.isfile(path) and name.rsplit('.', 1)[-1].lower() in IMAGE_EXTENSIONS:
            results.append({'filename': name, 'processed_path': path})
    if len(results) != len(image_paths):
        raise RuntimeError(
            f"{model_type} produced {len(results)} result(s) for "
            f"{len(image_paths)} input image(s). Output directory: {output_dir}"
        )
    return results


def process_images(image_paths, model_types, session_id, job_work_dir):
    """Run one or more model passes sequentially in an isolated workspace."""
    model_types = normalize_model_types(model_types)
    _validate_runtime_files(model_types)
    current_paths = list(image_paths)
    results = []
    for pass_number, model_type in enumerate(model_types, start=1):
        pass_started = time.perf_counter()
        job_log(
            session_id,
            'PASS',
            f"Starting {model_type} ({pass_number}/{len(model_types)})",
        )
        try:
            results = _run_model_pass(
                current_paths,
                model_type,
                session_id,
                pass_number,
                len(model_types),
                job_work_dir,
            )
        except Exception as exc:
            duration = time.perf_counter() - pass_started
            job_log(
                session_id,
                'PASS',
                f"Failed {model_type} ({pass_number}/{len(model_types)}): "
                f"{type(exc).__name__}: {exc} ({format_elapsed_duration(duration)})",
            )
            raise
        duration = time.perf_counter() - pass_started
        job_log(
            session_id,
            'PASS',
            f"Completed {model_type} ({pass_number}/{len(model_types)}): "
            f"{len(results)} output image(s) ({format_elapsed_duration(duration)})",
        )
        current_paths = [result['processed_path'] for result in results]
    return session_id, results

def process_images_thread(
    image_paths,
    model_types,
    session_id,
    upload_dir,
    archive_context=None
):
    """Run the image processing in a separate thread."""
    succeeded = False
    job_work_dir = tempfile.mkdtemp(prefix=f"camelia-{session_id[:8]}-")
    # The pipeline uses shared input/temp/output directories, so processing
    # and cleanup must both finish before another job can use them.
    with processing_lock, interprocess_processing_lock():
        try:
            result_session_id, results = process_images(
                image_paths,
                model_types,
                session_id,
                job_work_dir,
            )

            if result_session_id and results:
                if archive_context:
                    archive_context["completed_methods"] = normalize_model_types(model_types)
                    archive_result = create_processed_cbz(
                        results,
                        archive_context,
                        session_id
                    )
                    app.config[f"archive_{session_id}"] = archive_result
                    job_log(
                        session_id,
                        'ARCHIVE',
                        f"Created and verified {archive_result['path']}",
                    )
                else:
                    result_dir = os.path.join(CAMELIA_OUTPUT, session_id)
                    ensure_directory(result_dir)
                    persistent_results = []
                    for result in results:
                        destination = os.path.join(result_dir, result['filename'])
                        copy_file_long_path(result['processed_path'], destination)
                        persistent_results.append({
                            'filename': result['filename'],
                            'processed_path': destination,
                        })
                    app.config[f"results_{session_id}"] = persistent_results
                succeeded = True
            else:
                # process_images() already logged the specific failure
                # and normally set the status to "error".
                process_status[session_id] = "error"

        except Exception as e:
            app.logger.exception(
                f"Thread error for session {session_id}: {e}"
            )
            if process_status.get(session_id) == 'cancelled':
                job_log(session_id, 'CANCEL', 'Active subprocess stopped')
            else:
                job_log(
                    session_id,
                    'ERROR',
                    f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
                )
                process_status[session_id] = "error"
        finally:
            try:
                if not succeeded:
                    shutil.rmtree(
                        os.path.join(CAMELIA_OUTPUT, session_id),
                        ignore_errors=True
                    )
                if upload_dir and os.path.isdir(upload_dir):
                    shutil.rmtree(upload_dir, ignore_errors=True)
                shutil.rmtree(job_work_dir, ignore_errors=True)
                job_log(session_id, 'CLEANUP', 'Removed isolated staging files')
            except Exception as e:
                app.logger.exception(
                    f"Cleanup error for session {session_id}: {e}"
                )
                job_log(session_id, 'CLEANUP', f"Warning: {type(e).__name__}: {e}")

    if process_status.get(session_id) == 'cancelled':
        job_log(session_id, 'CANCEL', 'Job cancelled')
    elif succeeded:
        process_status[session_id] = "completed"
        job_log(session_id, 'COMPLETE', 'Job finished successfully')
    else:
        process_status[session_id] = "error"
        job_log(session_id, 'FAILED', 'Job failed; see the preceding error for details')


def process_cbz_archive_sync(
    source_path,
    model_type="black_bars",
    output_mode="suffix",
    backup_dir=None,
    model_types=None,
    destination_dir=None,
    event_callback=None,
    output_dir=None,
    reprocess=False,
):
    """Process one local CBZ synchronously for CLI and integration callers.

    ``replace`` keeps the existing path after backing up the untouched source.
    ``copy`` installs a verified result under ``output_dir`` and leaves the
    source untouched; it never overwrites an existing destination.
    """
    selected_models = normalize_model_types(
        model_types if model_types is not None else model_type
    )
    if output_mode not in {'suffix', 'replace', 'copy'}:
        raise ValueError(f"Invalid output mode: {output_mode}")

    source_path = normalize_local_path(source_path)
    if not os.path.isfile(filesystem_path(source_path)) or not source_path.lower().endswith('.cbz'):
        raise ValueError(f"CBZ file not found: {source_path}")
    if output_mode == 'replace' and not backup_dir:
        raise ValueError("Replace mode requires a backup directory")
    if output_mode == 'copy' and not output_dir:
        raise ValueError("Copy mode requires an output directory")
    destination = planned_archive_destination(
        os.path.basename(source_path),
        source_path=source_path,
        output_root=output_dir if output_mode == 'copy' else None,
        output_mode=output_mode,
    )
    eligibility_destination = (
        os.path.join(normalize_local_path(destination_dir), os.path.basename(source_path))
        if destination_dir else destination
    )
    if not path_is_under_comix(eligibility_destination):
        raise ValueError(
            f"Skipped {os.path.basename(source_path)}: destination is not under a "
            f"comix directory ({eligibility_destination})"
        )
    inspection = inspect_cbz_eligibility(filesystem_path(source_path))
    if inspection.get("reason") == "Archive could not be inspected":
        raise ValueError(f"Cannot inspect CBZ: {inspection.get('parse_error')}")
    pending_models = pending_decensor_methods(inspection, selected_models, reprocess)

    session_id = str(uuid.uuid4())
    process_logs[session_id] = queue.Queue()
    if not pending_models:
        process_status[session_id] = "completed"
        job_log(session_id, 'SKIP', f"All requested decensor methods are already recorded for {source_path}; use --reprocess to override")
        return {"status": "skipped", "path": source_path, "applied_methods": inspection["applied_methods"]}, session_id
    selected_models = pending_models
    process_status[session_id] = "processing"
    if event_callback is not None:
        process_log_listeners[session_id] = event_callback
    temp_dir = tempfile.mkdtemp()

    try:
        job_log(
            session_id,
            'JOB',
            f"Processing {os.path.basename(source_path)}; stages: "
            f"{' -> '.join(selected_models)}; destination: "
            f"{eligibility_destination}",
        )
        image_paths, archive_details = extract_cbz_images(
            source_path,
            temp_dir,
            set(),
        )
        context = {
            **archive_details,
            "original_name": os.path.basename(source_path),
            "source_path": source_path,
            "output_mode": output_mode,
            "output_root": normalize_local_path(output_dir) if output_dir else None,
            "backup_dir": normalize_local_path(backup_dir)
            if backup_dir else None,
        }
        process_images_thread(
            image_paths,
            selected_models,
            session_id,
            temp_dir,
            context,
        )
        if process_status.get(session_id) != "completed":
            messages = []
            while not process_logs[session_id].empty():
                messages.append(process_logs[session_id].get())
            raise RuntimeError("\n".join(messages) or "Camelia processing failed")
        return app.config[f"archive_{session_id}"], session_id
    finally:
        process_log_listeners.pop(session_id, None)
        if os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


BACKLOG_STATE_DIR = os.path.join(WORKSPACE_ROOT, "camelia-decensor", "state")
BACKLOG_DATABASE = os.environ.get(
    "CAMELIA_BACKLOG_DATABASE",
    os.path.join(BACKLOG_STATE_DIR, "library_backlog.sqlite3"),
)


def _configured_comic_automation_handoff():
    database = os.environ.get("CAMELIA_COMIC_AUTOMATION_DATABASE", "").strip()
    if not database:
        return None
    suite_root = os.environ.get("CAMELIA_COMIC_AUTOMATION_ROOT", r"C:\git\ComicAutomation")
    suite_python = os.environ.get("CAMELIA_COMIC_AUTOMATION_PYTHON", r"C:\Python311\python.exe")
    required = (database, suite_python, os.path.join(suite_root, "comic_automation", "library", "camelia_handoff.py"))
    if not all(os.path.isfile(path) for path in required):
        app.logger.error("ComicAutomation handoff is configured but its database, Python, or module is missing")
        return None

    def sync_completed_jobs():
        # The consumer is idempotent: its cursor and each same-path discovery
        # update commit together in ComicAutomation's own SQLite database.
        while True:
            command = [
                suite_python, "-m", "comic_automation.library.camelia_handoff",
                "--camelia-database", BACKLOG_DATABASE,
                "--database", database,
                "--root", r"X:\comix", "--limit", "25", "--apply",
            ]
            run = subprocess.run(command, cwd=suite_root, capture_output=True,
                                 text=True, timeout=3600, check=False)
            if run.returncode:
                raise RuntimeError((run.stderr or run.stdout or "ComicAutomation handoff failed").strip()[:1200])
            count = int(json.loads(run.stdout)["count"])
            if count:
                app.logger.info("ComicAutomation handoff reconciled %s completed X: book(s)", count)
            if count < 25:
                return

    return sync_completed_jobs


def _configured_source_registration():
    database = os.environ.get("CAMELIA_COMIC_AUTOMATION_DATABASE", "").strip()
    suite_root = os.environ.get("CAMELIA_COMIC_AUTOMATION_ROOT", r"C:\git\ComicAutomation")
    suite_python = os.environ.get("CAMELIA_COMIC_AUTOMATION_PYTHON", r"C:\Python311\python.exe")
    module = os.path.join(suite_root, "comic_automation", "library", "camelia_handoff.py")
    if not database or not all(os.path.isfile(path) for path in (database, suite_python, module)):
        return None

    def register_source(job):
        command = [
            suite_python, "-m", "comic_automation.library.camelia_handoff",
            "--database", database, "--root", r"X:\comix",
            "--prepare-source", job["source_path"], "--apply",
        ]
        run = subprocess.run(command, cwd=suite_root, capture_output=True,
                             text=True, timeout=3600, check=False)
        if run.returncode:
            raise RuntimeError((run.stderr or run.stdout or "Source registration failed").strip()[:1200])
        preparation = json.loads(run.stdout)["preparation"]
        app.logger.info("ComicAutomation source %s: archive %s, %s",
                        preparation["action"], preparation["archive_id"], job["source_path"])

    return register_source


def _configured_backup_offload():
    suite_root = os.environ.get("CAMELIA_COMIC_AUTOMATION_ROOT", r"C:\git\ComicAutomation")
    suite_python = os.environ.get("CAMELIA_COMIC_AUTOMATION_PYTHON", r"C:\Python311\python.exe")
    offload_module = os.path.join(suite_root, "scripts", "ai_decensor_backups.py")
    if not os.path.isfile(suite_python) or not os.path.isfile(offload_module):
        app.logger.error("Backlog F: backup offload requires ComicAutomation and its Python runtime")
        return None
    destination = os.path.abspath(os.environ.get("CAMELIA_BACKUP_ARCHIVE_ROOT", r"F:\ai-decensor-originals"))
    if os.path.splitdrive(destination)[0].casefold() != "f:":
        app.logger.error("Backlog original backup destination must be on F: %s", destination)
        return None
    source_root = os.path.join(BACKLOG_STATE_DIR, "originals")

    def offload(job, result):
        drive = os.path.splitdrive(destination)[0] + os.sep
        if not os.path.isdir(drive):
            raise FileNotFoundError(f"Backup drive is unavailable: {drive}")
        if os.path.splitdrive(os.path.realpath(destination))[0].casefold() != "f:":
            raise ValueError("Resolved original-backup destination is not on F:")
        required = os.path.getsize(job["source_path"]) if job and result is None else 0
        if shutil.disk_usage(drive).free < required + 1024 * 1024 * 1024:
            raise OSError(f"Insufficient free space on backup drive {drive}")
        command = [
            suite_python, "-m", "scripts.ai_decensor_backups",
            "--source-root", source_root, "--destination-root", destination, "--apply",
        ]
        if result is not None:
            backup_path = result.get("backup_path")
            if not backup_path:
                raise RuntimeError("Completed replace job did not report its original backup")
            command.extend(("--source-file", backup_path))
        elif job is not None:
            return  # Capacity preflight before replacing the original.
        run = subprocess.run(command, cwd=suite_root, capture_output=True,
                             text=True, timeout=3600, check=False)
        if run.returncode:
            raise RuntimeError((run.stderr or run.stdout or "Original backup offload failed").strip()[:1200])
        if result is not None:
            app.logger.info("Backlog original archived on F: %s", run.stdout.strip())

    return offload


def _process_backlog_archive(source_path, sequence, job):
    """Adapt one durable queue job to the existing ordered CBZ pipeline."""
    backup_root = os.path.join(BACKLOG_STATE_DIR, "originals")
    stage_positions = {stage: index for index, stage in enumerate(sequence, start=1)}
    stage_starts = {}
    phase_starts = {}

    def record_pipeline_event(step, message):
        if step.startswith("FINALIZE:"):
            phase = step.split(":", 1)[1]
            if message.startswith("Starting"):
                phase_starts[phase] = time.perf_counter()
                event = "phase_start"
                duration = None
                detail = f"Finalization start: {message.removeprefix('Starting ').strip()}"
            elif message.startswith("Completed"):
                duration = max(0.0, time.perf_counter() - phase_starts.get(phase, time.perf_counter()))
                event = "phase_complete"
                detail = (
                    f"Finalization complete: {message.removeprefix('Completed ').rsplit(' (', 1)[0]} "
                    f"({format_elapsed_duration(duration)})"
                )
            elif message.startswith("Failed"):
                duration = max(0.0, time.perf_counter() - phase_starts.get(phase, time.perf_counter()))
                event = "phase_failure"
                detail = message
            else:
                return
            library_backlog.store.update_phase(job["id"], phase, event, detail, duration)
            return
        if step != "PASS":
            return
        for stage, position in stage_positions.items():
            if stage not in message:
                continue
            if message.startswith("Starting"):
                event = "stage_start"
                stage_starts[stage] = time.perf_counter()
                duration = None
                detail = f"Stage {position}/{len(sequence)} start: {stage}"
            elif message.startswith("Completed"):
                event = "stage_complete"
                duration = max(0.0, time.perf_counter() - stage_starts.get(stage, time.perf_counter()))
                detail = (
                    f"Stage {position}/{len(sequence)} complete: {stage} "
                    f"({format_elapsed_duration(duration)})"
                )
            elif message.startswith("Failed"):
                event = "stage_failure"
                duration = max(0.0, time.perf_counter() - stage_starts.get(stage, time.perf_counter()))
                detail = (
                    f"Stage {position}/{len(sequence)} failure: {stage} "
                    f"({format_elapsed_duration(duration)}): {message}"
                )
            else:
                return
            library_backlog.store.update_stage(
                job["id"],
                stage,
                event,
                detail,
                duration,
            )
            return

    result, _session_id = process_cbz_archive_sync(
        source_path,
        model_types=sequence,
        output_mode="replace",
        backup_dir=backup_root,
        destination_dir=job["root_path"],
        event_callback=record_pipeline_event,
    )
    return result


def _detect_backlog_censorship(source_path, _job):
    """Audit all pages without changing the source or loading models in Flask."""
    root, mosaic_python = validate_aletheia_runtime()
    script = os.path.join(WORKSPACE_ROOT, "censorship_detection.py")
    run = subprocess.run(
        [str(mosaic_python), "-u", script, "--archive", source_path,
         "--aletheia-root", str(root)],
        cwd=WORKSPACE_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=4 * 3600, check=False,
    )
    if run.returncode:
        raise RuntimeError((run.stderr or run.stdout or "Detector failed").strip()[-2000:])
    result = json.loads(run.stdout)
    app.logger.info("Censorship check: %s", ", ".join(result["detected_methods"]) or "no detection")
    return result


library_backlog = LibraryBacklog(
    BacklogStore(BACKLOG_DATABASE), _process_backlog_archive,
    detector=_detect_backlog_censorship,
    sync_callback=_configured_comic_automation_handoff(),
    prepare_callback=_configured_source_registration(),
    require_source_registration=True,
    backup_callback=_configured_backup_offload(),
    require_backup_offload=True,
)


def _local_backlog_request():
    if request_is_local():
        return None
    return jsonify({"error": "Library backlog controls are restricted to this computer"}), 403


@app.route('/api/library-backlog', methods=['GET'])
def get_library_backlog():
    blocked = _local_backlog_request()
    if blocked:
        return blocked
    status = library_backlog.store.snapshot()
    status["comic_automation_handoff_configured"] = library_backlog.sync_callback is not None
    status["source_registration_configured"] = library_backlog.prepare_callback is not None
    status["backup_offload_configured"] = library_backlog.backup_callback is not None
    return jsonify(status)


@app.route('/api/library-backlog/book-progress', methods=['GET'])
def get_library_book_progress():
    blocked = _local_backlog_request()
    if blocked:
        return blocked
    source_path = str(request.args.get('source_path') or '').strip()
    if not source_path:
        return jsonify({"error": "source_path is required"}), 400
    book = library_backlog.store.book_progress(source_path)
    if book is None:
        return jsonify({"error": "Book is not in the library backlog"}), 404
    return jsonify(book)


@app.route('/api/library-backlog/scan/start', methods=['POST'])
def start_library_scan():
    blocked = _local_backlog_request()
    if blocked:
        return blocked
    root = str((request.get_json(silent=True) or {}).get('root', '')).strip()
    try:
        scan_id = library_backlog.start_scan(root)
        return jsonify({"success": True, "scan_id": scan_id})
    except ValueError as exc:
        app.logger.warning("Library crawl refused: %s", exc)
        try:
            library_backlog.store.record_event("scan_start_refused", str(exc))
        except Exception:
            app.logger.exception("Could not persist refused crawl event")
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        app.logger.exception("Library crawl could not start: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route('/api/library-backlog/scan/<int:scan_id>/<action>', methods=['POST'])
def control_library_scan(scan_id, action):
    blocked = _local_backlog_request()
    if blocked:
        return blocked
    try:
        if action == 'pause':
            library_backlog.pause_scan(scan_id)
        elif action == 'resume':
            snapshot = library_backlog.store.snapshot()
            scan = snapshot.get('scan')
            if not scan or scan['id'] != scan_id:
                raise ValueError('Scan not found')
            library_backlog.start_scan(scan['root_path'])
        elif action == 'stop':
            library_backlog.stop_scan(scan_id)
        else:
            raise ValueError(f"Invalid scan action: {action}")
        return jsonify({"success": True})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route('/api/library-backlog/queue/<action>', methods=['POST'])
def control_library_queue(action):
    blocked = _local_backlog_request()
    if blocked:
        return blocked
    try:
        retried = 0
        if action == 'resume':
            retried = library_backlog.resume_queue(retry_failed=True)
        elif action == 'pause':
            library_backlog.pause_queue()
        elif action == 'stop':
            library_backlog.stop_queue()
        else:
            raise ValueError(f"Invalid queue action: {action}")
        return jsonify({"success": True, "retried": retried})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route('/api/library-backlog/known-types-only', methods=['POST'])
def control_library_detection_mode():
    blocked = _local_backlog_request()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get('enabled'), bool):
        return jsonify({"error": "enabled must be true or false"}), 400
    try:
        library_backlog.store.control_known_types_only(data['enabled'])
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify({"success": True, "known_types_only": library_backlog.store.known_types_only()})


@app.route('/api/library-backlog/schedule', methods=['POST'])
def control_library_schedule():
    blocked = _local_backlog_request()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get('enabled'), bool):
        return jsonify({"error": "enabled must be true or false"}), 400
    try:
        library_backlog.set_batch_schedule(data['enabled'])
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify({"success": True, "batch_schedule": library_backlog.store.snapshot()['batch_schedule']})


@app.route('/api/library-backlog/queue/book', methods=['POST'])
def requeue_library_book():
    blocked = _local_backlog_request()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    try:
        source = str(data.get('source_path') or '').strip()
        if not source:
            raise ValueError('Choose a CBZ path from the crawled library')
        methods = normalize_model_types(data.get('model_types') or DEFAULT_SEQUENCE)
        sequence = library_backlog.store.requeue_book(source, methods, bool(data.get('reprocess')))
        return jsonify({"success": True, "selected_sequence": sequence})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

@app.route('/api/local-cbz-sources', methods=['POST'])
def get_local_cbz_sources():
    """List local CBZ files from one or more localhost-only source paths."""
    if not request_is_local():
        return jsonify({
            "error": "Local-path processing is restricted to this computer"
        }), 403

    request_data = request.get_json(silent=True) or {}
    output_location = str(request_data.get('output_location', 'automatic'))
    custom_output_root = str(request_data.get('output_root', '')).strip().strip('"')
    source_paths = request_data.get('source_paths')
    if source_paths is None:
        source_paths = [request_data.get('source_path', '')]
    if not isinstance(source_paths, list):
        return jsonify({"error": "source_paths must be a list"}), 400
    source_paths = [str(path).strip().strip('"') for path in source_paths]
    source_paths = [path for path in source_paths if path]
    if not source_paths:
        return jsonify({"error": "No local file or directory paths provided"}), 400

    try:
        output_root = resolve_output_root(output_location, custom_output_root)
        archives = []
        skipped = []
        seen = set()
        for source_path in source_paths:
            resolved_source = normalize_local_path(source_path)
            relative_base = os.path.dirname(resolved_source)
            for archive_path in find_local_cbz_archives(resolved_source):
                key = os.path.normcase(archive_path)
                if key not in seen:
                    seen.add(key)
                    relative_path = os.path.relpath(archive_path, relative_base)
                    destination = planned_archive_destination(
                        os.path.basename(archive_path),
                        relative_path=relative_path,
                        source_path=archive_path,
                        output_root=output_root,
                    )
                    item = {
                        'path': archive_path,
                        'name': os.path.basename(archive_path),
                        'relative_path': relative_path,
                        'destination': destination,
                    }
                    if path_is_under_comix(destination):
                        archives.append(item)
                    else:
                        item['reason'] = 'Destination is not under a comix directory'
                        skipped.append(item)
        if len(archives) + len(skipped) > MAX_BATCH_ARCHIVES:
            raise ValueError(
                f"The selected paths contain more than {MAX_BATCH_ARCHIVES} CBZ archives"
            )
        return jsonify({
            "sources": archives,
            "skipped": skipped,
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        app.logger.exception(f"Could not scan local CBZ sources: {e}")
        return jsonify({
            "error": f"Could not scan local sources ({type(e).__name__}: {e})"
        }), 500

@app.route('/api/process', methods=['POST'])
def process():
    """API endpoint to process uploads or a localhost CBZ path."""
    files = request.files.getlist('files') if 'files' in request.files else []
    source_path_value = request.form.get('source_path', '').strip().strip('"')
    source_relative_path = request.form.get('source_relative_path', '').strip()
    custom_output_root = request.form.get('output_root', '').strip().strip('"')
    output_location = request.form.get('output_location', 'automatic').strip()
    reprocess = request.form.get('reprocess', '').casefold() in {'1', 'true', 'yes', 'on'}

    if (custom_output_root or output_location == 'camelia') and not request_is_local():
        return jsonify({"error": "Local output paths are restricted to this computer"}), 403

    if source_path_value and files:
        return jsonify({
            "error": "Choose either uploaded files or a local CBZ path, not both"
        }), 400
    if not source_path_value and (not files or files[0].filename == ''):
        return jsonify({"error": "No images or CBZ archive selected"}), 400

    try:
        model_types = parse_model_types(request.form)
        output_root = resolve_output_root(output_location, custom_output_root)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    
    # Generate a session ID
    session_id = str(uuid.uuid4())
    process_logs[session_id] = queue.Queue()
    process_status[session_id] = "processing"
    
    temp_dir = tempfile.mkdtemp()
    saved_paths = []
    processing_started = False
    archive_context = None
    
    try:
        used_names = set()
        job_log(
            session_id,
            'JOB',
            f"Accepted request; models: {' -> '.join(model_types)}; "
            f"output: {output_root or 'automatic'}",
        )
        if source_path_value:
            if not request_is_local():
                return jsonify({
                    "error": "Local-path processing is restricted to this computer"
                }), 403

            source_path = normalize_local_path(source_path_value)
            if not os.path.isfile(filesystem_path(source_path)):
                raise ValueError(f"CBZ file not found: {source_path}")
            if os.path.splitext(source_path)[1].lower() != '.cbz':
                raise ValueError("The local source path must point to a .cbz file")

            output_path = planned_archive_destination(
                os.path.basename(source_path),
                relative_path=source_relative_path,
                source_path=source_path,
                output_root=output_root,
            )
            if not path_is_under_comix(output_path):
                message = (
                    f"Skipped {os.path.basename(source_path)}: destination is not "
                    f"under a comix directory ({output_path})"
                )
                job_log(session_id, 'FILTER', message)
                raise ValueError(message)
            inspection = inspect_cbz_eligibility(filesystem_path(source_path))
            if inspection.get('reason') == 'Archive could not be inspected':
                raise ValueError(f"Cannot inspect CBZ: {inspection.get('parse_error')}")
            model_types = pending_decensor_methods(inspection, model_types, reprocess)
            if not model_types:
                raise ValueError('Selected methods are already recorded; enable reprocess to run them again')
            if os.path.exists(filesystem_path(output_path)):
                raise ValueError(f"Output archive already exists: {output_path}")

            staged_archive = os.path.join(temp_dir, 'source.cbz')
            job_log(session_id, 'STAGE', f"Copying archive from {source_path}")
            copy_file_long_path(source_path, staged_archive)
            saved_paths, archive_details = extract_cbz_images(
                staged_archive,
                temp_dir,
                used_names
            )
            archive_context = {
                **archive_details,
                "original_name": os.path.basename(source_path),
                "source_path": source_path,
                "relative_path": source_relative_path or os.path.basename(source_path),
                "output_root": output_root or None,
            }
            job_log(
                session_id,
                'ARCHIVE',
                f"Extracted {len(saved_paths)} image(s) from {source_path}; "
                f"destination: {output_path}",
            )
        else:
            supported_files = [
                file for file in files
                if file and allowed_file(file.filename)
            ]
            archive_files = [
                file for file in supported_files
                if file.filename.rsplit('.', 1)[-1].lower() in ARCHIVE_EXTENSIONS
            ]
            if archive_files and len(supported_files) != 1:
                raise ValueError(
                    "Process one CBZ archive at a time without loose images"
                )

            for file in supported_files:
                extension = file.filename.rsplit('.', 1)[-1].lower()
                filename = secure_filename(file.filename)
                if not filename:
                    filename = f"upload.{extension}"
                filename = unique_filename(filename, used_names)
                filepath = os.path.join(temp_dir, filename)
                file.save(filepath)

                if extension in ARCHIVE_EXTENSIONS:
                    inspection = inspect_cbz_eligibility(filepath)
                    if inspection.get('reason') == 'Archive could not be inspected':
                        raise ValueError(f"Cannot inspect CBZ: {inspection.get('parse_error')}")
                    model_types = pending_decensor_methods(inspection, model_types, reprocess)
                    if not model_types:
                        raise ValueError('Selected methods are already recorded; enable reprocess to run them again')
                    output_path = planned_archive_destination(
                        file.filename,
                        relative_path=source_relative_path or file.filename,
                        source_path=None,
                        output_root=output_root,
                    )
                    if not path_is_under_comix(output_path):
                        message = (
                            f"Skipped {file.filename}: destination is not under a "
                            f"comix directory ({output_path})"
                        )
                        job_log(session_id, 'FILTER', message)
                        raise ValueError(message)
                    if os.path.exists(filesystem_path(output_path)):
                        raise ValueError(f"Output archive already exists: {output_path}")
                    archive_images, archive_details = extract_cbz_images(
                        filepath,
                        temp_dir,
                        used_names
                    )
                    saved_paths.extend(archive_images)
                    archive_context = {
                        **archive_details,
                        "original_name": file.filename,
                        "relative_path": source_relative_path or file.filename,
                        "source_path": None,
                        "output_root": output_root or None,
                    }
                    job_log(
                        session_id,
                        'ARCHIVE',
                        f"Extracted {len(archive_images)} image(s) from {file.filename}; "
                        f"destination: {output_path}",
                    )
                else:
                    saved_paths.append(filepath)
        
        if not saved_paths:
            job_log(session_id, 'VALIDATION', 'No valid image or CBZ files provided')
            process_status[session_id] = "error"
            return jsonify({"error": "No valid image or CBZ files provided"}), 400
        
        # Start processing in a separate thread
        process_thread = threading.Thread(
            target=process_images_thread,
            args=(
                saved_paths,
                model_types,
                session_id,
                temp_dir,
                archive_context
            )
        )
        process_thread.daemon = True
        process_thread.start()
        processing_started = True
        
        # Return session ID
        return jsonify({
            "success": True,
            "session_id": session_id,
            "message": "Processing started"
        })
        
    except ValueError as e:
        app.logger.warning(f"Invalid upload: {e}")
        job_log(session_id, 'ERROR', f"{type(e).__name__}: {e}")
        process_status[session_id] = "error"
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        app.logger.exception(f"Error starting process for session {session_id}: {e}")
        job_log(
            session_id,
            'ERROR',
            f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
        )
        process_status[session_id] = "error"
        return jsonify({
            "error": f"Could not prepare processing job ({type(e).__name__}: {e})",
            "session_id": session_id,
        }), 500
    finally:
        # Once started, the worker owns this directory and removes it after
        # processing. Clean it here only when no worker was launched.
        if not processing_started and os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)

@app.route('/api/status/<session_id>', methods=['GET'])
def get_status(session_id):
    """Get the current status of a processing job."""
    if session_id not in process_status:
        return jsonify({"error": "Session not found"}), 404
        
    status = process_status[session_id]
    
    if status == "completed":
        response = {
            "status": status,
            "session_id": session_id,
        }

        if f"results_{session_id}" in app.config:
            results = app.config[f"results_{session_id}"]
            response["results"] = [
                {"filename": result["filename"]}
                for result in results
            ]

        if f"archive_{session_id}" in app.config:
            archive_result = app.config[f"archive_{session_id}"]
            response["archive"] = {
                "filename": archive_result["filename"],
                "downloadable": archive_result["downloadable"],
                "local_path": (
                    archive_result["path"]
                    if not archive_result["downloadable"]
                    else None
                ),
                "original_deleted": archive_result["original_deleted"],
            }

        return jsonify(response)
    
    return jsonify({
        "status": status,
        "session_id": session_id
    })

@app.route('/api/archives/<session_id>/<filename>', methods=['GET'])
def get_archive(session_id, filename):
    """Download a processed CBZ created from an uploaded archive."""
    if '..' in session_id or '..' in filename:
        return jsonify({"error": "Invalid path"}), 400

    archive_result = app.config.get(f"archive_{session_id}")
    if (
        not archive_result
        or not archive_result["downloadable"]
        or filename != archive_result["filename"]
    ):
        return jsonify({"error": "Archive not found"}), 404

    filepath = archive_result["path"]
    if not os.path.isfile(filepath) or not filepath.lower().endswith('.cbz'):
        return jsonify({"error": "Archive not found"}), 404

    return send_file(
        filepath,
        mimetype='application/vnd.comicbook+zip',
        as_attachment=True,
        download_name=os.path.basename(filepath)
    )

@app.route('/api/logs/<session_id>', methods=['GET'])
def stream_logs(session_id):
    """Stream logs for a given session."""
    if session_id not in process_logs:
        return jsonify({"error": "Session not found"}), 404
    
    def generate():
        log_queue = process_logs[session_id]

        def event(message):
            lines = str(message).splitlines() or ['']
            return ''.join(f"data: {line}\n" for line in lines) + '\n'
        
        # Send any existing logs
        while not log_queue.empty():
            log_message = log_queue.get()
            yield event(log_message)
        
        # Stream new logs as they come in
        while session_id in process_status and process_status[session_id] in ["processing", "starting"]:
            try:
                try:
                    log = log_queue.get(timeout=0.1)
                    if log:
                        yield event(log)
                except queue.Empty:
                    yield f": keep-alive\n\n"
            except Exception as e:
                app.logger.error(f"Error in log streaming: {e}")
                break
        
        # Send any remaining logs
        while not log_queue.empty():
            log_message = log_queue.get()
            yield event(log_message)
        
        # Send completion message
        if session_id in process_status:
            status = process_status[session_id]
            yield event(f"Processing {status}")
    
    response = Response(stream_with_context(generate()), mimetype="text/event-stream")
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    return response

@app.route('/api/results/<session_id>/<filename>', methods=['GET'])
def get_result(session_id, filename):
    """API endpoint to get a processed image."""
    if '..' in session_id or '..' in filename:
        return jsonify({"error": "Invalid path"}), 400
    
    output_format = request.args.get('format', None)
    
    filepath = os.path.join(CAMELIA_OUTPUT, session_id, secure_filename(filename))
    
    if not os.path.exists(filepath):
        return jsonify({"error": "File not found"}), 404
    
    if not output_format:
        return send_file(filepath)
    
    if output_format.lower() not in ['png', 'jpeg', 'webp']:
        return jsonify({"error": "Invalid format requested"}), 400
        
    try:
        img = Image.open(filepath)
        
        output_buffer = io.BytesIO()
        
        if output_format.lower() == 'jpeg':
            if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[3] if img.mode == 'RGBA' else None)
                img = background
            img.save(output_buffer, format='JPEG', quality=100, optimize=True)
        elif output_format.lower() == 'webp':
            img.save(output_buffer, format='WEBP', quality=100, method=6)
        else:
            img.save(output_buffer, format=output_format.upper())
            
        output_buffer.seek(0)
        
        mimetype = f'image/{output_format.lower()}'
        if output_format.lower() == 'jpeg':
            mimetype = 'image/jpeg'
            
        return send_file(
            output_buffer,
            mimetype=mimetype,
            as_attachment=False,
            download_name=f"{os.path.splitext(filename)[0]}.{output_format.lower()}"
        )
        
    except Exception as e:
        app.logger.error(f"Error converting image: {e}")
        return jsonify({"error": f"Error converting image: {str(e)}"}), 500

@app.route('/api/original/<filename>', methods=['GET'])
def get_original(filename):
    """API endpoint to get the original image (for comparison)."""
    if '..' in filename:
        return jsonify({"error": "Invalid path"}), 400
    
    filepath = os.path.join(CAMELIA_TEMP, "images", secure_filename(filename))
    
    if not os.path.exists(filepath):
        return jsonify({"error": "File not found"}), 404
    
    return send_file(filepath)

@app.route('/api/cancel/<session_id>', methods=['POST'])
def cancel_job(session_id):
    """Cancel a running processing job."""
    if session_id not in process_status:
        return jsonify({"error": "Session not found"}), 404
    
    try:
        if process_status[session_id] == "processing":
            job_log(session_id, 'CANCEL', 'Cancellation requested by user')
            process_status[session_id] = "cancelled"

            running_process = active_processes.get(session_id)
            if running_process and running_process.poll() is None:
                try:
                    parent = psutil.Process(running_process.pid)
                    children = parent.children(recursive=True)
                    for child in children:
                        child.terminate()
                    parent.terminate()
                    _, alive = psutil.wait_procs([*children, parent], timeout=3)
                    for remaining in alive:
                        remaining.kill()
                    job_log(
                        session_id,
                        'CANCEL',
                        f"Stopped subprocess tree rooted at PID {running_process.pid}",
                    )
                except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                    job_log(session_id, 'CANCEL', f"Process stop warning: {exc}")
            
            return jsonify({
                "success": True,
                "message": "Job cancelled successfully"
            })
        else:
            return jsonify({
                "success": False,
                "message": f"Job is not in a cancellable state. Current status: {process_status[session_id]}"
            }), 400
    except Exception as e:
        app.logger.error(f"Error cancelling job: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


if __name__ == '__main__':
    # Ensure output directories exist
    ensure_directory(CAMELIA_TEMP)
    ensure_directory(CAMELIA_OUTPUT)
    if (os.environ.get('WERKZEUG_RUN_MAIN') == 'true'
            and library_backlog.store.schedule_enabled()
            and library_backlog.store.scheduled_autostart()):
        # Only the serving process may own the scheduled worker. The store
        # safely requeues interrupted work before this point.
        try:
            library_backlog.resume_queue(retry_failed=False)
        except ValueError as exc:
            app.logger.error("Scheduled backlog could not resume safely: %s", exc)
            library_backlog.pause_queue()
    app.run(host='0.0.0.0', port=5000, debug=True)
