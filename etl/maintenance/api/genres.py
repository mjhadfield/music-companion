"""Maintenance > Genres: reviewing the genre tags the sweeps apply automatically, and adding or
removing them by hand. Every change is journaled (genres.edit_album / set_rule / clear_rule) and
undoable; removals and hide/merge rules are remembered, so a refresh never undoes a decision.
See etl/genres.py for how tags are applied."""
import genres as genre_tags
from api.core import ApiError, read_conn, route, write_tx

UNTAGGED_LIMIT = 300


def _in(ids) -> str:
    return ",".join("?" * len(ids))


def _album_rows(c, where: str, params=(), limit: int = 500) -> list[dict]:
    return [{"albumId": aid, "title": t, "year": y, "mbid": m, "artistId": arid, "artistName": an, "plays": pl, "vinyl": v, "cover": cv == "ok"}
            for aid, t, y, m, arid, an, pl, v, cv in c.execute(f"""
                SELECT al.id, al.title, al.year, al.mbid, ar.id, ar.name,
                       (SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id),
                       (SELECT count(*) FROM vinyl_holdings v WHERE v.album_id = al.id), al.cover_status
                FROM albums al JOIN artists ar ON ar.id = al.artist_id {where} LIMIT {int(limit)}""", params)]


def album_chips(c, ids) -> dict[int, list[dict]]:
    ids = list(ids)
    out = {i: [] for i in ids}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for aid, gid, name, src, votes in c.execute(
                f"SELECT ag.album_id, ge.id, ge.name, ag.source, ag.votes FROM album_genres ag JOIN genres ge ON ge.id = ag.genre_id "
                f"WHERE ag.album_id IN ({_in(chunk)}) ORDER BY ag.source = 'manual' DESC, coalesce(ag.votes, 0) DESC, ge.name", chunk):
            out[aid].append({"genreId": gid, "name": name, "source": src, "votes": votes})
    return out


def _with_chips(c, rows: list[dict]) -> list[dict]:
    chips = album_chips(c, [r["albumId"] for r in rows])
    for r in rows:
        r["genres"] = chips[r["albumId"]]
    return rows


@route("GET", "/api/genres/summary")
def summary(req):
    with read_conn() as c:
        one = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "genres": one("SELECT count(DISTINCT genre_id) FROM album_genres"),
            "albums": one("SELECT count(*) FROM albums"),
            "tagged": one("SELECT count(DISTINCT album_id) FROM album_genres"),
            "vinylAlbums": one("SELECT count(DISTINCT album_id) FROM vinyl_holdings"),
            "vinylTagged": one("SELECT count(DISTINCT v.album_id) FROM vinyl_holdings v JOIN album_genres ag ON ag.album_id = v.album_id"),
            "artistsToFetch": one("""SELECT count(*) FROM artists ar WHERE ar.mbid IS NOT NULL
                AND EXISTS (SELECT 1 FROM album_artists aa JOIN albums al ON al.id = aa.album_id WHERE aa.artist_id = ar.id AND al.mbid IS NOT NULL)
                AND NOT EXISTS (SELECT 1 FROM mb_cache m WHERE m.key = 'browse:rg-genres:' || ar.mbid)"""),
            "pressingsToFetch": one("SELECT count(*) FROM vinyl_holdings v WHERE v.discogs_release_id IS NOT NULL "
                                    "AND NOT EXISTS (SELECT 1 FROM vinyl_details d WHERE d.holding_id = v.id)"),
            "rules": one("SELECT count(*) FROM genre_rules"),
        }


