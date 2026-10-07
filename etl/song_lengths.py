"""
Fill in each song's length (songs.length_ms, with songs.length_source saying where it came from).

In order, cheapest and most exact first:
  1. 'tracklist' -- the album's MusicBrainz tracklist (album_tracklists): the same recording id, else the same
     title on the song's own album;
  2. 'pressing'  -- your record's Discogs tracklist (vinyl_details), the same title on the song's album;
  3. 'lastfm'    -- Last.fm's track.getInfo duration (one request each, most-played songs first). A song Last.fm
     has no length for is marked 'none', so it isn't asked about again.

A live / demo / remix version only takes a length matched by recording id or from Last.fm -- never the studio
track's by title. Lengths are information, not identity: nothing is merged, renamed or linked.

Why it matters: Last.fm only counts a play of a track longer than 30 seconds, so a shorter one (Black Sabbath's
"Embryo", 0:23) can never show a play; and with lengths the site can show listening time.

Usage:
    python etl/song_lengths.py                # tracklists for every song, then Last.fm for up to 500
    python etl/song_lengths.py --lastfm 5000  # ... Last.fm for up to 5000
    python etl/song_lengths.py --lastfm 0     # tracklists only
"""
import argparse
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent / "maintenance"))
from common import connect, load_env  # noqa: E402
from titles import split_title, track_key  # noqa: E402

API_URL = "https://ws.audioscrobbler.com/2.0/"
LASTFM_GAP = 0.25                      # Last.fm asks for no more than ~5 requests a second
VARIANTS = {"live", "demo", "remix", "edit", "acoustic", "instrumental", "version"}


def _secs(duration: str) -> int | None:
    """Discogs' "4:32" / "1:02:03" -> seconds."""
    try:
        parts = [int(p) for p in str(duration or "").strip().split(":")]
    except ValueError:
        return None
    if len(parts) < 2:
        return None
    total = 0
    for p in parts:
        total = total * 60 + p
    return total or None


def _variant(title: str) -> bool:
    return bool(split_title(title)[1] & VARIANTS)


def from_tracklists(conn) -> dict[str, int]:
    """Steps 1 and 2 for every song without a length. -> counts by source."""
    filled = {"tracklist": 0, "pressing": 0}
    # 1a. the same MusicBrainz recording
    filled["tracklist"] += conn.execute("""
        UPDATE songs SET length_ms = (SELECT t.length_ms FROM album_tracklists t WHERE t.recording_mbid = songs.mbid AND t.length_ms > 0 LIMIT 1),
                         length_source = 'tracklist'
        WHERE length_ms IS NULL AND mbid IS NOT NULL
          AND EXISTS (SELECT 1 FROM album_tracklists t WHERE t.recording_mbid = songs.mbid AND t.length_ms > 0)""").rowcount
    # 1b / 2. the same title on the song's own album: MusicBrainz's tracklist, then your pressing's
    by_album: dict[int, dict[str, tuple[int, str]]] = {}
    for album_id, title, ms in conn.execute("SELECT album_id, title, length_ms FROM album_tracklists WHERE length_ms > 0"):
        by_album.setdefault(album_id, {}).setdefault(track_key(title), (ms, "tracklist"))
    for album_id, tracklist in conn.execute("SELECT v.album_id, d.tracklist FROM vinyl_holdings v JOIN vinyl_details d ON d.holding_id = v.id WHERE d.tracklist IS NOT NULL"):
        for t in json.loads(tracklist or "[]"):
            secs = _secs(t.get("duration"))
            if secs and t.get("title") and (t.get("type") or "track") == "track":
                by_album.setdefault(album_id, {}).setdefault(track_key(t["title"]), (secs * 1000, "pressing"))
    rows = conn.execute("SELECT id, title, album_id FROM songs WHERE length_ms IS NULL AND album_id IS NOT NULL").fetchall()
    for song_id, title, album_id in rows:
        hit = by_album.get(album_id, {}).get(track_key(title))
        if hit and not _variant(title):
            conn.execute("UPDATE songs SET length_ms = ?, length_source = ? WHERE id = ?", (hit[0], hit[1], song_id))
            filled[hit[1]] += 1
    conn.commit()
    return filled


def lastfm_length(session: requests.Session, api_key: str, artist: str, title: str) -> int | None:
    """Last.fm's length for a track, in ms (None when it doesn't know)."""
    for attempt in range(3):
        resp = session.get(API_URL, params={"method": "track.getInfo", "api_key": api_key, "artist": artist, "track": title,
                                            "autocorrect": 1, "format": "json"}, timeout=20)
        if resp.status_code in (429, 500, 502, 503):
            time.sleep(2 ** (attempt + 1))
            continue
        resp.raise_for_status()
        ms = int(((resp.json() or {}).get("track") or {}).get("duration") or 0)
        return ms or None
    return None


def from_lastfm(conn, limit: int, log=print, progress=None, cancelled=lambda: False) -> dict[str, int]:
    """Step 3: Last.fm for up to `limit` songs still without a length, most played first."""
    load_env()
    import os
    api_key = os.environ.get("LASTFM_API_KEY")
    if not api_key:
        log("No LASTFM_API_KEY in .env -- skipping Last.fm.")
        return {"lastfm": 0, "none": 0}
    rows = conn.execute("""
        SELECT so.id, so.title, ar.name, (SELECT count(*) FROM scrobbles s WHERE s.song_id = so.id) AS plays
        FROM songs so JOIN artists ar ON ar.id = so.artist_id
        WHERE so.length_ms IS NULL AND so.length_source IS NULL
        ORDER BY plays DESC LIMIT ?""", (limit,)).fetchall()
    got = {"lastfm": 0, "none": 0}
    session = requests.Session()
    for i, (song_id, title, artist, _plays) in enumerate(rows):
        if cancelled():
            log("Cancelled.")
            break
        if progress:
            progress(i, len(rows))
        try:
            ms = lastfm_length(session, api_key, artist, title)
        except Exception as exc:  # one failure shouldn't end the run
            log(f"  ! {artist} -- {title}: {exc}")
            continue
        conn.execute("UPDATE songs SET length_ms = ?, length_source = ? WHERE id = ?", (ms, "lastfm" if ms else "none", song_id))
        got["lastfm" if ms else "none"] += 1
        if i % 50 == 49:
            conn.commit()
        time.sleep(LASTFM_GAP)
    conn.commit()
    if progress:
        progress(len(rows), len(rows))
    return got


