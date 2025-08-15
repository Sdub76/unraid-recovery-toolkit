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
    # Map various roots to a consistent 'movies/...'
    segs = split_path_anysep(p)
    if not segs:
        return p
    lowers = [s.lower() for s in segs]
    # strip known roots like /mnt/user/movies, /movies, /data/movies, etc.
    if lowers[0] in {"mnt", "data", "pool"} and len(lowers) >= 3 and lowers[2] in {"movies", "films"}:
        segs = segs[2:]  # drop 'mnt','user' or 'data','something'
    elif lowers[0] in {"movies", "films"}:
        pass
    elif len(lowers) >= 2 and lowers[1] in {"movies", "films"}:
        segs = segs[1:]
    # force root name
    segs[0] = "movies"
    return "/".join(segs)

def extract_collection_from_path(path: str) -> str:
    """Extract collection name from movie path like movies/Library/Unwatched/Movie -> Unwatched"""
    segs = split_path_anysep(path)
    if len(segs) >= 3:
        # movies/Library/Collection/Movie -> Collection
        return segs[2]
    elif len(segs) >= 2:
        # movies/Collection/Movie -> Collection  
        return segs[1]
    else:
        return "Unknown"

# ----------------------
# Radarr API helpers
# ----------------------
def find_all_deletions_on_date(session: requests.Session, base_url: str, start_utc: dt.datetime, end_utc: dt.datetime):
    """
    Scan ALL history records to find movieFileDeleted events on the target date.
    Returns dict: {movieId: deleted_file_path}
    """
    print(f"Scanning history for deletions between {start_utc} and {end_utc}")
    
    deletions = OrderedDict()  # movieId -> deleted file path
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
                
                # Only process movieFileDeleted events
                if event_type == "moviefiledeleted":
                    movie_id = rec.get("movieId") or (rec.get("movie") or {}).get("id")
                    if movie_id:
                        movie_id = int(movie_id)
                        
                        # Get the deleted file path
                        raw_path = (rec.get("data", {}).get("path") or 
                                  rec.get("sourceTitle") or 
                                  rec.get("data", {}).get("movieFilePath") or 
                                  rec.get("movieFilePath") or "")
                        
                        if raw_path:
                            normalized_path = normalize_root_prefix(raw_path)
                            deletions[movie_id] = normalized_path
                        
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
    
    print(f"Fetched {total_records_fetched} history records, found {len(deletions)} movie deletions on target date")
    return deletions

def get_current_movie_status(session: requests.Session, base_url: str, movie_id: int):
    """
    Check if a movie currently has a file and return its current path.
    Returns (has_file: bool, current_path: str or None)
    """
    try:
        # Get movie info
        r = session.get(f"{base_url}/api/v3/movie/{movie_id}", timeout=30)
        if r.status_code != 200:
            return False, None
            
        movie = r.json()
        if not movie.get("hasFile", False):
            return False, None
            
        # Get current file details
        fr = session.get(f"{base_url}/api/v3/moviefile", params={"movieId": movie_id}, timeout=30)
        if fr.status_code != 200:
            return False, None
            
        files = fr.json() or []
        if not files:
            return False, None
            
        # Choose the largest file if multiple exist
        files.sort(key=lambda x: x.get("size", 0), reverse=True)
        current_file = files[0]
        
        # Get the current file path
        current_path = current_file.get("path") or current_file.get("relativePath")
        if not current_path:
            # Fallback: construct from movie path + relative path
            movie_path = movie.get("path", "")
            rel_path = current_file.get("relativePath", "")
            if movie_path and rel_path:
                current_path = movie_path.rstrip("/") + "/" + rel_path.lstrip("/")
        
        if current_path:
            normalized_path = normalize_root_prefix(current_path)
            return True, normalized_path
        else:
            return True, None  # Has file but couldn't determine path
            
    except Exception as e:
        return False, None

def ensure_monitored_true(session: requests.Session, base_url: str, movie_ids):
    """Set monitored=True for the given movie IDs"""
    for mid in tqdm(movie_ids, desc="Set monitored=True", unit="mov"):
        try:
            r = session.get(f"{base_url}/api/v3/movie/{mid}", timeout=30)
            if r.status_code != 200:
                continue
            mv = r.json()
            mv["monitored"] = True
            u = session.put(f"{base_url}/api/v3/movie/{mid}", json=mv, timeout=30)
        except Exception:
            pass

def queue_movies_search(session: requests.Session, base_url: str, movie_ids):
    """Queue a MoviesSearch command for the given movie IDs"""
    if not movie_ids:
        return
    payload = {"name": "MoviesSearch", "movieIds": movie_ids}
    try:
        r = session.post(f"{base_url}/api/v3/command", json=payload, timeout=60)
        if r.status_code in [200, 201]:
            print(f"Queued search for {len(movie_ids)} movies")
        else:
            print(f"Failed to queue search: HTTP {r.status_code}")
    except Exception as e:
        print(f"Error queueing search: {e}")

