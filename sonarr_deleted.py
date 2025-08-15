#!/usr/bin/env python3
import os, sys, argparse, datetime as dt, time
from collections import OrderedDict, Counter
import requests
from tqdm import tqdm

# ----------------------
# Time / window helpers
# ----------------------
def parse_offset(tzoff: str) -> dt.timezone:
    sign = 1 if tzoff.startswith("+") else -1
    hh, mm = tzoff[1:].split(":")
    return dt.timezone(sign * dt.timedelta(hours=int(hh), minutes=int(mm)))

def local_window_to_utc(ymd: str, tzoff: str):
    tz = parse_offset(tzoff)
    start_local = dt.datetime.strptime(ymd, "%Y-%m-%d").replace(tzinfo=tz)
    end_local = start_local + dt.timedelta(days=1)
    return start_local.astimezone(dt.timezone.utc), end_local.astimezone(dt.timezone.utc)

# ----------------------
# HTTP
# ----------------------
def make_session(base_url: str, api_key: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"X-Api-Key": api_key, "Accept": "application/json"})
    s.base_url = base_url.rstrip("/")
    return s

# ----------------------
# Path normalization
# ----------------------
def split_path_anysep(p: str):
    r"""Split on either / or \ so Windows-style paths don't break logic."""
    return [seg for seg in p.replace("\\", "/").split("/") if seg]

def normalize_root_prefix(p: str) -> str:
    # Map various roots to a consistent 'tv/...'
    segs = split_path_anysep(p)
    if not segs:
        return p
    lowers = [s.lower() for s in segs]
    # strip known roots like /mnt/user/tv, /tv, etc.
    if lowers[0] in {"mnt", "data", "pool"} and len(lowers) >= 3 and lowers[2] in {"tv", "shows"}:
        segs = segs[2:]
    elif lowers[0] in {"tv", "shows"}:
        pass
    elif len(lowers) >= 2 and lowers[1] in {"tv", "shows"}:
        segs = segs[1:]
    segs[0] = "tv"
    return "/".join(segs)

def extract_show_from_path(path: str) -> str:
    """Extract show name from episode path like tv/Library/Ken/Show Name/Season -> Show Name"""
    segs = split_path_anysep(path)
    if len(segs) >= 4:
        # tv/Library/Ken/Show Name/Season/Episode -> Show Name
        return segs[3]
    elif len(segs) >= 3:
        # tv/Library/Show Name/Season/Episode -> Show Name
        return segs[2]
    elif len(segs) >= 2:
        # tv/Show Name/Season/Episode -> Show Name
        return segs[1]
    else:
        return "Unknown"

# ----------------------
# Sonarr API helpers
# ----------------------
def find_all_deletions_on_date(session: requests.Session, base_url: str, start_utc: dt.datetime, end_utc: dt.datetime):
    """
    Scan ALL history records to find episodeFileDeleted events on the target date.
    Returns dict: {episodeId: deleted_file_path}
    """
    print(f"Scanning history for deletions between {start_utc} and {end_utc}")
    
    deletions = OrderedDict()  # episodeId -> deleted file path
    page = 1
    found_older_than_range = False
    total_records_fetched = 0
    
    while True:
        params = {
            "page": page,
            "pageSize": 1000,
            "sortKey": "date", 
            "sortDirection": "descending"
        }
        
        r = session.get(f"{base_url}/api/v3/history", params=params, timeout=60)
        r.raise_for_status()
        
        data = r.json()
        records = data.get("records") or data
        if not records:
            break
            
        total_records_fetched += len(records)
        page_has_target_date = False
        
        for rec in records:
            date_str = rec.get("date") or rec.get("eventDate")
            event_type = rec.get("eventType", "").lower()
            
            if not date_str:
                continue
                
            # Parse the record date
            try:
                record_time = dt.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except Exception:
                continue
            
            # Check if this record is in our target date range
            if start_utc <= record_time < end_utc:
                page_has_target_date = True
                
                # Only process episodeFileDeleted events
                if event_type == "episodefiledeleted":
                    episode_id = rec.get("episodeId")
                    if episode_id:
                        episode_id = int(episode_id)
                        
                        # Get the deleted file path
                        raw_path = (rec.get("data", {}).get("path") or 
                                  rec.get("sourceTitle") or 
                                  rec.get("episodeFilePath") or "")
                        
                        if raw_path:
                            normalized_path = normalize_root_prefix(raw_path)
                            deletions[episode_id] = normalized_path
                        
            elif record_time < start_utc:
                # We've gone past our target date range
                found_older_than_range = True
        
        # Stop conditions
        if len(records) < params["pageSize"]:
            # We've reached the end of all history
            break
            
        if found_older_than_range and not page_has_target_date:
            # We've gone past the target date and this page has no target dates
            break
            
        page += 1
    
    print(f"Fetched {total_records_fetched} history records, found {len(deletions)} episode deletions on target date")
    return deletions

