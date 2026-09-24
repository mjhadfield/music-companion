"""
Shared helpers used by every ETL script: env loading and the
get-or-create-by-name matching logic for artists/albums/songs.

This is deliberately the *naive* first-pass matcher (case-insensitive
exact name match, optionally backfilling an MBID when a source hands us
one). It's what gets data in the door from each source independently.
The later cross-source entity-resolution pass is what reconciles the
inevitable near-misses (e.g. "Beatles, The" vs "The Beatles") using
MusicBrainz IDs and etl/common's alias_overrides table -- it is not
this module's job.
"""
import json
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
# MUSIC_DB_PATH points every script (and the maintenance server) at a different database --
# used to run a throwaway test instance against a scratch copy without ever touching the real
# data/music.sqlite. Read at import time, not from .env: it's a per-process override, set on
# the command line for that one test run, never a standing setting.
DB_PATH = Path(os.environ["MUSIC_DB_PATH"]) if os.environ.get("MUSIC_DB_PATH") else ROOT / "data" / "music.sqlite"
DB_OVERRIDDEN = bool(os.environ.get("MUSIC_DB_PATH"))


def load_env() -> None:
    """Minimal .env loader (no external dependency needed for `KEY=value` lines)."""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(
            f"Missing {name} -- set it in .env (see .env.example) or the environment."
        )
    return value


def connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise SystemExit(f"{DB_PATH} doesn't exist yet -- run `python etl/init_db.py` first.")
    conn = sqlite3.connect(DB_PATH, timeout=15)  # background sweeps + UI writes can briefly overlap
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# -- Import inbox ---------------------------------------------------------------------------
# An importer calls start_import_run(); from then on, everything the get_or_create_* functions
# create or can't place is recorded in import_events for review (maintenance > Inbox). With no
# run started (e.g. the Discogs import, whose results are always reviewed by hand) nothing is
# recorded.
_import_run = {"id": None, "seen": set()}


def start_import_run(conn: sqlite3.Connection, source: str) -> int | None:
    try:
        run_id = conn.execute("INSERT INTO import_runs (source) VALUES (?)", (source,)).lastrowid
    except sqlite3.OperationalError:  # pre-migration database
        return None
    conn.commit()
    _import_run["id"] = run_id
    _import_run["seen"] = set()
    return run_id


def finish_import_run(conn: sqlite3.Connection, summary: dict) -> None:
    if _import_run["id"] is None:
        return
    conn.execute("UPDATE import_runs SET finished_at = datetime('now'), summary_json = ? WHERE id = ?",
                 (json.dumps(summary), _import_run["id"]))
    conn.commit()
    _import_run["id"] = None


def record_event(conn: sqlite3.Connection, kind: str, entity_type: str | None, entity_id: int | None, detail: dict | None = None) -> None:
    if _import_run["id"] is None:
        return
    # once per thing per run -- the album/song paths see the same clash on every scrobble
    sig = (kind, entity_type, entity_id, json.dumps(detail or {}, sort_keys=True))
    if sig in _import_run["seen"]:
        return
    _import_run["seen"].add(sig)
    # an exact MusicBrainz edition -> album link is for the record only; everything else awaits review
    informational = kind == "edition_linked" and (detail or {}).get("how") == "release group"
    conn.execute("INSERT INTO import_events (run_id, kind, entity_type, entity_id, detail_json, reviewed_at) "
                 "VALUES (?, ?, ?, ?, ?, CASE WHEN ? THEN datetime('now') END)",
                 (_import_run["id"], kind, entity_type, entity_id, json.dumps(detail or {}), informational))


