"""Read-only per-CBZ censorship type check using Aletheia-Lens detection.

The detector runs once per page in its isolated environment. Its generic bar
mask is separated into black, white, and translucent/mid-tone bar types using
the median color under the mask. No source archive is written here.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import os
import sys
import zipfile
from pathlib import Path


METHODS = ("black_bars", "transparent_black", "white_bars", "mosaic")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".avif", ".bmp", ".tif", ".tiff"}
MINIMUM_SCORE = 0.90


def archive_pages(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    pages = [member for member in archive.infolist()
             if not member.is_dir() and Path(member.filename).suffix.casefold() in IMAGE_EXTENSIONS]
    if not pages:
        raise ValueError("CBZ has no supported image pages to inspect")
    return pages


def reliable_region(mask, *, minimum_fraction: float = 0.0005,
                    minimum_pixels: int = 256) -> bool:
    """Require a contiguous region, not a few noisy detector pixels."""
    import cv2
    import numpy as np

    binary = np.asarray(mask, dtype=np.uint8)
    if binary.ndim != 2 or not binary.any():
        return False
    count, _, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    required = max(minimum_pixels, int(binary.size * minimum_fraction))
    return count > 1 and bool((stats[1:, cv2.CC_STAT_AREA] >= required).any())


def classify_detection(pixels, detection: dict) -> set[str]:
    """Assign one type per high-confidence detector region on a page."""
    import numpy as np

    found: set[str] = set()
    masks = detection["masks"]
    for index, (kind, score) in enumerate(zip(detection["class_ids"], detection["scores"])):
        if score < MINIMUM_SCORE:
            continue
        mask = masks[:, :, index]
        if not reliable_region(mask):
            continue
        if kind == 2:
            found.add("mosaic")
        elif kind == 1:
            # Median resists anti-aliased borders and underlying line art.
            luminance = float(np.median(np.asarray(pixels)[mask].mean(axis=1)))
            if luminance <= 64:
                found.add("black_bars")
            elif luminance >= 192:
                found.add("white_bars")
            else:
                found.add("transparent_black")
    return found


def load_classifier(aletheia_root: Path):
    import numpy as np
    from PIL import Image, ImageOps

    root = aletheia_root.resolve()
    if not (root / "detector.py").is_file():
        raise FileNotFoundError(f"Aletheia-Lens detector is missing: {root}")
    os.chdir(root)
    sys.path.insert(0, str(root))
    with contextlib.redirect_stdout(sys.stderr):
        detector = importlib.import_module("detector")

    def classify(payload: bytes) -> set[str]:
        with Image.open(io.BytesIO(payload)) as opened:
            pixels = np.asarray(ImageOps.exif_transpose(opened).convert("RGB"))
        with contextlib.redirect_stdout(sys.stderr):
            detection = detector.detect_image(pixels)
        return classify_detection(pixels, detection)

    return classify


def inspect_archive(path: str, classifier) -> dict:
    """Inspect every page; decoding or inference errors fail closed."""
    pages_by_method = {method: [] for method in METHODS}
    with zipfile.ZipFile(path) as archive:
        pages = archive_pages(archive)
        for index, member in enumerate(pages, start=1):
            try:
                found = classifier(archive.read(member))
            except Exception as exc:
                raise RuntimeError(f"Censorship check failed on {member.filename}: {exc}") from exc
            if not set(found).issubset(METHODS):
                raise ValueError(f"Detector returned an unknown type for {member.filename}")
            for method in found:
                pages_by_method[method].append(member.filename)
            if index == 1 or index == len(pages) or index % 25 == 0:
                print(f"Checked {index}/{len(pages)} pages", file=sys.stderr, flush=True)
    return {"page_count": len(pages), "pages_by_method": pages_by_method,
            "detected_methods": [method for method in METHODS if pages_by_method[method]]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only censorship type check for one CBZ")
    parser.add_argument("--archive", required=True)
    parser.add_argument("--aletheia-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = inspect_archive(args.archive, load_classifier(args.aletheia_root))
        print(json.dumps(result), flush=True)
        return 0
    except Exception as exc:
        print(f"Censorship check failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
