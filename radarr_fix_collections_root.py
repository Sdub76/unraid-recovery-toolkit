#!/usr/bin/env python3
r"""
radarr_fix_collections_root_batch.py

Batch-fix Radarr collection root folders:

- For each collection:
  - Expand /api/v3/collection/{id}
  - Map items to your local library via tmdbId (fallback imdbId)
  - Pick the OLDEST present movie (by year; missing year sorts newest)
  - Set collection.rootFolderPath to that movie's root folder

Dry-run by default. Use --execute to apply.

Env:
  RADARR_URL (e.g., http://radarr:7878)
  RADARR_API_KEY
"""

import os, sys, json, time, argparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from pathlib import Path

# ---------- tiny HTTP helper ----------
def env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise SystemExit(f"ERROR: required env var {name} is not set")
    return v

def api(path: str, method="GET", data=None):
    base = env("RADARR_URL").rstrip("/")
    key  = env("RADARR_API_KEY")
    url  = f"{base}{path}"
    headers = {
        "X-Api-Key": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "radarr-fix-collections-root/batch-1.0",
    }
    body = None if data is None else json.dumps(data).encode("utf-8")
    req  = Request(url, data=body, headers=headers, method=method)
    with urlopen(req, timeout=30) as r:
        raw = r.read()
        return {} if not raw else json.loads(raw)

# ---------- helpers ----------
def longest_root(path: str, roots):
    """Return rootfolder dict with longest prefix of path."""
    p = str(Path(path)).rstrip("/")
    best, bestlen = None, -1
    for rf in roots:
        rp = str(Path(rf.get("path",""))).rstrip("/")
        if rp and p.startswith(rp) and len(rp) > bestlen:
            best, bestlen = rf, len(rp)
    return best

def year_key(m):
    y = m.get("year")
    return (y if isinstance(y, int) else 99999, m.get("added") or "")

def expand_collection_items(coll_id: int):
    """Return list of item dicts for a collection id."""
    detail = api(f"/api/v3/collection/{coll_id}")
    items  = detail.get("items") or detail.get("movies") or []
    return detail, items

def resolve_items_to_library(items, by_tmdb, by_imdb):
    """Return list of local movie objs matching collection items."""
    present = []
    for it in items:
        mov  = it.get("movie") or {}
        tmdb = (mov.get("tmdbId") if isinstance(mov, dict) else None) or it.get("tmdbId")
        imdb = (mov.get("imdbId") if isinstance(mov, dict) else None) or it.get("imdbId")
        m = (by_tmdb.get(tmdb) if tmdb else None) or (by_imdb.get(imdb) if imdb else None)
        if m:
            present.append(m)
    return present

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser(description="Batch-fix collection rootFolderPath using oldest present movie's root.")
    ap.add_argument("--only", help="Substring match on collection name/title (case-insensitive).")
    ap.add_argument("--limit", type=int, help="Process at most N collections (after filtering).")
    ap.add_argument("--execute", action="store_true", help="Apply changes. Omit for dry-run.")
    ap.add_argument("--sleep", type=float, default=0.15, help="Pause between updates (seconds).")
    args = ap.parse_args()

    roots  = api("/api/v3/rootfolder")
    movies = api("/api/v3/movie")
    by_tmdb = {m.get("tmdbId"): m for m in movies if m.get("tmdbId")}
    by_imdb = {m.get("imdbId"): m for m in movies if m.get("imdbId")}

    colls = api("/api/v3/collection")
    if not isinstance(colls, list):
        print("No collections returned."); return

    # filter name
    if args.only:
        needle = args.only.lower()
        colls = [c for c in colls if needle in (c.get("name") or c.get("title","")).lower()]
    # limit
    if args.limit:
        colls = colls[:args.limit]

    print(f"Considering {len(colls)} collection(s).\n")

    updated = 0
    already = 0
    skipped = 0
    failed  = 0
    failures = []

    for c in colls:
        cid  = c.get("id")
        name = c.get("name") or c.get("title") or f"Collection {cid}"

        detail, items = expand_collection_items(cid)
        present = resolve_items_to_library(items, by_tmdb, by_imdb)

        if not present:
            print(f"[SKIP] {name}: none of its items are in your library.")
            skipped += 1
            continue

        present.sort(key=year_key)
        oldest = present[0]
        opath  = oldest.get("path") or ""
        root   = longest_root(opath, roots)
        if not opath or not root:
            print(f"[FAIL] {name}: cannot map '{opath}' to any configured root.")
            failed += 1; failures.append(name)
            continue

        desired = root["path"]
        current = detail.get("rootFolderPath")
        print(f"{name}")
        print(f"  - oldest: {oldest.get('title')} ({oldest.get('year')})")
        print(f"  - movie path: {opath}")
        print(f"  - chosen root: {desired}")

        if current == desired:
            print("  - already correct; no change.\n")
            already += 1
            continue

        if not args.execute:
            print("  - DRY RUN: would PUT rootFolderPath →", desired, "\n")
            continue

        payload = dict(detail)
        payload["rootFolderPath"] = desired
        try:
            api(f"/api/v3/collection/{cid}", method="PUT", data=payload)
            print("  - UPDATED\n")
            updated += 1
        except (HTTPError, URLError) as e:
            print(f"  - FAIL: {e}\n")
            failed += 1
            failures.append(name)
        time.sleep(args.sleep)

    # ---- Summary ----
    print("\nSummary")
    print(f"  Updated: {updated}")
    print(f"  Already correct: {already}")
    print(f"  Skipped (no library members): {skipped}")
    print(f"  Failures: {failed}")
    if failures:
        for n in failures:
            print(f"    - {n}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted.")

