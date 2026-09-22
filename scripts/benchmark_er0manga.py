"""Read-only synthetic-mask benchmark for Camelia's installed LaMa checkpoint.

The input CBZs are opened in read mode and no image is written back to them.
Ground truth is an intact crop; only an in-memory copy receives artificial
black-bar or mosaic damage. The reported scores therefore measure restoration
of *synthetic* damage, not recovery of real censored pixels.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import statistics
import sys
import time
import zipfile
from pathlib import Path

import cv2
import numpy as np
import torch


REPO_ROOT = Path(__file__).resolve().parents[1]
LAMA_ROOT = REPO_ROOT / "lama-inpainting"
CHECKPOINT = LAMA_ROOT / "pretrained" / "best"
PUBLISHED_ER0MANGA_SHA256 = "dec1f60c927f9a8c70c480ef1d89459c9c11d001540ac31977cf6a8891daa32e"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_inpainter():
    script = LAMA_ROOT / "bin" / "uncen.py"
    sys.path[:0] = [str(script.parent), str(LAMA_ROOT)]
    try:
        spec = importlib.util.spec_from_file_location("camelia_benchmark_uncen", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(script.parent))
        sys.path.remove(str(LAMA_ROOT))
    return module


def sample_page(archive_path: Path, crop_size: int) -> tuple[np.ndarray, str]:
    with zipfile.ZipFile(archive_path) as archive:
        members = sorted(
            (member for member in archive.infolist()
             if not member.is_dir() and Path(member.filename).suffix.casefold() in IMAGE_SUFFIXES),
            key=lambda member: member.filename.casefold(),
        )
        if not members:
            raise ValueError("Archive has no supported image members")
        candidates = members[len(members) // 2:] + members[:len(members) // 2]
        for member in candidates:
            data = np.frombuffer(archive.read(member), dtype=np.uint8)
            image = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if image is None or min(image.shape[:2]) < crop_size:
                continue
            height, width = image.shape[:2]
            top = (height - crop_size) // 2
            left = (width - crop_size) // 2
            crop = cv2.cvtColor(image[top:top + crop_size, left:left + crop_size], cv2.COLOR_BGR2RGB)
            return crop, member.filename
    raise ValueError(f"Archive has no decodable page at least {crop_size}px wide and tall")


def damage_image(truth: np.ndarray, damage_type: str) -> tuple[np.ndarray, np.ndarray]:
    size = truth.shape[0]
    damaged = truth.copy()
    mask = np.zeros((size, size), dtype=np.uint8)
    if damage_type == "bar":
        y0, y1 = int(size * .46), int(size * .54)
        x0, x1 = int(size * .27), int(size * .73)
        damaged[y0:y1, x0:x1] = 0
    elif damage_type == "mosaic":
        y0, y1 = int(size * .34), int(size * .66)
        x0, x1 = int(size * .34), int(size * .66)
        region = damaged[y0:y1, x0:x1]
        tiny = cv2.resize(region, (12, 12), interpolation=cv2.INTER_AREA)
        damaged[y0:y1, x0:x1] = cv2.resize(tiny, (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST)
    else:
        raise ValueError(f"Unknown synthetic damage type: {damage_type}")
    mask[y0:y1, x0:x1] = 255
    return damaged, mask


def scores(truth: np.ndarray, output: np.ndarray, mask: np.ndarray) -> dict:
    selected = mask > 0
    errors = truth.astype(np.float32) - output.astype(np.float32)
    mse = float(np.mean(np.square(errors[selected])))
    mae = float(np.mean(np.abs(errors[selected])))
    truth_gray = cv2.cvtColor(truth, cv2.COLOR_RGB2GRAY)
    output_gray = cv2.cvtColor(output, cv2.COLOR_RGB2GRAY)
    truth_edges = cv2.Laplacian(truth_gray, cv2.CV_32F)
    output_edges = cv2.Laplacian(output_gray, cv2.CV_32F)
    return {
        "masked_psnr_db": round(10 * math.log10(255 * 255 / mse), 3) if mse else 99.0,
        "masked_mae": round(mae, 3),
        "masked_edge_mae": round(float(np.mean(np.abs((truth_edges - output_edges)[selected]))), 3),
    }


def benchmark(archives: list[Path], crop_size: int, device: str) -> dict:
    checkpoint_file = CHECKPOINT / "models" / "best.ckpt"
    checkpoint_hash = sha256_file(checkpoint_file)
    if checkpoint_hash != PUBLISHED_ER0MANGA_SHA256:
        raise RuntimeError(f"Installed checkpoint differs from published Er0manga copy: {checkpoint_hash}")
    inpainter = load_inpainter()
    started = time.perf_counter()
    model = inpainter.init_inpaint_model(str(CHECKPOINT))
    model.to(torch.device(device))
    load_seconds = time.perf_counter() - started
    warmup_truth, _ = sample_page(archives[0], crop_size)
    warmup_damaged, warmup_mask = damage_image(warmup_truth, "bar")
    if device.startswith("cuda"):
        torch.cuda.synchronize()
    started = time.perf_counter()
    inpainter.inpaint(model, warmup_damaged, np.repeat(warmup_mask[:, :, None], 3, axis=2))
    if device.startswith("cuda"):
        torch.cuda.synchronize()
    warmup_seconds = time.perf_counter() - started
    samples = []
    for archive_path in archives:
        truth, member_name = sample_page(archive_path, crop_size)
        for damage_type in ("bar", "mosaic"):
            damaged, mask = damage_image(truth, damage_type)
            mask_rgb = np.repeat(mask[:, :, None], 3, axis=2)
            if device.startswith("cuda"):
                torch.cuda.synchronize()
                torch.cuda.reset_peak_memory_stats()
            started = time.perf_counter()
            restored, _debug = inpainter.inpaint(model, damaged, mask_rgb)
            if device.startswith("cuda"):
                torch.cuda.synchronize()
            model_seconds = time.perf_counter() - started
            started = time.perf_counter()
            telea = cv2.inpaint(damaged, mask, 3, cv2.INPAINT_TELEA)
            telea_seconds = time.perf_counter() - started
            samples.append({
                "archive": str(archive_path), "page": member_name,
                "damage": damage_type,
                "er0manga": {**scores(truth, restored, mask), "seconds": round(model_seconds, 3)},
                "telea": {**scores(truth, telea, mask), "seconds": round(telea_seconds, 3)},
                "gpu_peak_allocated_mib": round(torch.cuda.max_memory_allocated() / 1048576, 1)
                    if device.startswith("cuda") else None,
                "masked_output_changed_fraction": round(float(np.mean(
                    np.any(restored != damaged, axis=2)[mask > 0]
                )), 4),
            })
    report = {
        "checkpoint_sha256": checkpoint_hash,
        "device": device,
        "crop_size": crop_size,
        "model_load_seconds": round(load_seconds, 3),
        "warmup_seconds": round(warmup_seconds, 3),
        "method": "in-memory synthetic masks over intact CBZ page crops",
        "samples": samples,
    }
    for method in ("er0manga", "telea"):
        report[method + "_aggregate"] = {
            metric: round(float(np.mean([sample[method][metric] for sample in samples])), 3)
            for metric in ("masked_psnr_db", "masked_mae", "masked_edge_mae", "seconds")
        }
        report[method + "_aggregate"]["median_seconds"] = round(
            statistics.median(sample[method]["seconds"] for sample in samples), 3
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", nargs="+", type=Path, help="Read-only source CBZ paths")
    parser.add_argument("--crop-size", type=int, default=512)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.crop_size < 256 or args.crop_size % 8:
        parser.error("--crop-size must be at least 256 and divisible by 8")
    report = benchmark(args.archive, args.crop_size, args.device)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "samples"}, indent=2))
    print(f"Sample details: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
