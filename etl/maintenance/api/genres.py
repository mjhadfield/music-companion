"""Maintenance > Genres: genre tags on albums -- suggested (MusicBrainz, Discogs styles), reviewed
and saved in Maintenance > Genres (By album / By artist), or added and removed by hand. Every change is journaled (genres.edit_album / set_rule / clear_rule) and
undoable; removals and hide/merge rules are remembered, so a refresh never undoes a decision.
See etl/genres.py for how tags are applied."""
import json

import genres as genre_tags
from api.core import ApiError, read_conn, route, write_tx

UNTAGGED_LIMIT = 300


def _in(ids) -> str:
    return ",".join("?" * len(ids))


def _album_rows(c, where: str, params=(), limit: int = 500, offset: int = 0) -> list[dict]:
    return [{"albumId": aid, "title": t, "year": y, "mbid": m, "artistId": arid, "artistName": an, "plays": pl, "vinyl": v, "cover": cv == "ok"}
            for aid, t, y, m, arid, an, pl, v, cv in c.execute(f"""
                SELECT al.id, al.title, al.year, al.mbid, ar.id, ar.name,
                       (SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id),
                       (SELECT count(*) FROM vinyl_holdings v WHERE v.album_id = al.id), al.cover_status
                FROM albums al JOIN artists ar ON ar.id = al.artist_id {where} LIMIT {int(limit)} OFFSET {int(offset)}""", params)]


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
            "vinylReviewed": one(f"SELECT count(DISTINCT v.album_id) FROM vinyl_holdings v JOIN review_marks m ON m.entity_type = 'album' "
                                 f"AND m.entity_id = v.album_id AND m.mark = '{genre_tags.REVIEWED_MARK}'"),
            "artistsToFetch": one("""SELECT count(*) FROM artists ar WHERE ar.mbid IS NOT NULL
                AND EXISTS (SELECT 1 FROM album_artists aa JOIN albums al ON al.id = aa.album_id WHERE aa.artist_id = ar.id AND al.mbid IS NOT NULL)
                AND NOT EXISTS (SELECT 1 FROM mb_cache m WHERE m.key = 'browse:rg-genres:' || ar.mbid)"""),
            "pressingsToFetch": one("SELECT count(*) FROM vinyl_holdings v WHERE v.discogs_release_id IS NOT NULL "
                                    "AND NOT EXISTS (SELECT 1 FROM vinyl_details d WHERE d.holding_id = v.id)"),
            "rules": one("SELECT count(*) FROM genre_rules"),
            "toReview": one(f"""SELECT count(*) FROM albums al WHERE NOT EXISTS (SELECT 1 FROM album_genres ag WHERE ag.album_id = al.id)
                AND NOT EXISTS (SELECT 1 FROM review_marks m WHERE m.entity_type = 'album' AND m.entity_id = al.id AND m.mark = '{genre_tags.REVIEWED_MARK}')"""),
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
        return {"artistId": artist_id, "name": name[0], "inherited": inherited, "albums": _links(c, _with_chips(c, rows))}


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


# -- Review queues (By album / By artist): suggestions, reviewed, then saved as a batch ------------------

QUEUE_PAGE = 20


def _links(c, rows: list[dict]) -> list[dict]:
    """Discogs releases and in-house record ids of each album's pressings, for the tile links."""
    ids = [r["albumId"] for r in rows]
    press = {i: [] for i in ids}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for aid, hid, rel in c.execute(f"SELECT album_id, id, discogs_release_id FROM vinyl_holdings WHERE album_id IN ({_in(chunk)})", chunk):
            press[aid].append({"holdingId": hid, "discogsReleaseId": rel})
    for r in rows:
        r["pressings"] = press[r["albumId"]]
    return rows