def _set_mbid_if_free(conn: sqlite3.Connection, table: str, row_id: int, mbid: str) -> None:
    """Best-effort: claim this mbid for this row, unless some other row already has it. That
    used to be silently ignored -- but "the source says this is the same thing as another row
    of yours" is the strongest duplicate signal there is, so it's now recorded for review."""
    try:
        conn.execute(f"UPDATE {table} SET mbid = ? WHERE id = ? AND mbid IS NULL", (mbid, row_id))
    except sqlite3.IntegrityError:
        holder = conn.execute(f"SELECT id FROM {table} WHERE mbid = ?", (mbid,)).fetchone()
        if holder and holder[0] != row_id:
            record_event(conn, "mbid_clash", {"artists": "artist", "albums": "album", "songs": "song"}[table], row_id,
                         {"mbid": mbid, "holderId": holder[0]})


VARIOUS_ARTISTS_MBID = "89ad4ac3-39f7-470e-963a-56509c546377"
RELEASE_PARENT_KEY = "lookup:release-parent:{}"
RESOLVE_KEY = "resolve:release-group:{}"
TRACKS_KEY = "tracks:release:{}"  # {track id: recording id} for one release
TITLES_KEY = "track-titles:release:{}"  # {recording id: title as printed} for one release
SORT_ID_KEY = "sort:lastfm-track-id:{}"  # {"recording", "kind"} for one Last.fm id, asked directly


def credit_mismatch(conn: sqlite3.Connection, artist_id: int, release_mbid: str) -> dict | None:
    """The release's group, if MusicBrainz credits it to someone else entirely -- Last.fm's album
    ids are occasionally just wrong (a same-titled album by another artist). None when it checks
    out, when either side has no artist mbid to compare, or for Various Artists compilations."""
    row = conn.execute("SELECT payload_json FROM mb_cache WHERE key = ?", (RELEASE_PARENT_KEY.format(release_mbid),)).fetchone()
    parent = json.loads(row[0]) if row else None
    credited = set((parent or {}).get("artistMbids") or [])
    mine = artist_mbid_sets(conn, [artist_id]).get(artist_id) or set()
    if not credited or not mine or credited & mine or VARIOUS_ARTISTS_MBID in credited:
        return None
    return parent


def release_group_for(conn: sqlite3.Connection, release_mbid: str):
    """Cached release -> release-group resolution (read-only; the importer resolves new releases
    up front, outside its write transaction). -> rg mbid, None if MusicBrainz says it's neither,
    or False if it hasn't been looked up."""
    row = conn.execute("SELECT payload_json FROM mb_cache WHERE key = ?", (RESOLVE_KEY.format(release_mbid),)).fetchone()
    return json.loads(row[0]) if row else False