def get_collection_summary(movies_with_paths):
    """Get a summary of movies by collection"""
    per_collection = Counter()
    for _, path in movies_with_paths:
        collection = extract_collection_from_path(path)
        per_collection[collection] += 1
    return per_collection

# ----------------------
# Main
# ----------------------
def main():
    ap = argparse.ArgumentParser(description="Radarr: Find movies deleted on a specific date and check if they've been restored")
    ap.add_argument("--date", default=dt.date.today().isoformat(), help="Date to check for deletions (YYYY-MM-DD)")
    ap.add_argument("--tz-offset", default="-04:00", help="Local timezone offset (default -04:00)")
    ap.add_argument("--radarr-url", default=os.environ.get("RADARR_URL", "http://localhost:7878"))
    ap.add_argument("--api-key", default=os.environ.get("RADARR_API_KEY"))
    ap.add_argument("--out", help="Output file prefix (default radarr_YYYYMMDD)")
    ap.add_argument("--redownload", action="store_true", help="Set monitored=True and queue MoviesSearch for missing movies")
    args = ap.parse_args()

    if not args.api_key:
        print("ERROR: Missing API key. Set RADARR_API_KEY or use --api-key")
        sys.exit(2)

    # Setup file paths
    ymd = args.date.replace("-", "")
    if args.out:
        missing_path = f"{args.out}_missing.txt"
        restored_path = f"{args.out}_restored.txt"
    else:
        missing_path = f"radarr_{ymd}_missing.txt"
        restored_path = f"radarr_{ymd}_restored.txt"

    session = make_session(args.radarr_url, args.api_key)
    start_utc, end_utc = local_window_to_utc(args.date, args.tz_offset)

    print(f"Analyzing Radarr deletions for {args.date}")
    print(f"Radarr URL: {args.radarr_url}")
    
    # Step 1: Find all movies deleted on the target date
    deletions = find_all_deletions_on_date(session, args.radarr_url, start_utc, end_utc)
    
    if not deletions:
        print(f"No movie deletions found on {args.date}")
        # Create empty files
        open(missing_path, "w").close()
        open(restored_path, "w").close()
        return

    # Step 2: Check current status of each deleted movie
    print(f"\nChecking current status of {len(deletions)} deleted movies...")
    
    missing_movies = []  # [(movieId, deleted_path)]
    restored_movies = []  # [(movieId, deleted_path, current_path)]
    
    for movie_id, deleted_path in tqdm(deletions.items(), desc="Checking status", unit="movie"):
        has_file, current_path = get_current_movie_status(session, args.radarr_url, movie_id)
        
        if has_file and current_path:
            restored_movies.append((movie_id, deleted_path, current_path))
        else:
            missing_movies.append((movie_id, deleted_path))
    
    # Step 3: Write output files
    print(f"\nWriting results...")
    
    # Missing movies file
    with open(missing_path, "w", encoding="utf-8") as f:
        for movie_id, deleted_path in sorted(missing_movies, key=lambda x: x[1]):
            f.write(f"{deleted_path}\n")
    
    # Restored movies file  
    with open(restored_path, "w", encoding="utf-8") as f:
        for movie_id, deleted_path, current_path in sorted(restored_movies, key=lambda x: x[2]):
            f.write(f"{current_path}\n")
    
    # Step 4: Summary
    print(f"\n=== RESULTS ===")
    print(f"Movies deleted on {args.date}: {len(deletions)}")
    print(f"Still missing: {len(missing_movies)} -> {missing_path}")
    print(f"Restored: {len(restored_movies)} -> {restored_path}")
    
    # Collection summaries
    if missing_movies:
        missing_collections = get_collection_summary([(mid, path) for mid, path in missing_movies])
        print(f"\nMissing movies by collection:")
        for collection in sorted(missing_collections):
            print(f"  {collection}: {missing_collections[collection]} movie(s)")
    
    if restored_movies:
        restored_collections = get_collection_summary([(mid, path) for mid, _, path in restored_movies])
        print(f"\nRestored movies by collection:")
        for collection in sorted(restored_collections):
            print(f"  {collection}: {restored_collections[collection]} movie(s)")
    
    # Step 5: Optional redownload
    if args.redownload and missing_movies:
        print(f"\nTriggering redownload for {len(missing_movies)} missing movies...")
        missing_movie_ids = [movie_id for movie_id, _ in missing_movies]
        ensure_monitored_true(session, args.radarr_url, missing_movie_ids)
        queue_movies_search(session, args.radarr_url, missing_movie_ids)
        print("Redownload requests queued!")

if __name__ == "__main__":
    main()