@route("GET", "/api/genres/queue")
def queue(req):
    """By album: albums still to review, most played first. mode=untagged (no genre yet) or
    single (a MusicBrainz genre that rests on one vote). By vinyl: mode=vinyl (records you own not
    reviewed yet, tagged or not, by artist) or vinyl-all (every record). Saved albums leave the queue."""
    mode = req.str("mode") or "untagged"
    offset, limit = max(0, req.int("offset", 0)), min(50, max(1, req.int("limit", QUEUE_PAGE)))
    not_reviewed = f"NOT EXISTS (SELECT 1 FROM review_marks m WHERE m.entity_type = 'album' AND m.entity_id = al.id AND m.mark = '{genre_tags.REVIEWED_MARK}')"
    order = ("(SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id) DESC, "
             "(SELECT count(*) FROM vinyl_holdings v WHERE v.album_id = al.id) DESC, al.id")
    if mode == "single":
        cond = "al.id IN (SELECT album_id FROM album_genres WHERE source = 'musicbrainz' AND coalesce(votes, 0) <= 1)"
    elif mode in ("vinyl", "vinyl-all"):   # By vinyl: every record you own, tagged or not, by artist
        cond = "EXISTS (SELECT 1 FROM vinyl_holdings v WHERE v.album_id = al.id)"
        order = "lower(ar.name), coalesce(al.year, 9999), lower(al.title)"
        if mode == "vinyl-all":
            not_reviewed = "1"
    else:
        cond = "NOT EXISTS (SELECT 1 FROM album_genres ag WHERE ag.album_id = al.id)"
    where = f"WHERE {cond} AND {not_reviewed}"
    with read_conn() as c:
        total = c.execute(f"SELECT count(*) FROM albums al JOIN artists ar ON ar.id = al.artist_id {where}").fetchone()[0]
        rows = _album_rows(c, where + f" ORDER BY {order}", limit=limit, offset=offset)
        reviewed = {i for (i,) in c.execute(f"SELECT entity_id FROM review_marks WHERE entity_type = 'album' AND mark = ? "
                                            f"AND entity_id IN ({_in(rows)})", [genre_tags.REVIEWED_MARK] + [r["albumId"] for r in rows])} if rows else set()
        for r in rows:
            r["reviewed"] = r["albumId"] in reviewed
        return {"total": total, "albums": _links(c, _with_chips(c, rows))}


@route("GET", "/api/genres/artists")
def artists_to_review(req):
    """By artist: artists with albums still to review, most played first (or a name search)."""
    q = req.str("q")
    with read_conn() as c:
        if q:
            rows = c.execute("""SELECT ar.id, ar.name, (SELECT count(*) FROM scrobbles s WHERE s.artist_id = ar.id) p FROM artists ar
                                WHERE ar.name LIKE ? ORDER BY p DESC LIMIT 15""", (f"%{q}%",)).fetchall()
            return {"artists": [{"artistId": i, "name": n, "plays": p} for i, n, p in rows]}
        rows = c.execute(f"""
            SELECT ar.id, ar.name, count(DISTINCT al.id) todo, (SELECT count(*) FROM scrobbles s WHERE s.artist_id = ar.id) p
            FROM artists ar JOIN album_artists aa ON aa.artist_id = ar.id AND aa.position = 0 JOIN albums al ON al.id = aa.album_id
            WHERE NOT EXISTS (SELECT 1 FROM album_genres ag WHERE ag.album_id = al.id)
              AND NOT EXISTS (SELECT 1 FROM review_marks m WHERE m.entity_type = 'album' AND m.entity_id = al.id AND m.mark = '{genre_tags.REVIEWED_MARK}')
            GROUP BY ar.id ORDER BY p DESC LIMIT 40""").fetchall()
    return {"artists": [{"artistId": i, "name": n, "untagged": t, "plays": p} for i, n, t, p in rows]}


def _resolved_name(c, name: str) -> str | None:
    """A suggested genre's name after hide / merge rules (None = hidden everywhere)."""
    n = genre_tags.genre_name(name)
    row = c.execute("SELECT id FROM genres WHERE name = ?", (n,)).fetchone()
    if not row:
        return n
    gid = genre_tags.resolve_rules(c, row[0])
    return None if gid is None else c.execute("SELECT name FROM genres WHERE id = ?", (gid,)).fetchone()[0]


