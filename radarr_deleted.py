#!/usr/bin/env python3
import os, sys, argparse, datetime as dt, time
import requests
from tqdm import tqdm
from collections import Counter

# ----------------------
# Time / window helpers
# ----------------------
def parse_offset(tzoff: str) -> dt.timezone:
    """tzoff like '+00:00' or '-04:00' -> datetime.timezone"""
    sign = 1 if tzoff.startswith("+") else -1
    hh, mm = tzoff[1:].split(":")
    return dt.timezone(sign * dt.timedelta(hours=int(hh), minutes=int(mm)))

def local_window_to_utc(ymd: str, tzoff: str):
    """Return (start_utc, end_utc) for the given local calendar day."""
    tz = parse_offset(tzoff)
    start_local = dt.datetime.strptime(ymd, "%Y-%m-%d").replace(tzinfo=tz)
    end_local = start_local + dt.timedelta(days=1)
    return start_local.astimezone(dt.timezone.utc), end_local.astimezone(dt.timezone.utc)

def iso_z(ts: dt.datetime) -> str:
    """UTC ISO string with 'Z' suffix."""
    return ts.isoformat(timespec="seconds").replace("+00:00", "Z")

def parse_rfc3339(s: str) -> dt.datetime:
    """Parse Sonarr/Radarr RFC3339 string into aware datetime."""
    if s.endswith("Z"):
        s = s.replace("Z", "+00:00")
    return dt.datetime.fromisoformat(s)

# ----------------------
# HTTP helpers
# ----------------------
def make_session(base_url: str, api_key: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"X-Api-Key": api_key})
    r = s.get(f"{base_url}/api/v3/system/status", timeout=15)
    r.raise_for_status()
    return s

# ----------------------
# Radarr logic
# ----------------------
def fetch_deleted_records(session: requests.Session, base_url: str, start_utc: dt.datetime, end_utc: dt.datetime):
    """Use /history/since then filter to [start_utc, end_utc) and eventType containing 'deleted'."""
    r = session.get(f"{base_url}/api/v3/history/since", params={"date": iso_z(start_utc)}, timeout=60)
    r.raise_for_status()
    out = []
    for rec in r.json():
        when_s = rec.get("date")
        if not when_s:
            continue
        when = parse_rfc3339(when_s)
        if not (start_utc <= when < end_utc):
            continue
        if "deleted" in str(rec.get("eventType", "")).lower():
            out.append(rec)
    return out

def ensure_monitored_true(session: requests.Session, base_url: str, movie_ids):
    for mid in tqdm(movie_ids, desc="Setting monitored=True", unit="movies"):
        r = session.get(f"{base_url}/api/v3/movie/{mid}", timeout=30)
        if r.status_code != 200:
            continue
        movie = r.json()
        if not movie.get("monitored", False):
            movie["monitored"] = True
            pr = session.put(f"{base_url}/api/v3/movie/{mid}", json=movie, timeout=60)
            pr.raise_for_status()

def queue_movies_search(session: requests.Session, base_url: str, movie_ids):
    BATCH = 100
    with tqdm(total=len(movie_ids), desc="Queueing MoviesSearch", unit="movies") as bar:
        for i in range(0, len(movie_ids), BATCH):
            chunk = movie_ids[i:i + BATCH]
            r = session.post(f"{base_url}/api/v3/command",
                             json={"name": "MoviesSearch", "movieIds": chunk},
                             timeout=60)
            r.raise_for_status()
            bar.update(len(chunk))
            time.sleep(0.15)

# ----------------------
# Path helpers
# ----------------------
def split_path_anysep(p: str):
    """Split on either / or \ so Windows-style paths don't break logic."""
    return [x for x in p.replace("\\", "/").split("/") if x]

def normalize_root_prefix_for_output(p: str) -> str:
    """
    For output file only:
      - '/movies/... ' -> 'movies/...'
      - '/tv/... '     -> 'tv/...'
    Everything else left as-is.
    """
    if p == "/tv": return "tv"
    if p == "/movies": return "movies"
    if p.startswith("/tv/"): return "tv" + p[len("/tv"):]
    if p.startswith("/movies/"): return "movies" + p[len("/movies"):]
    return p.lstrip("/") if p in ("/", "") else p  # a tiny safety

def root_folder_triplet(p: str) -> str:
    """
    Summary key: first three components (without leading slash).
    '/movies/Library/Dan/Title (2024)/file.mkv' -> 'movies/Library/Dan'
    """
    parts = split_path_anysep(p.lstrip("/"))
    if len(parts) >= 3:
        return "/".join(parts[:3])
    return "/".join(parts) if parts else "unknown"

# ----------------------
# Main
# ----------------------
def main():
    ap = argparse.ArgumentParser(description="Radarr deletions by local date; optional re-download")
    ap.add_argument("--date", required=True, help="Local calendar date (YYYY-MM-DD)")
    ap.add_argument("--tz-offset", default="-04:00", help="Local timezone offset (default -04:00)")
    ap.add_argument("--radarr-url", default=os.environ.get("RADARR_URL", "http://localhost:7878"))
    ap.add_argument("--api-key", default=os.environ.get("RADARR_API_KEY"))
    ap.add_argument("--out", help="Output file (default radarr_deleted_YYYYMMDD.txt)")
    ap.add_argument("--redownload", action="store_true", help="Set monitored=True and queue MoviesSearch")
    args = ap.parse_args()

    if not args.api_key:
        print("Missing API key"); sys.exit(2)

    ymd = args.date.replace("-", "")
    out_path = args.out or f"radarr_deleted_{ymd}.txt"

    session = make_session(args.radarr_url, args.api_key)
    start_utc, end_utc = local_window_to_utc(args.date, args.tz_offset)

    recs = fetch_deleted_records(session, args.radarr_url, start_utc, end_utc)
    if not recs:
        print(f"No deletions found on {args.date}")
        open(out_path, "w").close()
        print(f"List written to: {out_path}")
        return

    # Collect file paths and movie IDs
    paths, mids = [], []
    for r in recs:
        data = r.get("data") or {}
        path = data.get("path") or r.get("sourceTitle") or ""
        if path:
            paths.append(path)
        mid = r.get("movieId")
        if mid:
            mids.append(int(mid))

    # Dedup preserve order
    seen = set(); uniq_paths = []
    for p in paths:
        if p not in seen:
            seen.add(p); uniq_paths.append(p)
    seen = set(); uniq_mids = []
    for m in mids:
        if m not in seen:
            seen.add(m); uniq_mids.append(m)

    # Write output file with normalized /movies or /tv prefix (no leading slash)
    with open(out_path, "w", encoding="utf-8") as f:
        for p in uniq_paths:
            f.write(normalize_root_prefix_for_output(p) + "\n")

    # Optional redownload: monitored=True then MoviesSearch
    if args.redownload and uniq_mids:
        ensure_monitored_true(session, args.radarr_url, uniq_mids)
        queue_movies_search(session, args.radarr_url, uniq_mids)

    # Alphabetical summary by root folder (3 components)
    per_root = Counter()
    for p in uniq_paths:
        per_root[root_folder_triplet(p)] += 1

    print("\n=== Radarr Summary (by root folder) ===")
    for folder in sorted(per_root):
        print(f"{folder}: {per_root[folder]} movie(s)")

    print(f"\nList written to: {out_path}")

if __name__ == "__main__":
    main()