@route("GET", "/api/genres/list")
def genre_list(req):
    with read_conn() as c:
        rows = c.execute("""
            SELECT ge.id, ge.name, ge.mbid, count(ag.album_id),
                   count(DISTINCT aa.artist_id),
                   sum(ag.source = 'musicbrainz'), sum(ag.source = 'discogs'), sum(ag.source = 'manual'),
                   count(DISTINCT CASE WHEN EXISTS (SELECT 1 FROM vinyl_holdings v WHERE v.album_id = ag.album_id) THEN ag.album_id END)
            FROM genres ge JOIN album_genres ag ON ag.genre_id = ge.id JOIN album_artists aa ON aa.album_id = ag.album_id AND aa.position = 0
            GROUP BY ge.id ORDER BY count(ag.album_id) DESC, ge.name""").fetchall()
        rules = c.execute("""SELECT r.genre_id, ge.name, r.action, t.id, t.name FROM genre_rules r JOIN genres ge ON ge.id = r.genre_id
                             LEFT JOIN genres t ON t.id = r.target_genre_id ORDER BY ge.name""").fetchall()
    return {"genres": [{"genreId": i, "name": n, "mbid": m, "albums": a, "artists": ar, "musicbrainz": mb or 0, "discogs": dg or 0,
                        "manual": mn or 0, "vinyl": v} for i, n, m, a, ar, mb, dg, mn, v in rows],
            "rules": [{"genreId": i, "name": n, "action": act, "targetId": ti, "targetName": tn} for i, n, act, ti, tn in rules]}


@route("GET", "/api/genres/albums")
def genre_albums(req):
    gid = req.int("genreId", required=True)
    with read_conn() as c:
        rows = _album_rows(c, "JOIN album_genres ag ON ag.album_id = al.id WHERE ag.genre_id = ? "
                              "ORDER BY (SELECT count(*) FROM vinyl_holdings v WHERE v.album_id = al.id) DESC, "
                              "(SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id) DESC", (gid,), limit=2000)
        return {"albums": _with_chips(c, rows)}


@route("GET", "/api/genres/search")
def genre_search(req):
    q = req.str("q").lower()
    with read_conn() as c:
        rows = c.execute("""SELECT ge.id, ge.name, (SELECT count(*) FROM album_genres ag WHERE ag.genre_id = ge.id) AS n FROM genres ge
                            WHERE ge.name LIKE ? AND NOT EXISTS (SELECT 1 FROM genre_rules r WHERE r.genre_id = ge.id)
                            ORDER BY ge.name NOT LIKE ?, n DESC, ge.name LIMIT 15""", (f"%{q}%", f"{q}%")).fetchall()
    return {"genres": [{"genreId": i, "name": n, "albums": k} for i, n, k in rows]}


@route("GET", "/api/genres/artist")
def artist_genres(req):
    """One artist's albums with their chips, plus what the artist inherits (artist_genres view)."""
    artist_id = req.int("artistId", required=True)
    with read_conn() as c:
        name = c.execute("SELECT name FROM artists WHERE id = ?", (artist_id,)).fetchone()
        if not name:
            raise ApiError("artist not found", 404)
        rows = _album_rows(c, "WHERE al.id IN (SELECT album_id FROM album_artists WHERE artist_id = ?) "
                              "ORDER BY (SELECT count(*) FROM vinyl_holdings v WHERE v.album_id = al.id) DESC, "
                              "(SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id) DESC", (artist_id,))
        inherited = [{"genreId": gid, "name": n, "albums": a, "vinylAlbums": v} for gid, n, a, v in c.execute(
            "SELECT ag.genre_id, ge.name, ag.albums, ag.vinyl_albums FROM artist_genres ag JOIN genres ge ON ge.id = ag.genre_id "
            "WHERE ag.artist_id = ? ORDER BY ag.vinyl_albums * 2 + ag.albums DESC, ge.name", (artist_id,))]
        return {"artistId": artist_id, "name": name[0], "inherited": inherited, "albums": _with_chips(c, rows)}


@route("GET", "/api/genres/album")
def one_album(req):
    album_id = req.int("albumId", required=True)
    with read_conn() as c:
        rows = _album_rows(c, "WHERE al.id = ?", (album_id,))
        if not rows:
            raise ApiError("album not found", 404)
        hidden = [{"genreId": i, "name": n} for i, n in c.execute(
            "SELECT ge.id, ge.name FROM genre_hidden h JOIN genres ge ON ge.id = h.genre_id WHERE h.album_id = ? ORDER BY ge.name", (album_id,))]
        return {**_with_chips(c, rows)[0], "hidden": hidden}


