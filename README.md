# Disaster Recovery Playbook (High‑Level Flow)

This is the intended end‑to‑end flow when things go sideways. It's opinionated, boringly reliable, and tested against real-world "oh no" moments.

1. **Nightly hygiene** — Run the filelist script every night on Unraid, with Healthchecks monitoring the job.
2. **Disaster strikes** — Array corruption or accidental deletes slip past parity. (It happens. Deep breath.)
3. **Freeze backups** — Suspend Borg backups to avoid snapshotting bad state; extract the latest file list from the most recent archive for reference.
4. **Scope the blast radius** — Run `recovery_analysis.py` to build the Excel summary and see what's missing by folder/depth.
5. **Audit the dark day** — Run `sonarr_deleted.py` and `radarr_deleted.py` to list what those apps marked as deleted around the incident window.
6. **Plan the recovery** — Run `recovery_plan.py` to classify: what still exists on the array, what should be in backup, what Sonarr/Radarr can redownload, and what's truly missing.
7. **Verify the backup** — In a Borg shell, mount the latest archive and run `recovery_restore.py` against the `.backup.txt` output to confirm which files are actually present in the archive.
8. **Cross‑check** — Feed the `recovery_restore` outputs back into `recovery_analysis.py` to ensure any missing files are expected (e.g., excluded via `.nobackup`).
9. **Queue redownloads** — Re‑run `sonarr_deleted.py` and `radarr_deleted.py` with `--redownload` to let your apps re‑request genuinely missing media.
10. **Restore from backup** — Re‑run `recovery_restore.py` with `--archive-path` and `--restore-path` to copy confirmed files back onto storage (mount the destination into the borgmatic container).
11. **Celebrate** — Your layered strategy worked. Any stragglers should be metadata or intentionally excluded files.  Don't forget to resume Borg backups.

---
# Recovery Toolkit

This toolkit currently includes these scripts:

- **array_file_list.sh** — Build a list of files in a backed up location.  Should be run nightly in Unraid User Scripts and monitored for failure.
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


Tip: when the venv is active you'll usually see `(<n>)` in your shell prompt. Using a fixed path like `$HOME/.venvs/recovery-toolkit`
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

## array_file_list.sh

Creates a **stable, sorted inventory** of files so you always have a recent point-in-time list to compare against after an incident.

**What it does**
- Walks one or more array roots (e.g., `/mnt/disk1`, `/mnt/disk2`, or `/mnt/user`).
- Writes relative paths (no leading slash) to `filelist.<target>.txt`.
- Pings a Healthchecks URL at start/success/failure.

**Notes**
- The output filenames are intentionally simple: `filelist.disk1.txt`, `filelist.disk2.txt`, etc.
- Healthchecks: set `HC_URL` in the environment (or hardcode) to track job health.
- These lists are the inputs to `recovery_analysis.py` and `recovery_plan.py` in the disaster flow.

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

Finds episodes deleted on a specific date and checks if they've been restored. Scans **all** Sonarr history to find `episodeFileDeleted` events, then verifies current status of each episode.

**Arguments**

- `--date YYYY-MM-DD` (default: today): Local calendar date to inspect for deletions.
- `--tz-offset ±HH:MM` (default `-04:00`): Local timezone offset used to compute the 24h window.
- `--sonarr-url` (default from `$SONARR_URL` or `http://localhost:8989`)
- `--api-key` (default from `$SONARR_API_KEY`)
- `--out PREFIX`: Override output filename prefix (default `sonarr_YYYYMMDD`)
- `--redownload`: Set monitored=True and queue EpisodeSearch for **missing episodes only**

**Outputs**

- `sonarr_YYYYMMDD_missing.txt` — episodes deleted on target date that are still missing
- `sonarr_YYYYMMDD_restored.txt` — episodes deleted on target date that have been restored

**Key Features**

- **Comprehensive scanning**: Pages through ALL history records until it finds the target date
- **Smart tracking**: Uses episodeId to track deletions, handles restored files with different paths
- **Missing vs Restored**: Separates items that are truly missing from those that have been restored
- **Show summaries**: Groups results by show name for easy review
- **Safe redownload**: Only queues searches for episodes that are actually missing

