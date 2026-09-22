<div align="center">
  <a href="https://github.com/windbow27/camelia">
    <img src="https://raw.githubusercontent.com/windbow27/camelia/refs/heads/main/camelia-ui/public/logo.png" width="125" alt="Camelia Logo">
  </a>
  
  # Camelia
</div>

Camelia is an image decensor tool for black bars, white bars, transparent black
bars, and mosaic censorship. Mosaic detection and reconstruction use the
Aletheia-Lens mode II backend in an isolated environment.

## Installation

### Prerequisites

-   Python 3.9
-   Conda (recommended for managing environments)
-   NVIDIA GPU with CUDA support (optional but recommended for faster processing)
-   Node.js 16+ (for Web UI)

### Steps

1. Clone the repository:

    ```bash
    git clone https://github.com/windbow27/camelia
    cd camelia
    ```

2. Create a Conda environment:

    ```bash
    conda create --name camelia_env python=3.9 -y
    conda activate camelia_env
    ```

3. Install dependencies:

    ```bash
    pip install -r requirements.txt
    ```

4. Verify PyTorch installation:
   Ensure that PyTorch is installed with CUDA support (if applicable):

    ```bash
    python -c "import torch; print(torch.cuda.is_available())"
    ```

    This should return `True` if CUDA is available.

5. For Web UI, install Node.js dependencies:
    ```bash
    cd camelia-ui
    npm install
    cd ..
    ```

6. Install the optional Aletheia-Lens mosaic backend. This creates an isolated
   Python 3.10 environment below `.third_party`, checks out a pinned upstream
   revision, downloads the detector, and verifies its SHA-256 checksum:

    ```powershell
    powershell -ExecutionPolicy Bypass -File scripts\setup_aletheia.ps1
    ```

   Add `-Gpu` to install Aletheia-Lens's CUDA runtime. Its legacy DeepCreamPy
   reconstruction model still runs on CPU for numerical compatibility. To use
   an existing checkout instead, set `CAMELIA_ALETHEIA_ROOT` and optionally
   `CAMELIA_ALETHEIA_PYTHON` before starting Camelia. Python 3.10 must be
   available through `py -3.10` for the default setup script.

### Models

