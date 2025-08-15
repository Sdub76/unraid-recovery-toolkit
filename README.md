# Disaster Recovery Playbook (High‑Level Flow)

This is the intended end‑to‑end flow when things go sideways. It’s opinionated, boringly reliable, and tested against real-world “oh no” moments.

1. **Nightly hygiene** — Run the filelist script every night on Unraid, with Healthchecks monitoring the job.
2. **Disaster strikes** — Array corruption or accidental deletes slip past parity. (It happens. Deep breath.)
3. **Freeze backups** — Suspend Borg backups to avoid snapshotting bad state; extract the latest file list from the most recent archive for reference.
4. **Scope the blast radius** — Run `recovery_analysis.py` to build the Excel summary and see what’s missing by folder/depth.
5. **Audit the dark day** — Run `sonarr_deleted.py` and `radarr_deleted.py` to list what those apps marked as deleted around the incident window.
6. **Plan the recovery** — Run `recovery_plan.py` to classify: what still exists on the array, what should be in backup, what Sonarr/Radarr can redownload, and what’s truly missing.
7. **Verify the backup** — In a Borg shell, mount the latest archive and run `recovery_restore.py` against the `.backup.txt` output to confirm which files are actually present in the archive.
8. **Cross‑check** — Feed the `recovery_restore` outputs back into `recovery_analysis.py` to ensure any missing files are expected (e.g., excluded via `.nobackup`).
9. **Queue redownloads** — Re‑run `sonarr_deleted.py` and `radarr_deleted.py` with `--redownload` to let your apps re‑request genuinely missing media.
10. **Restore from backup** — Re‑run `recovery_restore.py` with `--archive-path` and `--restore-path` to copy confirmed files back onto storage (mount the destination into the borgmatic container).
11. **Celebrate** — Your layered strategy worked. Any stragglers should be metadata or intentionally excluded files.

---
# Recovery Toolkit

This toolkit currently includes these scripts:

- **recovery_analysis.py** — summarize file counts by directory depth into Excel workbooks.
- **recovery_plan.py** — classify files as FOUND / BACKUP / REDOWNLOAD / MISSING.
- **recovery_restore.py** — verify files in a mounted archive and optionally restore with `--restore-path`.
- **sonarr_deleted.py** — export Sonarr-deleted items for a date range; with `--redownload` re-request only **missing** episodes.
- **radarr_deleted.py** — export Radarr-deleted items for a date range; with `--redownload` re-request only **missing** movies.

## Setup

### 0) Create & activate a Python virtual environment (venv)

**macOS / Linux (bash/zsh) — keep venvs under your home directory:**
```bash
mkdir -p "$HOME/.venvs"
export VENV_DIR="$HOME/.venvs/recovery-toolkit"
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"    # deactivate later with: deactivate
python -m pip install -U pip wheel
python -m pip install -r requirements.txt
```


Tip: when the venv is active you’ll usually see `(<name>)` in your shell prompt. Using a fixed path like `$HOME/.venvs/recovery-toolkit`
means you can activate it from any repo directory with:
```bash
source "$HOME/.venvs/recovery-toolkit/bin/activate"
```
### 1) Environment

Install dependencies (if running outside Docker/borgmatic):

```bash
python3 -m pip install -r requirements.txt
# packages: openpyxl, tqdm, requests
```

Set API credentials via environment variables (or pass via flags):

```bash
export SONARR_URL="http://localhost:8989"
export SONARR_API_KEY="your-sonarr-api-key"
export RADARR_URL="http://localhost:7878"
export RADARR_API_KEY="your-radarr-api-key"
```

> Tip: put these in your shell profile or a `.env` that your shell sources.

### 2) Create `backup_list.txt`

This file lists the **top-level folders** that are fully covered by your Borg backups (one per line, **no leading slash**). Refer to Borgmatic config.yaml.  Example:

```
tv
movies
pictures
documents
```

`recovery_plan.py` will read `backup_folders.txt` by default. If you prefer the name `backup_list.txt`, just point the tool at it:

```bash
python recovery_plan.py --backup-file backup_list.txt --base-path /mnt/user filelist.disk8.txt
```

(Only the **top-most path component** needs to be listed; the script handles deeper paths automatically.)

---

## recovery_analysis.py

Summarizes path counts by directory depth into an Excel workbook for quick triage.

**Key features**

- N+ roll-up: each file contributes once at each requested level.
- `--folder` filter uses **component-boundary**, case-sensitive matching.
- Progress updates during processing.
- Output: an Excel workbook with sheets `level1..levelN` containing columns `[Count, Path]` and a totals row.

**Usage**

```bash
python recovery_analysis.py [--levels N] [--folder FOLDER] filelist.disk8.txt
```

**Examples**

```bash
python recovery_analysis.py filelist.disk8.txt
python recovery_analysis.py --levels 4 --folder tv/Library/Ken filelist.disk8.txt
```

---

## recovery_plan.py

Scans each path in your file list and determines whether it already exists under a given base path, is covered by your **backup set**, is listed in your **Sonarr/Radarr deleted exports** (so can be redownloaded), or is truly **missing**.

