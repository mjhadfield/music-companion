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
        # one id, or several that count as the same artist ("also releases as" aliases)
        ids = [artist_mbid] if isinstance(artist_mbid, str) else list(artist_mbid)
        query = f'releasegroup:"{title}" AND ' + (f"arid:{ids[0]}" if len(ids) == 1 else "(" + " OR ".join(f"arid:{i}" for i in ids) + ")")
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


def _mb_lookup(entity: str, mbid: str, params: dict | None = None) -> dict | None:
    """Direct entity-by-id lookup (unlike _mb_get's search-by-query) -- returns None on a 404,
    a meaningful, expected outcome here ("this id isn't a <entity>"), not an error condition;
    raises on anything else. Same throttle/retry spirit as _mb_get, just without its
    unconditional raise_for_status."""
    for attempt in range(MAX_RETRIES + 1):
        _throttle()
        try:
            resp = requests.get(
                f"{BASE_URL}/{entity}/{mbid}" if mbid else f"{BASE_URL}/{entity}",
                params={**(params or {}), "fmt": "json"},
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT_SECONDS,
            )
        except requests.exceptions.RequestException:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            raise
        if resp.status_code == 404:
            return None
        if resp.status_code == 503 and attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()
    return None


def resolve_release_group(mbid: str) -> str | None:
    """`albums.mbid` is documented as always a release-group id, but nothing has ever actually
    enforced that -- a release's own MusicBrainz page is very often the one that gets copied
    from (it's usually what you land on browsing MB, one click short of its parent
    release-group), so a manually-pasted id can easily be a release id instead. That's not
    cosmetic: Cover Art Archive treats /release/{id}/ and /release-group/{id}/ as genuinely
    different lookups, and a release with no cover of its own doesn't inherit its
    release-group's -- confirmed on a real case (Frehley's Comet: the stored id 404s as a
    release-group and as a release on CAA, but its *actual* release-group has art).

    Returns the real release-group id to store: unchanged if `mbid` already is one, resolved to
    its parent if `mbid` is actually a release, or None if it's neither (not a valid MusicBrainz
    id at all, or a different entity type entirely)."""
    if _mb_lookup("release-group", mbid) is not None:
        return mbid
    release = _mb_lookup("release", mbid, {"inc": "release-groups"})
    if release and release.get("release-group"):
        return release["release-group"]["id"]
    return None


def resolve_release_first(mbid: str) -> tuple:
    """resolve_release_group for ids that are *usually releases* (Last.fm's album mbids): asks
    for the release first, so the common case is one request instead of two -- and that same
    request brings its group's summary (artist credit, type, first-release date) and its
    tracklist. -> (release-group id or None, summary or None, {track id: recording id} or None,
    {recording id: track title} or None).
    The track map matters because Last.fm's per-track "mbid" is sometimes a track id, which only
    the release's tracklist can turn into the recording (the song)."""
    release = _mb_lookup("release", mbid, {"inc": "release-groups+artist-credits+recordings"})
    if release and release.get("release-group"):
        rg = dict(release["release-group"])
        rg.setdefault("artist-credit", release.get("artist-credit") or [])
        return rg["id"], _release_group_summary(rg), release_track_map(release), release_track_titles(release)
    if _mb_lookup("release-group", mbid) is not None:
        return mbid, None, None, None
    return None, None, None, None


def sort_recording_or_track(mbid: str) -> dict:
    """What a Last.fm per-track id really is, asked directly (for ids with no edition to check
    against). A recording lookup first -- which also follows MusicBrainz's redirect when that
    recording has since been merged into another -- then, failing that, a search for a *track*
    with this id. -> {"recording": id or None, "kind": "recording" | "track" | None}."""
    rec = _mb_lookup("recording", mbid)
    if rec and rec.get("id"):
        return {"recording": rec["id"], "kind": "recording"}
    found = (_mb_get("recording", {"query": f"tid:{mbid}", "limit": 1}).get("recordings") or [])
    if found:
        return {"recording": found[0]["id"], "kind": "track"}
    return {"recording": None, "kind": None}


