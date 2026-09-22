"""Process one CBZ for local automation integrations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api import process_cbz_archive_sync, process_logs  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    """Build the stable command-line interface used by CBZ Watcher."""
    parser = argparse.ArgumentParser(description="AI-decensor one CBZ archive")
    parser.add_argument("archive", type=Path)
    parser.add_argument(
        "--model-type",
        choices=("black_bars", "white_bars", "transparent_black", "mosaic"),
        action="append",
        dest="model_types",
        help=(
            "Camelia stage to run. Repeat this option to run stages in order; "
            "defaults to black_bars."
        ),
    )
    parser.add_argument(
        "--reprocess", action="store_true",
        help="Run the selected methods again even when their ComicInfo tags are present.",
    )
    output_mode = parser.add_mutually_exclusive_group()
    output_mode.add_argument(
        "--replace",
        action="store_true",
        help="Keep the existing filename and atomically replace its contents.",
    )
    output_mode.add_argument(
        "--copy",
        action="store_true",
        help="Write a verified archive to --output-dir without changing the source.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Required with --copy; existing destination archives are never overwritten.",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        help="Required with --replace; stores the untouched source archive.",
    )
    parser.add_argument(
        "--destination-dir",
        type=Path,
        help=(
            "Planned library destination used for the Comix eligibility check. "
            "The archive is still replaced at its current path."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one archive job and emit a machine-readable final result line."""
    args = build_parser().parse_args(argv)
    if args.replace and args.backup_dir is None:
        build_parser().error("--replace requires --backup-dir")
    if args.copy and args.output_dir is None:
        build_parser().error("--copy requires --output-dir")

    try:
        result, session_id = process_cbz_archive_sync(
            args.archive,
            model_types=args.model_types or ["black_bars"],
            output_mode="replace" if args.replace else "copy" if args.copy else "suffix",
            backup_dir=args.backup_dir,
            destination_dir=args.destination_dir,
            output_dir=args.output_dir,
            reprocess=args.reprocess,
        )
        while not process_logs[session_id].empty():
            print(process_logs[session_id].get(), flush=True)
        print(f"CAMELIA_RESULT={json.dumps(result)}", flush=True)
        return 0
    except Exception as exc:
        print(f"Camelia failed: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
