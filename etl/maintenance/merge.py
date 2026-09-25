"""
The one merge engine (artists, albums, songs) plus identity edits -- every destructive change
the maintenance tool makes goes through here, and every one of them is reversible:

  * A merge writes an undo journal into merge_log.undo_json: the absorbed row exactly as it
    was, the id of every row whose foreign key it moved, every album_artists credit it dropped,
    and every alias_overrides row it inserted/updated/rekeyed. undo_merge() replays that
    backwards in one transaction.
  * An identity edit (mbid/title/year) records old -> new values in edit_log; undo_edit()
    puts the old values back.

Replaces the old "copy the whole 126MB database before every merge" safety net (65GB of
copies by September 2026) with one full snapshot per server session -- ensure_snapshot(),
called before the first write -- which is still there for anything the journal can't cover.

Every function here takes an open connection and does NOT commit: callers own the
transaction (BEGIN IMMEDIATE ... commit/rollback), so a batch of merges can be all-or-nothing.
"""
import json
import sqlite3
import threading
import time
from datetime import datetime

from common import DB_PATH

SNAPSHOT_DIR = DB_PATH.parent / ".snapshots"
SNAPSHOT_KEEP = 5
SNAPSHOT_MAX_AGE_SECONDS = 6 * 3600
_snapshot_lock = threading.Lock()
_last_snapshot_at = 0.0


class MergeError(Exception):
    """A merge/undo that can't go ahead -- message is shown to the user verbatim."""

    def __init__(self, message: str, code: str = "merge_error"):
        super().__init__(message)
        self.code = code


# -- Session snapshot ------------------------------------------------------------------------

def ensure_snapshot() -> str | None:
    """Full copy of the database via SQLite's online backup API (consistent even mid-write),
    at most once per SNAPSHOT_MAX_AGE_SECONDS per server process. Only auto-* files are ever
    pruned -- a hand-made snapshot (e.g. pre-redesign-*.sqlite) is never touched."""
    global _last_snapshot_at
    with _snapshot_lock:
        if time.monotonic() - _last_snapshot_at < SNAPSHOT_MAX_AGE_SECONDS and _last_snapshot_at:
            return None
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        dest_path = SNAPSHOT_DIR / f"auto-{datetime.now():%Y%m%d-%H%M%S}.sqlite"
        src = sqlite3.connect(DB_PATH, timeout=15)
        dest = sqlite3.connect(dest_path)
        try:
            src.backup(dest)
        finally:
            dest.close()
            src.close()
        _last_snapshot_at = time.monotonic()
        autos = sorted(SNAPSHOT_DIR.glob("auto-*.sqlite"))
        for old in autos[:-SNAPSHOT_KEEP]:
            old.unlink()
        return str(dest_path)


# -- Helpers ---------------------------------------------------------------------------------

def _row_dict(conn, table: str, row_id: int) -> dict | None:
    cur = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,))
    row = cur.fetchone()
    return dict(zip([d[0] for d in cur.description], row)) if row else None


def _move(conn, journal: dict, rows_moved: dict, table: str, column: str, absorbed_id: int, canonical_id: int, extra_where: str = "") -> None:
    ids = [r[0] for r in conn.execute(f"SELECT id FROM {table} WHERE {column} = ? {extra_where}", (absorbed_id,))]
    if ids:
        conn.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ? {extra_where}", (canonical_id, absorbed_id))
    journal["moved"][f"{table}.{column}"] = ids
    rows_moved[table] = rows_moved.get(table, 0) + len(ids)


def _move_album_genres(conn, journal: dict, rows_moved: dict, absorbed_id: int, canonical_id: int) -> None:
    """Genres (and removed-genre decisions) follow the album. Keyed (album, genre), so ones the
    survivor already has are dropped instead -- journaled either way for undo."""
    for table, cols in (("album_genres", "genre_id, source, votes, created_at"), ("genre_hidden", "genre_id, created_at")):
        dup = f"genre_id IN (SELECT genre_id FROM {table} WHERE album_id = ?)"
        dropped = [list(r) for r in conn.execute(f"SELECT {cols} FROM {table} WHERE album_id = ? AND {dup}", (absorbed_id, canonical_id))]
        conn.execute(f"DELETE FROM {table} WHERE album_id = ? AND {dup}", (absorbed_id, canonical_id))
        moved = [r[0] for r in conn.execute(f"SELECT genre_id FROM {table} WHERE album_id = ?", (absorbed_id,))]
        conn.execute(f"UPDATE {table} SET album_id = ? WHERE album_id = ?", (canonical_id, absorbed_id))
        journal[f"{table}_moved"], journal[f"{table}_dropped"] = moved, dropped
        rows_moved[table] = len(moved)


def _move_album_parts(conn, journal: dict, absorbed_id: int, canonical_id: int) -> None:
    """Set links follow the album on either side (the set, or a part of one). Journaled: the
    original rows and the ones written in their place, for undo."""
    orig = [list(r) for r in conn.execute("SELECT album_id, part_album_id, position FROM album_parts WHERE album_id = ? OR part_album_id = ?",
                                          (absorbed_id, absorbed_id))]
    if not orig:
        return
    conn.execute("DELETE FROM album_parts WHERE album_id = ? OR part_album_id = ?", (absorbed_id, absorbed_id))
    inserted = []
    for a, p, pos in orig:
        a2, p2 = (canonical_id if a == absorbed_id else a), (canonical_id if p == absorbed_id else p)
        if a2 != p2 and conn.execute("INSERT OR IGNORE INTO album_parts (album_id, part_album_id, position) VALUES (?, ?, ?)", (a2, p2, pos)).rowcount:
            inserted.append([a2, p2])
    journal["album_parts"] = {"orig": orig, "inserted": inserted}


def set_album_parts(conn, album_id: int, part_ids: list[int], reason: str = "set of albums") -> dict:
    """The albums a set contains (see migration 011), replacing any before. [] = not a set.
    Journaled in edit_log as {"_parts": {"before", "after"}}; undo_edit puts "before" back."""
    part_ids = list(dict.fromkeys(int(p) for p in part_ids))
    album = _row_dict(conn, "albums", album_id)
    if not album:
        raise MergeError("album not found", "not_found")
    if album_id in part_ids:
        raise MergeError("A set can't contain itself.")
    for pid in part_ids:
        if not _row_dict(conn, "albums", pid):
            raise MergeError(f"album #{pid} not found", "not_found")
        if conn.execute("SELECT 1 FROM album_parts WHERE album_id = ?", (pid,)).fetchone():
            raise MergeError("That album is itself a set -- sets can't be nested.")
    if part_ids and conn.execute("SELECT 1 FROM album_parts WHERE part_album_id = ?", (album_id,)).fetchone():
        raise MergeError("This album is part of a set itself -- it can't be a set too.")
    before = [r[0] for r in conn.execute("SELECT part_album_id FROM album_parts WHERE album_id = ? ORDER BY position", (album_id,))]
    if before == part_ids:
        return {"editId": None, "parts": part_ids}
    conn.execute("DELETE FROM album_parts WHERE album_id = ?", (album_id,))
    conn.executemany("INSERT INTO album_parts (album_id, part_album_id, position) VALUES (?, ?, ?)",
                     [(album_id, pid, i) for i, pid in enumerate(part_ids)])
    edit_id = conn.execute("INSERT INTO edit_log (entity_type, entity_id, entity_name, changes_json, reason) VALUES ('album', ?, ?, ?, ?)",
                           (album_id, album["title"], json.dumps({"_parts": {"before": before, "after": part_ids}}), reason)).lastrowid
    return {"editId": edit_id, "parts": part_ids}