def _release_tracks(release: dict):
    for medium in release.get("media") or []:
        for track in [medium.get("pregap"), *(medium.get("tracks") or []), *(medium.get("data-tracks") or [])]:
            if track and track.get("id") and (track.get("recording") or {}).get("id"):
                yield track


def release_track_map(release: dict) -> dict:
    """{track id: recording id} for every track on a release (incl. pregap and data tracks)."""
    return {t["id"]: t["recording"]["id"] for t in _release_tracks(release)}


def release_track_titles(release: dict) -> dict:
    """{recording id: title as printed on this release} -- for matching ids that are stale."""
    return {t["recording"]["id"]: t.get("title") or t["recording"].get("title") or "" for t in _release_tracks(release)}


def _release_group_summary(rg: dict) -> dict:
    return {
        "mbid": rg.get("id"),
        "title": rg.get("title"),
        "artistCredit": _credited_name(rg.get("artist-credit") or []),
        "artistMbids": [ac["artist"]["id"] for ac in (rg.get("artist-credit") or []) if ac.get("artist")],
        "primaryType": rg.get("primary-type"),
        "secondaryTypes": rg.get("secondary-types") or [],
        "disambiguation": rg.get("disambiguation") or None,
        "firstReleaseDate": rg.get("first-release-date") or None,
    }


def lookup_artist(mbid: str) -> dict | None:
    """One artist by id -- None if MusicBrainz has no such artist."""
    data = _mb_lookup("artist", mbid)
    if data is None:
        return None
    life_span = data.get("life-span") or {}
    return {
        "mbid": data.get("id"),
        "name": data.get("name"),
        "sortName": data.get("sort-name"),
        "disambiguation": data.get("disambiguation") or None,
        "type": data.get("type"),
        "country": data.get("country"),
        "beginDate": life_span.get("begin"),
        "endDate": life_span.get("end"),
    }


def lookup_release_group(mbid: str) -> dict | None:
    """One release group by id, with its artist credit -- None if it isn't a release group."""
    data = _mb_lookup("release-group", mbid, {"inc": "artist-credits"})
    return _release_group_summary(data) if data is not None else None


def browse_release_groups(artist_mbid: str, max_pages: int = 3) -> list[dict]:
    """Every release group credited to one artist (MB's "browse", not a fuzzy search) -- the
    "pick from this artist's real discography" list. Paged 100 at a time; capped at
    max_pages so a hugely prolific artist can't turn one click into dozens of requests."""
    groups: list[dict] = []
    for page in range(max_pages):
        data = _mb_get("release-group", {"artist": artist_mbid, "limit": 100, "offset": page * 100, "inc": "artist-credits"})
        batch = data.get("release-groups", [])
        groups += [_release_group_summary(rg) for rg in batch]
        if len(groups) >= int(data.get("release-group-count", 0)) or not batch:
            break
    return groups


def lookup_discogs_release(release_id: int) -> list[dict]:
    """MusicBrainz's own URL relationships map a Discogs release page to the MB release(s) that
    link to it -- an exact, curated mapping, not a fuzzy guess. Returns [{releaseMbid,
    releaseTitle}] (usually exactly one; empty when MB has no link to that Discogs release)."""
    data = _mb_lookup("url", "", {"resource": f"https://www.discogs.com/release/{int(release_id)}", "inc": "release-rels"})
    if not data:
        return []
    return [
        {"releaseMbid": rel["release"]["id"], "releaseTitle": rel["release"].get("title")}
        for rel in data.get("relations", [])
        if rel.get("target-type") == "release" and rel.get("release")
    ]


def lookup_discogs_master(master_id: int) -> list[str]:
    """Release-group ids MusicBrainz links to a Discogs *master* (every pressing of one album) --
    the exact album identity even when MB doesn't know the specific pressing."""
    data = _mb_lookup("url", "", {"resource": f"https://www.discogs.com/master/{int(master_id)}", "inc": "release-group-rels"})
    if not data:
        return []
    return [rel["release_group"]["id"] for rel in data.get("relations", [])
            if rel.get("target-type") == "release_group" and rel.get("release_group")]


