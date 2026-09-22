"""Batch adapter for Aletheia-Lens mode II (automatic mosaic repair)."""

from __future__ import annotations

import argparse
import importlib
import os
import shutil
import sys
from pathlib import Path


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".avif", ".bmp", ".tif", ".tiff"}


def natural_sort_key(path: Path):
    import re

    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", str(path))]


def load_processor(aletheia_root: Path):
    """Import Aletheia-Lens from its isolated checkout, never from site-packages."""
    root = aletheia_root.resolve()
    if not (root / "detector.py").is_file() or not (root / "decensor.py").is_file():
        raise FileNotFoundError(f"Aletheia-Lens detector/decensor modules were not found in {root}")
    os.chdir(root)
    sys.path.insert(0, str(root))
    detector = importlib.import_module("detector")
    decensor = importlib.import_module("decensor")

    def process_if_detected(image_bytes):
        import io
        import numpy as np
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as opened:
            image = opened.copy()
        detection = detector.detect_image(np.asarray(image.convert("RGB")))
        classes = detection["class_ids"]
        mosaic_masks = detection["masks"][:, :, classes == 2]
        if not np.any(mosaic_masks):
            return None
        mask = np.any(mosaic_masks, axis=2).astype(np.bool_)
        return decensor.decensor(image, image, is_mosaic=True, repair_mask=mask)

    return process_if_detected


def process_directory(input_dir: Path, output_dir: Path, processor) -> int:
    """Process every staged page and preserve its filename for Camelia."""
    images = sorted(
        (path for path in input_dir.rglob("*") if path.is_file() and not path.is_symlink()
         and path.suffix.casefold() in IMAGE_EXTENSIONS),
        key=natural_sort_key,
    )
    if not images:
        raise ValueError(f"No supported images found in {input_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    for index, source in enumerate(images, start=1):
        print(f"[Aletheia-Lens] {index}/{len(images)} detecting mosaics in {source.name}", flush=True)
        result = processor(source.read_bytes())
        destination = output_dir / source.relative_to(input_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if result is None:
            shutil.copyfile(source, destination)
            print(f"[Aletheia-Lens] no mosaic detected; copied {source.name} unchanged", flush=True)
            continue
        # A selected mosaic-only pass can also serve standalone images, where
        # the filename/bytes must agree. CBZ rebuilding separately retains the
        # original member's image metadata and format.
        format_name = {
            ".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG",
            ".webp": "WEBP", ".avif": "AVIF", ".bmp": "BMP",
            ".tif": "TIFF", ".tiff": "TIFF",
        }[source.suffix.casefold()]
        if format_name == "JPEG" and result.mode not in {"RGB", "L"}:
            from PIL import Image

            background = Image.new("RGB", result.size, (255, 255, 255))
            if "A" in result.getbands():
                background.paste(result.convert("RGBA"), mask=result.getchannel("A"))
            else:
                background.paste(result.convert("RGB"))
            result = background
        save_options = {"quality": 95, "subsampling": 0} if format_name == "JPEG" else {}
        result.save(destination, format=format_name, **save_options)
        print(f"[Aletheia-Lens] wrote {destination.name}", flush=True)
    return len(images)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Aletheia-Lens automatic mosaic repair")
    parser.add_argument("--aletheia-root", required=True, type=Path)
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        input_dir = args.input_dir.resolve()
        output_dir = args.output_dir.resolve()
        processor = load_processor(args.aletheia_root)
        count = process_directory(input_dir, output_dir, processor)
        print(f"[Aletheia-Lens] completed {count} image(s)", flush=True)
        return 0
    except Exception as exc:
        print(f"[Aletheia-Lens] failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
