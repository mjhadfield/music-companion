"""
Cached front door to etl/musicbrainz.py -- every MusicBrainz call the maintenance tool makes
goes through here, so each distinct lookup is made at most once per expiry window no matter
how many times a page, sweep or verify pass asks for it. Politeness to MusicBrainz is the
point: musicbrainz.py already throttles to 1 request/second; this makes sure we rarely need to.

"Not found" is cached too (as JSON null) -- asking MB again tomorrow whether an id it just
404'd exists yet is exactly the kind of repeat traffic this exists to avoid.
"""
import json
import threading

import discogs_api
import musicbrainz as mb
from common import connect as db_connect

TTL_DAYS = {"search": 7, "lookup": 30, "browse": 30}

# Outbound-request counter (cache misses) -- lets verification prove a second run is fully cached.
stats = {"hits": 0, "misses": 0}
_stats_lock = threading.Lock()


def _cached(key: str, kind: str, fetch):
    conn = db_connect()
    try:
        row = conn.execute(
            "SELECT payload_json FROM mb_cache WHERE key = ? AND fetched_at > datetime('now', ?)",
            (key, f"-{TTL_DAYS[kind]} days"),
        ).fetchone()
    finally:
        conn.close()
    if row:
        with _stats_lock:
            stats["hits"] += 1
        return json.loads(row[0])

    with _stats_lock:
        stats["misses"] += 1
    payload = fetch()  # network -- deliberately outside any open connection/transaction
    conn = db_connect()
    try:
        conn.execute(
            "INSERT INTO mb_cache (key, payload_json, fetched_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT (key) DO UPDATE SET payload_json = excluded.payload_json, fetched_at = excluded.fetched_at",
            (key, json.dumps(payload)),
        )
        conn.commit()
    finally:
        conn.close()
    return payload


def peek(key: str):
    """Cached value without ever fetching -- (found, payload). For showing extra detail only
    when it's already known (e.g. an MB disambiguation on a comparison card)."""
    conn = db_connect()
    try:
        row = conn.execute("SELECT payload_json FROM mb_cache WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    return (True, json.loads(row[0])) if row else (False, None)


def search_artists(q: str, limit: int = 10):
    return _cached(f"search:artist:{limit}:{q.strip().lower()}", "search", lambda: mb.search_artists(q, limit=limit))


def artist(mbid: str):
    return _cached(f"lookup:artist:{mbid}", "lookup", lambda: mb.lookup_artist(mbid))


def artist_release_groups(artist_mbid: str):
    return _cached(f"browse:release-groups:{artist_mbid}", "browse", lambda: mb.browse_release_groups(artist_mbid))


def search_release_groups(title: str, artist_name=None, artist_mbid=None, limit: int = 10):
    mb_key = artist_mbid if isinstance(artist_mbid, str) or artist_mbid is None else ",".join(sorted(artist_mbid))
    key = f"search:release-group:{limit}:{mb_key or ''}:{(artist_name or '').lower()}:{title.strip().lower()}"
    return _cached(key, "search", lambda: mb.search_release_groups(title, artist_name=artist_name, artist_mbid=artist_mbid, limit=limit))


def release_group(mbid: str):
    return _cached(f"lookup:release-group:{mbid}", "lookup", lambda: mb.lookup_release_group(mbid))


def resolve_release_group(mbid: str):
    return _cached(f"resolve:release-group:{mbid}", "lookup", lambda: mb.resolve_release_group(mbid))


def discogs_release(release_id: int):
    return _cached(f"lookup:discogs-release:{int(release_id)}", "lookup", lambda: mb.lookup_discogs_release(release_id))


def release_parent_group(release_mbid: str):
    return _cached(f"lookup:release-parent:{release_mbid}", "lookup", lambda: mb.release_parent_group(release_mbid))


def release_group_tracklist(rg_mbid: str):
    return _cached(f"browse:tracklist:{rg_mbid}", "browse", lambda: mb.release_group_tracklist(rg_mbid))


def search_recordings(title: str, artist_mbid=None, limit: int = 10):
    key = f"search:recording:{limit}:{artist_mbid or ''}:{title.strip().lower()}"
    return _cached(key, "search", lambda: mb.search_recordings(title, artist_mbid=artist_mbid, limit=limit))


def discogs_master_groups(master_id: int):
    return _cached(f"lookup:discogs-master:{int(master_id)}", "lookup", lambda: mb.lookup_discogs_master(master_id))


def discogs_release_details(release_id: int):
    """Discogs' own API (not MusicBrainz) -- cached in the same table, same expiry rules."""
    return _cached(f"lookup:discogs-api:{int(release_id)}", "lookup", lambda: discogs_api.release(release_id))


def recording_release_groups(title: str, artist_mbid: str):
    key = f"search:recording-rgs:{artist_mbid}:{title.strip().lower()}"
    return _cached(key, "browse", lambda: mb.recording_release_groups(title, artist_mbid))  # 30 days: albums rarely change


def recording_release_groups_key(title: str, artist_mbid: str) -> str:
    return f"search:recording-rgs:{artist_mbid}:{title.strip().lower()}"
