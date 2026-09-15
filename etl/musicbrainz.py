"""
Thin client for MusicBrainz's web service -- used by the maintenance tool
to help resolve artists (and later albums/songs) that have no mbid yet.

No API key required, but MusicBrainz's usage etiquette asks for two
things from unauthenticated callers: an identifying User-Agent (so they
can reach you if a script misbehaves) and no more than ~1 request/second.
Both are enforced here centrally so every search function gets them for
free.
"""
import threading
import time

import requests

USER_AGENT = "MusicCompanionMaintenance/1.0 (mikehadfield89@gmail.com)"
BASE_URL = "https://musicbrainz.org/ws/2"
MIN_INTERVAL_SECONDS = 1.0
TIMEOUT_SECONDS = 10

_rate_lock = threading.Lock()
_last_request_at = 0.0


def _throttle() -> None:
    """Block until at least MIN_INTERVAL_SECONDS has passed since the last
    outbound request. Guarded by a lock so this is safe even though the
    maintenance server handles requests on multiple threads."""
    global _last_request_at
    with _rate_lock:
        wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


MAX_RETRIES = 2  # on top of the initial attempt
RETRY_BACKOFF_SECONDS = 1.5


def _mb_get(path: str, params: dict) -> dict:
    # MB's search endpoint returns a 503 "currently busy" fairly often
    # under load -- it's explicitly asking the caller to try again, not
    # reporting a problem with the request -- and the odd connection
    # reset/timeout is just normal internet flakiness. A short
    # retry-with-backoff smooths both over instead of surfacing them as
    # an error every time. A non-503 HTTP error (a genuinely bad request)
    # still raises immediately -- retrying that would just fail the same
    # way three times instead of once.
    for attempt in range(MAX_RETRIES + 1):
        _throttle()
        try:
            resp = requests.get(
                f"{BASE_URL}/{path}",
                params={**params, "fmt": "json"},
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT_SECONDS,
            )
        except requests.exceptions.RequestException:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            raise
        if resp.status_code == 503 and attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()


def search_artists(query: str, limit: int = 10) -> list[dict]:
    """Fuzzy-search MusicBrainz for artists matching `query`. Returns
    candidates ordered by MB's own relevance score (best first)."""
    data = _mb_get("artist", {"query": query, "limit": limit})
    candidates = []
    for artist in data.get("artists", []):
        life_span = artist.get("life-span") or {}
        candidates.append({
            "mbid": artist.get("id"),
            "name": artist.get("name"),
            "disambiguation": artist.get("disambiguation") or None,
            "type": artist.get("type"),
            "country": artist.get("country"),
            # MB returns this as a string attribute on each match, not a number.
            "score": int(artist.get("score", 0)),
            "beginDate": life_span.get("begin"),
            "endDate": life_span.get("end"),
            "ended": life_span.get("ended", False),
        })
    return candidates


def _credited_name(artist_credit: list[dict]) -> str:
    """MB's convention for turning an artist-credit list back into display
    text: concatenate each entry's name with its own joinphrase (e.g. "Kiss"
    + ", " + "Ace Frehley" -> "Kiss, Ace Frehley")."""
    return "".join((ac.get("name") or "") + (ac.get("joinphrase") or "") for ac in artist_credit)


def search_release_groups(title: str, artist_name: str | None = None, artist_mbid: str | None = None, limit: int = 10) -> list[dict]:
    """Fuzzy-search MusicBrainz for release groups (i.e. "albums" in the
    work-level sense MB uses -- one release group covers the original
    pressing, remasters, special editions, etc.) matching `title`.

    Scoped to one artist when we have something to scope by: an mbid is the
    precise filter, an artist name is a softer hint, and with neither we
    fall back to a plain title search. Candidates are ordered by MB's own
    relevance score (best first)."""
    title = (title or "").strip().replace('"', "")
    if not title:
        return []

    if artist_mbid:
        query = f'releasegroup:"{title}" AND arid:{artist_mbid}'
    elif artist_name:
        query = f'releasegroup:"{title}" AND artist:"{artist_name.strip().replace(chr(34), "")}"'
    else:
        query = f'releasegroup:"{title}"'

    data = _mb_get("release-group", {"query": query, "limit": limit})
    candidates = []
    for rg in data.get("release-groups", []):
        candidates.append({
            "mbid": rg.get("id"),
            "title": rg.get("title"),
            "artistCredit": _credited_name(rg.get("artist-credit") or []),
            "primaryType": rg.get("primary-type"),
            "secondaryTypes": rg.get("secondary-types") or [],
            "disambiguation": rg.get("disambiguation") or None,
            "firstReleaseDate": rg.get("first-release-date") or None,
            "score": int(rg.get("score", 0)),
        })
    return candidates


# search_recordings(query, artist_mbid=None, limit=10) slots in here later
# for a songs maintenance pass, reusing _mb_get/_throttle as-is.
