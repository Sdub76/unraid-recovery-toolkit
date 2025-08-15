#!/usr/bin/env python3
import os, sys, argparse, datetime as dt, pathlib, time
import requests
from tqdm import tqdm
from collections import Counter

# ----- Helpers -----
def parse_offset(tzoff: str) -> dt.timezone:
    sign = 1 if tzoff.startswith("+") else -1
    hh, mm = tzoff[1:].split(":")
    return dt.timezone(sign * dt.timedelta(hours=int(hh), minutes=int(mm)))

def local_window_to_utc(ymd: str, tzoff: str):
    tz = parse_offset(tzoff)
    start_local = dt.datetime.strptime(ymd, "%Y-%m-%d").replace(tzinfo=tz)
    end_local = start_local + dt.timedelta(days=1)
    return start_local.astimezone(dt.timezone.utc), end_local.astimezone(dt.timezone.utc)

def make_session(url, api_key):
    s = requests.Session()
    s.headers.update({"X-Api-Key": api_key})
    r = s.get(f"{url}/api/v3/system/status", timeout=15)
    r.raise_for_status()
    return s

def fetch_deleted_records(session, base_url, start_utc, end_utc):
    """Use /history/since and keep only [start_utc, end_utc) with eventType containing 'deleted'."""
    r = session.get(
        f"{base_url}/api/v3/history/since",
        params={"date": start_utc.isoformat().replace("+00:00", "Z")},
        timeout=60,
    )
    r.raise_for_status()
    out = []
    for rec in r.json():
        d = rec.get("date")
        if not d:
            continue
        when = dt.datetime.fromisoformat(d.replace("Z", "+00:00"))
        if not (start_utc <= when < end_utc):
            continue
        if "deleted" in str(rec.get("eventType", "")).lower():
            out.append(rec)
    return out

def queue_episode_search(session, base_url, episode_ids):
    BATCH = 100
    with tqdm(total=len(episode_ids), desc="Queueing EpisodeSearch", unit="eps") as bar:
        for i in range(0, len(episode_ids), BATCH):
            chunk = episode_ids[i:i + BATCH]
            r = session.post(
                f"{base_url}/api/v3/command",
                json={"name": "EpisodeSearch", "episodeIds": chunk},
                timeout=60,
            )
            r.raise_for_status()
            bar.update(len(chunk))
            time.sleep(0.15)

def fetch_episode_summary(session, base_url, episode_ids):
    """Return (per_show Counter, printable list) by fetching episode + series titles."""
    per_show = Counter()
    series_cache = {}
    printable = []

    for eid in tqdm(episode_ids, desc="Fetching episode metadata", unit="eps"):
        er = session.get(f"{base_url}/api/v3/episode/{eid}", timeout=30)
        if er.status_code != 200:
            continue
        ep = er.json()
        sid = ep.get("seriesId")
        if sid not in series_cache:
            sr = session.get(f"{base_url}/api/v3/series/{sid}", timeout=30)
            if sr.status_code == 200:
                series_cache[sid] = sr.json().get("title") or f"Series {sid}"
            else:
                series_cache[sid] = f"Series {sid}"
        show_title = series_cache[sid]
        per_show[show_title] += 1

        s = ep.get("seasonNumber")
        e = ep.get("episodeNumber")
        ep_title = ep.get("title") or ""
        label = f"{show_title} S{int(s):02d}E{int(e):02d}"
        if ep_title:
            label += f" - {ep_title}"
        printable.append(label)

    return per_show, printable

def normalize_root_prefix(p: str) -> str:
    """Output formatting: drop leading '/' on /tv and /movies roots."""
    if p == "/tv": return "tv"
    if p == "/movies": return "movies"
    if p.startswith("/tv/"): return "tv" + p[len("/tv"):]
    if p.startswith("/movies/"): return "movies" + p[len("/movies"):]
    return p

# ----- Main -----
def main():
    ap = argparse.ArgumentParser(description="Sonarr deletions by local date; optional re-download")
    ap.add_argument("--date", required=True, help="Local calendar date (YYYY-MM-DD)")
    ap.add_argument("--tz-offset", default="-04:00", help="Local timezone offset (default -04:00 for EDT)")
    ap.add_argument("--sonarr-url", default=os.environ.get("SONARR_URL","http://localhost:8989"))
    ap.add_argument("--api-key", default=os.environ.get("SONARR_API_KEY"))
    ap.add_argument("--basenames", action="store_true", help="Write only basenames to file")
    ap.add_argument("--out", help="Output file (default sonarr_deleted_YYYYMMDD.txt)")
    ap.add_argument("--redownload", action="store_true", help="Queue EpisodeSearch for matching episodes")
    args = ap.parse_args()

    if not args.api_key:
        print("Missing API key"); sys.exit(2)

    ymd = args.date.replace("-", "")
    out_path = args.out or f"sonarr_deleted_{ymd}.txt"

    session = make_session(args.sonarr_url, args.api_key)
    start_utc, end_utc = local_window_to_utc(args.date, args.tz_offset)

    records = fetch_deleted_records(session, args.sonarr_url, start_utc, end_utc)
    if not records:
        print(f"No deletions found on {args.date}")
        open(out_path, "w").close()
        return

    # collect paths + episode IDs
    paths, eids = [], []
    for r in records:
        data = r.get("data") or {}
        path = data.get("path") or data.get("importedPath") or data.get("droppedPath") or r.get("sourceTitle")
        if path: paths.append(path)
        eid = r.get("episodeId")
        if eid: eids.append(int(eid))

    # dedup while preserving order
    seen = set(); uniq_paths = []
    for p in paths:
        if p not in seen:
            seen.add(p); uniq_paths.append(p)
    seen = set(); uniq_eids = []
    for eid in eids:
        if eid not in seen:
            seen.add(eid); uniq_eids.append(eid)

    # write output file (normalize /tv and /movies unless basenames requested)
    to_write = [normalize_root_prefix(p) for p in uniq_paths]
    if args.basenames:
        to_write = [pathlib.Path(p).name for p in uniq_paths]
    with open(out_path, "w", encoding="utf-8") as f:
        for line in to_write:
            f.write(line + "\n")

    # optional redownload
    if args.redownload and uniq_eids:
        print(f"Queuing {len(uniq_eids)} episode searches…")
        queue_episode_search(session, args.sonarr_url, uniq_eids)

    # per-show summary (alphabetical now)
    if uniq_eids:
        per_show, _ = fetch_episode_summary(session, args.sonarr_url, uniq_eids)
        print("\n=== Sonarr Summary (by show) ===")
        for show in sorted(per_show):
            print(f"{show}: {per_show[show]} episode(s)")
    else:
        print("\n=== Sonarr Summary (by show) ===\n(no episode IDs found)")

    print(f"\nList written to: {out_path}")

if __name__ == "__main__":
    main()
