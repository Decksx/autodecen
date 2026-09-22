"""Configuration boundary for the optional Aletheia-Lens mosaic backend.

The GPL-licensed Aletheia-Lens checkout and its Python environment stay in a
separate, ignored directory.  Camelia only invokes its public processing
entrypoint in a subprocess, which also prevents its pinned ONNX dependencies
from colliding with Camelia's older PyTorch environment.
"""

from __future__ import annotations

import os
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parent
DEFAULT_ALETHEIA_ROOT = WORKSPACE_ROOT / ".third_party" / "Aletheia-Lens"
ALETHEIA_REQUIRED_FILES = (
    Path("detector.py"),
    Path("decensor.py"),
    Path("predict.py"),
    Path("models/mrcnn/weights.onnx"),
    Path("models/deepcreampy/mosaic.onnx"),
    # The upstream predict module initializes both sessions at import time.
    Path("models/deepcreampy/bar.onnx"),
)


def aletheia_root() -> Path:
    """Return the configured Aletheia-Lens source checkout."""
    configured = os.environ.get("CAMELIA_ALETHEIA_ROOT")
    return Path(configured).expanduser().resolve() if configured else DEFAULT_ALETHEIA_ROOT


def aletheia_python(root: Path | None = None) -> Path:
    """Return the isolated interpreter used for Aletheia-Lens inference."""
    configured = os.environ.get("CAMELIA_ALETHEIA_PYTHON")
    if configured:
        return Path(configured).expanduser().resolve()

    root = root or aletheia_root()
    relative = Path(".venv/Scripts/python.exe") if os.name == "nt" else Path(".venv/bin/python")
    candidate = root / relative
    return candidate


def missing_runtime_files(root: Path | None = None) -> list[Path]:
    """List missing source/model files without importing the external project."""
    root = root or aletheia_root()
    missing = [root / relative for relative in ALETHEIA_REQUIRED_FILES if not (root / relative).is_file()]
    python = aletheia_python(root)
    if not python.is_file():
        missing.append(python)
    return missing


def validate_aletheia_runtime(root: Path | None = None) -> tuple[Path, Path]:
    """Validate the external backend and return ``(root, python)``."""
    root = root or aletheia_root()
    missing = missing_runtime_files(root)
    if missing:
        details = "\n".join(f"  - {path}" for path in missing)
        raise FileNotFoundError(
            "Aletheia-Lens mosaic support is not installed. Run "
            "scripts\\setup_aletheia.ps1, or set CAMELIA_ALETHEIA_ROOT and "
            "CAMELIA_ALETHEIA_PYTHON. Missing:\n" + details
        )
    return root, aletheia_python(root)


def build_mosaic_command(input_dir: str | os.PathLike, output_dir: str | os.PathLike) -> list[str]:
    """Build the isolated directory-processing command for one mosaic pass."""
    root, python = validate_aletheia_runtime()
    return [
        str(python),
        "-u",
        str(WORKSPACE_ROOT / "scripts" / "aletheia_mosaic.py"),
        "--aletheia-root",
        str(root),
        "--input-dir",
        str(Path(input_dir).resolve()),
        "--output-dir",
        str(Path(output_dir).resolve()),
    ]