def get_or_create_artist(
    conn: sqlite3.Connection,
    cache: dict,
    name: str,
    mbid: str | None = None,
    source: str | None = None,
) -> int:
    # A manual merge decision beats everything else -- check alias_overrides
    # before either mbid or name matching, exactly as schema.sql's docstring
    # always promised. `source` identifies which importer is asking
    # ('lastfm' | 'setlistfm' | 'discogs'); callers that don't pass one
    # (or a name with no override on file) fall straight through to the
    # matching below, unchanged.
    if source:
        override_key = ("override", source, name.strip().lower())
        if override_key in cache:
            return cache[override_key]
        row = conn.execute(
            "SELECT canonical_id FROM alias_overrides WHERE source = ? AND source_key = ? AND canonical_type = 'artist'",
            (source, name.strip().lower()),
        ).fetchone()
        if row:
            artist_id = row[0]
            cache[override_key] = artist_id
            if mbid:
                _set_mbid_if_free(conn, "artists", artist_id, mbid)
            return artist_id

    # An mbid a local artist "also releases as" (maintenance > artist_mb_aliases), e.g. The Jimi
    # Hendrix Experience -> Jimi Hendrix: same artist for this library's purposes.
    if mbid:
        try:
            row = conn.execute("SELECT artist_id FROM artist_mb_aliases WHERE mbid = ?", (mbid,)).fetchone()
        except sqlite3.OperationalError:  # table not created yet (pre-migration database)
            row = None
        if row:
            cache[name.lower()] = row[0]
            return row[0]

    # mbid is the stronger signal -- check it first so two different text
    # spellings of the same mbid (e.g. from different sources) resolve to
    # one row instead of colliding on artists.mbid's UNIQUE constraint.
    if mbid:
        mbid_key = ("mbid", mbid)
        if mbid_key in cache:
            return cache[mbid_key]
        row = conn.execute("SELECT id FROM artists WHERE mbid = ?", (mbid,)).fetchone()
        if row:
            cache[mbid_key] = cache[name.lower()] = row[0]
            return row[0]

    key = name.lower()
    if key in cache:
        artist_id = cache[key]
    else:
        row = conn.execute("SELECT id FROM artists WHERE lower(name) = ?", (key,)).fetchone()
        if row:
            artist_id = row[0]
        else:
            cur = conn.execute(
                "INSERT INTO artists (name, sort_name, mbid) VALUES (?, ?, ?)",
                (name, name, mbid or None),
            )
            artist_id = cur.lastrowid
            record_event(conn, "new_artist", "artist", artist_id, {"name": name, "mbid": mbid, "source": source})
        cache[key] = artist_id
    if mbid:
        held = conn.execute("SELECT mbid FROM artists WHERE id = ?", (artist_id,)).fetchone()
        if held and held[0] and held[0] != mbid:
            # Same name, different MusicBrainz artist (two bands called Nirvana): filed under the
            # local one as before, but flagged -- artist mbids are trusted, so this needs a look.
            record_event(conn, "suspect", "artist", artist_id,
                         {"reason": "artist-mbid-differs", "name": name, "mbid": mbid, "localMbid": held[0], "source": source})
        else:
            _set_mbid_if_free(conn, "artists", artist_id, mbid)
            cache[("mbid", mbid)] = artist_id
    return artist_id