Download the models here: [models](https://drive.google.com/drive/folders/1AAyv6ms_694VGEtET6TFRLKZ1rx7ue11?usp=sharing)

Put the segmentation models in smp-segmentation/pretrained and inpainting model (the whole folder) in lama-inpainting/pretrained.

## Usage

### CLI Mode

1. Place your input images in the correct `camelia-decensor/input` directory (`input/black_bars`, `input/white_bars`, `input/transparent_black`, or `input/mosaic`)

    - Subdirectories can be used

2. Run:

    ```bash
    python main.py --model_type <model_type>
    ```

    Replace `<model_type>` with one of the following options:

    - `black_bars`
    - `white_bars`
    - `transparent_black`
    - `mosaic`

3. The output will be saved under `camelia-decensor/output`.

### Web UI Mode

On Windows, double-click `Start Camelia.bat` in the repository root. It starts
the API and web UI, waits until both are ready, and opens
http://localhost:3000. Keep the launcher window open and press Enter there to
stop both services. Service logs are written to the temporary
`camelia-launcher` directory.

To start the services manually instead:

1. Start the API server, making sure to use the correct environment:

    ```bash
    python api.py
    ```

2. In a separate terminal, start the web UI:

    ```bash
    cd camelia-ui
    npm run dev
    ```

3. Open your browser and navigate to http://localhost:3000

### CBZ batch processing

The web UI has one source area. You can choose/drop images, CBZ files, or a
folder; the expandable **Use local paths** option is the reliable choice for
very long Windows paths and large batches because it bypasses browser upload
limits. Enter one file or directory per line. Books are processed sequentially.

- Uploaded folder output mirrors the browser-supplied folder tree under
  `camelia-decensor/output/archives`; session UUIDs are not used as archive
  directory names.
- The output dropdown offers automatic placement, Camelia's output directory,
  or a custom local/mapped-drive directory. Source folder structure is mirrored
  below the selected root.
- Original CBZ filenames are always retained in output directories. Automatic
  placement beside a source adds ` - AI Decensored` only to avoid overwriting
  the source archive.
- Only CBZ files whose computed output destination contains a directory named
  `comix` are processed. Other destinations (including `manga` and
  `graphic novel`) are skipped with an explicit log entry.
- More than one censoring mode can be selected. The selected modes run in the
  displayed order, and each pass consumes the preceding pass's output.
- Each job is staged in a short temporary workspace and uses Windows
  extended-length filesystem paths for source and destination operations.
- Job logs identify each stage, archive and destination, subprocess command and
  working directory, child output, exit code, and exception details.
- Rebuilt archives preserve original member paths and ordering, `ComicInfo.xml`
  and other non-image files, archive comments, and ZIP entry metadata. Processed
  pages are encoded back into their original JPEG, PNG, WebP, or AVIF format.

For automation integrations, `scripts/process_cbz.py` processes one local CBZ.
Its `--replace` mode requires `--backup-dir`; it verifies the rebuilt archive
and saves the untouched source there before atomically replacing the archive.
For a non-destructive standalone run, `--copy --output-dir PATH_TO_COMIX`
writes a tagged, verified copy and leaves the source untouched. It refuses any
existing destination and uses a hard-link installation to avoid a concurrent
overwrite; the output filesystem must support hard links. Repeat
`--model-type` to choose ordered passes. Each successful archive records both
`uncensored` and exact ComicInfo `<Tags>` tokens such as `camelia:black_bars`,
`camelia:transparent_black`, `camelia:white_bars`, and `camelia:mosaic`.
Previously recorded selected methods are skipped by default, while methods
without tags can run later. `--reprocess` (or the GUI checkbox) deliberately
runs all selected methods again. Older archives carrying only a generic
`uncensored`/`decensored` marker have unknown method history and remain skipped
unless that override is enabled.

### Resumable library backlog

The **Library Decensor Backlog** section crawls either `X:\comix` or
`Z:\comics\Comix` recursively. It reads the filename and
`ComicInfo.xml` directly from each CBZ. Generic `uncensored`/`decensored`
markers without method tags are skipped, while books with method tags are
queued only for the remaining passes. Image pages are not extracted during
discovery.

Scan checkpoints, archive fingerprints, queue transitions, retry/error data,
stage events, and counts are stored in
`camelia-decensor/state/library_backlog.sqlite3`. The `book_stages` table
keeps a marker per method and archive: pending, running, pass finished,
failed/interrupted, or applied. A pass is marked **applied** only after the
rebuilt CBZ is installed and its method tags are verified; temporary work
from a failed install is not reported as applied. The UI shows the current
and recent books and can look up any crawled CBZ by path. Retry/reprocess
attempts retain the timestamp of an earlier successful application. Pass
start/finish timestamps and elapsed durations are persisted as well. Final
CBZ rebuild, ComicInfo.xml tagging, backup, destination installation, and
verification timings are retained per book, together with the total run time.
A full local timestamp prefixes each processing and backlog event line.
A restart changes interrupted
crawls to paused with the current directory pending, and returns interrupted
processing jobs to the queue. Use the UI to resume them; a source whose
fingerprint changed during interruption is refused for manual review rather
than silently processed again. Unchanged files reuse
their stored eligibility result instead of reopening the CBZ. Existing databases
gain an `eligibility_version` column automatically; previously scanned rows are
reclassified once under the method-aware policy, then return to fingerprint
reuse. The UI can manually queue one previously crawled book with selected
methods and an explicit reprocess override.
The crawl excludes sync and system history directories (`.stversions`,
`.stfolder`, `@eaDir`, `$RECYCLE.BIN`, and `System Volume Information`),
including directories retained by an older checkpoint. A previously queued
history file is skipped before model work.

Eligible books run sequentially through `black_bars`, `transparent_black`,
`white_bars`, and detector-gated Aletheia-Lens `mosaic` restoration using the
same shared pipeline as manual processing. The source is
backed up below `camelia-decensor/state/originals` before atomic replacement.
The backlog also offers an opt-in **Only process books with a detected
censorship type** mode. Before backup or replacement, Aletheia-Lens checks
every supported page in each queued CBZ. High-confidence bar detections are
classified as black, white, or mid-tone/transparent by the median color under
the detector mask; mosaic detections remain separate. Only detected, still
pending methods run. An archive with no reliable detection is skipped for
review, with its bytes and ComicInfo.xml unchanged—not labeled uncensored.
Detector results, affected page names, count, and elapsed time are saved in
the backlog database and reused after an interrupted job. A changed archive
invalidates its prior result. Manually queued methods bypass the automatic
check. The option is off by default, so an existing queue will retain its
current behavior until you pause it and enable the checkbox. Detection is a
conservative filter, not proof that a book is uncensored; review skipped books
or queue selected methods manually when a detector misses an unusual mask.
After a completed job, the original is SHA-256-verified into a series-named
folder on `F:\ai-decensor-originals` and only then removed from C:. Pending
local originals are retried before the queue claims another book or after a
restart. If the F: drive is unavailable, lacks space, or the original has no
usable ComicInfo Series, the queue pauses and leaves the C: copy intact.
After all passes succeed, Camelia adds idempotent generic and method tags to the
existing `ComicInfo.xml`. When metadata is absent, it creates a minimal
`ComicInfo.xml`; malformed existing metadata is left untouched and the job
fails for manual review.

Pause and stop requests for the processing queue take effect at the next safe
book boundary; Camelia does not terminate a model midway through an archive.
The backlog also has an opt-in `X:\comix` batch schedule: every night from
00:00–05:00 and Monday–Friday from 09:00–17:00 in America/Denver. Enable it in
the Library Decensor Backlog panel after choosing/crawling `X:\comix` **and**
configuring the ComicAutomation handoff described below. The
F: backup offload integration is also required. Set
`CAMELIA_BACKUP_ARCHIVE_ROOT` only if a different folder on F: is desired.
The schedule setting is stored in the same SQLite database and resumes after the
normal `python api.py` server restarts. The server must remain running; the
schedule does not launch Camelia or initiate a crawl. It only claims new books
from `X:\comix` inside a window; a book already processing is allowed to finish after
the window closes. Disabling the schedule pauses the queue after the current
book. A manual Pause/Stop suppresses automatic restart until Resume queue is
pressed; failed books are *not* automatically retried after restart.
`X:` processing is refused unless `CAMELIA_COMIC_AUTOMATION_DATABASE` names an
existing ComicAutomation database and the separate handoff module and Python
runtime are available. Optional overrides are `CAMELIA_COMIC_AUTOMATION_ROOT`
(default `C:\git\ComicAutomation`) and `CAMELIA_COMIC_AUTOMATION_PYTHON`
(default `C:\Python311\python.exe`). The handoff reads durable Camelia
completion events, preserves the same logical archive ID at the same path,
updates its whole-archive hash/current revision, and queues fresh inspection
and page-hash jobs in ComicAutomation. If it fails, Camelia pauses before
claiming another book; the completed event remains for an idempotent retry.
Before model work on each X: book, a registration preflight establishes an
untracked source's ComicAutomation identity and original hash/revision. A
changed, excluded, or ambiguous source is refused before using the GPU.
It never auto-applies ComicAutomation migrations. See that project's
`docs/camelia_decensor.md` for the read-only preview command and readiness
checks. Its watcher monitors an incoming folder, not the X: library.
The reviewed working ComicAutomation database is
`G:\ComicAutomation\TestDatabase\inspection-working.db` (migrations 001–014);
the similarly named `G:\ComicAutomation\database\comics.db` is an old copy.
Keep the queue disabled until the handoff has been previewed and the F: backup
destination has been checked on this machine.

The schedule currently governs the existing decensor pipeline, not a new
inpainting model. The installed LaMa checkpoint's SHA-256 matches a published
copy of the [Er0manga model](https://huggingface.co/df1412/er0manga-inpaint/commit/a37cd1d749641b8eda91815d57eb7ef03d212fa2),
so Camelia is already using it. A small synthetic-mask GPU benchmark is in
`docs/er0manga_benchmark.md`; general-image models such as
[PowerPaint](https://github.com/open-mmlab/PowerPaint) and
[MAT](https://github.com/fenglinglwb/MAT) are not assumed to improve manga
linework or screentones without a representative side-by-side pilot. No
full-library experimental-model run should be enabled before that pilot.

The final archive installation enforces an overwrite invariant: an existing
book marked uncensored/decensored cannot be replaced by an incoming archive
that lacks the same kind of marker. Refusals are explicit in the job log and
durable event history.

## Acknowledgments

-   [Er0manga](https://github.com/Er0manga/Er0mangaDemo)
-   [smp](https://github.com/qubvel-org/segmentation_models.pytorch)
-   [lama](https://github.com/advimman/lama)
-   [Aletheia-Lens](https://github.com/Cec1c/Aletheia-Lens) (separate GPL-3.0 backend)

### Aletheia-Lens licensing and operation

The setup script installs Aletheia-Lens as a separate, ignored checkout rather
than copying its GPL-3.0 source into Camelia. Camelia invokes it through a
subprocess adapter. The detector model is about 254 MB, and the first mosaic
pass loads both the detector and DeepCreamPy ONNX sessions. CPU processing can
therefore be substantially slower than the existing GPU-backed bar stages.

## Disclaimer

This project is intended for personal use only. Do not share the results on public sites. If you choose to do so anyway, please do not credit me or Camelia.

Sharing the tool is welcomed. A ☆ would also be greatly appreciated.