def _undo_parts(conn, album_id: int, d: dict) -> None:
    now = [r[0] for r in conn.execute("SELECT part_album_id FROM album_parts WHERE album_id = ? ORDER BY position", (album_id,))]
    if now != d["after"]:
        raise MergeError("The set's albums have changed again since -- undo the later change first.", "diverged")
    conn.execute("DELETE FROM album_parts WHERE album_id = ?", (album_id,))
    conn.executemany("INSERT INTO album_parts (album_id, part_album_id, position) VALUES (?, ?, ?)",
                     [(album_id, pid, i) for i, pid in enumerate(d["before"])])


_TRACKLIST_COLS = "position, number, disc, title, recording_mbid, length_ms"
_TL_SOURCE_COLS = "source, release_mbid, release_title, release_date, country, format, fetched_at"


def _move_album_tracklist(conn, journal: dict, absorbed_id: int, canonical_id: int) -> None:
    """The album's MusicBrainz tracklist: the survivor keeps its own; if it has none it takes the
    absorbed album's; otherwise the absorbed one's is dropped. Journaled for undo."""
    try:
        has_own = conn.execute("SELECT 1 FROM album_tracklist_sources WHERE album_id = ?", (canonical_id,)).fetchone()
    except Exception:  # pre-migration database
        return
    if not conn.execute("SELECT 1 FROM album_tracklist_sources WHERE album_id = ?", (absorbed_id,)).fetchone():
        return
    if not has_own:
        conn.execute("UPDATE album_tracklists SET album_id = ? WHERE album_id = ?", (canonical_id, absorbed_id))
        conn.execute("UPDATE album_tracklist_sources SET album_id = ? WHERE album_id = ?", (canonical_id, absorbed_id))
        journal["tracklist_moved"] = True
        return
    journal["tracklist_dropped"] = {
        "tracks": [list(r) for r in conn.execute(f"SELECT {_TRACKLIST_COLS} FROM album_tracklists WHERE album_id = ?", (absorbed_id,))],
        "source": list(conn.execute(f"SELECT {_TL_SOURCE_COLS} FROM album_tracklist_sources WHERE album_id = ?", (absorbed_id,)).fetchone())}
    conn.execute("DELETE FROM album_tracklists WHERE album_id = ?", (absorbed_id,))
    conn.execute("DELETE FROM album_tracklist_sources WHERE album_id = ?", (absorbed_id,))


def _upsert_alias(conn, journal: dict, source: str, source_key: str, canonical_type: str, canonical_id: int, note: str) -> None:
    existing = conn.execute(
        "SELECT id, canonical_id, note FROM alias_overrides WHERE source = ? AND source_key = ? AND canonical_type = ?",
        (source, source_key, canonical_type),
    ).fetchone()
    if existing:
        if existing[1] != canonical_id:
            conn.execute("UPDATE alias_overrides SET canonical_id = ?, note = ? WHERE id = ?", (canonical_id, note, existing[0]))
            journal["alias_updated"].append([existing[0], existing[1], existing[2]])
        return
    cur = conn.execute(
        "INSERT INTO alias_overrides (source, source_key, canonical_type, canonical_id, note) VALUES (?, ?, ?, ?, ?)",
        (source, source_key, canonical_type, canonical_id, note),
    )
    journal["alias_inserted"].append(cur.lastrowid)


def _repoint_aliases(conn, journal: dict, rows_moved: dict, canonical_type: str, absorbed_id: int, canonical_id: int) -> None:
    """absorbed may itself have been an earlier merge's target -- repoint those overrides."""
    rows = conn.execute(
        "SELECT id FROM alias_overrides WHERE canonical_type = ? AND canonical_id = ?", (canonical_type, absorbed_id)
    ).fetchall()
    for (alias_id,) in rows:
        conn.execute("UPDATE alias_overrides SET canonical_id = ? WHERE id = ?", (canonical_id, alias_id))
        journal["alias_updated"].append([alias_id, absorbed_id, None])
    rows_moved["alias_overrides_repointed"] = len(rows)


# Which import sources look albums/songs up by (artist id, lowercased title) -- and which raw
# columns hold the text each source used.
_RAW_TITLE_SOURCES = {
    "albums": [("lastfm", "SELECT DISTINCT raw_album_text FROM scrobbles WHERE album_id = ?"),
               ("discogs", "SELECT DISTINCT raw_title_text FROM vinyl_holdings WHERE album_id = ?")],
    "songs": [("lastfm", "SELECT DISTINCT raw_track_text FROM scrobbles WHERE song_id = ?"),
              ("setlistfm", "SELECT DISTINCT raw_song_text FROM setlist_songs WHERE song_id = ?")],
}


def alias_old_titles(conn, table: str, row: dict, new_title: str) -> list[int]:
    """Importers find an album/song by exact (artist, title) -- so renaming one would make the
    next import of its OLD spelling create a fresh duplicate. Before a rename, route every
    spelling the row is currently known by (its old title + each source's raw text) to it via
    alias_overrides. Existing overrides are never overwritten. Returns the inserted alias ids.
    (Harmless to leave behind if the rename is undone: they point at this same row.)"""
    if table not in _RAW_TITLE_SOURCES:
        return []
    ctype = "album" if table == "albums" else "song"
    new_key = (new_title or "").strip().lower()
    inserted = []
    for source, query in _RAW_TITLE_SOURCES[table]:
        raws = {row["title"]} | {r[0] for r in conn.execute(query, (row["id"],)) if r[0]}
        for raw in raws:
            raw_key = raw.strip().lower()
            if not raw_key or raw_key == new_key:
                continue
            key = f"{row['artist_id']}:{raw_key}"
            if conn.execute("SELECT 1 FROM alias_overrides WHERE source = ? AND source_key = ? AND canonical_type = ?",
                            (source, key, ctype)).fetchone():
                continue
            cur = conn.execute(
                "INSERT INTO alias_overrides (source, source_key, canonical_type, canonical_id, note) VALUES (?, ?, ?, ?, ?)",
                (source, key, ctype, row["id"], f"renamed to {new_title!r} on {datetime.now():%Y-%m-%d}"))
            inserted.append(cur.lastrowid)
    return inserted


def _set_canonical_fields(conn, journal: dict, table: str, canonical_id: int, fields: dict) -> None:
    """Change fields on the surviving row, remembering what they were (for undo)."""
    if not fields:
        return
    before = _row_dict(conn, table, canonical_id)
    if "title" in fields and fields["title"] != before["title"]:
        journal["alias_inserted"] += alias_old_titles(conn, table, before, fields["title"])
    journal["canonical_before"].update({k: before[k] for k in fields if k not in journal["canonical_before"]})
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*fields.values(), canonical_id))


def _new_journal(absorbed_row: dict) -> dict:
    return {
        "absorbed_row": absorbed_row, "moved": {}, "notes": [],
        "album_artists_deleted": [], "album_artists_moved": [],
        "alias_inserted": [], "alias_updated": [], "alias_rekeyed": [], "alias_deleted": [],
        "canonical_before": {},
    }