def get_current_episode_status(session: requests.Session, base_url: str, episode_id: int, series_cache=None, efile_cache=None):
    """
    Check if an episode currently has a file and return its current path.
    Returns (has_file: bool, current_path: str or None)
    """
    if series_cache is None: series_cache = {}
    if efile_cache is None: efile_cache = {}
    
    try:
        # Get episode info
        r = session.get(f"{base_url}/api/v3/episode/{episode_id}", timeout=30)
        if r.status_code != 200:
            return False, None
            
        episode = r.json()
        if not episode.get("hasFile", False):
            return False, None
            
        # Get series and episode file info
        series_id = episode.get("seriesId")
        episode_file_id = episode.get("episodeFileId")
        
        if not series_id or not episode_file_id:
            return False, None
            
        # Get series path (with caching)
        if series_id in series_cache:
            series_path = series_cache[series_id]
        else:
            sr = session.get(f"{base_url}/api/v3/series/{series_id}", timeout=30)
            if sr.status_code != 200:
                return False, None
            series_path = sr.json().get("path", "")
            series_cache[series_id] = series_path
            
        # Get episode file relative path (with caching)
        if episode_file_id in efile_cache:
            relative_path = efile_cache[episode_file_id]
        else:
            er = session.get(f"{base_url}/api/v3/episodefile/{episode_file_id}", timeout=30)
            if er.status_code != 200:
                return False, None
            relative_path = er.json().get("relativePath", "")
            efile_cache[episode_file_id] = relative_path
            
        # Construct full path
        if series_path and relative_path:
            current_path = series_path.rstrip("/") + "/" + relative_path.lstrip("/")
            normalized_path = normalize_root_prefix(current_path)
            return True, normalized_path
        else:
            return True, None  # Has file but couldn't determine path
            
    except Exception as e:
        return False, None

def ensure_monitored_true(session: requests.Session, base_url: str, episode_ids):
    """Set monitored=True for the given episode IDs"""
    for eid in tqdm(episode_ids, desc="Set monitored=True", unit="eps"):
        try:
            r = session.get(f"{base_url}/api/v3/episode/{eid}", timeout=30)
            if r.status_code != 200:
                continue
            ep = r.json()
            ep["monitored"] = True
            u = session.put(f"{base_url}/api/v3/episode/{eid}", json=ep, timeout=30)
        except Exception:
            pass

def queue_episode_search(session: requests.Session, base_url: str, episode_ids):
    """Queue an EpisodeSearch command for the given episode IDs"""
    if not episode_ids:
        return
    payload = {"name": "EpisodeSearch", "episodeIds": episode_ids}
    try:
        r = session.post(f"{base_url}/api/v3/command", json=payload, timeout=60)
        if r.status_code in [200, 201]:
            print(f"Queued search for {len(episode_ids)} episodes")
        else:
            print(f"Failed to queue search: HTTP {r.status_code}")
    except Exception as e:
        print(f"Error queueing search: {e}")

def get_show_summary(episodes_with_paths):
    """Get a summary of episodes by show"""
    per_show = Counter()
    for _, path in episodes_with_paths:
        show = extract_show_from_path(path)
        per_show[show] += 1
    return per_show

