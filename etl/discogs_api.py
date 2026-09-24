"""
Minimal Discogs API client -- just the one read the maintenance tool needs: a release's master id
(the Discogs grouping of every pressing of an album) and its pressing details (country, year).

Unauthenticated, which Discogs allows at 25 requests/minute; this stays under that with a
central throttle (one request per MIN_INTERVAL_SECONDS across every thread), identifies itself
with a User-Agent as Discogs asks, and backs off on 429. Callers go through mbcache, so each
release is fetched at most once per cache window.
"""
import threading
import time

import requests

USER_AGENT = "MusicCompanionMaintenance/1.0 (mikehadfield89@gmail.com)"
BASE_URL = "https://api.discogs.com"
MIN_INTERVAL_SECONDS = 2.6  # ~23/min, under the 25/min unauthenticated limit
TIMEOUT_SECONDS = 15

_lock = threading.Lock()
_last = 0.0


def _throttle() -> None:
    global _last
    with _lock:
        wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.monotonic()


def release(release_id: int) -> dict | None:
    """{masterId, country, year, released, title, artists, formats} -- None if Discogs has no such release."""
    for attempt in range(3):
        _throttle()
        resp = requests.get(f"{BASE_URL}/releases/{int(release_id)}", headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS)
        if resp.status_code == 404:
            return None
        if resp.status_code == 429 and attempt < 2:
            time.sleep(30 * (attempt + 1))  # Discogs' limit window is a minute
            continue
        resp.raise_for_status()
        d = resp.json()
        return {
            "masterId": d.get("master_id") or None,
            "country": d.get("country") or None,
            "year": d.get("year") or None,
            "released": d.get("released") or None,
            "title": d.get("title"),
            "artists": [a.get("name") for a in d.get("artists", [])],
            "formats": [{"name": f.get("name"), "qty": f.get("qty"), "descriptions": f.get("descriptions") or [], "text": f.get("text")}
                        for f in d.get("formats", [])],
        }
    return None
