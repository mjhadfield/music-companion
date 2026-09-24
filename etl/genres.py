"""
Genres: a tag on albums (release groups), inherited by artists (the artist_genres view).

Sources, in priority order (one row per album + genre, the highest-priority source wins):
  manual       added by hand in maintenance -- never touched by a refresh
  musicbrainz  MusicBrainz's voted genres for the album's release group (exact data for an exact
               album, applied automatically): genres with >= MB_MIN_VOTES votes, or the single
               top genre when every genre has fewer
  discogs      Discogs *styles* (e.g. "Thrash", "NWOBHM") of the vinyl pressings you own

Human decisions always survive a refresh:
  genre_hidden  a genre removed from one album stays removed
  genre_rules   a genre hidden everywhere, or merged into another (synonyms, e.g. metal -> heavy metal)

Hand edits are journaled in edit_log ({"_genres": ...} / {"_genreRule": ...}) and undone by
merge.undo_edit -> undo_genre_edit / undo_genre_rule.
"""
import json
import sqlite3

MB_MIN_VOTES = 2
PRIORITY = {"manual": 3, "musicbrainz": 2, "discogs": 1}
# Discogs style names that are a MusicBrainz genre under another name. Everything else is just
# lowercased (Discogs "Heavy Metal" == MusicBrainz "heavy metal"); anything missed can be merged
# by hand in Maintenance > Genres, and the merge then applies to every future refresh.
STYLE_SYNONYMS = {
    "thrash": "thrash metal",
    "prog rock": "progressive rock",
    "nwobhm": "new wave of british heavy metal",
    "rock & roll": "rock and roll",
    "nu metal": "nu metal",
    "hardcore": "hardcore punk",
}


def genre_name(raw: str) -> str:
    n = " ".join((raw or "").strip().lower().split())
    return STYLE_SYNONYMS.get(n, n)


def resolve_rules(conn: sqlite3.Connection, genre_id: int | None) -> int | None:
    """Follows merge rules to the genre that should be used; None if it's hidden everywhere."""
    for _ in range(8):  # merge chains are short; a cycle can't loop forever
        if genre_id is None:
            return None
        rule = conn.execute("SELECT action, target_genre_id FROM genre_rules WHERE genre_id = ?", (genre_id,)).fetchone()
        if not rule:
            return genre_id
        if rule[0] == "hide":
            return None
        genre_id = rule[1]
    return genre_id


def genre_id(conn: sqlite3.Connection, name: str, mbid: str | None = None, create: bool = True) -> int | None:
    """The genre row for a name (created if new), after hide/merge rules -- None if hidden."""
    name = genre_name(name)
    if not name:
        return None
    row = conn.execute("SELECT id, mbid FROM genres WHERE name = ?", (name,)).fetchone()
    if row:
        if mbid and not row[1] and not conn.execute("SELECT 1 FROM genres WHERE mbid = ?", (mbid,)).fetchone():
            conn.execute("UPDATE genres SET mbid = ? WHERE id = ?", (mbid, row[0]))
        return resolve_rules(conn, row[0])
    if not create:
        return None
    if mbid and conn.execute("SELECT 1 FROM genres WHERE mbid = ?", (mbid,)).fetchone():
        mbid = None  # MusicBrainz renamed it: keep the id on the row that has it
    return resolve_rules(conn, conn.execute("INSERT INTO genres (name, mbid) VALUES (?, ?)", (name, mbid)).lastrowid)


def pick_musicbrainz(genres: list[dict]) -> list[tuple[str, str | None, int]]:
    """Which of a release group's voted genres to keep: >= MB_MIN_VOTES votes, else the top one."""
    items = [(g.get("name"), g.get("id"), int(g.get("count") or 0)) for g in genres or [] if g.get("name")]
    keep = [i for i in items if i[2] >= MB_MIN_VOTES]
    if not keep and items:
        top = max(i[2] for i in items)
        keep = [i for i in items if i[2] == top][:1]
    return keep


def apply(conn: sqlite3.Connection, album_id: int, source: str, items: list[tuple[str, str | None, int | None]]) -> dict:
    """Sets one source's genres for an album (a refresh): adds new ones, drops ones that source no
    longer gives, never touches a higher-priority source's rows or a hidden genre."""
    hidden = {g for (g,) in conn.execute("SELECT genre_id FROM genre_hidden WHERE album_id = ?", (album_id,))}
    want: dict[int, int | None] = {}
    for name, mbid, votes in items:
        gid = genre_id(conn, name, mbid)
        if gid is not None and gid not in hidden:
            want[gid] = max(votes or 0, want.get(gid) or 0) or None
    existing = {g: (src, v) for g, src, v in conn.execute("SELECT genre_id, source, votes FROM album_genres WHERE album_id = ?", (album_id,))}
    added = removed = 0
    for gid, (src, _v) in existing.items():
        if src == source and gid not in want:
            conn.execute("DELETE FROM album_genres WHERE album_id = ? AND genre_id = ?", (album_id, gid))
            removed += 1
    for gid, votes in want.items():
        if gid not in existing:
            conn.execute("INSERT INTO album_genres (album_id, genre_id, source, votes) VALUES (?, ?, ?, ?)", (album_id, gid, source, votes))
            added += 1
        elif existing[gid][0] == source or PRIORITY[source] > PRIORITY[existing[gid][0]]:
            conn.execute("UPDATE album_genres SET source = ?, votes = ? WHERE album_id = ? AND genre_id = ?", (source, votes, album_id, gid))
    return {"added": added, "removed": removed}


