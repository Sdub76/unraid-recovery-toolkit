
#!/usr/bin/env python3
# recovery_plan.py — Classify files as FOUND on current filesystem, in BACKUP set, or MISSING.
#
# Categories (checked in this order):
#   1) FOUND   -> if os.path.exists(os.path.join(--base-path, <relative_path>)) is True
#   2) BACKUP  -> if NOT found, but the file's top-level directory is in the known backup set
#   3) MISSING -> everything else
#
# Filtering:
#   --folder uses a case-sensitive, path-component-boundary match (same semantics as recovery_analysis.py).
#
# Outputs (written to current working dir, named from input file's stem):
#   <stem>.found.txt
#   <stem>.backup.txt
#   <stem>.missing.txt
#
# Progress:
#   Single tqdm bar over total input lines; prints a summary table at the end.
#
# Usage:
#   python recovery_plan.py --base-path /mnt/user [--folder pictures/photoprism] filelist.disk8.txt

import argparse
import os
import sys

def load_backup_folders(file_path="backup_folders.txt"):
    if not os.path.isfile(file_path):
        print(f"Backup folder list not found: {file_path}", file=sys.stderr)
        sys.exit(2)
    folders = set()
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            folders.add(s)
    return folders


from datetime import datetime
from tqdm import tqdm



def count_lines_binary(file_path: str, chunk_size: int = 8 * 1024 * 1024) -> int:
    total = 0
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            total += chunk.count(b"\n")
    # Count last line if file doesn't end with \n
    try:
        size = os.path.getsize(file_path)
        if size > 0:
            with open(file_path, "rb") as f:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    total += 1
    except Exception:
        pass
    return total

def folder_matcher(folder: str):
    if folder is None:
        return lambda p: True
    while folder.endswith("/"):
        folder = folder[:-1]
    prefix = folder + "/"
    def _match(path: str) -> bool:
        return path == folder or path.startswith(prefix)
    return _match

def top_level_component(relpath: str) -> str:
    parts = relpath.split("/", 1)
    return parts[0] if parts else relpath

def parse_args():
    ap = argparse.ArgumentParser(description="Classify file list into FOUND/BACKUP/MISSING by probing a base path.")
    ap.add_argument("--backup-file", type=str, default="backup_folders.txt", help="Path to backup_folders.txt (default: backup_folders.txt).")
    ap.add_argument("--folder", type=str, default=None,
                    help="Case-sensitive component-boundary prefix filter (e.g., 'tv/Library/Ken').")
    ap.add_argument("--base-path", type=str, default="/mnt/user",
                    help="Absolute base path to probe for file existence (default: /mnt/user).")
    ap.add_argument("input_file", type=str, help="UTF-8 file with relative paths (one per line).")
    return ap.parse_args()

def main():
    args = parse_args()

    if not os.path.isfile(args.input_file):
        print(f"Input file not found: {args.input_file}", file=sys.stderr)
        sys.exit(2)

    if not os.path.isabs(args.base_path):
        print("--base-path must be an absolute path (e.g., /mnt/user).", file=sys.stderr)
        sys.exit(2)

    total_lines = count_lines_binary(args.input_file)
    if total_lines == 0:
        print("Input file is empty.", file=sys.stderr)
        sys.exit(2)

    match = folder_matcher(args.folder)

    backup_set = load_backup_folders(args.backup_file)

    stem = os.path.splitext(os.path.basename(args.input_file))[0]
    found_path   = f"{stem}.found.txt"
    backup_path  = f"{stem}.backup.txt"
    missing_path = f"{stem}.missing.txt"

    # Counters
    n_read = n_blank = n_filtered = 0
    c_found = c_backup = c_missing = 0

    # Open outputs once, stream write
    with open(found_path, "w", encoding="utf-8") as f_found, \
         open(backup_path, "w", encoding="utf-8") as f_backup, \
         open(missing_path, "w", encoding="utf-8") as f_missing, \
         open(args.input_file, "r", encoding="utf-8", errors="strict") as fin, \
         tqdm(total=total_lines, unit=" lines", mininterval=5.0, desc="Scanning") as bar:

        for line in fin:
            bar.update(1)
            s = line.strip()
            if not s:
                n_blank += 1
                continue
            if not match(s):
                n_filtered += 1
                continue

            n_read += 1

            # Found?
            probe = os.path.join(args.base_path, s)
            if os.path.exists(probe):
                f_found.write(s + "\n")
                c_found += 1
                continue

            # Backup set?
            if top_level_component(s) in backup_set:
                f_backup.write(s + "\n")
                c_backup += 1
            else:
                f_missing.write(s + "\n")
                c_missing += 1

    if n_read == 0:
        msg = "No matching records to classify"
        if args.folder:
            msg += f" (check --folder='{args.folder}' ?)"
        print(msg, file=sys.stderr)
        sys.exit(2)

    # Summary
    print("\n=== Recovery Plan Summary ===")
    print(f"Input file:  {args.input_file}")
    if args.folder:
        print(f"Filter:      {args.folder} (component-boundary, case-sensitive)")
    print(f"Base path:   {args.base_path}")
    print(f"Considered:  {n_read:,} files")
    if n_filtered:
        print(f"Filtered:    {n_filtered:,} lines (did not match --folder)")
    if n_blank:
        print(f"Blank:       {n_blank:,} lines ignored")

    print("\nBreakdown:")
    print(f"  FOUND:     {c_found:,}")
    print(f"  BACKUP:    {c_backup:,}")
    print(f"  MISSING:   {c_missing:,}")

    print("\nOutputs:")
    print(f"  {found_path}")
    print(f"  {backup_path}")
    print(f"  {missing_path}")

if __name__ == "__main__":
    main()
