from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

import torch
import torch.nn.functional as F


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LAMA_BIN = REPOSITORY_ROOT / "lama-inpainting" / "bin"
LAMA_ROOT = REPOSITORY_ROOT / "lama-inpainting"
sys.path[:0] = [str(LAMA_BIN), str(LAMA_ROOT)]
try:
    module_spec = importlib.util.spec_from_file_location(
        "camelia_uncen",
        LAMA_BIN / "uncen.py",
    )
    uncen = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(uncen)
finally:
    sys.path.remove(str(LAMA_BIN))
    sys.path.remove(str(LAMA_ROOT))


class InpaintingPaddingTests(unittest.TestCase):
    def test_legal_padding_remains_reflection_padding(self):
        tensor = torch.arange(45, dtype=torch.float32).reshape(1, 1, 5, 9)

        actual = uncen.pad_crop_context(tensor, 2)
        expected = F.pad(tensor, (2, 2, 2, 2), mode="reflect")

        self.assertTrue(torch.equal(actual, expected))

    def test_oversized_padding_falls_back_per_axis(self):
        # Mirrors the reported landscape pages: the padding is legal for the
        # width but too large for the height.
        tensor = torch.arange(45, dtype=torch.float32).reshape(1, 1, 5, 9)

        padded = uncen.pad_crop_context(tensor, 6)

        self.assertEqual(tuple(padded.shape), (1, 1, 17, 21))
        # Vertical replication makes every added top row equal to the first
        # horizontally-reflected source row.
        self.assertTrue(torch.equal(padded[:, :, 0, :], padded[:, :, 6, :]))

    def test_padding_larger_than_both_dimensions_is_supported(self):
        tensor = torch.tensor([[[[1.0, 2.0], [3.0, 4.0]]]])

        padded = uncen.pad_crop_context(tensor, 3)

        self.assertEqual(tuple(padded.shape), (1, 1, 8, 8))
        self.assertEqual(padded[0, 0, 0, 0].item(), 1.0)
        self.assertEqual(padded[0, 0, -1, -1].item(), 4.0)


if __name__ == "__main__":
    unittest.main()
