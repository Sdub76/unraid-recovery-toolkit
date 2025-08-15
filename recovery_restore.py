#!/usr/bin/env python3
"""
recovery_restore.py — Verify (and optionally restore) files listed in a *.backup.txt against a mounted archive.

Phase 1 (verify): reads a file list (one relative path per line) and checks if each file exists under --archive-path.
Phase 2 (restore): if --restore-path is provided, any verified file is copied from --archive-path to --restore-path,
                   preserving subdirectories. Existing destination files are skipped (no overwrite).

Stdlib-only. Designed to run inside a borgmatic container where the archive is mounted into the filesystem.

Inputs:
  - Positional: path to *.backup.txt (one relative path per line)
  - --archive-path: absolute path inside the mounted archive that corresponds to the root of your file list
  - --restore-path: optional destination root on a temporarily-mounted drive (enables restoration)

Outputs (verify stage; written to CWD):
  - <stem>.backup_confirmed.txt      — found under the archive path
  - <stem>.backup_missing.txt        — not found under the archive path

Additional outputs when --restore-path is set:
  - <stem>.restored_ok.txt           — successfully copied
  - <stem>.restored_skipped.txt      — destination already existed; copy skipped
  - <stem>.restored_errors.txt       — copy failed (tab-separated: path<TAB>error)

Progress:
  - Verify-only: prints lines like "\rScanned N/T (P%) | est: XmYs left"
  - Restore mode: prints lines like "\rProcessed N/T (P%) | restored: R | skipped: S | errors: E | est: XmYs left"
  - Default cadence: every 25 lines processed.
"""
import argparse
import os
import sys
import shutil
import time
from typing import Tuple


def folder_matcher(folder: str):
    """Case-sensitive, component-boundary prefix match (path == folder or startswith folder+'/')."""
    if folder is None:
        return lambda p: True
    while folder.endswith('/'):
        folder = folder[:-1]
    prefix = folder + '/'
    def _match(path: str) -> bool:
        return path == folder or path.startswith(prefix)
    return _match


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Verify presence of files from a *.backup.txt under an archive path, and optionally restore them."
    )
    ap.add_argument("input_file", type=str,
                    help="Path to the *.backup.txt file (one relative path per line).")
    ap.add_argument("--archive-path", required=True, type=str,
                    help="Root directory inside the mounted archive where listed paths are rooted.")
    ap.add_argument("--restore-path", type=str, default=None,
                    help="If provided, copy verified files from --archive-path to this destination root (directories created as needed).")
    ap.add_argument("--folder", type=str, default=None,
                    help="Case-sensitive component-boundary prefix filter (e.g., 'tv/Library/Ken').")
    ap.add_argument("--strict-files", action="store_true",
                    help="Require a regular file at the path (os.path.isfile). Default: any path exists (file/dir/symlink)." )
    ap.add_argument("--encoding", type=str, default="utf-8",
                    help="Input/output text encoding (default: utf-8)." )
    return ap.parse_args()


def classify_line(relpath: str, root: str, strict_files: bool) -> Tuple[str, bool, str]:
    """Return (normalized_relpath, exists_bool, source_fullpath) under the archive root."""
    s = relpath.strip()
    if not s:
        return s, False, ''
    src = os.path.normpath(os.path.join(root, s))
    if strict_files:
        ok = os.path.isfile(src)
    else:
        ok = os.path.exists(src)
    return s, ok, src


def safe_join(root: str, rel: str) -> str:
    """Join root+rel and ensure the result stays within root (prevents path traversal)."""
    full = os.path.normpath(os.path.join(root, rel))
    root_abs = os.path.abspath(root)
    full_abs = os.path.abspath(full)
    if not (full_abs == root_abs or full_abs.startswith(root_abs + os.sep)):
        raise ValueError(f"Unsafe path outside root: {rel}")
    return full_abs


def count_lines_binary(file_path: str, chunk_size: int = 8 * 1024 * 1024) -> int:
    """Fast-ish newline count; adds 1 if file doesn't end with \n but isn't empty."""
    total = 0
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(chunk_size), b''):
            total += chunk.count(b'\n')
    try:
        size = os.path.getsize(file_path)
        if size > 0:
            with open(file_path, 'rb') as f:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b'\n':
                    total += 1
    except Exception:
        pass
    return total