@route("GET", "/api/genres/suggest")
def suggest(req):
    """Suggested genres for one album, for review -- nothing is saved here. Ticked: the album's own
    MusicBrainz genres (>= 2 votes, else its top one), else its releases' (editions are often tagged
    when the album isn't), and the Discogs styles of a pressing you own. Unticked hints: the rest of
    those, and the artist's MusicBrainz genres. Already on the album, or removed from it before: left out.
    MusicBrainz lookups are cached (about 3 requests for an album never looked at)."""
    import mbcache
    from common import artist_mbid_sets
    album_id = req.int("albumId", required=True)
    with read_conn() as c:
        row = c.execute("SELECT al.mbid, al.artist_id FROM albums al WHERE al.id = ?", (album_id,)).fetchone()
        if not row:
            raise ApiError("album not found", 404)
        mbid, artist_id = row
        have = {n for (n,) in c.execute("SELECT ge.name FROM album_genres ag JOIN genres ge ON ge.id = ag.genre_id WHERE ag.album_id = ?", (album_id,))}
        hidden = {n for (n,) in c.execute("SELECT ge.name FROM genre_hidden h JOIN genres ge ON ge.id = h.genre_id WHERE h.album_id = ?", (album_id,))}
        styles = set()
        for (st,) in c.execute("SELECT d.styles FROM vinyl_details d JOIN vinyl_holdings v ON v.id = d.holding_id WHERE v.album_id = ?", (album_id,)):
            styles |= set(json.loads(st or "[]"))
        artist_mbids = sorted(artist_mbid_sets(c, [artist_id]).get(artist_id, set()))
    # network (cached) outside any open connection
    notes, group, releases, edition, artist = [], [], [], None, []
    try:
        if mbid:
            rg = mbcache.release_group_genres(mbid)
            if rg is None:
                edition = mbcache.release_genres(mbid) or []
                notes.append("identified by one edition — its genres" if edition else "identified by one edition, which has no genres")
            else:
                group, releases = rg["group"], rg["releases"]
                if group:
                    notes.append(f"MusicBrainz: {len(group)} on the album")
                elif releases:
                    notes.append(f"MusicBrainz: none on the album itself — {len(releases)} from its {rg['releaseCount']} edition(s)")
                else:
                    notes.append(f"MusicBrainz: no genres on the album or its {rg['releaseCount']} edition(s)")
        else:
            notes.append("no MusicBrainz id — Find it in Albums for MusicBrainz genres")
        for m in artist_mbids[:2]:
            artist += mbcache.artist_genres(m) or []
    except Exception as exc:  # a lookup failing shouldn't lose the rest
        notes.append(f"MusicBrainz lookup failed: {exc}")
    out: dict[str, dict] = {}
    with read_conn() as c:
        def add(name, mbid_, votes, frm, source, tick):
            n = _resolved_name(c, name)
            if not n or n in have or n in hidden or n in out:
                return
            out[n] = {"name": n, "mbid": mbid_, "votes": votes or None, "from": frm, "source": source, "tick": tick}
        picked = {g[0] for g in genre_tags.pick_musicbrainz(group)}
        for g in sorted(group, key=lambda g: -g["count"]):
            add(g["name"], g["id"], g["count"], "album", "musicbrainz", g["name"] in picked)
        rel_picked = {g[0] for g in genre_tags.pick_musicbrainz(releases)} if not group else set()
        for g in releases:
            add(g["name"], g["id"], g["count"], "editions", "musicbrainz", g["name"] in rel_picked)
        ed_picked = {g[0] for g in genre_tags.pick_musicbrainz(edition or [])}
        for g in edition or []:
            add(g["name"], g["id"], g["count"], "edition", "musicbrainz", g["name"] in ed_picked)
        for st in sorted(styles):
            add(st, None, None, "discogs", "discogs", True)
        if styles:
            notes.append(f"Discogs: {len(styles)} style(s) from your pressing")
        # nothing of the album's own, and nothing on it yet: the artist's top genres (>= 3 votes, at
        # most 2) arrive ticked -- you still see and untick them before saving
        fallback = not have and not any(x["tick"] for x in out.values())
        ticked = 0
        for g in sorted(artist, key=lambda g: -g["count"])[:6]:
            tick = fallback and ticked < 2 and g["count"] >= 3
            ticked += tick
            add(g["name"], g["id"], g["count"], "artist", "manual", tick)
        if fallback and ticked:
            notes.append("nothing of its own — the artist's top genres are ticked")
    return {"albumId": album_id, "suggestions": list(out.values()), "notes": notes}