def get_or_create_album(
    conn: sqlite3.Connection,
    cache: dict,
    artist_ids: list[int],
    title: str,
    year: int | None = None,
    mbid: str | None = None,
    source: str | None = None,
    release_mbid: str | None = None,
) -> int:
    """artist_ids[0] is the primary/display artist; the full credit list (e.g. Discogs' "Kiss,
    Ace Frehley") is written to album_artists on first insert.

    `mbid` is a release GROUP id (the album); `release_mbid` is a specific edition's release id
    -- what Last.fm hands us. Editions never become albums of their own: a release resolves, in
    order, via
      1. a human's alias override for this raw title (source-scoped),
      2. album_releases -- an edition already seen,
      3. legacy: an album still storing that release id as its mbid,
      4. its release group (resolved via MusicBrainz, cached) -> the album that has it,
      5. the existing (artist, exact title) match,
    and only then is a new album created -- identified by the release group, never the release.
    Every edition learned is remembered in album_releases; everything new or unresolved is
    recorded for the Import inbox (see record_event).

    `source` scopes the alias override key: (source, "<primary artist id>:<raw title, lowercased>")."""
    primary_artist_id = artist_ids[0]
    title_key = (primary_artist_id, title.lower())

    rg = None
    unresolved = False

    def learn(album_id: int, how: str | None) -> int:
        """Remember this edition. `how` records an automatic link for the inbox: "release group"
        (exact -- informational) or "title" (by name only -- for review)."""
        if release_mbid:
            cur = conn.execute("INSERT OR IGNORE INTO album_releases (release_mbid, album_id, title, source) VALUES (?, ?, ?, ?)",
                               (release_mbid, album_id, title, source))
            if cur.rowcount and how:
                record_event(conn, "edition_linked", "album", album_id,
                             {"release": release_mbid, "releaseGroup": rg or None, "title": title, "how": how})
            if cur.rowcount and not unresolved and _import_run["id"] is not None:
                # an earlier run's "not looked up yet" for this edition has now settled itself
                conn.execute("UPDATE import_events SET reviewed_at = datetime('now') WHERE kind = 'unresolved_release' "
                             "AND reviewed_at IS NULL AND json_extract(detail_json, '$.release') = ?", (release_mbid,))
            cache[("release", release_mbid)] = album_id
        return album_id

    if source:
        source_key = f"{primary_artist_id}:{title.strip().lower()}"
        override_key = ("override", source, source_key)
        if override_key in cache:
            return learn(cache[override_key], None)
        row = conn.execute(
            "SELECT canonical_id FROM alias_overrides WHERE source = ? AND source_key = ? AND canonical_type = 'album'",
            (source, source_key),
        ).fetchone()
        if row:
            album_id = row[0]
            cache[override_key] = album_id
            if mbid:
                _set_mbid_if_free(conn, "albums", album_id, mbid)
            return learn(album_id, None)

    if release_mbid:
        if ("release", release_mbid) in cache:
            return cache[("release", release_mbid)]
        row = conn.execute("SELECT album_id FROM album_releases WHERE release_mbid = ?", (release_mbid,)).fetchone()
        if row:
            cache[("release", release_mbid)] = row[0]
            return row[0]
        row = conn.execute("SELECT id FROM albums WHERE mbid = ?", (release_mbid,)).fetchone()
        if row:  # an album still carrying a release id as its mbid (pre-conversion data)
            return learn(row[0], None)
        rg = release_group_for(conn, release_mbid)
        other = rg and credit_mismatch(conn, primary_artist_id, release_mbid)
        if other:
            # don't trust it: fall back to plain title matching, and flag the edition for review
            # (once ever -- the inbox decision is what settles it)
            if _import_run["id"] is not None and not conn.execute(
                    "SELECT 1 FROM import_events WHERE kind = 'suspect' AND json_extract(detail_json, '$.release') = ?",
                    (release_mbid,)).fetchone():
                # filed under the artist: a wrong artist mbid from the source is the usual cause
                record_event(conn, "suspect", "artist", primary_artist_id, {"reason": "release-credited-elsewhere", "release": release_mbid,
                             "title": title, "artistId": primary_artist_id, "releaseGroup": other})
            rg, release_mbid = None, None
        looked_up = rg is not False
        if rg:
            row = conn.execute("SELECT id FROM albums WHERE mbid = ?", (rg,)).fetchone()
            if row:
                return learn(row[0], "release group")  # an exact MusicBrainz match: linked automatically
            mbid = mbid or rg  # a new album (or a title match below) gets the release group, never the release
        elif not other:
            unresolved = True

    if mbid:
        mbid_key = ("mbid", mbid)
        if mbid_key in cache:
            return learn(cache[mbid_key], None)
        row = conn.execute("SELECT id FROM albums WHERE mbid = ?", (mbid,)).fetchone()
        if row:
            cache[mbid_key] = cache[title_key] = row[0]
            return learn(row[0], "release group")

    created = False
    if title_key in cache:
        album_id = cache[title_key]
    else:
        row = conn.execute(
            "SELECT id FROM albums WHERE artist_id = ? AND lower(title) = ?",
            (primary_artist_id, title.lower()),
        ).fetchone()
        if row:
            album_id = row[0]
        else:
            cur = conn.execute(
                "INSERT INTO albums (artist_id, title, year, mbid) VALUES (?, ?, ?, ?)",
                (primary_artist_id, title, year, mbid or None),
            )
            album_id = cur.lastrowid
            created = True
            for position, artist_id in enumerate(artist_ids):
                conn.execute(
                    "INSERT OR IGNORE INTO album_artists (album_id, artist_id, position) VALUES (?, ?, ?)",
                    (album_id, artist_id, position),
                )
            record_event(conn, "new_album", "album", album_id,
                         {"title": title, "artistId": primary_artist_id, "mbid": mbid, "release": release_mbid, "source": source})
        cache[title_key] = album_id
    if mbid:
        _set_mbid_if_free(conn, "albums", album_id, mbid)
        cache[("mbid", mbid)] = album_id
    if unresolved:
        record_event(conn, "unresolved_release", "album", album_id,
                     {"release": release_mbid, "title": title, "why": "not on MusicBrainz" if looked_up else "not looked up yet"})
        if not looked_up:
            return album_id  # not remembered: the next run looks it up (see releases.resolve_new)
    return learn(album_id, None if created or unresolved else "title")


