"""
Minimal Discogs API client:
  * release(id) -- a release's master id (the Discogs grouping of every pressing of an album) and its
    pressing details (country, date, format descriptions, identifiers such as barcode and matrix/runout,
    companies such as the pressing plant, tracklist with side positions, notes, genres and styles);
  * identity(), collection_fields(user), collection_instances(user) -- your own collection, for the
    collection sync (discogs_import.py --api). These need DISCOGS_TOKEN.

With a personal access token (DISCOGS_TOKEN in .env -- discogs.com > Settings > Developers) Discogs
allows 60 requests/minute; without one, 25. A central throttle keeps under whichever applies (one
request per interval across every thread), identifies itself with a User-Agent as Discogs asks, and
backs off on 429. release() callers go through mbcache, so each release is fetched at most once per
cache window.
"""
import os
import threading
import time
from datetime import datetime

import requests

from common import load_env

USER_AGENT = "MusicCompanionMaintenance/1.0 (mikehadfield89@gmail.com)"
BASE_URL = "https://api.discogs.com"
MIN_INTERVAL_SECONDS = 2.6         # ~23/min, under the 25/min unauthenticated limit
MIN_INTERVAL_WITH_TOKEN = 1.1       # ~54/min, under the 60/min limit with a token
TIMEOUT_SECONDS = 15

_lock = threading.Lock()
_last = 0.0


class DiscogsError(RuntimeError):
    """Something to show as-is ("Discogs says the token is wrong")."""


def token() -> str | None:
    load_env()
    return (os.environ.get("DISCOGS_TOKEN") or "").strip() or None


def _headers() -> dict:
    h = {"User-Agent": USER_AGENT}
    t = token()
    if t:
        h["Authorization"] = f"Discogs token={t}"
    return h


def _throttle() -> None:
    global _last
    interval = MIN_INTERVAL_WITH_TOKEN if token() else MIN_INTERVAL_SECONDS
    with _lock:
        wait = interval - (time.monotonic() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.monotonic()


def _get(path: str, params: dict | None = None) -> dict | None:
    """GET a JSON resource, throttled, backing off on 429. None for a 404."""
    for attempt in range(3):
        _throttle()
        resp = requests.get(f"{BASE_URL}{path}", params=params, headers=_headers(), timeout=TIMEOUT_SECONDS)
        if resp.status_code == 404:
            return None
        if resp.status_code == 401:
            raise DiscogsError("Discogs refused the token (401) -- check DISCOGS_TOKEN in .env")
        if resp.status_code == 429 and attempt < 2:
            time.sleep(30 * (attempt + 1))  # Discogs' limit window is a minute (and a 429 still counts)
            continue
        resp.raise_for_status()
        return resp.json()
    return None


def release(release_id: int) -> dict | None:
    """{masterId, country, year, released, title, artists, formats, genres, styles, labels, identifiers,
    companies, tracklist, notes} -- None if Discogs has no such release."""
    d = _get(f"/releases/{int(release_id)}")
    if d is None:
        return None
    return {
        "masterId": d.get("master_id") or None,
        "country": d.get("country") or None,
        "year": d.get("year") or None,
        "released": d.get("released") or None,
        "title": d.get("title"),
        "artists": [a.get("name") for a in d.get("artists", [])],
        "formats": [{"name": f.get("name"), "qty": f.get("qty"), "descriptions": f.get("descriptions") or [], "text": f.get("text")}
                    for f in d.get("formats", [])],
        "genres": d.get("genres") or [],
        "styles": d.get("styles") or [],
        "labels": [{"name": lb.get("name"), "catno": lb.get("catno")} for lb in d.get("labels", [])],
        "identifiers": [{"type": i.get("type"), "value": i.get("value"), "description": i.get("description")}
                        for i in d.get("identifiers", [])],
        "companies": [{"name": co.get("name"), "role": co.get("entity_type_name")} for co in d.get("companies", [])],
        "tracklist": [{"position": t.get("position"), "title": t.get("title"), "duration": t.get("duration"), "type": t.get("type_")}
                      for t in d.get("tracklist", [])],
        "notes": d.get("notes") or None,
        # the release's own photos (sleeve front first) -- the pressing as it really looks
        "images": [{"type": im.get("type"), "uri": im.get("uri"), "uri150": im.get("uri150"), "width": im.get("width"), "height": im.get("height")}
                   for im in d.get("images", []) if im.get("uri")],
    }


# ---- your collection (needs DISCOGS_TOKEN) -------------------------------------------------------

def identity() -> str:
    """The token's Discogs username."""
    if not token():
        raise DiscogsError("No DISCOGS_TOKEN in .env -- create one at discogs.com > Settings > Developers")
    d = _get("/oauth/identity")
    if not d or not d.get("username"):
        raise DiscogsError("Discogs didn't say whose token this is")
    return d["username"]


def collection_fields(username: str) -> dict[int, str]:
    """{field id: name} -- "Media Condition", "Sleeve Condition", "Notes" and any of your own."""
    d = _get(f"/users/{username}/collection/fields") or {}
    return {int(f["id"]): f.get("name", "") for f in d.get("fields", [])}


def collection_instances(username: str) -> list[dict]:
    """Every copy in your collection (folder 0 = all folders), oldest addition first, as Discogs returns them."""
    out, page = [], 1
    while True:
        d = _get(f"/users/{username}/collection/folders/0/releases",
                 {"page": page, "per_page": 100, "sort": "added", "sort_order": "asc"}) or {}
        out.extend(d.get("releases", []))
        pages = (d.get("pagination") or {}).get("pages", 1)
        if page >= pages:
            return out
        page += 1


def export_time(iso: str | None) -> str:
    """Discogs' "2026-09-05T07:56:29-07:00" -> "2026-09-05 07:56:29": the clock time as Discogs gives it (its own US
    Pacific zone), which is what the CSV export writes -- not converted to UTC, or every record would look changed."""
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso
