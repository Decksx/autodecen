# Er0manga LaMa pilot (2026-09-16)

The installed Camelia checkpoint at `lama-inpainting/pretrained/best/models/best.ckpt`
has SHA-256 `dec1f60c927f9a8c70c480ef1d89459c9c11d001540ac31977cf6a8891daa32e`.
That is the published Git-LFS hash of a copy identified as the [Er0manga
checkpoint](https://huggingface.co/df1412/er0manga-inpaint/commit/a37cd1d749641b8eda91815d57eb7ef03d212fa2).
The [author's repository](https://github.com/Er0manga/Er0mangaInpaint) describes
the checkpoint as manga-finetuned. Camelia was already running this model; no
second copy was installed.

`scripts/benchmark_er0manga.py` opens four `X:\comix` CBZs **read-only**, takes
one intact 512×512 page crop from each, applies an artificial black bar and an
artificial mosaic mask to in-memory copies, and compares the reconstruction
against the intact crop. OpenCV Telea is a simple non-neural reference. Neither
the source CBZs nor the live ComicAutomation database are written.

| Metric (8 masked crops) | Er0manga LaMa | OpenCV Telea |
| --- | ---: | ---: |
| Mean masked PSNR (higher is better) | 18.38 dB | 14.03 dB |
| Mean masked absolute pixel error (lower) | 17.51 | 31.56 |
| Mean masked edge error (lower) | 26.33 | 24.82 |
| Median inference time after warm-up | 0.163 s | 0.017 s |

Hardware: RTX 3080 (10 GB), Ryzen 9 3900X, 64 GB RAM. Model load took 0.924 s;
the first inference/warm-up took 5.75 s. Peak PyTorch GPU allocation during
the measured crops was 228.2 MiB (not total process or driver VRAM). The model
changed 94–100% of pixels within the artificial masks, so the results are not
an accidental no-op.

On one of the same books (two masks), CPU inference had a 0.982 s median
after a 1.337 s warm-up. The corresponding GPU measurements were 0.156 and
0.160 s, about 6.2× faster on this narrow crop test. This does not predict
whole-book throughput; archive I/O, segmentation and other stages also matter.

This is a **small synthetic-mask pilot**, not a claim that real censorship can
be reversed or that one model is visually superior. Its edge metric slightly
favors Telea, and the sample does not cover full-page runtime, mask detection
accuracy, diverse screentones, or human review. Keep experimental model changes
and bulk reprocessing disabled until a broader paired and visual comparison.
