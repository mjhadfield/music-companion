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
    python etl/lastfm_pull.py --resolve-limit 0 # no MusicBrainz lookups this run

Every scrobble's album id from Last.fm is a MusicBrainz *release* (one edition). Each new one is
resolved to its release group once (see releases.py) so editions land on the album you already
have instead of becoming new albums; the exact release is still kept per scrobble
(scrobble_releases). Last.fm's per-track id is sorted the same way: a recording id becomes the
song's mbid, a *track* id (one slot on one release) is kept in song_tracks instead -- the
edition's tracklist, fetched in that same one lookup, tells them apart. Anything new, unmatched
or odd is listed in maintenance > Inbox.
"""
import argparse
import json
import time
from datetime import datetime, timedelta, timezone

import requests
import releases
from common import (connect, finish_import_run, get_or_create_album, get_or_create_artist, get_or_create_song, load_env,
                    record_event, require_env, start_import_run)

API_URL = "https://ws.audioscrobbler.com/2.0/"
PAGE_SIZE = 200          # max allowed by Last.fm for this endpoint
REQUEST_DELAY_SECONDS = 0.25   # polite pacing, well under Last.fm's rate limit
RESOLVE_LIMIT = 150            # default cap on new MusicBrainz release lookups per run (1/s)
EARLIEST_PLAUSIBLE = datetime(2002, 1, 1, tzinfo=timezone.utc)  # Last.fm/Audioscrobbler didn't exist before this


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


def pull(full: bool, max_pages: int | None, resolve_limit: int = RESOLVE_LIMIT) -> None:
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

    run_id = start_import_run(conn, "lastfm")
    budget = {"left": resolve_limit}
    session = requests.Session()
    page = 1
    total_pages = None
    imported = 0
    skipped_now_playing = 0
    skipped_duplicate = 0
    skipped_invalid = 0
    releases_looked_up = 0

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

        # Resolve this page's new editions first, before any of its writes (network with no lock held).
        # Only a pre-migration database lacks the tables -- then album ids are just ignored.
        # An edition also needs its tracklist when a track id played from it isn't placed yet.
        if run_id is not None:
            track_ids = {}
            for t in tracks:
                rel = (t.get("album") or {}).get("mbid")
                if rel and t.get("mbid"):
                    track_ids.setdefault(rel, set()).add(t["mbid"])
            releases_looked_up += releases.resolve_new(
                conn, [(t.get("album") or {}).get("mbid") for t in tracks if (t.get("album") or {}).get("#text")], budget, track_ids)
            releases_looked_up += releases.sort_new_ids(
                conn, [t.get("mbid") for t in tracks if not (t.get("album") or {}).get("mbid")], budget)

        now = datetime.now(timezone.utc)
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
            played = datetime.fromtimestamp(int(uts), tz=timezone.utc)
            played_at = played.isoformat()

            raw_artist = (track.get("artist", {}).get("#text") or "").strip()
            raw_track = (track.get("name") or "").strip()
            raw_album = (track.get("album", {}).get("#text") or "").strip()
            if not raw_artist or not raw_track:
                skipped_invalid += 1
                continue
            if played < EARLIEST_PLAUSIBLE or played > now + timedelta(hours=1):
                # a device with a wrong clock: keep it out of the stats, list it for a look
                record_event(conn, "suspect", "scrobble", None, {"reason": "implausible-date", "playedAt": played_at,
                             "artist": raw_artist, "track": raw_track, "album": raw_album})
                skipped_invalid += 1
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

            if raw_artist.lower() in ("[unknown]", "unknown artist", "unknown"):
                record_event(conn, "suspect", "artist", artist_id, {"reason": "placeholder-artist", "name": raw_artist})

            album_id = None
            release_mbid = None
            if raw_album:
                release_mbid = track.get("album", {}).get("mbid") or None
                album_id = get_or_create_album(conn, album_cache, [artist_id], raw_album,
                                               release_mbid=release_mbid if run_id is not None else None, source="lastfm")

            # Last.fm's per-track id may be a recording OR a track id -- sorted using the edition's tracklist
            lastfm_id = track.get("mbid") or None
            song_id = get_or_create_song(conn, song_cache, artist_id, raw_track, album_id=album_id, source="lastfm",
                                         lastfm_id=lastfm_id if run_id is not None else None,
                                         release_mbid=(track.get("album") or {}).get("mbid") or None)

            conn.execute(
                """
                INSERT INTO scrobbles (
                    song_id, artist_id, album_id, played_at,
                    raw_artist_text, raw_track_text, raw_album_text, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'lastfm')
                """,
                (song_id, artist_id, album_id, played_at, raw_artist, raw_track, raw_album or None),
            )
            scrobble_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            if release_mbid and run_id is not None:
                conn.execute("INSERT INTO scrobble_releases (scrobble_id, release_mbid) VALUES (?, ?)", (scrobble_id, release_mbid))
            if lastfm_id and run_id is not None:
                conn.execute("INSERT INTO scrobble_tracks (scrobble_id, lastfm_mbid) VALUES (?, ?)", (scrobble_id, lastfm_id))
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

    summary = {"imported": imported, "nowPlaying": skipped_now_playing, "duplicates": skipped_duplicate,
               "invalid": skipped_invalid, "releasesLookedUp": releases_looked_up}
    if run_id is not None:
        summary["events"] = dict(conn.execute("SELECT kind, count(*) FROM import_events WHERE run_id = ? GROUP BY kind", (run_id,)).fetchall())
    finish_import_run(conn, summary)
    conn.close()
    print(
        f"Done. Imported {imported} scrobbles "
        f"(skipped {skipped_now_playing} now-playing/undated, {skipped_duplicate} duplicates, {skipped_invalid} invalid)."
    )
    if releases_looked_up or budget["left"] < resolve_limit:
        print(f"Looked up {releases_looked_up} new album editions on MusicBrainz.")
    if summary.get("events"):
        print("For review (maintenance > Inbox): " + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(summary["events"].items())))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="pull entire history, ignoring what's already stored")
    parser.add_argument("--max-pages", type=int, default=None, help="stop after N pages (for testing)")
    parser.add_argument("--resolve-limit", type=int, default=RESOLVE_LIMIT, help="max new MusicBrainz release lookups this run")
    args = parser.parse_args()
    pull(full=args.full, max_pages=args.max_pages, resolve_limit=args.resolve_limit)