def apply_musicbrainz_groups(conn: sqlite3.Connection, groups: list[dict], artist_ids: list[int],
                             resolve_key: str = "resolve:release-group:{}") -> int:
    """MusicBrainz release groups (with their genres, from an artist browse) onto the local albums
    that are exactly those release groups -- by albums.mbid, or, for an album still identified by
    one edition (a legacy release id), by that edition's already-resolved release group. -> albums updated."""
    by_rg = {g["mbid"]: g for g in groups if g.get("mbid")}
    if not by_rg:
        return 0
    targets: dict[int, str] = {}
    ids = list(by_rg)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for aid, mbid in conn.execute(f"SELECT id, mbid FROM albums WHERE mbid IN ({','.join('?' * len(chunk))})", chunk):
            targets[aid] = mbid
    # legacy albums of these artists: their stored edition id resolves (cached, from Albums >
    # Editions or an import) to one of these release groups
    keys = {}
    marks = ",".join("?" * len(artist_ids))
    for aid, mbid in conn.execute(f"SELECT DISTINCT al.id, al.mbid FROM albums al JOIN album_artists aa ON aa.album_id = al.id "
                                  f"WHERE aa.artist_id IN ({marks}) AND al.mbid IS NOT NULL", list(artist_ids)) if artist_ids else []:
        if aid not in targets:
            keys[resolve_key.format(mbid)] = aid
    if keys:
        klist = list(keys)
        for i in range(0, len(klist), 500):
            chunk = klist[i:i + 500]
            for k, payload in conn.execute(f"SELECT key, payload_json FROM mb_cache WHERE key IN ({','.join('?' * len(chunk))})", chunk):
                rg = json.loads(payload)
                if rg in by_rg:
                    targets[keys[k]] = rg
    for aid, rg in targets.items():
        apply(conn, aid, "musicbrainz", pick_musicbrainz(by_rg[rg].get("genres")))
    return len(targets)


def apply_discogs_styles(conn: sqlite3.Connection, album_id: int) -> dict:
    """The styles of every pressing of this album you own (vinyl_details) -> its discogs genres."""
    names = set()
    for (styles,) in conn.execute("SELECT d.styles FROM vinyl_details d JOIN vinyl_holdings v ON v.id = d.holding_id WHERE v.album_id = ?",
                                  (album_id,)):
        names |= set(json.loads(styles or "[]"))
    return apply(conn, album_id, "discogs", [(n, None, None) for n in sorted(names)])


# -- Hand edits (journaled) ---------------------------------------------------------------------

def edit_album(conn: sqlite3.Connection, album_id: int, add: list[str] = (), remove: list[int] = (), reason: str = "genres") -> int | None:
    """Adds genres by hand (source manual) and removes genres (remembered in genre_hidden, so a
    refresh never brings them back). Journaled; -> edit_log id (None if nothing changed)."""
    title = conn.execute("SELECT title FROM albums WHERE id = ?", (album_id,)).fetchone()
    if not title:
        raise ValueError("album not found")
    j = {"albumId": album_id, "inserted": [], "updated": [], "deleted": [], "hiddenAdded": [], "hiddenRemoved": []}
    for name in add:
        gid = genre_id(conn, name)
        if gid is None:
            continue
        if conn.execute("DELETE FROM genre_hidden WHERE album_id = ? AND genre_id = ?", (album_id, gid)).rowcount:
            j["hiddenRemoved"].append(gid)
        row = conn.execute("SELECT source, votes FROM album_genres WHERE album_id = ? AND genre_id = ?", (album_id, gid)).fetchone()
        if row is None:
            conn.execute("INSERT INTO album_genres (album_id, genre_id, source) VALUES (?, ?, 'manual')", (album_id, gid))
            j["inserted"].append(gid)
        elif row[0] != "manual":
            conn.execute("UPDATE album_genres SET source = 'manual' WHERE album_id = ? AND genre_id = ?", (album_id, gid))
            j["updated"].append([gid, row[0], row[1]])
    for gid in remove:
        row = conn.execute("SELECT source, votes FROM album_genres WHERE album_id = ? AND genre_id = ?", (album_id, gid)).fetchone()
        if row:
            conn.execute("DELETE FROM album_genres WHERE album_id = ? AND genre_id = ?", (album_id, gid))
            j["deleted"].append([gid, row[0], row[1]])
        if conn.execute("INSERT OR IGNORE INTO genre_hidden (album_id, genre_id) VALUES (?, ?)", (album_id, gid)).rowcount:
            j["hiddenAdded"].append(gid)
    if not any(j[k] for k in ("inserted", "updated", "deleted", "hiddenAdded", "hiddenRemoved")):
        return None
    return conn.execute("INSERT INTO edit_log (entity_type, entity_id, entity_name, changes_json, reason) VALUES ('album', ?, ?, ?, ?)",
                        (album_id, title[0], json.dumps({"_genres": j}), reason)).lastrowid


