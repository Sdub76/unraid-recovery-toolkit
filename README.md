# Recovery Toolkit

This toolkit currently includes two scripts:

- **recovery_analysis.py** — summarize file counts by directory depth into Excel workbooks.
- **recovery_plan.py** — classify files as FOUND / BACKUP / MISSING based on filesystem presence and top-level backup set.

A third script for automated **Borg backup restores** will be added later.

---

## Setup

### 1) Install required system packages (Debian/Ubuntu)

On Debian/Ubuntu with Python 3.11+ you may hit the *“externally managed environment”* error due to [PEP 668].  
Fix it by installing the full Python venv tooling:

```bash
sudo apt update
sudo apt install -y python3-venv python3-full
```

### 2) Create and activate a **virtual environment**

> Replace `$HOME/recovery-venv` with whatever folder you want, preferably somewhere you own (not a shared mount).

**Linux / macOS (bash/zsh)**
```bash
python3 -m venv $HOME/recovery-venv
source $HOME/recovery-venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
```

**Windows (PowerShell)**
```powershell
python -m venv $HOME\recovery-venv
$HOME\recovery-venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements.txt
```

### 3) Re-using the environment later

```bash
source $HOME/recovery-venv/bin/activate
```
When finished:
```bash
deactivate
```

---

## Configuring Backup Folders

Both **recovery_analysis.py** and **recovery_plan.py** use a text file named `backup_folders.txt`
to determine which top-level directories are considered part of your backup set.

- Location: same directory where you run the scripts (current working directory).
- Format: one folder name per line (case-sensitive).
- Lines starting with `#` are treated as comments and ignored.

Example (`backup_folders.txt`):

```
# Top-level folders considered backed up
appdata
backup
books
documents
music
nextcloud
pictures
sports
```

To add or remove from your backup set, simply edit this file and re-run the scripts.
If the file is missing, the scripts will exit with an error.

---

## recovery\_analysis.py

Generates an Excel summary of a filelist with counts by depth.

**Usage:**

```bash
python recovery_analysis.py [--levels N] [--folder PREFIX] [--backup-file FILE] filelist.diskX.txt
```

**Arguments:**

* `filelist.diskX.txt`: Input text file with one file path per line (case-sensitive).
* `--levels`: Number of levels to analyze (default: 1).
* `--folder`: Optional prefix filter (case-sensitive, matches whole path components).
* `--backup-file`: File listing top-level backup directories (default: `backup_folders.txt`).

**Output:**

* Excel file named after input, e.g. `filelist.disk8_2025-08-14_123456.xlsx`
* One sheet per level (`Level 1`, `Level 2`, …).
* Each sheet shows:

  * Path components split into columns
  * `backup` column (`true` if in backup set, blank otherwise)
  * `count` column
  * Final bold **Total**

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

- `--folder` semantics match `recovery_analysis.py`: a file is included if it equals `FOLDER` (rare for files-only lists) or starts with `FOLDER/`.
- Progress bar updates at least every 5 seconds.
- All outputs are UTF-8, one path per line.
- `--sonarr-list` and `--radarr-list` can be repeated to include multiple exported files. If you omit them, the script ignores redownload detection entirely.
- At the end of the run, the on-screen summary includes a breakdown of **MISSING** files grouped by top-level folder, with counts split by file extension (case-insensitive).

---
## radarr_deleted.py

Summarizes deleted movies from Radarr on a given date, grouped by **root folder**.

**Usage:**

```bash
# env vars are optional; flags override them
export RADARR_URL="http://radarr.local:7878"
export RADARR_API_KEY="your_api_key"

# Run for a specific date (local time, defaults to TZ offset -04:00)
python3 radarr_deleted.py --date 2025-08-01
```

**Output:**

- On-screen summary grouped by Radarr root folder (3rd-level folder under `/movies/...`).
- A text file listing deleted paths with leading `/movies` removed.

---

## sonarr_deleted.py

Summarizes deleted episodes from Sonarr on a given date, grouped by **series name**.

**Usage:**

```bash
# env vars are optional; flags override them
export SONARR_URL="http://sonarr.local:8989"
export SONARR_API_KEY="your_api_key"

# Run for a specific date (local time, defaults to TZ offset -04:00)
python3 sonarr_deleted.py --date 2025-08-01
```

**Output:**

- On-screen summary grouped alphabetically by show, with counts of deleted episodes.
- A text file listing deleted episode paths with leading `/tv` removed.