def _log_merge(conn, entity_type: str, absorbed: dict, name_field: str, canonical: dict, rows_moved: dict, journal: dict) -> int:
    cur = conn.execute(
        """
        INSERT INTO merge_log (entity_type, absorbed_id, absorbed_name, absorbed_mbid,
                               canonical_id, canonical_name, rows_moved_json, undo_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (entity_type, absorbed["id"], absorbed[name_field], absorbed.get("mbid"), canonical["id"], canonical[name_field],
         json.dumps(rows_moved), json.dumps(journal)),
    )
    return cur.lastrowid


def _load_pair(conn, table: str, absorbed_id: int, canonical_id: int) -> tuple[dict, dict]:
    if not absorbed_id or not canonical_id:
        raise MergeError("absorbedId and canonicalId are required")
    if absorbed_id == canonical_id:
        raise MergeError("absorbedId and canonicalId must differ")
    absorbed = _row_dict(conn, table, absorbed_id)
    canonical = _row_dict(conn, table, canonical_id)
    if not absorbed or not canonical:
        raise MergeError(f"both ids must be existing {table}", "not_found")
    return absorbed, canonical


def _note_rows(conn, journal, rows_moved, entity_type, absorbed_id, canonical_id):
    ids = [r[0] for r in conn.execute("SELECT id FROM notes WHERE entity_type = ? AND entity_id = ?", (entity_type, absorbed_id))]
    if ids:
        conn.execute("UPDATE notes SET entity_id = ? WHERE entity_type = ? AND entity_id = ?", (canonical_id, entity_type, absorbed_id))
    journal["notes"] = ids
    rows_moved["notes"] = len(ids)


# -- Merges ----------------------------------------------------------------------------------

def merge_artists(conn, absorbed_id: int, canonical_id: int) -> dict:
    absorbed, canonical = _load_pair(conn, "artists", absorbed_id, canonical_id)
    if absorbed["mbid"] and canonical["mbid"] and absorbed["mbid"] != canonical["mbid"]:
        raise MergeError(
            "Both artists have different MusicBrainz ids -- artist mbids are trusted, so these are "
            "treated as different artists. Correct one of the mbids first if one is wrong.",
            "distinct_mbids",
        )
    journal, rows_moved = _new_journal(absorbed), {}

    # album_artists: PK (album_id, artist_id) -- drop absorbed's credit where canonical is
    # already credited on the same album, then reassign what's left.
    dropped = conn.execute(
        "SELECT album_id, artist_id, position FROM album_artists WHERE artist_id = ? "
        "AND album_id IN (SELECT album_id FROM album_artists WHERE artist_id = ?)",
        (absorbed_id, canonical_id),
    ).fetchall()
    conn.execute(
        "DELETE FROM album_artists WHERE artist_id = ? AND album_id IN (SELECT album_id FROM album_artists WHERE artist_id = ?)",
        (absorbed_id, canonical_id),
    )
    journal["album_artists_deleted"] = [list(r) for r in dropped]
    moved_aa = [r[0] for r in conn.execute("SELECT album_id FROM album_artists WHERE artist_id = ?", (absorbed_id,))]
    conn.execute("UPDATE album_artists SET artist_id = ? WHERE artist_id = ?", (canonical_id, absorbed_id))
    journal["album_artists_moved"] = moved_aa
    rows_moved["album_artists_collisions_dropped"] = len(dropped)
    rows_moved["album_artists_reassigned"] = len(moved_aa)

    for table in ("albums", "songs", "scrobbles", "setlists", "artist_mb_aliases"):
        _move(conn, journal, rows_moved, table, "artist_id", absorbed_id, canonical_id)
    _note_rows(conn, journal, rows_moved, "artist", absorbed_id, canonical_id)
    _repoint_aliases(conn, journal, rows_moved, "artist", absorbed_id, canonical_id)

    # Album/song overrides are keyed "<artist id>:<title>" -- rekey absorbed's to canonical so
    # they keep matching after this merge (they silently went stale before).
    prefix = f"{absorbed_id}:"
    for alias_id, source, key, ctype, cid, note in conn.execute(
        "SELECT id, source, source_key, canonical_type, canonical_id, note FROM alias_overrides "
        "WHERE canonical_type IN ('album','song') AND substr(source_key, 1, ?) = ?",
        (len(prefix), prefix),
    ).fetchall():
        new_key = f"{canonical_id}:{key[len(prefix):]}"
        clash = conn.execute(
            "SELECT 1 FROM alias_overrides WHERE source = ? AND source_key = ? AND canonical_type = ?", (source, new_key, ctype)
        ).fetchone()
        if clash:  # canonical already has an override for that exact title -- it wins
            conn.execute("DELETE FROM alias_overrides WHERE id = ?", (alias_id,))
            journal["alias_deleted"].append([alias_id, source, key, ctype, cid, note])
        else:
            conn.execute("UPDATE alias_overrides SET source_key = ? WHERE id = ?", (new_key, alias_id))
            journal["alias_rekeyed"].append([alias_id, key])

    note = f"merged from duplicate artist id {absorbed_id} on {datetime.now():%Y-%m-%d}"
    for source in ("lastfm", "setlistfm", "discogs"):
        _upsert_alias(conn, journal, source, absorbed["name"].strip().lower(), "artist", canonical_id, note)

    conn.execute("DELETE FROM artists WHERE id = ?", (absorbed_id,))
    if absorbed["mbid"] and not canonical["mbid"]:
        _set_canonical_fields(conn, journal, "artists", canonical_id, {"mbid": absorbed["mbid"]})
    log_id = _log_merge(conn, "artist", absorbed, "name", canonical, rows_moved, journal)
    return {"logId": log_id, "absorbedId": absorbed_id, "absorbedName": absorbed["name"],
            "canonicalId": canonical_id, "canonicalName": canonical["name"], "rowsMoved": rows_moved}


def merge_albums(conn, absorbed_id: int, canonical_id: int, identity: dict | None = None) -> dict:
    """`identity` ({mbid, title, year}, all optional) is applied to the survivor after the
    absorbed row is gone -- so an mbid the absorbed album held can move to whichever album was
    picked as primary, in the same transaction. Without it, the survivor adopts the absorbed
    album's mbid only if it has none of its own."""
    absorbed, canonical = _load_pair(conn, "albums", absorbed_id, canonical_id)
    if absorbed["artist_id"] != canonical["artist_id"]:
        raise MergeError("These albums belong to different artists -- only albums by the same artist can be merged.", "different_artist")
    journal, rows_moved = _new_journal(absorbed), {}

    dropped = conn.execute(
        "SELECT album_id, artist_id, position FROM album_artists WHERE album_id = ? "
        "AND artist_id IN (SELECT artist_id FROM album_artists WHERE album_id = ?)",
        (absorbed_id, canonical_id),
    ).fetchall()
    conn.execute(
        "DELETE FROM album_artists WHERE album_id = ? AND artist_id IN (SELECT artist_id FROM album_artists WHERE album_id = ?)",
        (absorbed_id, canonical_id),
    )
    journal["album_artists_deleted"] = [list(r) for r in dropped]
    moved_aa = [r[0] for r in conn.execute("SELECT artist_id FROM album_artists WHERE album_id = ?", (absorbed_id,))]
    conn.execute("UPDATE album_artists SET album_id = ? WHERE album_id = ?", (canonical_id, absorbed_id))
    journal["album_artists_moved"] = moved_aa
    rows_moved["album_artists_collisions_dropped"] = len(dropped)
    rows_moved["album_artists_reassigned"] = len(moved_aa)

    for table in ("songs", "vinyl_holdings", "scrobbles", "album_releases"):  # editions follow the album
        _move(conn, journal, rows_moved, table, "album_id", absorbed_id, canonical_id)
    _move_album_genres(conn, journal, rows_moved, absorbed_id, canonical_id)
    _move_album_tracklist(conn, journal, absorbed_id, canonical_id)
    _move_album_parts(conn, journal, absorbed_id, canonical_id)
    _note_rows(conn, journal, rows_moved, "album", absorbed_id, canonical_id)
    _repoint_aliases(conn, journal, rows_moved, "album", absorbed_id, canonical_id)

    # Close the loop for future imports: an override per raw title the *moved* rows were known
    # by in each source, keyed on the artist id (see get_or_create_album's docstring).
    note = f"merged from duplicate album id {absorbed_id} on {datetime.now():%Y-%m-%d}"
    moved_vinyl = journal["moved"].get("vinyl_holdings.album_id", [])
    moved_scrobbles = journal["moved"].get("scrobbles.album_id", [])
    titles = {"discogs": {absorbed["title"]}, "lastfm": {absorbed["title"]}}
    if moved_vinyl:
        titles["discogs"] |= {r[0] for r in conn.execute(
            f"SELECT DISTINCT raw_title_text FROM vinyl_holdings WHERE id IN ({','.join('?' * len(moved_vinyl))})", moved_vinyl)}
    if moved_scrobbles:
        titles["lastfm"] |= {r[0] for r in conn.execute(
            "SELECT DISTINCT raw_album_text FROM scrobbles WHERE album_id = ? AND raw_album_text IS NOT NULL "
            "AND id IN (SELECT value FROM json_each(?))", (canonical_id, json.dumps(moved_scrobbles)))}
    for source, raw_titles in titles.items():
        for raw in raw_titles:
            if raw and raw.strip():
                _upsert_alias(conn, journal, source, f"{absorbed['artist_id']}:{raw.strip().lower()}", "album", canonical_id, note)

    conn.execute("DELETE FROM albums WHERE id = ?", (absorbed_id,))

    fields = {}
    identity = identity or {}
    if identity.get("mbid"):
        fields["mbid"] = identity["mbid"]
    elif absorbed["mbid"] and not canonical["mbid"]:
        fields["mbid"] = absorbed["mbid"]
    if identity.get("title"):
        fields["title"] = identity["title"]
    if identity.get("year"):
        fields["year"] = int(identity["year"])
    if "mbid" in fields and fields["mbid"] != canonical["mbid"]:
        clash = conn.execute("SELECT id, title FROM albums WHERE mbid = ? AND id != ?", (fields["mbid"], canonical_id)).fetchone()
        if clash:
            raise MergeError(f'That mbid is still linked to "{clash[1]}" -- merge that album too, or pick another id.', "mbid_conflict")
        # Old art may be for a different pressing -- let the next sweep refetch under the new id.
        fields.update({"cover_status": None, "cover_updated_at": None})
    fields = {k: v for k, v in fields.items() if canonical.get(k) != v}
    _set_canonical_fields(conn, journal, "albums", canonical_id, fields)

    log_id = _log_merge(conn, "album", absorbed, "title", canonical, rows_moved, journal)
    return {"logId": log_id, "absorbedId": absorbed_id, "absorbedTitle": absorbed["title"],
            "canonicalId": canonical_id, "canonicalTitle": fields.get("title", canonical["title"]), "rowsMoved": rows_moved}


def merge_songs(conn, absorbed_id: int, canonical_id: int, identity: dict | None = None) -> dict:
    """`identity` ({title, mbid}, optional) renames the survivor -- e.g. the studio "Creeping Death
    (Remastered)" absorbing the setlist's "Creeping Death" can end up called just "Creeping
    Death". Old spellings keep routing to it (alias_old_titles, journaled for undo)."""
    absorbed, canonical = _load_pair(conn, "songs", absorbed_id, canonical_id)
    if absorbed["artist_id"] != canonical["artist_id"]:
        raise MergeError("These songs belong to different artists -- only songs by the same artist can be merged.", "different_artist")
    journal, rows_moved = _new_journal(absorbed), {}

    _move(conn, journal, rows_moved, "scrobbles", "song_id", absorbed_id, canonical_id)
    _move(conn, journal, rows_moved, "setlist_songs", "song_id", absorbed_id, canonical_id)
    _move(conn, journal, rows_moved, "song_tracks", "song_id", absorbed_id, canonical_id)  # track ids follow the song
    _note_rows(conn, journal, rows_moved, "song", absorbed_id, canonical_id)
    _repoint_aliases(conn, journal, rows_moved, "song", absorbed_id, canonical_id)

    note = f"merged from duplicate song id {absorbed_id} on {datetime.now():%Y-%m-%d}"
    moved_sc = journal["moved"].get("scrobbles.song_id", [])
    moved_sl = journal["moved"].get("setlist_songs.song_id", [])
    titles = {"lastfm": {absorbed["title"]}, "setlistfm": {absorbed["title"]}}
    if moved_sc:
        titles["lastfm"] |= {r[0] for r in conn.execute(
            "SELECT DISTINCT raw_track_text FROM scrobbles WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(moved_sc),))}
    if moved_sl:
        titles["setlistfm"] |= {r[0] for r in conn.execute(
            "SELECT DISTINCT raw_song_text FROM setlist_songs WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(moved_sl),))}
    for source, raw_titles in titles.items():
        for raw in raw_titles:
            if raw and raw.strip():
                _upsert_alias(conn, journal, source, f"{absorbed['artist_id']}:{raw.strip().lower()}", "song", canonical_id, note)

    conn.execute("DELETE FROM songs WHERE id = ?", (absorbed_id,))
    fields = {}
    if identity and identity.get("mbid"):
        fields["mbid"] = identity["mbid"]
    elif absorbed["mbid"] and not canonical["mbid"]:
        fields["mbid"] = absorbed["mbid"]
    if fields.get("mbid") and fields["mbid"] != canonical["mbid"]:
        clash = conn.execute("SELECT id, title FROM songs WHERE mbid = ? AND id != ?", (fields["mbid"], canonical_id)).fetchone()
        if clash:
            raise MergeError(f'That recording id is still linked to "{clash[1]}" -- merge that song too.', "mbid_conflict")
    if absorbed["album_id"] and not canonical["album_id"]:
        fields["album_id"] = absorbed["album_id"]
    if identity and identity.get("title") and identity["title"] != canonical["title"]:
        fields["title"] = identity["title"]
    _set_canonical_fields(conn, journal, "songs", canonical_id, fields)

    log_id = _log_merge(conn, "song", absorbed, "title", canonical, rows_moved, journal)
    return {"logId": log_id, "absorbedId": absorbed_id, "absorbedTitle": absorbed["title"],
            "canonicalId": canonical_id, "canonicalTitle": fields.get("title", canonical["title"]), "rowsMoved": rows_moved}


# -- Undo ------------------------------------------------------------------------------------

_TABLE = {"artist": "artists", "album": "albums", "song": "songs", "vinyl": "vinyl_holdings"}


def undo_merge(conn, log_id: int) -> dict:
    row = conn.execute(
        "SELECT entity_type, absorbed_id, canonical_id, undo_json, undone_at, absorbed_name, canonical_name FROM merge_log WHERE id = ?",
        (log_id,),
    ).fetchone()
    if not row:
        raise MergeError("merge not found", "not_found")
    entity_type, absorbed_id, canonical_id, undo_json, undone_at, absorbed_name, canonical_name = row
    if undone_at:
        raise MergeError("This merge was already undone.", "already_undone")
    if not undo_json:
        raise MergeError("This merge predates the undo journal -- it can only be reversed by restoring a snapshot.", "no_journal")
    j = json.loads(undo_json)
    table = _TABLE[entity_type]

    if not conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (canonical_id,)).fetchone():
        raise MergeError(f'"{canonical_name}" has itself been merged away since -- undo that later merge first.', "canonical_gone")
    if conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (absorbed_id,)).fetchone():
        raise MergeError("The absorbed row's id is in use again -- can't restore it.", "id_in_use")

    # Every moved row that still exists must still point at canonical -- if a later action moved
    # it elsewhere, reversing this one would silently clobber that.
    skipped = 0
    for key, ids in j["moved"].items():
        tbl, col = key.split(".")
        if not ids:
            continue
        current = dict(conn.execute(f"SELECT id, {col} FROM {tbl} WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(ids),)).fetchall())
        wrong = [i for i, v in current.items() if v != canonical_id]
        if wrong:
            raise MergeError(f"{len(wrong)} {tbl} row(s) have been moved again since this merge -- undo the later change first.", "diverged")
        skipped += len(ids) - len(current)  # re-imported since (e.g. a setlist refresh) -- nothing to move back

    # Canonical first: a field it adopted (e.g. the absorbed row's mbid) must be released
    # before the absorbed row can have it back under the UNIQUE constraint.
    before = j["canonical_before"]
    if before:
        conn.execute(f"UPDATE {table} SET {', '.join(f'{k} = ?' for k in before)} WHERE id = ?", (*before.values(), canonical_id))
    cols = list(j["absorbed_row"])
    conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", [j["absorbed_row"][c] for c in cols])

    for key, ids in j["moved"].items():
        tbl, col = key.split(".")
        if ids:
            conn.execute(f"UPDATE {tbl} SET {col} = ? WHERE {col} = ? AND id IN (SELECT value FROM json_each(?))",
                         (absorbed_id, canonical_id, json.dumps(ids)))
    if j["notes"]:
        conn.execute("UPDATE notes SET entity_id = ? WHERE id IN (SELECT value FROM json_each(?))", (absorbed_id, json.dumps(j["notes"])))

    if entity_type == "artist":
        for album_id in j["album_artists_moved"]:
            conn.execute("UPDATE album_artists SET artist_id = ? WHERE album_id = ? AND artist_id = ?", (absorbed_id, album_id, canonical_id))
    elif entity_type == "album":
        for artist_id in j["album_artists_moved"]:
            conn.execute("UPDATE album_artists SET album_id = ? WHERE artist_id = ? AND album_id = ?", (absorbed_id, artist_id, canonical_id))
    for album_id, artist_id, position in j["album_artists_deleted"]:
        conn.execute("INSERT OR IGNORE INTO album_artists (album_id, artist_id, position) VALUES (?, ?, ?)", (album_id, artist_id, position))
    if entity_type == "album" and j.get("tracklist_moved"):
        conn.execute("UPDATE album_tracklists SET album_id = ? WHERE album_id = ?", (absorbed_id, canonical_id))
        conn.execute("UPDATE album_tracklist_sources SET album_id = ? WHERE album_id = ?", (absorbed_id, canonical_id))
    if entity_type == "album" and j.get("tracklist_dropped"):
        d = j["tracklist_dropped"]
        conn.executemany(f"INSERT OR IGNORE INTO album_tracklists (album_id, {_TRACKLIST_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         [(absorbed_id, *t) for t in d["tracks"]])
        conn.execute(f"INSERT OR IGNORE INTO album_tracklist_sources (album_id, {_TL_SOURCE_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (absorbed_id, *d["source"]))
    if entity_type == "album" and j.get("album_parts"):  # set links went with the album
        for a, p in j["album_parts"]["inserted"]:
            conn.execute("DELETE FROM album_parts WHERE album_id = ? AND part_album_id = ?", (a, p))
        conn.executemany("INSERT OR IGNORE INTO album_parts (album_id, part_album_id, position) VALUES (?, ?, ?)",
                         [tuple(r) for r in j["album_parts"]["orig"]])
    if entity_type == "album":  # genres went with the album (merges from before genres have no entries)
        for table, cols in (("album_genres", "genre_id, source, votes, created_at"), ("genre_hidden", "genre_id, created_at")):
            moved = j.get(f"{table}_moved") or []
            if moved:
                conn.execute(f"UPDATE {table} SET album_id = ? WHERE album_id = ? AND genre_id IN (SELECT value FROM json_each(?))",
                             (absorbed_id, canonical_id, json.dumps(moved)))
            for row in j.get(f"{table}_dropped") or []:
                conn.execute(f"INSERT OR IGNORE INTO {table} (album_id, {cols}) VALUES (?, {', '.join('?' * len(row))})", (absorbed_id, *row))

    if j["alias_inserted"]:
        conn.execute("DELETE FROM alias_overrides WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(j["alias_inserted"]),))
    for alias_id, old_canonical, old_note in j["alias_updated"]:
        if old_note is None:
            conn.execute("UPDATE alias_overrides SET canonical_id = ? WHERE id = ?", (old_canonical, alias_id))
        else:
            conn.execute("UPDATE alias_overrides SET canonical_id = ?, note = ? WHERE id = ?", (old_canonical, old_note, alias_id))
    for alias_id, old_key in j["alias_rekeyed"]:
        conn.execute("UPDATE alias_overrides SET source_key = ? WHERE id = ?", (old_key, alias_id))
    for alias_id, source, key, ctype, cid, note in j["alias_deleted"]:
        conn.execute("INSERT OR IGNORE INTO alias_overrides (id, source, source_key, canonical_type, canonical_id, note) VALUES (?, ?, ?, ?, ?, ?)",
                     (alias_id, source, key, ctype, cid, note))

    conn.execute("UPDATE merge_log SET undone_at = datetime('now') WHERE id = ?", (log_id,))
    return {"logId": log_id, "entityType": entity_type, "restoredId": absorbed_id, "restoredName": absorbed_name,
            "canonicalName": canonical_name, "skippedReimported": skipped}


# -- Identity edits --------------------------------------------------------------------------

EDITABLE = {"artists": {"mbid", "name"}, "albums": {"mbid", "title", "year", "cover_status", "cover_updated_at"},
            "songs": {"mbid", "title", "album_id"}, "vinyl_holdings": {"album_id", "mb_release_id", "disc_colour", "cover_file", "display_title", "release_year"}}
_NAME_FIELD = {"artists": "name", "albums": "title", "songs": "title", "vinyl_holdings": "raw_title_text"}
_UNIQUE_MBID = {"artists", "albums", "songs"}  # vinyl_holdings.mb_release_id is deliberately not unique


def edit_entity(conn, entity_type: str, entity_id: int, changes: dict, reason: str) -> dict:
    """Apply field changes to one artist/album/song, recording old values in edit_log.
    Returns {"editId", "changes"} (only fields that actually changed). Raises MergeError with
    code "mbid_conflict" (and .conflict set) if the new mbid is already taken."""
    table = _TABLE[entity_type]
    row = _row_dict(conn, table, entity_id)
    if not row:
        raise MergeError(f"{entity_type} not found", "not_found")
    unknown = set(changes) - EDITABLE[table]
    if unknown:
        raise MergeError(f"can't edit {sorted(unknown)} here")
    diff = {k: [row[k], v] for k, v in changes.items() if row[k] != v}
    if not diff:
        return {"editId": None, "changes": {}}
    if "mbid" in diff and diff["mbid"][1] and table in _UNIQUE_MBID:
        clash = conn.execute(f"SELECT id, {_NAME_FIELD[table]} FROM {table} WHERE mbid = ? AND id != ?", (diff["mbid"][1], entity_id)).fetchone()
        if clash:
            err = MergeError(f'That MusicBrainz id is already linked to "{clash[1]}" in your library.', "mbid_conflict")
            err.conflict = {"id": clash[0], "name": clash[1]}
            raise err
    if "title" in diff:
        alias_old_titles(conn, table, row, diff["title"][1])
    conn.execute(f"UPDATE {table} SET {', '.join(f'{k} = ?' for k in diff)} WHERE id = ?", (*[v[1] for v in diff.values()], entity_id))
    cur = conn.execute(
        "INSERT INTO edit_log (entity_type, entity_id, entity_name, changes_json, reason) VALUES (?, ?, ?, ?, ?)",
        (entity_type, entity_id, row[_NAME_FIELD[table]], json.dumps(diff), reason),
    )
    return {"editId": cur.lastrowid, "changes": diff}


def relink_live(conn, from_song: int, to_song: int, performer_id: int, reason: str, created_song: bool = False) -> int:
    """Point ONE band's live plays of a song at a different song -- the case merges can't cover:
    setlist.fm files a cover under its original artist (Anthrax playing "Antisocial" lands on
    Trust's song), but the band's own studio recording is the version that matters. Only this
    performer's setlist entries move; the original artist's song, scrobbles and own shows stay
    exactly as they are. A performer-scoped alias ("cover:<performer>:<title>") makes
    setlist.fm's next re-pull link them the same way. Journaled in edit_log for undo."""
    src = _row_dict(conn, "songs", from_song)
    dst = _row_dict(conn, "songs", to_song)
    if not src or not dst:
        raise MergeError("both songs must exist", "not_found")
    rows = conn.execute(
        """SELECT ss.id, ss.raw_song_text FROM setlist_songs ss JOIN setlists st ON st.id = ss.setlist_id
           WHERE ss.song_id = ? AND st.artist_id = ?""", (from_song, performer_id)).fetchall()
    if not rows:
        raise MergeError("that band has no live plays of this song to re-link", "not_found")
    ids = [r[0] for r in rows]
    conn.execute(f"UPDATE setlist_songs SET song_id = ? WHERE id IN ({','.join('?' * len(ids))})", (to_song, *ids))
    alias_inserted, alias_updated = [], []
    for raw in {r[1] for r in rows if r[1]}:
        key = f"cover:{performer_id}:{raw.strip().lower()}"
        existing = conn.execute("SELECT id, canonical_id FROM alias_overrides WHERE source = 'setlistfm' AND source_key = ? AND canonical_type = 'song'",
                                (key,)).fetchone()
        if existing:
            if existing[1] != to_song:
                conn.execute("UPDATE alias_overrides SET canonical_id = ? WHERE id = ?", (to_song, existing[0]))
                alias_updated.append([existing[0], existing[1]])
        else:
            cur = conn.execute("INSERT INTO alias_overrides (source, source_key, canonical_type, canonical_id, note) VALUES ('setlistfm', ?, 'song', ?, ?)",
                               (key, to_song, f"live re-link for performer {performer_id} on {datetime.now():%Y-%m-%d}"))
            alias_inserted.append(cur.lastrowid)
    detail = {"from": from_song, "to": to_song, "performer": performer_id, "setlistSongIds": ids, "aliasInserted": alias_inserted,
              "aliasUpdated": alias_updated, "createdSong": to_song if created_song else None, "toTitle": dst["title"]}
    cur = conn.execute("INSERT INTO edit_log (entity_type, entity_id, entity_name, changes_json, reason) VALUES ('song', ?, ?, ?, ?)",
                       (from_song, src["title"], json.dumps({"_relink": detail}), reason))
    return cur.lastrowid


def _undo_relink(conn, d: dict) -> None:
    ids = d["setlistSongIds"]
    current = dict(conn.execute(f"SELECT id, song_id FROM setlist_songs WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall())
    if any(v != d["to"] for v in current.values()):
        raise MergeError("Some of those live plays have been re-linked again since -- undo the later change first.", "diverged")
    if not conn.execute("SELECT 1 FROM songs WHERE id = ?", (d["from"],)).fetchone():
        raise MergeError("The original song has been merged away since -- undo that merge first.", "gone")
    if current:
        conn.execute(f"UPDATE setlist_songs SET song_id = ? WHERE id IN ({','.join('?' * len(current))})", (d["from"], *current))
    for alias_id in d["aliasInserted"]:
        conn.execute("DELETE FROM alias_overrides WHERE id = ?", (alias_id,))
    for alias_id, old in d["aliasUpdated"]:
        conn.execute("UPDATE alias_overrides SET canonical_id = ? WHERE id = ?", (old, alias_id))
    if d.get("createdSong"):
        sid = d["createdSong"]
        refs = sum(conn.execute(q, (sid,)).fetchone()[0] for q in (
            "SELECT count(*) FROM scrobbles WHERE song_id = ?", "SELECT count(*) FROM setlist_songs WHERE song_id = ?",
            "SELECT count(*) FROM notes WHERE entity_type = 'song' AND entity_id = ?"))
        if refs:
            raise MergeError("The song created by this re-link is in use elsewhere now -- can't remove it.", "diverged")
        conn.execute("DELETE FROM alias_overrides WHERE canonical_type = 'song' AND canonical_id = ?", (sid,))
        conn.execute("DELETE FROM songs WHERE id = ?", (sid,))


def create_album(conn, artist_ids: list[int], title: str, year: int | None, mbid: str | None, reason: str) -> tuple[int, int]:
    """A new album a human asked for -- e.g. from MusicBrainz, for a song only ever heard live, so
    it has somewhere to belong before it's been scrobbled. artist_ids[0] is the album artist.
    Journaled in edit_log as {"_created": ...}; undo_edit removes it again (only while nothing
    points at it). -> (album id, edit id)"""
    if mbid:
        clash = conn.execute("SELECT title FROM albums WHERE mbid = ?", (mbid,)).fetchone()
        if clash:
            err = MergeError(f'That MusicBrainz id is already linked to "{clash[0]}" in your library.', "mbid_conflict")
            err.conflict = {"name": clash[0]}
            raise err
    album_id = conn.execute("INSERT INTO albums (artist_id, title, year, mbid) VALUES (?, ?, ?, ?)",
                            (artist_ids[0], title, year, mbid)).lastrowid
    for pos, aid in enumerate(dict.fromkeys(artist_ids)):
        conn.execute("INSERT INTO album_artists (album_id, artist_id, position) VALUES (?, ?, ?)", (album_id, aid, pos))
    edit_id = conn.execute("INSERT INTO edit_log (entity_type, entity_id, entity_name, changes_json, reason) VALUES ('album', ?, ?, ?, ?)",
                           (album_id, title, json.dumps({"_created": {"artistIds": list(dict.fromkeys(artist_ids)), "mbid": mbid}}), reason)).lastrowid
    return album_id, edit_id


def _undo_created(conn, album_id: int) -> None:
    for table, col in (("songs", "album_id"), ("scrobbles", "album_id"), ("vinyl_holdings", "album_id"), ("album_releases", "album_id")):
        if conn.execute(f"SELECT 1 FROM {table} WHERE {col} = ? LIMIT 1", (album_id,)).fetchone():
            raise MergeError("That album has songs, plays or pressings on it now -- undo those first.", "diverged")
    if conn.execute("SELECT 1 FROM merge_log WHERE entity_type = 'album' AND canonical_id = ? AND undone_at IS NULL", (album_id,)).fetchone():
        raise MergeError("Another album has been merged into it since -- undo that merge first.", "diverged")
    conn.execute("DELETE FROM review_marks WHERE entity_type = 'album' AND entity_id = ?", (album_id,))
    conn.execute("DELETE FROM notes WHERE entity_type = 'album' AND entity_id = ?", (album_id,))
    conn.execute("DELETE FROM album_genres WHERE album_id = ?", (album_id,))  # derived since -- goes with it
    conn.execute("DELETE FROM album_parts WHERE album_id = ? OR part_album_id = ?", (album_id, album_id))
    conn.execute("DELETE FROM album_tracklists WHERE album_id = ?", (album_id,))
    conn.execute("DELETE FROM album_tracklist_sources WHERE album_id = ?", (album_id,))
    conn.execute("DELETE FROM genre_hidden WHERE album_id = ?", (album_id,))
    conn.execute("DELETE FROM album_artists WHERE album_id = ?", (album_id,))
    conn.execute("DELETE FROM albums WHERE id = ?", (album_id,))


def split_album(conn, album_id: int, raw_titles: list[str], identity: dict, releases: list[str] = (),
                merge_log_id: int | None = None) -> dict:
    """Takes an absorbed album back out of `album_id` -- for merges made before undo journals
    existed, which undo_merge can't reverse. What belonged to it is recovered from the source
    text every row still carries: scrobbles (and pressings) whose raw album title is one of
    `raw_titles` move to a new album with `identity` ({title, year, mbid}), with every song
    that was only ever played from them, the editions in `releases`, and the import aliases for
    those titles. Journaled in edit_log as {"_split": ...} -- undo_edit puts it all back."""
    src = _row_dict(conn, "albums", album_id)
    if not src:
        raise MergeError("album not found", "not_found")
    if identity.get("mbid"):
        clash = conn.execute("SELECT title FROM albums WHERE mbid = ?", (identity["mbid"],)).fetchone()
        if clash:
            raise MergeError(f'That mbid is already linked to "{clash[0]}".', "mbid_conflict")
    lowered = sorted({t.strip().lower() for t in raw_titles if t and t.strip()})
    marks = ",".join("?" * len(lowered))
    scrobbles = [r[0] for r in conn.execute(
        f"SELECT id FROM scrobbles WHERE album_id = ? AND lower(raw_album_text) IN ({marks})", (album_id, *lowered))]
    vinyl = [r[0] for r in conn.execute(
        f"SELECT id FROM vinyl_holdings WHERE album_id = ? AND lower(raw_title_text) IN ({marks})", (album_id, *lowered))]
    if not scrobbles and not vinyl:
        raise MergeError("Nothing on this album carries those source titles.", "merge_error")
    new_id = conn.execute("INSERT INTO albums (artist_id, title, year, mbid) VALUES (?, ?, ?, ?)",
                          (src["artist_id"], identity.get("title") or raw_titles[0], identity.get("year"), identity.get("mbid"))).lastrowid
    conn.execute("INSERT INTO album_artists (album_id, artist_id, position) SELECT ?, artist_id, position FROM album_artists WHERE album_id = ?",
                 (new_id, album_id))
    moved = json.dumps(scrobbles)
    conn.execute("UPDATE scrobbles SET album_id = ? WHERE id IN (SELECT value FROM json_each(?))", (new_id, moved))
    conn.execute("UPDATE vinyl_holdings SET album_id = ? WHERE id IN (SELECT value FROM json_each(?))", (new_id, json.dumps(vinyl)))
    # songs only ever played from the absorbed album's rows (none of their plays are left behind)
    songs = [r[0] for r in conn.execute(
        "SELECT id FROM songs WHERE album_id = ? AND id IN (SELECT song_id FROM scrobbles WHERE album_id = ?) "
        "AND NOT EXISTS (SELECT 1 FROM scrobbles x WHERE x.song_id = songs.id AND x.album_id = ?)", (album_id, new_id, album_id))]
    conn.execute("UPDATE songs SET album_id = ? WHERE id IN (SELECT value FROM json_each(?))", (new_id, json.dumps(songs)))
    aliases = [r[0] for r in conn.execute(
        f"SELECT id FROM alias_overrides WHERE canonical_type = 'album' AND canonical_id = ? "
        f"AND substr(source_key, instr(source_key, ':') + 1) IN ({marks})", (album_id, *lowered))]
    conn.execute("UPDATE alias_overrides SET canonical_id = ? WHERE id IN (SELECT value FROM json_each(?))", (new_id, json.dumps(aliases)))
    editions = [r[0] for r in conn.execute(
        f"SELECT id FROM album_releases WHERE album_id = ? AND release_mbid IN ({','.join('?' * len(releases)) or 'NULL'})", (album_id, *releases))]
    conn.execute("UPDATE album_releases SET album_id = ? WHERE id IN (SELECT value FROM json_each(?))", (new_id, json.dumps(editions)))
    if merge_log_id:
        conn.execute("UPDATE merge_log SET undone_at = datetime('now') WHERE id = ? AND undone_at IS NULL", (merge_log_id,))
    detail = {"from": album_id, "fromTitle": src["title"], "to": new_id, "scrobbleIds": scrobbles, "vinylIds": vinyl, "songIds": songs,
              "aliasIds": aliases, "editionIds": editions, "mergeLogId": merge_log_id}
    edit_id = conn.execute("INSERT INTO edit_log (entity_type, entity_id, entity_name, changes_json, reason) VALUES ('album', ?, ?, ?, ?)",
                           (new_id, identity.get("title") or raw_titles[0], json.dumps({"_split": detail}),
                            f"split out of album #{album_id}" + (f" (reverses merge #{merge_log_id})" if merge_log_id else ""))).lastrowid
    return {"editId": edit_id, "albumId": new_id, "scrobbles": len(scrobbles), "vinyl": len(vinyl), "songs": len(songs),
            "aliases": len(aliases), "editions": len(editions)}


def _undo_split(conn, d: dict) -> None:
    back, new = d["from"], d["to"]
    if conn.execute("SELECT 1 FROM merge_log WHERE entity_type = 'album' AND canonical_id = ? AND undone_at IS NULL", (new,)).fetchone():
        raise MergeError("Another album has been merged into the split-out one since -- undo that merge first.", "diverged")
    if not _row_dict(conn, "albums", back):
        raise MergeError("The album it was split from is gone -- undo that first.", "gone")
    for table in ("scrobbles", "vinyl_holdings", "songs", "album_releases"):
        conn.execute(f"UPDATE {table} SET album_id = ? WHERE album_id = ?", (back, new))
    conn.execute("UPDATE alias_overrides SET canonical_id = ? WHERE canonical_type = 'album' AND canonical_id = ?", (back, new))
    conn.execute("DELETE FROM review_marks WHERE entity_type = 'album' AND entity_id = ?", (new,))
    conn.execute("DELETE FROM album_genres WHERE album_id = ?", (new,))
    conn.execute("DELETE FROM album_parts WHERE album_id = ? OR part_album_id = ?", (new, new))
    conn.execute("DELETE FROM album_tracklists WHERE album_id = ?", (new,))
    conn.execute("DELETE FROM album_tracklist_sources WHERE album_id = ?", (new,))
    conn.execute("DELETE FROM genre_hidden WHERE album_id = ?", (new,))
    conn.execute("DELETE FROM album_artists WHERE album_id = ?", (new,))
    conn.execute("DELETE FROM albums WHERE id = ?", (new,))
    if d.get("mergeLogId"):
        conn.execute("UPDATE merge_log SET undone_at = NULL WHERE id = ?", (d["mergeLogId"],))


def move_album_plays(conn, from_album: int, to_album: int, raw_titles: list[str], reason: str) -> dict:
    """Moves what arrived under a given album name from one album to ANOTHER EXISTING album -- for
    scrobbles filed under the wrong album (e.g. an old merge put "Led Zeppelin (Remaster)" into
    Led Zeppelin II). Recovered from the source text every row keeps: those scrobbles (and
    pressings), songs only ever played from them, the editions they were played from, and the
    import aliases for those names (so future imports go to the right album too). Journaled in
    edit_log as {"_movePlays": ...}; undo_edit moves exactly those rows back."""
    src, dst = _row_dict(conn, "albums", from_album), _row_dict(conn, "albums", to_album)
    if not src or not dst:
        raise MergeError("both albums must exist", "not_found")
    if src["artist_id"] != dst["artist_id"]:
        raise MergeError("Plays can only move between albums of the same artist.", "different_artist")
    lowered = sorted({t.strip().lower() for t in raw_titles if t and t.strip()})
    marks = ",".join("?" * len(lowered))
    scrobbles = [r[0] for r in conn.execute(
        f"SELECT id FROM scrobbles WHERE album_id = ? AND lower(raw_album_text) IN ({marks})", (from_album, *lowered))]
    vinyl = [r[0] for r in conn.execute(
        f"SELECT id FROM vinyl_holdings WHERE album_id = ? AND lower(raw_title_text) IN ({marks})", (from_album, *lowered))]
    if not scrobbles and not vinyl:
        raise MergeError("Nothing on this album arrived under that name.", "merge_error")
    moved = json.dumps(scrobbles)
    conn.execute("UPDATE scrobbles SET album_id = ? WHERE id IN (SELECT value FROM json_each(?))", (to_album, moved))
    conn.execute("UPDATE vinyl_holdings SET album_id = ? WHERE id IN (SELECT value FROM json_each(?))", (to_album, json.dumps(vinyl)))
    songs = [r[0] for r in conn.execute(
        "SELECT id FROM songs WHERE album_id = ? AND id IN (SELECT song_id FROM scrobbles WHERE id IN (SELECT value FROM json_each(?))) "
        "AND NOT EXISTS (SELECT 1 FROM scrobbles x WHERE x.song_id = songs.id AND x.album_id = ?)", (from_album, moved, from_album))]
    conn.execute("UPDATE songs SET album_id = ? WHERE id IN (SELECT value FROM json_each(?))", (to_album, json.dumps(songs)))
    aliases = [r[0] for r in conn.execute(
        f"SELECT id FROM alias_overrides WHERE canonical_type = 'album' AND canonical_id = ? "
        f"AND substr(source_key, instr(source_key, ':') + 1) IN ({marks})", (from_album, *lowered))]
    conn.execute("UPDATE alias_overrides SET canonical_id = ? WHERE id IN (SELECT value FROM json_each(?))", (to_album, json.dumps(aliases)))
    # editions only these scrobbles were played from follow them
    editions = [r[0] for r in conn.execute(
        "SELECT ar.id FROM album_releases ar WHERE ar.album_id = ? AND ar.release_mbid IN "
        "(SELECT sr.release_mbid FROM scrobble_releases sr WHERE sr.scrobble_id IN (SELECT value FROM json_each(?))) "
        "AND NOT EXISTS (SELECT 1 FROM scrobble_releases x JOIN scrobbles s ON s.id = x.scrobble_id "
        "               WHERE x.release_mbid = ar.release_mbid AND s.album_id = ?)", (from_album, moved, from_album))]
    conn.execute("UPDATE album_releases SET album_id = ? WHERE id IN (SELECT value FROM json_each(?))", (to_album, json.dumps(editions)))
    detail = {"from": from_album, "fromTitle": src["title"], "to": to_album, "toTitle": dst["title"], "rawTitles": lowered,
              "scrobbleIds": scrobbles, "vinylIds": vinyl, "songIds": songs, "aliasIds": aliases, "editionIds": editions}
    edit_id = conn.execute("INSERT INTO edit_log (entity_type, entity_id, entity_name, changes_json, reason) VALUES ('album', ?, ?, ?, ?)",
                           (to_album, dst["title"], json.dumps({"_movePlays": detail}), reason)).lastrowid
    return {"editId": edit_id, "scrobbles": len(scrobbles), "vinyl": len(vinyl), "songs": len(songs), "aliases": len(aliases),
            "editions": len(editions)}


def _undo_move_plays(conn, d: dict) -> None:
    back, to = d["from"], d["to"]
    if not _row_dict(conn, "albums", back) or not _row_dict(conn, "albums", to):
        raise MergeError("One of those albums has been merged away since -- undo that first.", "gone")
    for table, key in (("scrobbles", "scrobbleIds"), ("vinyl_holdings", "vinylIds"), ("songs", "songIds"), ("album_releases", "editionIds")):
        ids = json.dumps(d[key])
        moved_again = conn.execute(f"SELECT count(*) FROM {table} WHERE id IN (SELECT value FROM json_each(?)) AND album_id != ?", (ids, to)).fetchone()[0]
        if moved_again:
            raise MergeError(f"Some of those {table.replace('_', ' ')} have moved again since -- undo the later change first.", "diverged")
        conn.execute(f"UPDATE {table} SET album_id = ? WHERE id IN (SELECT value FROM json_each(?))", (back, ids))
    conn.execute("UPDATE alias_overrides SET canonical_id = ? WHERE id IN (SELECT value FROM json_each(?))", (back, json.dumps(d["aliasIds"])))


def undo_edit(conn, edit_id: int) -> dict:
    row = conn.execute("SELECT entity_type, entity_id, entity_name, changes_json, undone_at FROM edit_log WHERE id = ?", (edit_id,)).fetchone()
    if not row:
        raise MergeError("edit not found", "not_found")
    entity_type, entity_id, name, changes_json, undone_at = row
    if undone_at:
        raise MergeError("This edit was already undone.", "already_undone")
    special = json.loads(changes_json)
    if "_genres" in special or "_genreRule" in special:
        import genres as genre_tags
        if "_genres" in special:
            if not _row_dict(conn, "albums", entity_id):
                raise MergeError(f'"{name}" has been merged away since -- undo that merge first.', "gone")
            genre_tags.undo_genre_edit(conn, special["_genres"])
        else:
            genre_tags.undo_genre_rule(conn, special["_genreRule"])
        conn.execute("UPDATE edit_log SET undone_at = datetime('now') WHERE id = ?", (edit_id,))
        return {"editId": edit_id, "entityType": entity_type, "entityId": entity_id, "name": name}
    table = _TABLE[entity_type]
    current = _row_dict(conn, table, entity_id)
    if not current:
        raise MergeError(f'"{name}" has been merged away since -- undo that merge first.', "gone")
    changes = json.loads(changes_json)
    if "_parts" in changes:
        _undo_parts(conn, entity_id, changes["_parts"])
        conn.execute("UPDATE edit_log SET undone_at = datetime('now') WHERE id = ?", (edit_id,))
        return {"editId": edit_id, "entityType": entity_type, "entityId": entity_id, "name": name}
    if "_movePlays" in changes:
        _undo_move_plays(conn, changes["_movePlays"])
        conn.execute("UPDATE edit_log SET undone_at = datetime('now') WHERE id = ?", (edit_id,))
        return {"editId": edit_id, "entityType": entity_type, "entityId": entity_id, "name": name}
    if "_created" in changes:
        _undo_created(conn, entity_id)
        conn.execute("UPDATE edit_log SET undone_at = datetime('now') WHERE id = ?", (edit_id,))
        return {"editId": edit_id, "entityType": entity_type, "entityId": entity_id, "name": name}
    if "_split" in changes:
        _undo_split(conn, changes["_split"])
        conn.execute("UPDATE edit_log SET undone_at = datetime('now') WHERE id = ?", (edit_id,))
        return {"editId": edit_id, "entityType": entity_type, "entityId": entity_id, "name": name}
    if "_relink" in changes:
        _undo_relink(conn, changes["_relink"])
        conn.execute("UPDATE edit_log SET undone_at = datetime('now') WHERE id = ?", (edit_id,))
        return {"editId": edit_id, "entityType": entity_type, "entityId": entity_id, "name": name}
    diverged = [k for k, (_old, new) in changes.items() if current[k] != new]
    if diverged:
        raise MergeError(f"{', '.join(diverged)} changed again since this edit -- undo the later change first.", "diverged")
    if changes.get("mbid") and changes["mbid"][0] and table in _UNIQUE_MBID:
        clash = conn.execute(f"SELECT 1 FROM {table} WHERE mbid = ? AND id != ?", (changes["mbid"][0], entity_id)).fetchone()
        if clash:
            raise MergeError("The previous mbid is now used by another row -- can't restore it.", "mbid_conflict")
    conn.execute(f"UPDATE {table} SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ?", (*[v[0] for v in changes.values()], entity_id))
    conn.execute("UPDATE edit_log SET undone_at = datetime('now') WHERE id = ?", (edit_id,))
    return {"editId": edit_id, "entityType": entity_type, "entityId": entity_id, "name": name}
