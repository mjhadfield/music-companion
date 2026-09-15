"""
Pull scrobble history from the Last.fm API into music.sqlite.

Requires LASTFM_API_KEY and LASTFM_USERNAME in .env (see .env.example).
Only a plain API key is needed -- user.getRecentTracks is a public,
read-only endpoint, no authenticated session required.

By default this runs *incrementally*: it looks at the most recent
played_at already stored and only fetches scrobbles since then, so it's
cheap to re-run periodically. Pass --full to ignore that and pull your
entire history from scratch (safe to do -- scrobbles are deduped below).

Usage:
    python etl/lastfm_pull.py                 # incremental (or full, if empty)
    python etl/lastfm_pull.py --full           # force full history re-pull
    python etl/lastfm_pull.py --max-pages 2    # smoke-test against a few pages
"""
import argparse
import json
import time
from datetime import datetime, timezone

import requests
from common import connect, get_or_create_album, get_or_create_artist, get_or_create_song, load_env, require_env

API_URL = "https://ws.audioscrobbler.com/2.0/"
PAGE_SIZE = 200          # max allowed by Last.fm for this endpoint
REQUEST_DELAY_SECONDS = 0.25   # polite pacing, well under Last.fm's rate limit


def fetch_page(session: requests.Session, api_key: str, username: str, page: int, from_ts: int | None) -> dict:
    params = {
        "method": "user.getrecenttracks",
        "user": username,
        "api_key": api_key,
        "format": "json",
        "limit": PAGE_SIZE,
        "page": page,
        "extended": 0,
    }
    if from_ts:
        params["from"] = from_ts

    for attempt in range(1, 4):
        resp = session.get(API_URL, params=params, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 500, 502, 503):
            wait = 2 ** attempt
            print(f"  ... got HTTP {resp.status_code}, retrying in {wait}s")
            time.sleep(wait)
            continue
        resp.raise_for_status()
    resp.raise_for_status()


def most_recent_played_at(conn) -> int | None:
    row = conn.execute(
        "SELECT played_at FROM scrobbles ORDER BY played_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    dt = datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def pull(full: bool, max_pages: int | None) -> None:
    load_env()
    api_key = require_env("LASTFM_API_KEY")
    username = require_env("LASTFM_USERNAME")

    conn = connect()
    from_ts = None if full else most_recent_played_at(conn)
    if from_ts:
        print(f"Incremental pull: fetching scrobbles after {datetime.fromtimestamp(from_ts, tz=timezone.utc).isoformat()}")
    else:
        print("Full history pull (this can take a while for a large library).")

    artist_cache: dict = {}
    album_cache: dict = {}
    song_cache: dict = {}

    session = requests.Session()
    page = 1
    total_pages = None
    imported = 0
    skipped_now_playing = 0
    skipped_duplicate = 0

    while True:
        data = fetch_page(session, api_key, username, page, from_ts)
        recent = data.get("recenttracks", {})
        attr = recent.get("@attr", {})
        if total_pages is None:
            total_pages = int(attr.get("totalPages", 1))
            total_tracks = attr.get("total", "?")
            print(f"Total scrobbles to fetch: {total_tracks} across {total_pages} pages")

        tracks = recent.get("track", [])
        if isinstance(tracks, dict):  # Last.fm returns a bare object, not a list, for a single result
            tracks = [tracks]

        for track in tracks:
            conn.execute(
                "INSERT INTO staging_lastfm_rows (raw_json) VALUES (?)", (json.dumps(track),)
            )

            if track.get("@attr", {}).get("nowplaying") == "true":
                # Currently-playing track has no timestamp yet -- skip, it'll
                # show up as a real scrobble on a future pull once it's done.
                skipped_now_playing += 1
                continue

            uts = track.get("date", {}).get("uts")
            if not uts:
                skipped_now_playing += 1
                continue
            played_at = datetime.fromtimestamp(int(uts), tz=timezone.utc).isoformat()

            raw_artist = (track.get("artist", {}).get("#text") or "").strip()
            raw_track = (track.get("name") or "").strip()
            raw_album = (track.get("album", {}).get("#text") or "").strip()
            if not raw_artist or not raw_track:
                skipped_duplicate += 1
                continue

            dup = conn.execute(
                "SELECT 1 FROM scrobbles WHERE raw_artist_text = ? AND raw_track_text = ? AND played_at = ?",
                (raw_artist, raw_track, played_at),
            ).fetchone()
            if dup:
                skipped_duplicate += 1
                continue

            artist_mbid = track.get("artist", {}).get("mbid") or None
            artist_id = get_or_create_artist(conn, artist_cache, raw_artist, mbid=artist_mbid, source="lastfm")

            album_id = None
            if raw_album:
                album_mbid = track.get("album", {}).get("mbid") or None
                album_id = get_or_create_album(conn, album_cache, [artist_id], raw_album, mbid=album_mbid, source="lastfm")

            track_mbid = track.get("mbid") or None
            song_id = get_or_create_song(conn, song_cache, artist_id, raw_track, album_id=album_id, mbid=track_mbid)

            conn.execute(
                """
                INSERT INTO scrobbles (
                    song_id, artist_id, album_id, played_at,
                    raw_artist_text, raw_track_text, raw_album_text, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'lastfm')
                """,
                (song_id, artist_id, album_id, played_at, raw_artist, raw_track, raw_album or None),
            )
            imported += 1

        conn.commit()
        print(f"  page {page}/{total_pages}: {imported} imported so far")

        if page >= total_pages:
            break
        if max_pages and page >= max_pages:
            print(f"Stopping early at --max-pages={max_pages}")
            break
        page += 1
        time.sleep(REQUEST_DELAY_SECONDS)

    conn.close()
    print(
        f"Done. Imported {imported} scrobbles "
        f"(skipped {skipped_now_playing} now-playing/undated, {skipped_duplicate} duplicates)."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="pull entire history, ignoring what's already stored")
    parser.add_argument("--max-pages", type=int, default=None, help="stop after N pages (for testing)")
    args = parser.parse_args()
    pull(full=args.full, max_pages=args.max_pages)