**Outputs** (written to the current directory; `<input_stem>` is the input filename without extension):

- `<input_stem>.found.txt`       — files already present at `--base-path`
- `<input_stem>.backup.txt`      — not found on disk but within your backed-up top-levels
- `<input_stem>.redownload.txt`  — not found, not in backup, but present in Sonarr/Radarr deleted lists
- `<input_stem>.missing.txt`     — neither found, backed up, nor in deleted lists

**Usage**

```bash
# Example: check if files are present under /mnt/user
python recovery_plan.py --base-path /mnt/user filelist.disk8.txt

# With a focused filter (path-component boundary; case-sensitive)
python recovery_plan.py --base-path /mnt/user --folder tv/Library/Ken filelist.disk8.txt

# Including deleted lists for redownload detection
python recovery_plan.py \
  --base-path /mnt/user \
  --sonarr-list sonarr_deleted_20250801.txt \
  --radarr-list radarr_deleted_20250801.txt \
  filelist.disk8.txt
```

**Notes**

- `--folder` semantics match `recovery_analysis.py`: a file is included if it equals `FOLDER` or starts with `FOLDER/`.
- If you omit `--sonarr-list` / `--radarr-list`, the redownload check is ignored (everything routes to MISSING unless found/in-backup).
- End-of-run summary includes a breakdown of **MISSING** files grouped by top-level folder, with counts split by file extension (case-insensitive).

---

## recovery_restore.py

Verifies that files listed in a `*.backup.txt` (from `recovery_plan.py`) actually exist under a **mounted archive path** and can optionally **restore** them to a destination.

**Outputs (verify-only)**

- `<input_stem>.backup_confirmed.txt` — found under `--archive-path`
- `<input_stem>.backup_missing.txt`   — not found under `--archive-path`

**Additional outputs when restoring**

- `<input_stem>.restored_ok.txt`      — successfully copied
- `<input_stem>.restored_skipped.txt` — destination existed or source was a directory; copy skipped
- `<input_stem>.restored_errors.txt`  — copy failed (tab-separated: `path<TAB>error`)

**Usage**

```bash
# Verify presence only
python recovery_restore.py \
  --archive-path /mnt/borg/mount/2025-08-01/mnt/user \
  filelist.disk8.backup.txt

# Verify subset
python recovery_restore.py \
  --archive-path /mnt/borg/mount/2025-08-01/mnt/user \
  --folder tv/Library/Ken \
  filelist.disk8.backup.txt

# Restore verified files to a mounted drive
python recovery_restore.py \
  --archive-path /mnt/borg/mount/2025-08-01/mnt/user \
  --restore-path /mnt/restore_target \
  filelist.disk8.backup.txt
```

**Notes**

- `--archive-path` points to the exact subdirectory in the mounted archive that corresponds to your file list root (e.g., `/mnt/borg/mount/<snapshot>/mnt/user`).
- `--folder` uses the same component-boundary semantics as the other tools.
- Uses `shutil.copy2` to preserve basic metadata; creates destination directories as needed; **skips** if the destination already exists.
- Read-only unless `--restore-path` is supplied.

---

## sonarr_deleted.py

Exports Sonarr-deleted items for a given local **date** (24h window), normalized to relative paths (e.g., `tv/...`).

**Arguments**

- `--date YYYY-MM-DD` (required): local calendar date to inspect.
- `--tz-offset ±HH:MM` (default `-04:00`): local timezone offset used to compute the 24h window.
- `--sonarr-url` (default from `$SONARR_URL` or `http://localhost:8989`)
- `--api-key` (default from `$SONARR_API_KEY`)
- `--basenames`: write only basenames instead of full relative paths
- `--out`: override output filename (default `sonarr_deleted_YYYYMMDD.txt`)
- `--redownload`: queue EpisodeSearch for matching episodes (**missing-only**, see below)

**Output**

- `sonarr_deleted_YYYYMMDD.txt` — one normalized path per line

**Redownload option**

If you pass `--redownload`, the script first checks each episode via the Sonarr API and **only queues EpisodeSearch for episodes that are actually missing** (`hasFile == false` or 404). Episodes that already have a file are skipped.

---

## radarr_deleted.py

Exports Radarr-deleted items for a given local **date** (24h window), normalized to relative paths (e.g., `movies/...`).

**Arguments**

- `--date YYYY-MM-DD` (required): local calendar date to inspect.
- `--tz-offset ±HH:MM` (default `-04:00`)
- `--radarr-url` (default from `$RADARR_URL` or `http://localhost:7878`)
- `--api-key` (default from `$RADARR_API_KEY`)
- `--out`: override output filename (default `radarr_deleted_YYYYMMDD.txt`)
- `--redownload`: queue MoviesSearch for matching movies (**missing-only**, see below)

**Output**

- `radarr_deleted_YYYYMMDD.txt` — one normalized path per line

**Redownload option**

If you pass `--redownload`, the script first checks each movie via the Radarr API and **only queues MoviesSearch for movies that are actually missing** (`hasFile == false` or 404). Existing files are skipped. `monitored` is set to true only for those missing titles.