def classify_lastfm_track_id(conn: sqlite3.Connection, lastfm_id: str, release_mbid: str | None):
    """Last.fm's per-track "mbid" is a MusicBrainz recording id OR a track id (one slot on one
    release) -- that release's tracklist tells them apart, or failing that a direct lookup of the
    id. -> (recording id, is_track), or (None, None) while neither has been looked up (see
    releases.resolve / releases.sort_id)."""
    if release_mbid:
        row = conn.execute("SELECT payload_json FROM mb_cache WHERE key = ?", (TRACKS_KEY.format(release_mbid),)).fetchone()
        tracks = json.loads(row[0]) or {} if row else {}
        if lastfm_id in tracks:
            return tracks[lastfm_id], True
        if lastfm_id in set(tracks.values()):
            return lastfm_id, False
    row = conn.execute("SELECT payload_json FROM mb_cache WHERE key = ?", (SORT_ID_KEY.format(lastfm_id),)).fetchone()
    direct = json.loads(row[0]) if row else None
    if direct and direct.get("recording"):
        return direct["recording"], direct["kind"] == "track"
    return None, None


def get_or_create_song(
    conn: sqlite3.Connection,
    cache: dict,
    artist_id: int,
    title: str,
    album_id: int | None = None,
    mbid: str | None = None,
    source: str | None = None,
    lastfm_id: str | None = None,
    release_mbid: str | None = None,
) -> int:
    """`mbid` is a verified RECORDING id (the song). `lastfm_id` is Last.fm's raw per-track id,
    which may be a recording or a *track* id: it's sorted using the tracklist of `release_mbid`
    (the edition it was played from). A recording id becomes the song's mbid; a track id is
    remembered in song_tracks instead (and routes the next scrobble of it straight here). An id
    that can't be sorted yet is never stored on the song -- the scrobble keeps it
    (scrobble_tracks) for Songs > Recording ids.

    `source` ('lastfm' | 'setlistfm') mirrors get_or_create_album's override check: a manual
    song merge (or rename) writes an alias_overrides row keyed on (source, "<artist id>:<raw
    title, lowercased>"), so the same raw title resolves straight to the surviving song instead
    of recreating the duplicate -- essential for setlist.fm, whose refresh re-links every setlist
    song from scratch."""
    is_track = False
    if lastfm_id:
        if ("track", lastfm_id) in cache:
            return cache[("track", lastfm_id)]
        row = conn.execute("SELECT song_id FROM song_tracks WHERE track_mbid = ?", (lastfm_id,)).fetchone()
        if row:
            cache[("track", lastfm_id)] = row[0]
            return row[0]
        recording, is_track = classify_lastfm_track_id(conn, lastfm_id, release_mbid)
        if recording:
            mbid = mbid or recording
        else:
            row = conn.execute("SELECT id FROM songs WHERE mbid = ?", (lastfm_id,)).fetchone()
            if row:  # a song still carrying this unsorted id as its mbid (pre-conversion data)
                return row[0]

    def learn(song_id: int) -> int:
        if is_track:
            conn.execute("INSERT OR IGNORE INTO song_tracks (track_mbid, song_id, release_mbid, source) VALUES (?, ?, ?, ?)",
                         (lastfm_id, song_id, release_mbid, source))
            cache[("track", lastfm_id)] = song_id
        return song_id

    if source:
        source_key = f"{artist_id}:{title.strip().lower()}"
        override_key = ("override", source, source_key)
        if override_key in cache:
            return learn(cache[override_key])
        row = conn.execute(
            "SELECT canonical_id FROM alias_overrides WHERE source = ? AND source_key = ? AND canonical_type = 'song'",
            (source, source_key),
        ).fetchone()
        if row and conn.execute("SELECT 1 FROM songs WHERE id = ?", (row[0],)).fetchone():
            song_id = row[0]
            cache[override_key] = song_id
            if mbid:
                _set_mbid_if_free(conn, "songs", song_id, mbid)
            if album_id:
                conn.execute("UPDATE songs SET album_id = ? WHERE id = ? AND album_id IS NULL", (album_id, song_id))
            return learn(song_id)

    if mbid:
        mbid_key = ("mbid", mbid)
        if mbid_key in cache:
            return learn(cache[mbid_key])
        row = conn.execute("SELECT id FROM songs WHERE mbid = ?", (mbid,)).fetchone()
        if row:
            cache[mbid_key] = cache[(artist_id, title.lower())] = row[0]
            return learn(row[0])

    key = (artist_id, title.lower())
    if key in cache:
        song_id = cache[key]
    else:
        row = conn.execute(
            "SELECT id FROM songs WHERE artist_id = ? AND lower(title) = ?",
            (artist_id, title.lower()),
        ).fetchone()
        if row:
            song_id = row[0]
        else:
            cur = conn.execute(
                "INSERT INTO songs (artist_id, album_id, title, mbid) VALUES (?, ?, ?, ?)",
                (artist_id, album_id, title, mbid or None),
            )
            song_id = cur.lastrowid
            record_event(conn, "new_song", "song", song_id, {"title": title, "artistId": artist_id, "albumId": album_id, "mbid": mbid, "source": source})
        cache[key] = song_id
    if mbid:
        _set_mbid_if_free(conn, "songs", song_id, mbid)
        cache[("mbid", mbid)] = song_id
    if album_id:
        conn.execute("UPDATE songs SET album_id = ? WHERE id = ? AND album_id IS NULL", (album_id, song_id))
    return learn(song_id)