def release_parent_group(release_mbid: str) -> dict | None:
    """The release group a release belongs to (summary incl. first-release date/artist credit)."""
    data = _mb_lookup("release", release_mbid, {"inc": "release-groups+artist-credits"})
    if not data or not data.get("release-group"):
        return None
    rg = dict(data["release-group"])
    rg.setdefault("artist-credit", data.get("artist-credit") or [])
    return _release_group_summary(rg)


def release_group_tracklist(rg_mbid: str) -> dict | None:
    """A canonical tracklist for a release group: its earliest official release's recordings.
    Two requests (browse releases, then that release's recordings)."""
    data = _mb_get("release", {"release-group": rg_mbid, "limit": 100, "status": "official"})
    releases = [r for r in data.get("releases", []) if r.get("id")]
    if not releases:
        return None
    releases.sort(key=lambda r: (r.get("date") or "9999", r.get("title") or ""))
    chosen = releases[0]
    detail = _mb_lookup("release", chosen["id"], {"inc": "recordings"})
    if not detail:
        return None
    tracks = []
    for medium in detail.get("media", []):
        for t in medium.get("tracks", []):
            rec = t.get("recording") or {}
            tracks.append({
                "position": f"{medium.get('position', 1)}-{t.get('position')}",
                "title": t.get("title") or rec.get("title"),
                "recordingMbid": rec.get("id"),
                "lengthMs": rec.get("length"),
            })
    return {"releaseMbid": chosen["id"], "releaseTitle": chosen.get("title"), "date": chosen.get("date"), "tracks": tracks}


def search_recordings(title: str, artist_mbid: str | None = None, limit: int = 10) -> list[dict]:
    title = (title or "").strip().replace('"', "")
    if not title:
        return []
    query = f'recording:"{title}"' + (f" AND arid:{artist_mbid}" if artist_mbid else "")
    data = _mb_get("recording", {"query": query, "limit": limit})
    return [
        {
            "mbid": r.get("id"),
            "title": r.get("title"),
            "artistCredit": _credited_name(r.get("artist-credit") or []),
            "disambiguation": r.get("disambiguation") or None,
            "lengthMs": r.get("length"),
            "score": int(r.get("score", 0)),
        }
        for r in data.get("recordings", [])
    ]


def recording_release_groups(title: str, artist_mbid: str, limit: int = 50) -> list[dict]:
    """Which albums (release groups) does this artist's recording called `title` appear on?
    For songs only ever heard live: finds the studio album so the live performance can be tied
    to it. Only recordings whose own title matches (case/punctuation-insensitive) count; release
    groups come back studio albums first, then earliest."""
    import re

    def norm(t):
        return re.sub(r"[^\w]+", " ", (t or "").casefold()).strip()

    title = (title or "").strip().replace('"', "")
    if not title or not artist_mbid:
        return []
    data = _mb_get("recording", {"query": f'recording:"{title}" AND arid:{artist_mbid}', "limit": limit})
    groups: dict[str, dict] = {}
    for rec in data.get("recordings", []):
        if norm(rec.get("title")) != norm(title):
            continue
        for rel in rec.get("releases", []) or []:
            rg = rel.get("release-group") or {}
            if not rg.get("id"):
                continue
            g = groups.setdefault(rg["id"], {
                "mbid": rg["id"], "title": rg.get("title"), "primaryType": rg.get("primary-type"),
                "secondaryTypes": rg.get("secondary-types") or [], "firstDate": None, "recordingMbid": rec.get("id"),
            })
            date = rel.get("date")
            if date and (not g["firstDate"] or date < g["firstDate"]):
                g["firstDate"] = date
    rank = lambda g: (g["primaryType"] != "Album", bool(g["secondaryTypes"]), g["firstDate"] or "9999")  # noqa: E731
    return sorted(groups.values(), key=rank)
