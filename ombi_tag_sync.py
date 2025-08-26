#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Ombi → Radarr/Sonarr tag backfill (no status filtering; 1:1 Ombi tags).

ENV VARS (required):
  OMBI_URL, OMBI_API_KEY
  RADARR_URL, RADARR_API_KEY
  SONARR_URL, SONARR_API_KEY

Default: preview only (writes CSVs).
--write radarr      apply tags to Radarr movies
--write sonarr      apply tags to Sonarr series
--apply-tags MODE   add|replace|remove (default: add)
"""

import argparse, csv, os, sys, time, requests
from typing import Dict, List, Optional, Tuple

# ---------- tiny progress/err helpers ----------
def progress(msg: str) -> None:
    print(msg, flush=True)

def backoff_sleep(attempt: int, base: float) -> None:
    time.sleep(base * (attempt + 1))

# ---------- HTTP with retries ----------
class Http:
    def __init__(self, timeout: int = 15, max_retries: int = 3, backoff: float = 1.5):
        self.s = requests.Session(); self.timeout=timeout; self.max_retries=max_retries; self.backoff=backoff
    def _req(self, method, url, **kw):
        last = None
        for i in range(self.max_retries):
            try:
                r = self.s.request(method, url, timeout=self.timeout, **kw)
                if r.status_code >= 500:
                    raise requests.HTTPError(f"{r.status_code} server error: {r.text[:200]}")
                return r
            except Exception as e:
                last = e
                progress(f"[HTTP] {method} {url} failed attempt {i+1}: {e}")
                backoff_sleep(i, self.backoff)
        raise last
    def get(self, url, **kw):  return self._req("GET", url, **kw)
    def post(self, url, **kw): return self._req("POST", url, **kw)
    def put(self, url, **kw):  return self._req("PUT", url, **kw)

# ---------- Ombi ----------
class Ombi:
    def __init__(self, base: str, key: str, http: Http):
        self.base = base.rstrip("/"); self.h = {"ApiKey": key, "Content-Type":"application/json"}; self.http=http
    def _rest(self, path: str) -> Optional[List[dict]]:
        url = f"{self.base}{path}"
        try:
            r = self.http.get(url, headers=self.h); 
            if r.status_code == 404: return None
            r.raise_for_status(); data = r.json(); out=[]
            for it in data:
                requester = (
                    (it.get("requestedUser") or {}).get("userName")
                    or (it.get("requestedUser") or {}).get("alias")
                    or it.get("requestedUserName") or it.get("userAlias")
                    or it.get("userName") or ""
                )
                title = it.get("title") or (it.get("theMovieDb") or {}).get("title") or it.get("seriesName") or ""
                year  = it.get("year") or it.get("releaseYear") or None
                tmdb  = it.get("theMovieDbId") or it.get("tmdbId")
                if isinstance(tmdb, dict): tmdb = tmdb.get("id") or tmdb.get("theMovieDbId")
                tvdb  = it.get("tvDbId") or it.get("tvdbId")
                out.append({"requester": requester, "title": title, "year": year,
                            "tmdbId": tmdb if isinstance(tmdb,int) else None,
                            "tvdbId": tvdb if isinstance(tvdb,int) else None})
            return out
        except Exception as e:
            progress(f"[Ombi] REST fetch failed {path}: {e}")
            return None
    def _graphql(self) -> Tuple[List[dict], List[dict]]:
        url = f"{self.base}/api/graphql"
        q = {"query": """
        query AllRequests {
          requests {
            requestedUser { userName alias }
            movies { title year theMovieDbId }
            tvRequests { seriesName tvDbId }
          }
        }"""}
        r = self.http.post(url, headers=self.h, json=q); r.raise_for_status()
        nodes = (r.json().get("data") or {}).get("requests") or []
        movies, tv = [], []
        for n in nodes:
            requester = (n.get("requestedUser") or {}).get("userName") or (n.get("requestedUser") or {}).get("alias") or ""
            for m in (n.get("movies") or []):
                movies.append({"requester": requester, "title": m.get("title"), "year": m.get("year"),
                               "tmdbId": m.get("theMovieDbId"), "tvdbId": None})
            for s in (n.get("tvRequests") or []):
                tv.append({"requester": requester, "title": s.get("seriesName"), "year": None,
                           "tmdbId": None, "tvdbId": s.get("tvDbId")})
        return movies, tv
    def fetch_all(self) -> Tuple[List[dict], List[dict]]:
        progress("Step 1/5: Fetching Ombi requests (movies & tv)…")
        m = self._rest("/api/v1/Request/movie"); t = self._rest("/api/v1/Request/tv")
        if m is None or t is None:
            progress("…REST not available; falling back to GraphQL")
            m, t = self._graphql()
        progress(f"Ombi items: movies={len(m)} tv={len(t)}")
        return m, t

# ---------- Radarr / Sonarr ----------
class Radarr:
    def __init__(self, base: str, key: str, http: Http):
        self.base = base.rstrip("/"); self.h={"X-Api-Key": key}; self.http=http
    def all_movies(self) -> List[dict]:
        progress("Step 2/5: Loading Radarr library…")
        r = self.http.get(f"{self.base}/api/v3/movie", headers=self.h); r.raise_for_status()
        data = r.json(); progress(f"Radarr movies: {len(data)}"); return data
    def get_or_create_tag(self, label: str) -> int:
        r = self.http.get(f"{self.base}/api/v3/tag", headers=self.h); r.raise_for_status()
        for t in r.json():
            if t.get("label","") == label: return t["id"]
        r = self.http.post(f"{self.base}/api/v3/tag", headers=self.h, json={"label": label}); r.raise_for_status()
        return r.json()["id"]
    def apply_tag(self, ids: List[int], tag_id: int, mode: str):
        if not ids: return
        payload = {"movieIds": sorted(set(ids)), "tags":[tag_id], "applyTags": mode}
        r = self.http.put(f"{self.base}/api/v3/movie/editor", headers=self.h, json=payload); r.raise_for_status()

class Sonarr:
    def __init__(self, base: str, key: str, http: Http):
        self.base = base.rstrip("/"); self.h={"X-Api-Key": key}; self.http=http
    def all_series(self) -> List[dict]:
        progress("Step 3/5: Loading Sonarr library…")
        r = self.http.get(f"{self.base}/api/v3/series", headers=self.h); r.raise_for_status()
        data = r.json(); progress(f"Sonarr series: {len(data)}"); return data
    def get_or_create_tag(self, label: str) -> int:
        r = self.http.get(f"{self.base}/api/v3/tag", headers=self.h); r.raise_for_status()
        for t in r.json():
            if t.get("label","") == label: return t["id"]
        r = self.http.post(f"{self.base}/api/v3/tag", headers=self.h, json={"label": label}); r.raise_for_status()
        return r.json()["id"]
    def apply_tag(self, ids: List[int], tag_id: int, mode: str):
        if not ids: return
        payload = {"seriesIds": sorted(set(ids)), "tags":[tag_id], "applyTags": mode}
        r = self.http.put(f"{self.base}/api/v3/series/editor", headers=self.h, json=payload); r.raise_for_status()

# ---------- mapping helpers ----------
def build_radarr_maps(movies: List[dict]):
    progress("Step 4/5: Building Radarr ID maps…")
    tmdb_to_id, ty_to_id = {}, {}
    for m in movies:
        mid = m.get("id"); tmdb = m.get("tmdbId"); title=(m.get("title") or "").strip().lower(); year=m.get("year")
        if isinstance(tmdb,int): tmdb_to_id[tmdb]=mid
        if title and isinstance(year,int): ty_to_id[(title,year)]=mid
    progress(f"Radarr maps: tmdb={len(tmdb_to_id)} title+year={len(ty_to_id)}")
    return tmdb_to_id, ty_to_id

def build_sonarr_maps(series: List[dict]):
    progress("Step 4b/5: Building Sonarr ID maps…")
    tvdb_to_id, title_to_id = {}, {}
    for s in series:
        sid=s.get("id"); tvdb=s.get("tvdbId"); title=(s.get("title") or "").strip().lower()
        if isinstance(tvdb,int): tvdb_to_id[tvdb]=sid
        if title: title_to_id[title]=sid
    progress(f"Sonarr maps: tvdb={len(tvdb_to_id)} title={len(title_to_id)}")
    return tvdb_to_id, title_to_id

# ---------- csv ----------
def write_csv(path: str, rows: List[dict], headers: List[str]):
    with open(path,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    progress(f"Wrote {len(rows)} rows -> {path}")

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser(description="Ombi→*arr tag backfill (no status filtering).")
    ap.add_argument("--write", choices=["radarr","sonarr"], action="append",
                    help="Apply tags to this service (can repeat). Omit for preview only.")
    ap.add_argument("--apply-tags", choices=["add","replace","remove"], default="add",
                    help="Tag apply mode (default add).")
    ap.add_argument("--outdir", default=".", help="Where to write CSV previews.")
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--backoff", type=float, default=1.5)
    args = ap.parse_args()

    # env check
    req = ["OMBI_URL","OMBI_API_KEY","RADARR_URL","RADARR_API_KEY","SONARR_URL","SONARR_API_KEY"]
    missing=[k for k in req if not os.getenv(k)]
    if missing:
        progress(f"Missing env vars: {', '.join(missing)}"); sys.exit(2)

    http = Http(timeout=args.timeout, max_retries=args.retries, backoff=args.backoff)

    try:
        ombi = Ombi(os.environ["OMBI_URL"], os.environ["OMBI_API_KEY"], http)
        rad  = Radarr(os.environ["RADARR_URL"], os.environ["RADARR_API_KEY"], http)
        son  = Sonarr(os.environ["SONARR_URL"], os.environ["SONARR_API_KEY"], http)

        # Ombi (no status filter)
        movies, shows = ombi.fetch_all()

        # Libraries + maps
        rad_movies = rad.all_movies()
        tmdb_to_movie, ty_to_movie = build_radarr_maps(rad_movies)

        son_series = son.all_series()
        tvdb_to_series, title_to_series = build_sonarr_maps(son_series)

        # Previews (tag = EXACT requester)
        progress("Step 5/5: Building previews…")
        rad_rows=[]; unmatched_r=0
        for i,reqd in enumerate(movies,1):
            if i % 100 == 0: progress(f"… processed {i}/{len(movies)} movie requests")
            title=(reqd.get("title") or "").strip(); year=reqd.get("year"); tmdb=reqd.get("tmdbId")
            mid=None
            if isinstance(tmdb,int) and tmdb in tmdb_to_movie: mid=tmdb_to_movie[tmdb]
            elif title and isinstance(year,int): mid=ty_to_movie.get((title.lower(),year))
            if mid is None: unmatched_r += 1
            rad_rows.append({"requester": reqd.get("requester") or "",
                             "tmdbId": tmdb if isinstance(tmdb,int) else "",
                             "radarrId": mid if isinstance(mid,int) else "",
                             "title": title,
                             "proposedTag": reqd.get("requester") or ""})

        son_rows=[]; unmatched_s=0
        for i,reqd in enumerate(shows,1):
            if i % 100 == 0: progress(f"… processed {i}/{len(shows)} show requests")
            title=(reqd.get("title") or "").strip(); tvdb=reqd.get("tvdbId")
            sid=None
            if isinstance(tvdb,int) and tvdb in tvdb_to_series: sid=tvdb_to_series[tvdb]
            elif title: sid=title_to_series.get(title.lower())
            if sid is None: unmatched_s += 1
            son_rows.append({"requester": reqd.get("requester") or "",
                             "tvdbId": tvdb if isinstance(tvdb,int) else "",
                             "sonarrId": sid if isinstance(sid,int) else "",
                             "title": title,
                             "proposedTag": reqd.get("requester") or ""})

        # write CSVs
        outdir = args.outdir.rstrip("/"); os.makedirs(outdir, exist_ok=True)
        write_csv(f"{outdir}/radarr_preview.csv", rad_rows,
                  headers=["requester","tmdbId","radarrId","title","proposedTag"])
        write_csv(f"{outdir}/sonarr_preview.csv", son_rows,
                  headers=["requester","tvdbId","sonarrId","title","proposedTag"])
        progress(f"Preview complete. Unmatched: Radarr={unmatched_r}, Sonarr={unmatched_s}")

        # apply if requested
        writes=set(args.write or [])
        if "radarr" in writes:
            progress("Applying tags to Radarr…")
            tag_to_ids: Dict[str,List[int]]={}
            for row in rad_rows:
                label=row["proposedTag"]; mid=row.get("radarrId")
                if label and (isinstance(mid,int) or (isinstance(mid,str) and str(mid).isdigit())):
                    tag_to_ids.setdefault(label,[]).append(int(mid))
            for label, ids in tag_to_ids.items():
                try:
                    tag_id = rad.get_or_create_tag(label)
                    progress(f" Radarr: '{label}' -> {len(ids)} movie(s)")
                    rad.apply_tag(ids, tag_id, mode=args.apply_tags)
                except Exception as e:
                    progress(f"[Radarr] tag apply failed '{label}': {e}")

        if "sonarr" in writes:
            progress("Applying tags to Sonarr…")
            tag_to_ids: Dict[str,List[int]]={}
            for row in son_rows:
                label=row["proposedTag"]; sid=row.get("sonarrId")
                if label and (isinstance(sid,int) or (isinstance(sid,str) and str(sid).isdigit())):
                    tag_to_ids.setdefault(label,[]).append(int(sid))
            for label, ids in tag_to_ids.items():
                try:
                    tag_id = son.get_or_create_tag(label)
                    progress(f" Sonarr: '{label}' -> {len(ids)} series")
                    son.apply_tag(ids, tag_id, mode=args.apply_tags)
                except Exception as e:
                    progress(f"[Sonarr] tag apply failed '{label}': {e}")

        progress("All done.")
    except Exception as e:
        progress(f"[FATAL] {e}"); sys.exit(1)

if __name__ == "__main__":
    main()