# ----------------------
# Main
# ----------------------
def main():
    ap = argparse.ArgumentParser(description="Sonarr: Find episodes deleted on a specific date and check if they've been restored")
    ap.add_argument("--date", default=dt.date.today().isoformat(), help="Date to check for deletions (YYYY-MM-DD)")
    ap.add_argument("--tz-offset", default="-04:00", help="Local timezone offset (default -04:00)")
    ap.add_argument("--sonarr-url", default=os.environ.get("SONARR_URL", "http://localhost:8989"))
    ap.add_argument("--api-key", default=os.environ.get("SONARR_API_KEY"))
    ap.add_argument("--out", help="Output file prefix (default sonarr_YYYYMMDD)")
    ap.add_argument("--redownload", action="store_true", help="Set monitored=True and queue EpisodeSearch for missing episodes")
    args = ap.parse_args()

    if not args.api_key:
        print("ERROR: Missing API key. Set SONARR_API_KEY or use --api-key")
        sys.exit(2)

    # Setup file paths
    ymd = args.date.replace("-", "")
    
    # Ensure output directory exists
    os.makedirs("out", exist_ok=True)
    
    if args.out:
        missing_path = f"out/{args.out}_missing.txt"
        restored_path = f"out/{args.out}_restored.txt"
    else:
        missing_path = f"out/sonarr_{ymd}_missing.txt"
        restored_path = f"out/sonarr_{ymd}_restored.txt"

    session = make_session(args.sonarr_url, args.api_key)
    start_utc, end_utc = local_window_to_utc(args.date, args.tz_offset)

    print(f"Analyzing Sonarr deletions for {args.date}")
    print(f"Sonarr URL: {args.sonarr_url}")
    
    # Step 1: Find all episodes deleted on the target date
    deletions = find_all_deletions_on_date(session, args.sonarr_url, start_utc, end_utc)
    
    if not deletions:
        print(f"No episode deletions found on {args.date}")
        # Create empty files
        open(missing_path, "w").close()
        open(restored_path, "w").close()
        return

    # Step 2: Check current status of each deleted episode
    print(f"\nChecking current status of {len(deletions)} deleted episodes...")
    
    missing_episodes = []  # [(episodeId, deleted_path)]
    restored_episodes = []  # [(episodeId, deleted_path, current_path)]
    
    series_cache = {}
    efile_cache = {}
    
    for episode_id, deleted_path in tqdm(deletions.items(), desc="Checking status", unit="episode"):
        has_file, current_path = get_current_episode_status(session, args.sonarr_url, episode_id, series_cache, efile_cache)
        
        if has_file and current_path:
            restored_episodes.append((episode_id, deleted_path, current_path))
        else:
            missing_episodes.append((episode_id, deleted_path))
    
    # Step 3: Write output files
    print(f"\nWriting results...")
    
    # Missing episodes file
    with open(missing_path, "w", encoding="utf-8") as f:
        for episode_id, deleted_path in sorted(missing_episodes, key=lambda x: x[1]):
            f.write(f"{deleted_path}\n")
    
    # Restored episodes file  
    with open(restored_path, "w", encoding="utf-8") as f:
        for episode_id, deleted_path, current_path in sorted(restored_episodes, key=lambda x: x[2]):
            f.write(f"{current_path}\n")
    
    # Step 4: Summary
    print(f"\n=== RESULTS ===")
    print(f"Episodes deleted on {args.date}: {len(deletions)}")
    print(f"Still missing: {len(missing_episodes)} -> {missing_path}")
    print(f"Restored: {len(restored_episodes)} -> {restored_path}")
    
    # Show summaries
    if missing_episodes:
        missing_shows = get_show_summary([(eid, path) for eid, path in missing_episodes])
        print(f"\nMissing episodes by show:")
        for show in sorted(missing_shows):
            print(f"  {show}: {missing_shows[show]} episode(s)")
    
    if restored_episodes:
        restored_shows = get_show_summary([(eid, path) for eid, _, path in restored_episodes])
        print(f"\nRestored episodes by show:")
        for show in sorted(restored_shows):
            print(f"  {show}: {restored_shows[show]} episode(s)")
    
    # Step 5: Optional redownload
    if args.redownload and missing_episodes:
        print(f"\nTriggering redownload for {len(missing_episodes)} missing episodes...")
        missing_episode_ids = [episode_id for episode_id, _ in missing_episodes]
        ensure_monitored_true(session, args.sonarr_url, missing_episode_ids)
        queue_episode_search(session, args.sonarr_url, missing_episode_ids)
        print("Redownload requests queued!")

if __name__ == "__main__":
    main()
