#!/usr/bin/env python3
"""
recovery_restore.py — Phase 1: verify that files listed in a *.backup.txt exist under a Borg mount.
Designed for use inside a borgmatic (or plain Borg) container shell with only Python stdlib.

Inputs:
  - Positional: path to *.backup.txt (one relative path per line)
  - --borg-mount: absolute path into the mounted Borg archive where those relative paths are rooted
Outputs:
  - <stem>.backup_confirmed.txt
  - <stem>.backup_missing.txt

Optional:
  - --folder: restrict to a path prefix (component boundary, case-sensitive)
  - --strict-files: require regular files only
  - --encoding: input/output encoding

Phase 2 (to be added): --restore PATH will actually copy files out, one-by-one.
"""
import argparse
import os
import sys
from typing import Tuple


def folder_matcher(folder: str):
    """Case-sensitive, component-boundary prefix match (path == folder or startswith folder+'/')."""
    if folder is None:
        return lambda p: True
    while folder.endswith("/"):
        folder = folder[:-1]
    prefix = folder + "/"
    def _match(path: str) -> bool:
        return path == folder or path.startswith(prefix)
    return _match


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Verify presence of files from a *.backup.txt under a Borg mount (no restore)."
    )
    ap.add_argument("input_file", type=str,
                    help="Path to the *.backup.txt file (one relative path per line).")
    ap.add_argument("--borg-mount", required=True, type=str,
                    help="Root directory of the mounted Borg archive where listed paths are rooted.")
    ap.add_argument("--folder", type=str, default=None,
                    help="Case-sensitive component-boundary prefix filter (e.g., 'tv/Library/Ken').")
    ap.add_argument("--strict-files", action="store_true",
                    help="Require a regular file at the path (os.path.isfile). Default: any path exists (file/dir/symlink).")
    ap.add_argument("--encoding", type=str, default="utf-8",
                    help="Input/output text encoding (default: utf-8).")
    return ap.parse_args()


def classify_line(relpath: str, root: str, strict_files: bool) -> Tuple[str, bool]:
    """Return (normalized_relpath, exists_bool) under the borg mount root."""
    s = relpath.strip()
    if not s:
        return s, False
    probe = os.path.normpath(os.path.join(root, s))
    if strict_files:
        ok = os.path.isfile(probe)
    else:
        ok = os.path.exists(probe)
    return s, ok


def count_lines_binary(file_path: str, chunk_size: int = 8 * 1024 * 1024) -> int:
    """Fast-ish newline count; adds 1 if file doesn't end with \\n but isn't empty."""
    total = 0
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            total += chunk.count(b"\n")
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


def main() -> None:
    args = parse_args()

    if not os.path.isfile(args.input_file):
        print(f"Input file not found: {args.input_file}", file=sys.stderr)
        sys.exit(2)
    if not os.path.isdir(args.borg_mount):
        print(f"--borg-mount is not a directory: {args.borg_mount}", file=sys.stderr)
        sys.exit(2)

    match = folder_matcher(args.folder)

    stem, _ = os.path.splitext(os.path.basename(args.input_file))
    confirmed_path = f"{stem}.backup_confirmed.txt"
    missing_path = f"{stem}.backup_missing.txt"

    total = blanks = filtered = confirmed = missing = 0

    total_lines = count_lines_binary(args.input_file)

    try:
        fin = open(args.input_file, "r", encoding=args.encoding, errors="strict")
    except UnicodeDecodeError:
        print(f"Unicode error reading {args.input_file}. Try --encoding latin-1?", file=sys.stderr)
        sys.exit(2)

    PROG_EVERY = 50000  # print every N lines
    try:
        with fin, \
             open(confirmed_path, "w", encoding=args.encoding) as fout_ok, \
             open(missing_path, "w", encoding=args.encoding) as fout_miss:

            for idx, line in enumerate(fin, 1):
                total += 1
                s = line.strip()
                if not s:
                    blanks += 1
                    continue
                if not match(s):
                    filtered += 1
                    continue

                rel, ok = classify_line(s, args.borg_mount, args.strict_files)
                if ok:
                    fout_ok.write(rel + "\n")
                    confirmed += 1
                else:
                    fout_miss.write(rel + "\n")
                    missing += 1

                # Progress: always print CR + newline
                if total_lines and (idx % PROG_EVERY == 0 or idx == total_lines):
                    pct = (idx / total_lines) * 100 if total_lines else 0.0
                    print(f"\rScanned {idx:,}/{total_lines:,} ({pct:.1f}%)", flush=True)
    finally:
        # nothing special to do; progress lines already newline-terminated
        pass

    # Summary
    print("\n=== Backup Presence Check Summary ===")
    print(f"Input file:     {args.input_file}")
    print(f"Borg mount:     {args.borg_mount}")
    if args.folder:
        print(f"Filter:         {args.folder} (component-boundary, case-sensitive)")
    print(f"Strict files:   {'yes' if args.strict_files else 'no (any path exists)'}")
    print(f"Encoding:       {args.encoding}")
    print(f"Lines read:     {total:,}")
    if blanks:
        print(f"Blank lines:    {blanks:,}")
    if filtered:
        print(f"Filtered:       {filtered:,} lines (did not match --folder)")
    print("\nBreakdown:")
    print(f"  Confirmed:    {confirmed:,}")
    print(f"  Missing:      {missing:,}")
    print("\nOutputs:")
    print(f"  {confirmed_path}")
    print(f"  {missing_path}")


if __name__ == "__main__":
    main()