def _fmt_dur(seconds: float) -> str:
    """Format seconds into a compact HhMMmSSs string."""
    if seconds < 0:
        seconds = 0
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h{m:02d}m{s:02d}s"
    if m > 0:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def main() -> None:
    args = parse_args()

    if not os.path.isfile(args.input_file):
        print(f"Input file not found: {args.input_file}", file=sys.stderr)
        sys.exit(2)
    if not os.path.isdir(args.archive_path):
        print(f"--archive-path is not a directory: {args.archive_path}", file=sys.stderr)
        sys.exit(2)
    if args.restore_path is not None and not os.path.isdir(args.restore_path):
        # Create the root if it doesn't exist
        try:
            os.makedirs(args.restore_path, exist_ok=True)
        except Exception as e:
            print(f"Failed to create --restore-path '{args.restore_path}': {e}", file=sys.stderr)
            sys.exit(2)

    match = folder_matcher(args.folder)

    stem, _ = os.path.splitext(os.path.basename(args.input_file))
    
    # Ensure output directory exists
    os.makedirs("out", exist_ok=True)
    confirmed_path = f"out/{stem}.backup_confirmed.txt"
    missing_path   = f"out/{stem}.backup_missing.txt"

    # Restore logs (only opened if restore_path provided)
    restored_ok_path      = f"out/{stem}.restored_ok.txt"
    restored_skipped_path = f"out/{stem}.restored_skipped.txt"
    restored_err_path     = f"out/{stem}.restored_errors.txt"

    total = blanks = filtered = confirmed = missing = 0
    r_ok = r_skip = r_err = 0

    total_lines = count_lines_binary(args.input_file)

    try:
        fin = open(args.input_file, 'r', encoding=args.encoding, errors='strict')
    except UnicodeDecodeError:
        print(f"Unicode error reading {args.input_file}. Try --encoding latin-1?", file=sys.stderr)
        sys.exit(2)

    PROG_EVERY = 25  # print every N lines
    t0 = time.monotonic()

    try:
        with fin,              open(confirmed_path, 'w', encoding=args.encoding) as fout_ok,              open(missing_path, 'w', encoding=args.encoding) as fout_miss:

            if args.restore_path:
                f_rest_ok  = open(restored_ok_path, 'w', encoding=args.encoding)
                f_rest_skip= open(restored_skipped_path, 'w', encoding=args.encoding)
                f_rest_err = open(restored_err_path, 'w', encoding=args.encoding)
            else:
                f_rest_ok = f_rest_skip = f_rest_err = None

            try:
                for idx, line in enumerate(fin, 1):
                    total += 1
                    s = line.strip()
                    if not s:
                        blanks += 1
                        continue
                    if not match(s):
                        filtered += 1
                        continue

                    rel, ok, src = classify_line(s, args.archive_path, args.strict_files)
                    if ok:
                        fout_ok.write(rel + "\n")
                        confirmed += 1

                        # Restore if requested
                        if args.restore_path:
                            try:
                                dst = safe_join(args.restore_path, rel)
                                dstdir = os.path.dirname(dst)
                                os.makedirs(dstdir, exist_ok=True)

                                if os.path.exists(dst):
                                    r_skip += 1
                                    if f_rest_skip:
                                        f_rest_skip.write(rel + "\n")
                                else:
                                    # Only copy regular files; if strict_files is False but src is a dir, skip
                                    if os.path.isdir(src):
                                        r_skip += 1
                                        if f_rest_skip:
                                            f_rest_skip.write(rel + "\n")
                                    else:
                                        # Print destination path before each copy for live visibility
                                        print(f"COPY -> {dst}", flush=True)
                                        shutil.copy2(src, dst)
                                        r_ok += 1
                                        if f_rest_ok:
                                            f_rest_ok.write(rel + "\n")
                            except Exception as e:
                                r_err += 1
                                if f_rest_err:
                                    f_rest_err.write(f"{rel}\t{e}\n")
                    else:
                        fout_miss.write(rel + "\n")
                        missing += 1
                        # don't attempt restore if source isn't present

                    # Progress (print after any restore attempt so counters are up to date)
                    if total_lines and (idx % PROG_EVERY == 0 or idx == total_lines):
                        pct = (idx / total_lines) if total_lines else 0.0
                        elapsed = time.monotonic() - t0
                        remaining = (elapsed * (1 - pct) / pct) if pct > 0 else None
                        if args.restore_path:
                            line = (f"\rProcessed {idx:,}/{total_lines:,} ({pct*100:.1f}%) | "
                                    f"restored: {r_ok:,} | skipped: {r_skip:,} | errors: {r_err:,} | missing: {missing:,}")
                        else:
                            line = (f"\rScanned {idx:,}/{total_lines:,} ({pct*100:.1f}%) | missing: {missing:,}")
                        if remaining is not None:
                            line += f" | est: {_fmt_dur(remaining)} left"
                        print(line, flush=True)
            finally:
                if f_rest_ok:   f_rest_ok.close()
                if f_rest_skip: f_rest_skip.close()
                if f_rest_err:  f_rest_err.close()

    finally:
        # nothing special; progress lines newline-terminated
        pass

    # Summary
    print("\n=== Backup Presence Check Summary ===")
    print(f"Input file:     {args.input_file}")
    print(f"Archive path:   {args.archive_path}")
    if args.folder:
        print(f"Filter:         {args.folder} (component-boundary, case-sensitive)")
    print(f"Strict files:   {'yes' if args.strict_files else 'no (any path exists)'}")
    print(f"Encoding:       {args.encoding}")
    print(f"Lines read:     {total:,}")
    if blanks:
        print(f"Blank lines:    {blanks:,}")
    if filtered:
        print(f"Filtered:       {filtered:,} lines (did not match --folder)")
    print("\nVerify breakdown:")
    print(f"  Confirmed:    {confirmed:,}")
    print(f"  Missing:      {missing:,}")

    if args.restore_path:
        print("\nRestore breakdown:")
        print(f"  Restored OK:  {r_ok:,}")
        print(f"  Skipped (exists/dir): {r_skip:,}")
        print(f"  Errors:       {r_err:,}")
        print("\nRestore logs:")
        print(f"  {restored_ok_path}")
        print(f"  {restored_skipped_path}")
        print(f"  {restored_err_path}")

    print("\nOutputs:")
    print(f"  {confirmed_path}")
    print(f"  {missing_path}")
    print(f"\nTotal time: {_fmt_dur(time.monotonic() - t0)}")

if __name__ == "__main__":
    main()