**Examples**

```bash
# Check deletions for a specific date
python sonarr_deleted.py --date 2025-08-01

# With custom output prefix
python sonarr_deleted.py --date 2025-08-01 --out /tmp/sonarr_audit

# Check and queue redownloads for missing episodes
python sonarr_deleted.py --date 2025-08-01 --redownload
```

**Sample Output**

```
Analyzing Sonarr deletions for 2025-08-01
Sonarr URL: https://sonarr.waun.net
Fetched 15000 history records, found 124 episode deletions on target date

Checking current status of 124 deleted episodes...
Checking status: 100%|████████| 124/124 [00:45<00:00, 2.7episode/s]

=== RESULTS ===
Episodes deleted on 2025-08-01: 124
Still missing: 89 -> sonarr_20250801_missing.txt
Restored: 35 -> sonarr_20250801_restored.txt

Missing episodes by show:
  1883: 10 episode(s)
  Adam Ruins Everything: 58 episode(s)
  Star Trek Discovery: 21 episode(s)

Restored episodes by show:
  Adam Ruins Everything: 8 episode(s)
  Star Trek Discovery: 27 episode(s)
```

---

## radarr_deleted.py

Finds movies deleted on a specific date and checks if they've been restored. Scans **all** Radarr history to find `movieFileDeleted` events, then verifies current status of each movie.

**Arguments**

- `--date YYYY-MM-DD` (default: today): Local calendar date to inspect for deletions.
- `--tz-offset ±HH:MM` (default `-04:00`): Local timezone offset used to compute the 24h window.
- `--radarr-url` (default from `$RADARR_URL` or `http://localhost:7878`)
- `--api-key` (default from `$RADARR_API_KEY`)
- `--out PREFIX`: Override output filename prefix (default `radarr_YYYYMMDD`)
- `--redownload`: Set monitored=True and queue MoviesSearch for **missing movies only**

**Outputs**

- `radarr_YYYYMMDD_missing.txt` — movies deleted on target date that are still missing
- `radarr_YYYYMMDD_restored.txt` — movies deleted on target date that have been restored

**Key Features**

- **Comprehensive scanning**: Pages through ALL history records until it finds the target date
- **Smart tracking**: Uses movieId to track deletions, handles restored files with different paths
- **Missing vs Restored**: Separates items that are truly missing from those that have been restored
- **Collection summaries**: Groups results by Radarr collection/folder for easy review
- **Safe redownload**: Only queues searches for movies that are actually missing

**Examples**

```bash
# Check deletions for a specific date
python radarr_deleted.py --date 2025-08-01

# With custom output prefix
python radarr_deleted.py --date 2025-08-01 --out /tmp/radarr_audit

# Check and queue redownloads for missing movies
python radarr_deleted.py --date 2025-08-01 --redownload
```

**Sample Output**

```
Analyzing Radarr deletions for 2025-08-01
Radarr URL: https://radarr.waun.net
Fetched 8000 history records, found 45 movie deletions on target date

Checking current status of 45 deleted movies...
Checking status: 100%|████████| 45/45 [00:23<00:00, 1.9movie/s]

=== RESULTS ===
Movies deleted on 2025-08-01: 45
Still missing: 35 -> radarr_20250801_missing.txt
Restored: 10 -> radarr_20250801_restored.txt

Missing movies by collection:
  Collection 2000-2009: 15 movie(s)
  Ken: 5 movie(s)
  Unwatched: 15 movie(s)

Restored movies by collection:
  Collection 2000-2009: 3 movie(s)
  Ken: 3 movie(s)
  Unwatched: 4 movie(s)
```

**Integration with recovery_plan.py**

Both tools produce outputs that integrate seamlessly with `recovery_plan.py`:

```bash
# Use the deletion lists to identify redownloadable content
python recovery_plan.py \
  --base-path /mnt/user \
  --sonarr-list sonarr_20250801_missing.txt \
  --radarr-list radarr_20250801_missing.txt \
  filelist.disk8.txt
```

This allows the recovery system to automatically classify deleted media as "redownloadable" rather than "missing" during disaster recovery.