def get_or_create_venue(
    conn: sqlite3.Connection,
    cache: dict,
    setlistfm_id: str | None,
    name: str,
    city: str | None = None,
    state: str | None = None,
    country: str | None = None,
) -> int | None:
    name = (name or "").strip()
    if not name:
        return None
    key = setlistfm_id or ("name", name.lower(), city)
    if key in cache:
        return cache[key]
    row = None
    if setlistfm_id:
        row = conn.execute("SELECT id FROM venues WHERE setlistfm_id = ?", (setlistfm_id,)).fetchone()
    if row:
        venue_id = row[0]
    else:
        cur = conn.execute(
            "INSERT INTO venues (setlistfm_id, name, city, state, country) VALUES (?, ?, ?, ?, ?)",
            (setlistfm_id, name, city, state, country),
        )
        venue_id = cur.lastrowid
    cache[key] = venue_id
    return venue_id


def artist_mbid_sets(conn: sqlite3.Connection, artist_ids=None) -> dict[int, set[str]]:
    """artist id -> every MusicBrainz artist id that counts as that artist (its own mbid plus any
    "also releases as" aliases)."""
    out: dict[int, set[str]] = {}
    where = f"WHERE id IN ({','.join('?' * len(artist_ids))})" if artist_ids else ""
    for aid, mbid in conn.execute(f"SELECT id, mbid FROM artists {where}", list(artist_ids or [])):
        out[aid] = {mbid} if mbid else set()
    try:
        where = f"WHERE artist_id IN ({','.join('?' * len(artist_ids))})" if artist_ids else ""
        for aid, mbid in conn.execute(f"SELECT artist_id, mbid FROM artist_mb_aliases {where}", list(artist_ids or [])):
            out.setdefault(aid, set()).add(mbid)
    except sqlite3.OperationalError:
        pass
    return out
