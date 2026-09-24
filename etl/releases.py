"""
Release -> release-group resolution for importers (and the maintenance conversion sweep).

Last.fm tags scrobbles with a MusicBrainz *release* id -- one specific edition (a remaster, a
country's pressing). The library's album is the release *group*, so each edition is resolved
once, exactly, and remembered: this module does the MusicBrainz half (throttled 1 req/s via
musicbrainz.py, cached in mb_cache for good), and get_or_create_album reads the answer from the
cache. Call it with no transaction open -- it commits its cache writes as it goes, and never
holds a write lock across a network request.
"""
import json
import sqlite3

import musicbrainz as mb
from common import RELEASE_PARENT_KEY, RESOLVE_KEY, SORT_ID_KEY, TITLES_KEY, TRACKS_KEY


def unknown_releases(conn: sqlite3.Connection, release_mbids, track_ids: dict | None = None) -> list[str]:
    """The releases that need a lookup: not already placed (album_releases / a legacy albums.mbid)
    or resolved -- or, given `track_ids` ({release: {Last.fm track ids played from it}}), with a
    track id that isn't placed yet (song_tracks / a song's recording id) while its tracklist
    isn't known."""
    out = []
    for r in dict.fromkeys(m for m in release_mbids if m):
        if conn.execute("SELECT 1 FROM mb_cache WHERE key = ?", (TRACKS_KEY.format(r),)).fetchone():
            continue  # fully looked up already
        placed = (conn.execute("SELECT 1 FROM album_releases WHERE release_mbid = ?", (r,)).fetchone()
                  or conn.execute("SELECT 1 FROM albums WHERE mbid = ?", (r,)).fetchone()
                  or conn.execute("SELECT 1 FROM mb_cache WHERE key = ?", (RESOLVE_KEY.format(r),)).fetchone())
        tracks_placed = all(
            conn.execute("SELECT 1 FROM song_tracks WHERE track_mbid = ?", (t,)).fetchone()
            or conn.execute("SELECT 1 FROM songs WHERE mbid = ?", (t,)).fetchone()
            for t in (track_ids or {}).get(r, ()))
        if placed and tracks_placed:
            continue
        out.append(r)
    return out


def resolve(conn: sqlite3.Connection, release_mbid: str) -> str | None:
    """One release, looked up (one request) and cached: its group id, the group's summary, and
    its tracklist as {track id: recording id} (stored even when empty, so it's never re-asked)."""
    rg, summary, tracks, *rest = mb.resolve_release_first(release_mbid)
    titles = rest[0] if rest else None
    rows = [(RESOLVE_KEY.format(release_mbid), json.dumps(rg)), (TRACKS_KEY.format(release_mbid), json.dumps(tracks or {})),
            (TITLES_KEY.format(release_mbid), json.dumps(titles or {}))]
    if summary:
        rows.append((RELEASE_PARENT_KEY.format(release_mbid), json.dumps(summary)))
    conn.executemany(
        "INSERT INTO mb_cache (key, payload_json, fetched_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT (key) DO UPDATE SET payload_json = excluded.payload_json, fetched_at = excluded.fetched_at", rows)
    conn.commit()
    return rg


def resolve_new(conn: sqlite3.Connection, release_mbids, budget: dict, track_ids: dict | None = None) -> int:
    """Resolve every not-yet-known release in `release_mbids`, spending from budget["left"]
    (a per-run cap, so a big backlog can't turn into hours of MusicBrainz traffic -- anything
    over it is imported by title and flagged "not looked up yet"). -> how many were looked up."""
    todo = unknown_releases(conn, release_mbids, track_ids)
    done = 0
    for r in todo:
        if budget["left"] <= 0:
            break
        budget["left"] -= 1
        try:
            resolve(conn, r)
        except Exception as e:  # network trouble: leave it unresolved, the import carries on
            print(f"  ... couldn't resolve release {r}: {e}")
            continue
        done += 1
    return done


def sort_id(conn: sqlite3.Connection, lastfm_id: str) -> dict:
    """One Last.fm per-track id sorted directly (see musicbrainz.sort_recording_or_track), cached."""
    result = mb.sort_recording_or_track(lastfm_id)
    conn.execute("INSERT INTO mb_cache (key, payload_json, fetched_at) VALUES (?, ?, datetime('now')) "
                 "ON CONFLICT (key) DO UPDATE SET payload_json = excluded.payload_json, fetched_at = excluded.fetched_at",
                 (SORT_ID_KEY.format(lastfm_id), json.dumps(result)))
    conn.commit()
    return result


def sort_new_ids(conn: sqlite3.Connection, lastfm_ids, budget: dict) -> int:
    """Direct lookups for Last.fm track ids played with no edition to sort them by -- only ones not
    already placed (song_tracks / a song's mbid) or looked up; same per-run budget."""
    done = 0
    for t in dict.fromkeys(i for i in lastfm_ids if i):
        if budget["left"] <= 0:
            break
        if (conn.execute("SELECT 1 FROM song_tracks WHERE track_mbid = ?", (t,)).fetchone()
                or conn.execute("SELECT 1 FROM songs WHERE mbid = ?", (t,)).fetchone()
                or conn.execute("SELECT 1 FROM mb_cache WHERE key = ?", (SORT_ID_KEY.format(t),)).fetchone()):
            continue
        budget["left"] -= 1
        try:
            sort_id(conn, t)
            done += 1
        except Exception as e:
            print(f"  ... couldn't look up track id {t}: {e}")
    return done
