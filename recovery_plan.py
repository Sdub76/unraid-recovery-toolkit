#!/usr/bin/env python3
# recovery_plan.py — Classify files as FOUND on current filesystem, in BACKUP set, REDOWNLOAD, or MISSING.
#
# Categories (checked in this order):
#   1) FOUND       -> if os.path.exists(os.path.join(--base-path, <relative_path>)) is True
#   2) BACKUP      -> if NOT found, but the file's top-level directory is in the known backup set
#   3) REDOWNLOAD  -> if NOT found and NOT in backup set, but present in Sonarr/Radarr deleted lists
#   4) MISSING     -> everything else
#
# Filtering:
#   --folder uses a case-sensitive, path-component-boundary match (same semantics as recovery_analysis.py).
#
# Outputs (written to current working dir, named from input file's stem):
#   <stem>.found.txt
#   <stem>.backup.txt
#   <stem>.redownload.txt
#   <stem>.missing.txt
#
# Progress:
#   Single tqdm bar over total input lines; prints a summary table at the end.
#
# Usage:
#   python recovery_plan.py --base-path /mnt/user [--folder pictures/photoprism] #       [--sonarr-list sonarr_deleted_YYYYMMDD.txt] [--radarr-list radarr_deleted_YYYYMMDD.txt] #       filelist.disk8.txt
#
import argparse
import os
import sys
from datetime import datetime
from tqdm import tqdm

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

def load_deleted_set(paths):
    """
    Load one or more text files containing paths of deleted items.
    Each file is expected to be UTF-8, one path per line, already normalized to
    leading-less 'tv/...' or 'movies/...' (as produced by sonarr_deleted.py / radarr_deleted.py).
    Returns a set of exact strings for O(1) membership checks.
    Silently skips unreadable files.
    """
    out = set()
    for p in paths or []:
        try:
            with open(p, "r", encoding="utf-8", errors="strict") as f:
                for line in f:
                    s = line.strip()
                    if s:
                        out.add(s)
        except FileNotFoundError:
            print(f"Warning: deleted list not found: {p}", file=sys.stderr)
        except Exception as e:
            print(f"Warning: could not read deleted list {p}: {e}", file=sys.stderr)
    return out

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
    ap = argparse.ArgumentParser(description="Classify file list into FOUND/BACKUP/REDOWNLOAD/MISSING by probing a base path.")
    ap.add_argument("--backup-file", type=str, default="backup_folders.txt", help="Path to backup_folders.txt (default: backup_folders.txt)." )
    ap.add_argument("--folder", type=str, default=None,
                    help="Case-sensitive component-boundary prefix filter (e.g., 'tv/Library/Ken').")
    ap.add_argument("--base-path", type=str, default="/mnt/user",
                    help="Absolute base path to probe for file existence (default: /mnt/user)." )
    ap.add_argument("--sonarr-list", action="append", default=None,
                    help="Path to a sonarr_deleted_YYYYMMDD.txt (can be repeated)." )
    ap.add_argument("--radarr-list", action="append", default=None,
                    help="Path to a radarr_deleted_YYYYMMDD.txt (can be repeated)." )
    ap.add_argument("input_file", type=str, help="UTF-8 file with relative paths (one per line)." )
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

    # Load optional deleted lists (normalized 'tv/...', 'movies/...')
    deleted_set = load_deleted_set((args.sonarr_list or []) + (args.radarr_list or []))

    stem = os.path.splitext(os.path.basename(args.input_file))[0]
    
    # Ensure output directory exists
    os.makedirs("out", exist_ok=True)
    found_path      = f"out/{stem}.found.txt"
    backup_path     = f"out/{stem}.backup.txt"
    redownload_path = f"out/{stem}.redownload.txt"
    missing_path    = f"out/{stem}.missing.txt"

    # Counters
    n_read = n_blank = n_filtered = 0
    c_found = c_backup = c_redl = c_missing = 0

    # Aggregation for on-screen summary of MISSING files:
    # { top_level: { ext: count } }, where ext is lowercase without dot; empty -> "<noext>"
    missing_tree = {}

    # Open outputs once, stream write
    with open(found_path, "w", encoding="utf-8") as f_found,          open(backup_path, "w", encoding="utf-8") as f_backup,          open(redownload_path, "w", encoding="utf-8") as f_redl,          open(missing_path, "w", encoding="utf-8") as f_missing,          open(args.input_file, "r", encoding="utf-8", errors="strict") as fin,          tqdm(total=total_lines, unit=" lines", mininterval=5.0, desc="Scanning") as bar:

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
                continue

            # Deleted (candidate for redownload)?
            if s in deleted_set:
                f_redl.write(s + "\n")
                c_redl += 1
            else:
                f_missing.write(s + "\n")
                c_missing += 1
                # Track into missing_tree
                tl = top_level_component(s)
                base = s.rsplit("/", 1)[-1]
                _, ext = os.path.splitext(base)
                ext_key = (ext[1:] if ext.startswith(".") else ext).lower() or "<noext>"
                bucket = missing_tree.setdefault(tl, {})
                bucket[ext_key] = bucket.get(ext_key, 0) + 1

    if n_read == 0:
        msg = "No matching records to classify"
        if args.folder:
            msg += f" (check --folder='{args.folder}' ? )"
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
    print(f"  FOUND:       {c_found:,}")
    print(f"  BACKUP:      {c_backup:,}")
    print(f"  REDOWNLOAD:  {c_redl:,}")
    print(f"  MISSING:     {c_missing:,}")

    # Tree summary of MISSING: top-level -> counts by extension (case-insensitive)
    if c_missing:
        print("\nMissing file breakdown (top-level ▶ ext: count):")
        for tl in sorted(missing_tree.keys()):
            total_tl = sum(missing_tree[tl].values())
            print(f"  {tl}/  —  {total_tl} file(s)")
            # Sort by count desc, then ext asc
            for ext, cnt in sorted(missing_tree[tl].items(), key=lambda kv: (-kv[1], kv[0])):
                print(f"    └─ {ext}: {cnt}")

    print("\nOutputs:")
    print(f"  {found_path}")
    print(f"  {backup_path}")
    print(f"  {redownload_path}")
    print(f"  {missing_path}")

if __name__ == "__main__":
    main()
