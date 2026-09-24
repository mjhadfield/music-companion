"""
Pull attended setlists from the Setlist.fm API into music.sqlite.

Requires SETLISTFM_API_KEY and SETLISTFM_USERNAME in .env.

Unlike the Last.fm puller, this always re-fetches everything: your total
setlist count is small (hundreds, not hundreds of thousands), setlists on
setlist.fm get corrected/edited after the fact (missing songs added,
tour names fixed), and a full re-pull is cheap. Each setlist is deleted
and reinserted by setlistfm_id, so re-running keeps this in sync rather
than accumulating duplicates or going stale.

A note on cover songs: when a setlist entry is marked as a cover (e.g.
you saw a band play a cover live), the canonical song/artist it's linked
to is the *original* artist -- not the performer on stage that night.
That's deliberate: it's what makes "I've heard this song live N times"
match up with the same canonical song your Last.fm scrobbles and Discogs
vinyl point at, which will almost always be filed under the original
artist. setlist_songs.raw_song_text/cover_of_artist_text still record
who actually performed it and what the cover credit said, so nothing is
lost -- it's just resolved to the right canonical entity.

Usage:
    python etl/setlistfm_pull.py
    python etl/setlistfm_pull.py --max-pages 2   # smoke-test
"""
import argparse
import json
import math
import time

import requests
from common import (connect, finish_import_run, get_or_create_artist, get_or_create_song, get_or_create_venue, load_env,
                    require_env, start_import_run)

API_URL = "https://api.setlist.fm/rest/1.0/user/{username}/attended"
REQUEST_DELAY_SECONDS = 0.6   # setlist.fm's free tier caps at 2 req/sec; stay well under it


def fetch_page(session: requests.Session, api_key: str, username: str, page: int) -> dict:
    headers = {"x-api-key": api_key, "Accept": "application/json"}
    url = API_URL.format(username=username)

    for attempt in range(1, 4):
        resp = session.get(url, headers=headers, params={"p": page}, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 500, 502, 503):
            wait = 2 ** attempt
            print(f"  ... got HTTP {resp.status_code}, retrying in {wait}s")
            time.sleep(wait)
            continue
        resp.raise_for_status()
    resp.raise_for_status()


def parse_event_date(date_str: str) -> str | None:
    # setlist.fm gives "DD-MM-YYYY"
    parts = (date_str or "").split("-")
    if len(parts) != 3:
        return None
    day, month, year = parts
    return f"{year}-{month}-{day}"


def replace_setlist(conn, setlistfm_id: str) -> None:
    """Delete any existing copy of this setlist so a re-pull reflects
    edits on setlist.fm's end instead of leaving stale song rows behind."""
    row = conn.execute("SELECT id FROM setlists WHERE setlistfm_id = ?", (setlistfm_id,)).fetchone()
    if row:
        conn.execute("DELETE FROM setlist_songs WHERE setlist_id = ?", (row[0],))
        conn.execute("DELETE FROM setlists WHERE id = ?", (row[0],))