def undo_genre_edit(conn: sqlite3.Connection, j: dict) -> None:
    aid = j["albumId"]
    for gid in j["inserted"]:
        conn.execute("DELETE FROM album_genres WHERE album_id = ? AND genre_id = ?", (aid, gid))
    for gid, source, votes in j["updated"]:
        conn.execute("UPDATE album_genres SET source = ?, votes = ? WHERE album_id = ? AND genre_id = ?", (source, votes, aid, gid))
    for gid, source, votes in j["deleted"]:
        conn.execute("INSERT OR IGNORE INTO album_genres (album_id, genre_id, source, votes) VALUES (?, ?, ?, ?)", (aid, gid, source, votes))
    for gid in j["hiddenAdded"]:
        conn.execute("DELETE FROM genre_hidden WHERE album_id = ? AND genre_id = ?", (aid, gid))
    for gid in j["hiddenRemoved"]:
        conn.execute("INSERT OR IGNORE INTO genre_hidden (album_id, genre_id) VALUES (?, ?)", (aid, gid))


def set_rule(conn: sqlite3.Connection, gid: int, action: str, target: int | None = None) -> int:
    """Hide a genre everywhere, or merge it into `target` -- applied to every album now, and to
    every refresh from now on. Journaled; -> edit_log id."""
    name = conn.execute("SELECT name FROM genres WHERE id = ?", (gid,)).fetchone()
    if not name:
        raise ValueError("genre not found")
    if action == "merge" and (not target or target == gid or resolve_rules(conn, target) in (None, gid)):
        raise ValueError("merge needs a different, visible target genre")
    before_rule = conn.execute("SELECT action, target_genre_id FROM genre_rules WHERE genre_id = ?", (gid,)).fetchone()
    rows = [list(r) for r in conn.execute("SELECT album_id, source, votes FROM album_genres WHERE genre_id = ?", (gid,))]
    target_rows = [r[0] for r in conn.execute("SELECT album_id FROM album_genres WHERE genre_id = ?", (target,))] if target else []
    conn.execute("INSERT INTO genre_rules (genre_id, action, target_genre_id) VALUES (?, ?, ?) "
                 "ON CONFLICT (genre_id) DO UPDATE SET action = excluded.action, target_genre_id = excluded.target_genre_id",
                 (gid, action, target if action == "merge" else None))
    conn.execute("DELETE FROM album_genres WHERE genre_id = ?", (gid,))
    added_to_target = []
    if action == "merge":
        for album_id, source, votes in rows:
            if album_id not in target_rows:
                conn.execute("INSERT INTO album_genres (album_id, genre_id, source, votes) VALUES (?, ?, ?, ?)", (album_id, target, source, votes))
                added_to_target.append(album_id)
    j = {"genreId": gid, "action": action, "target": target, "beforeRule": list(before_rule) if before_rule else None,
         "rows": rows, "addedToTarget": added_to_target}
    return conn.execute("INSERT INTO edit_log (entity_type, entity_id, entity_name, changes_json, reason) VALUES ('album', 0, ?, ?, ?)",
                        (name[0], json.dumps({"_genreRule": j}), f"genre {action}")).lastrowid


def clear_rule(conn: sqlite3.Connection, gid: int) -> int | None:
    """Stops hiding/merging a genre from now on (albums get it back at their next refresh).
    Journaled; -> edit_log id, None if there was no rule."""
    rule = conn.execute("SELECT action, target_genre_id FROM genre_rules WHERE genre_id = ?", (gid,)).fetchone()
    if not rule:
        return None
    name = conn.execute("SELECT name FROM genres WHERE id = ?", (gid,)).fetchone()[0]
    conn.execute("DELETE FROM genre_rules WHERE genre_id = ?", (gid,))
    j = {"genreId": gid, "action": "clear", "target": None, "beforeRule": list(rule), "rows": [], "addedToTarget": []}
    return conn.execute("INSERT INTO edit_log (entity_type, entity_id, entity_name, changes_json, reason) VALUES ('album', 0, ?, ?, 'genre rule cleared')",
                        (name, json.dumps({"_genreRule": j}))).lastrowid


def undo_genre_rule(conn: sqlite3.Connection, j: dict) -> None:
    gid, target = j["genreId"], j.get("target")
    for album_id in j["addedToTarget"]:
        conn.execute("DELETE FROM album_genres WHERE album_id = ? AND genre_id = ?", (album_id, target))
    for album_id, source, votes in j["rows"]:
        conn.execute("INSERT OR IGNORE INTO album_genres (album_id, genre_id, source, votes) VALUES (?, ?, ?, ?)", (album_id, gid, source, votes))
    if j["beforeRule"]:
        conn.execute("UPDATE genre_rules SET action = ?, target_genre_id = ? WHERE genre_id = ?", (*j["beforeRule"], gid))
    else:
        conn.execute("DELETE FROM genre_rules WHERE genre_id = ?", (gid,))