def tracklist_lines(conn) -> list[tuple[int, str, str]]:
    """Tracklist lines with no length and none looked up yet: [(album_id, title, artist)] -- your pressings' Discogs
    tracklists (often no durations) and MusicBrainz lines without one."""
    have = {(a, t) for a, t in conn.execute("SELECT album_id, title FROM track_lengths")}
    out, seen = [], set()
    for album_id, artist, tracklist in conn.execute("""SELECT v.album_id, ar.name, d.tracklist FROM vinyl_holdings v JOIN vinyl_details d ON d.holding_id = v.id
                                                       JOIN albums al ON al.id = v.album_id JOIN artists ar ON ar.id = al.artist_id WHERE d.tracklist IS NOT NULL"""):
        for t in json.loads(tracklist or "[]"):
            title = (t.get("title") or "").strip()
            if title and (t.get("type") or "track") == "track" and not _secs(t.get("duration")) and (album_id, title) not in have | seen:
                seen.add((album_id, title))
                out.append((album_id, title, artist))
    for album_id, title, artist in conn.execute("""SELECT t.album_id, t.title, ar.name FROM album_tracklists t JOIN albums al ON al.id = t.album_id
                                                   JOIN artists ar ON ar.id = al.artist_id WHERE coalesce(t.length_ms, 0) = 0"""):
        if (album_id, title) not in have | seen:
            seen.add((album_id, title))
            out.append((album_id, title, artist))
    return out


def lines_from_songs(conn, lines) -> int:
    """A line whose song you've played and whose length is known: free."""
    known = {}
    for album_id, title, ms in conn.execute("SELECT album_id, title, length_ms FROM songs WHERE length_ms IS NOT NULL AND album_id IS NOT NULL"):
        known.setdefault((album_id, track_key(title)), ms)
    n = 0
    for album_id, title, _artist in lines:
        ms = known.get((album_id, track_key(title)))
        if ms:
            conn.execute("INSERT OR IGNORE INTO track_lengths (album_id, title, length_ms, source) VALUES (?, ?, ?, 'song')", (album_id, title, ms))
            n += 1
    conn.commit()
    return n


def lines_from_lastfm(conn, limit: int, log=print, cancelled=lambda: False) -> int:
    load_env()
    import os
    api_key = os.environ.get("LASTFM_API_KEY")
    if not api_key:
        return 0
    todo = tracklist_lines(conn)[:limit]
    session, n = requests.Session(), 0
    for i, (album_id, title, artist) in enumerate(todo):
        if cancelled():
            break
        try:
            ms = lastfm_length(session, api_key, artist, title)
        except Exception as exc:
            log(f"  ! {artist} -- {title}: {exc}")
            continue
        conn.execute("INSERT OR IGNORE INTO track_lengths (album_id, title, length_ms, source) VALUES (?, ?, ?, ?)", (album_id, title, ms, "lastfm" if ms else "none"))
        n += bool(ms)
        if i % 50 == 49:
            conn.commit()
        time.sleep(LASTFM_GAP)
    conn.commit()
    return n


def run(lastfm_limit: int = 500, log=print, progress=None, cancelled=lambda: False) -> dict[str, int]:
    conn = connect()
    try:
        if "length_ms" not in {r[1] for r in conn.execute("PRAGMA table_info(songs)")}:
            log("Songs have no length column yet -- the maintenance server adds it when it next starts. Skipped.")
            return {}
        filled = from_tracklists(conn)
        log(f"From tracklists: {filled['tracklist']} (MusicBrainz) + {filled['pressing']} (your pressings).")
        if lastfm_limit:
            lines = tracklist_lines(conn)          # tracklist lines with no length (incl. tracks never played) come first
            got_songs = lines_from_songs(conn, lines)
            got_lfm = lines_from_lastfm(conn, lastfm_limit, log, cancelled) if lines else 0
            log(f"Tracklist lines without a length: {len(lines)} -- {got_songs} from your songs, {got_lfm} from Last.fm.")
            lastfm_limit = max(0, lastfm_limit - (len(lines) - got_songs))
            got = from_lastfm(conn, lastfm_limit, log, progress, cancelled) if lastfm_limit else {"lastfm": 0, "none": 0}
            filled.update(got)
            log(f"From Last.fm: {got['lastfm']} (and {got['none']} it has no length for).")
        left = conn.execute("SELECT count(*) FROM songs WHERE length_ms IS NULL AND length_source IS NULL").fetchone()[0]
        log(f"{left} song(s) still to look up." if left else "Every song has been looked up.")
        return filled
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lastfm", type=int, default=500, metavar="N", help="look up to N songs on Last.fm (most played first; 0 = none)")
    args = parser.parse_args()
    try:
        run(args.lastfm)
    except Exception as exc:  # part of every Last.fm refresh: never the reason one fails
        print(f"Song lengths stopped: {exc}")