def _untagged_reason(c, album: dict, fetched_artist: bool) -> str:
    if not album["mbid"]:
        return "no MusicBrainz id"
    if not fetched_artist:
        return "not fetched yet"
    if c.execute("SELECT 1 FROM album_releases WHERE release_mbid = ? UNION SELECT 1 FROM scrobble_releases WHERE release_mbid = ? LIMIT 1",
                 (album["mbid"], album["mbid"])).fetchone():
        return "still on an edition id"
    return "no genres on MusicBrainz"


@route("GET", "/api/genres/untagged")
def untagged(req):
    """Albums without any genre -- vinyl first, then most played -- and why."""
    only_vinyl = req.str("vinyl") == "1"
    with read_conn() as c:
        where = ("WHERE NOT EXISTS (SELECT 1 FROM album_genres ag WHERE ag.album_id = al.id)"
                 + (" AND EXISTS (SELECT 1 FROM vinyl_holdings v WHERE v.album_id = al.id)" if only_vinyl else ""))
        total = c.execute(f"SELECT count(*) FROM albums al {where}").fetchone()[0]
        rows = _album_rows(c, where + " ORDER BY (SELECT count(*) FROM vinyl_holdings v WHERE v.album_id = al.id) DESC, "
                                      "(SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id) DESC", limit=UNTAGGED_LIMIT)
        fetched = {aid for (aid,) in c.execute(
            "SELECT ar.id FROM artists ar JOIN mb_cache m ON m.key = 'browse:rg-genres:' || ar.mbid")}
        for r in rows:
            r["reason"] = _untagged_reason(c, r, r["artistId"] in fetched)
            r["genres"] = []
        return {"total": total, "albums": rows}


@route("GET", "/api/genres/low")
def low_confidence(req):
    """MusicBrainz genres that came from a single vote -- a glance, nothing waits on it."""
    with read_conn() as c:
        rows = _album_rows(c, "WHERE al.id IN (SELECT album_id FROM album_genres WHERE source = 'musicbrainz' AND coalesce(votes, 0) <= 1) "
                              "ORDER BY (SELECT count(*) FROM vinyl_holdings v WHERE v.album_id = al.id) DESC, "
                              "(SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id) DESC", limit=UNTAGGED_LIMIT)
        return {"albums": _with_chips(c, rows)}


@route("POST", "/api/genres/edit", mutating=True)
def edit(req):
    """Add genres (by name) to / remove genres (by id) from one or more albums."""
    ids = [int(i) for i in (req.body.get("albumIds") or ([req.body["albumId"]] if req.body.get("albumId") else []))]
    add = [str(n) for n in (req.body.get("add") or []) if str(n).strip()]
    remove = [int(i) for i in (req.body.get("remove") or [])]
    if not ids or not (add or remove):
        raise ApiError("albumIds and something to add or remove are required")
    edits = []
    with write_tx() as c:
        for aid in ids:
            try:
                e = genre_tags.edit_album(c, aid, add=add, remove=remove, reason="genres (by hand)")
            except ValueError as exc:
                raise ApiError(str(exc), 404)
            if e:
                edits.append(e)
    return {"changed": len(edits), "undo": {"kind": "edit", "id": edits} if edits else None}


@route("POST", "/api/genres/rule", mutating=True)
def rule(req):
    gid, action = req.int("genreId", required=True), req.str("action")
    if action not in ("hide", "merge", "clear"):
        raise ApiError("action must be hide, merge or clear")
    with write_tx() as c:
        try:
            e = genre_tags.clear_rule(c, gid) if action == "clear" else genre_tags.set_rule(c, gid, action, req.int("targetId"))
        except ValueError as exc:
            raise ApiError(str(exc))
    return {"editId": e, "undo": {"kind": "edit", "id": e} if e else None}