def pull(max_pages: int | None) -> None:
    load_env()
    api_key = require_env("SETLISTFM_API_KEY")
    username = require_env("SETLISTFM_USERNAME")

    conn = connect()
    artist_cache: dict = {}
    song_cache: dict = {}
    venue_cache: dict = {}
    run_id = start_import_run(conn, "setlistfm")  # new artists/songs go to maintenance > Inbox

    session = requests.Session()
    page = 1
    total_pages = None
    imported = 0
    skipped = 0
    songs_linked = 0

    while True:
        data = fetch_page(session, api_key, username, page)
        if total_pages is None:
            total = data.get("total", 0)
            items_per_page = data.get("itemsPerPage", 20) or 20
            total_pages = max(1, math.ceil(total / items_per_page))
            print(f"Total attended setlists: {total} across {total_pages} pages")

        setlists = data.get("setlist", [])
        if isinstance(setlists, dict):
            setlists = [setlists]

        for sl in setlists:
            conn.execute("INSERT INTO staging_setlistfm_rows (raw_json) VALUES (?)", (json.dumps(sl),))

            setlistfm_id = sl.get("id")
            raw_artist = (sl.get("artist", {}).get("name") or "").strip()
            event_date = parse_event_date(sl.get("eventDate", ""))
            if not setlistfm_id or not raw_artist or not event_date:
                skipped += 1
                continue

            artist_mbid = sl.get("artist", {}).get("mbid") or None
            artist_id = get_or_create_artist(conn, artist_cache, raw_artist, mbid=artist_mbid, source="setlistfm")

            venue = sl.get("venue", {}) or {}
            city = venue.get("city", {}) or {}
            venue_id = get_or_create_venue(
                conn,
                venue_cache,
                setlistfm_id=venue.get("id"),
                name=venue.get("name"),
                city=city.get("name"),
                state=city.get("state"),
                country=(city.get("country") or {}).get("name"),
            )

            tour = sl.get("tour") or {}
            tour_name = (tour.get("name") or "").strip() or None

            replace_setlist(conn, setlistfm_id)
            cur = conn.execute(
                """
                INSERT INTO setlists (setlistfm_id, artist_id, venue_id, event_date, tour_name, raw_artist_text)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (setlistfm_id, artist_id, venue_id, event_date, tour_name, raw_artist),
            )
            setlist_id = cur.lastrowid

            position = 0
            for set_block in (sl.get("sets", {}) or {}).get("set", []):
                is_encore = bool(set_block.get("encore"))
                set_name = set_block.get("name") or ("Encore" if is_encore else None)
                for song in set_block.get("song", []):
                    song_name = (song.get("name") or "").strip()
                    if not song_name:
                        continue  # tape/intro entries with no actual song name
                    position += 1

                    cover = song.get("cover")
                    if cover:
                        cover_artist_name = (cover.get("name") or "").strip()
                        cover_artist_mbid = cover.get("mbid") or None
                        song_artist_id = (
                            get_or_create_artist(conn, artist_cache, cover_artist_name, mbid=cover_artist_mbid, source="setlistfm")
                            if cover_artist_name
                            else artist_id
                        )
                        is_cover = 1
                    else:
                        cover_artist_name = None
                        song_artist_id = artist_id
                        is_cover = 0

                    # A cover a human re-linked to the band's OWN recording (maintenance > Songs):
                    # honour that for this performer only, before falling back to the original artist.
                    relinked = conn.execute(
                        "SELECT a.canonical_id FROM alias_overrides a JOIN songs s ON s.id = a.canonical_id "
                        "WHERE a.source = 'setlistfm' AND a.canonical_type = 'song' AND a.source_key = ?",
                        (f"cover:{artist_id}:{song_name.lower()}",),
                    ).fetchone() if is_cover else None
                    song_id = relinked[0] if relinked else get_or_create_song(conn, song_cache, song_artist_id, song_name, source="setlistfm")
                    songs_linked += 1
                    conn.execute(
                        """
                        INSERT INTO setlist_songs (
                            setlist_id, song_id, position, set_name,
                            is_cover, cover_of_artist_text, raw_song_text
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (setlist_id, song_id, position, set_name, is_cover, cover_artist_name, song_name),
                    )
            imported += 1

        conn.commit()
        print(f"  page {page}/{total_pages}: {imported} setlists so far")

        if page >= total_pages:
            break
        if max_pages and page >= max_pages:
            print(f"Stopping early at --max-pages={max_pages}")
            break
        page += 1
        time.sleep(REQUEST_DELAY_SECONDS)

    summary = {"imported": imported, "songEntries": songs_linked, "skipped": skipped}
    if run_id is not None:
        summary["events"] = dict(conn.execute("SELECT kind, count(*) FROM import_events WHERE run_id = ? GROUP BY kind", (run_id,)).fetchall())
    finish_import_run(conn, summary)
    conn.close()
    print(f"Done. Imported {imported} setlists ({songs_linked} song entries), skipped {skipped}.")
    if summary.get("events"):
        print("For review (maintenance > Inbox): " + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(summary["events"].items())))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-pages", type=int, default=None, help="stop after N pages (for testing)")
    args = parser.parse_args()
    pull(max_pages=args.max_pages)
