"""Album + vinyl endpoints, moved out of server.py. Response shapes are unchanged from before
the redesign (vinyl.html still uses them as-is until
their own rebuild); what changed underneath is that merges/assignments now go through
merge.py (undo journal, edit_log) and every MusicBrainz call through mbcache."""
import json
import re
from collections import defaultdict
from itertools import combinations

import requests
from rapidfuzz import fuzz, process
from rapidfuzz.utils import default_process

import covers
import mbcache
import merge
from api.core import ApiError, read_conn, route, write_tx
from common import artist_mbid_sets
from titles import base_key, fold, sequel_marker, split_title

MBID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

# Album duplicate scan: scoped per artist (comparing "Greatest Hits" against every other
# "Greatest Hits" in the library would be noise), which already does most of the false-positive
# filtering, so a shorter floor than the artist scan is enough.
DUPLICATE_SCORER = fuzz.token_set_ratio
ALBUM_DUPLICATE_DEFAULT_MIN_SCORE = 90
ALBUM_DUPLICATE_MIN_SHORT_LEN = 6


@route("GET", "/api/albums/missing-mbid")
def missing_mbid(req):
    min_count = req.int("minCount", 3)
    with read_conn() as c:
        rows = c.execute(
            """
            WITH album_stats AS (
                SELECT al.id AS album_id, al.title, ar.id AS artist_id, ar.name AS artist_name, ar.mbid AS artist_mbid,
                       (SELECT count(*) FROM scrobbles sc WHERE sc.album_id = al.id) AS scrobble_count,
                       (SELECT count(*) FROM vinyl_holdings vh WHERE vh.album_id = al.id) AS vinyl_count
                FROM albums al JOIN artists ar ON ar.id = al.artist_id WHERE al.mbid IS NULL
                  AND NOT EXISTS (SELECT 1 FROM album_parts ap WHERE ap.album_id = al.id)  -- a set of albums has no release group to find
            )
            SELECT album_id, title, artist_id, artist_name, artist_mbid, scrobble_count, vinyl_count FROM album_stats
            WHERE (scrobble_count + vinyl_count) >= ? ORDER BY (scrobble_count + vinyl_count) DESC LIMIT 500
            """, (min_count,)).fetchall()
        total_missing = c.execute("SELECT count(*) FROM albums al WHERE al.mbid IS NULL AND NOT EXISTS (SELECT 1 FROM album_parts ap WHERE ap.album_id = al.id)").fetchone()[0]
    return {"minCount": min_count, "totalMissing": total_missing, "queueCount": len(rows), "rows": [{
        "albumId": r[0], "title": r[1], "artistId": r[2], "artistName": r[3], "artistMbid": r[4],
        "scrobbleCount": r[5], "vinylCount": r[6]} for r in rows]}


@route("GET", "/api/albums/mb-search")
def mb_search(req):
    q = req.str("q")
    if not q:
        raise ApiError("q is required")
    artist_mbid = req.str("artistMbid") or None
    if artist_mbid:  # widen to every MusicBrainz artist that counts as this local artist
        with read_conn() as c:
            row = c.execute("SELECT id FROM artists WHERE mbid = ?", (artist_mbid,)).fetchone()
            ids = sorted(artist_mbid_sets(c, [row[0]]).get(row[0], {artist_mbid})) if row else [artist_mbid]
        artist_mbid = ids[0] if len(ids) == 1 else ids
    try:
        candidates = mbcache.search_release_groups(q, artist_name=req.str("artistName") or None,
                                                   artist_mbid=artist_mbid, limit=min(req.int("limit", 10), 25))
    except requests.RequestException as exc:
        raise ApiError(f"MusicBrainz request failed: {exc}", 502)
    return {"candidates": candidates}


@route("GET", "/api/albums/local-search")
def local_search(req):
    q = req.str("q")
    if not q:
        return {"results": []}
    exclude_id = req.int("excludeId")
    with read_conn() as c:
        rows = c.execute(
            """
            SELECT al.id, al.title, ar.name, al.year, al.mbid,
                   (SELECT count(*) FROM scrobbles sc WHERE sc.album_id = al.id),
                   (SELECT count(*) FROM vinyl_holdings vh WHERE vh.album_id = al.id)
            FROM albums al JOIN artists ar ON ar.id = al.artist_id
            """).fetchall()
    choices, meta = {}, {}
    for r in rows:
        if r[0] == exclude_id:
            continue
        choices[r[0]] = f"{r[2]} — {r[1]}"
        meta[r[0]] = {"title": r[1], "artistName": r[2], "year": r[3], "mbid": r[4], "scrobbleCount": r[5], "vinylCount": r[6]}
    matches = process.extract(q, choices, scorer=fuzz.WRatio, limit=min(req.int("limit", 10), 40), score_cutoff=55, processor=default_process)
    return {"results": [{"albumId": aid, "label": label, "score": round(score, 1), **meta[aid]} for label, score, aid in matches]}


@route("GET", "/api/albums/detail")
def detail(req):
    album_id = req.int("id", required=True)
    with read_conn() as c:
        row = c.execute("SELECT al.id, al.title, al.mbid, al.year, ar.id, ar.name FROM albums al JOIN artists ar ON ar.id = al.artist_id WHERE al.id = ?",
                        (album_id,)).fetchone()
        if not row:
            raise ApiError("album not found", 404)
        scrobbles = c.execute("SELECT count(*) FROM scrobbles WHERE album_id = ?", (album_id,)).fetchone()[0]
        vinyl = c.execute("SELECT count(*) FROM vinyl_holdings WHERE album_id = ?", (album_id,)).fetchone()[0]
    return {"albumId": row[0], "title": row[1], "mbid": row[2], "year": row[3], "artistId": row[4], "artistName": row[5],
            "scrobbleCount": scrobbles, "vinylCount": vinyl}


@route("GET", "/api/albums/tracklist")
def tracklist(req):
    album_id = req.int("id", required=True)
    with read_conn() as c:
        songs = c.execute(
            "SELECT s.id, s.title, count(sc.id) cnt FROM songs s LEFT JOIN scrobbles sc ON sc.song_id = s.id WHERE s.album_id = ? "
            "GROUP BY s.id ORDER BY cnt DESC, s.title", (album_id,)).fetchall()
        vinyl = c.execute("SELECT id, format, label, catalog_number, media_condition, date_added FROM vinyl_holdings WHERE album_id = ? ORDER BY date_added",
                          (album_id,)).fetchall()
    return {"albumId": album_id,
            "songs": [{"songId": r[0], "title": r[1], "scrobbleCount": r[2]} for r in songs],
            "vinylCopies": [{"vinylId": r[0], "format": r[1], "label": r[2], "catalogNumber": r[3], "condition": r[4], "dateAdded": r[5]} for r in vinyl]}


@route("GET", "/api/albums/duplicate-candidates")
def duplicate_candidates(req):
    try:
        min_score = float(req.str("minScore") or ALBUM_DUPLICATE_DEFAULT_MIN_SCORE)
    except ValueError:
        min_score = ALBUM_DUPLICATE_DEFAULT_MIN_SCORE
    limit = req.int("limit", 100)
    with read_conn() as c:
        rows = c.execute(
            """
            SELECT al.id, al.title, al.mbid, al.year, al.artist_id, ar.name,
                   (SELECT count(*) FROM scrobbles sc WHERE sc.album_id = al.id),
                   (SELECT count(*) FROM vinyl_holdings vh WHERE vh.album_id = al.id)
            FROM albums al JOIN artists ar ON ar.id = al.artist_id ORDER BY al.artist_id
            """).fetchall()
        dismissed = {(a, b) for a, b in c.execute("SELECT entity_id_a, entity_id_b FROM duplicate_dismissals WHERE entity_type = 'album'")}
    by_artist = defaultdict(list)
    for r in rows:
        by_artist[r[4]].append({"albumId": r[0], "title": r[1], "mbid": r[2], "year": r[3], "artistId": r[4], "artistName": r[5],
                                "scrobbleCount": r[6], "vinylCount": r[7]})
    candidates = []
    for albums in by_artist.values():
        for a, b in combinations(albums, 2):
            if min(len(a["title"]), len(b["title"])) < ALBUM_DUPLICATE_MIN_SHORT_LEN:
                continue
            score = DUPLICATE_SCORER(a["title"], b["title"])
            if score < min_score or (min(a["albumId"], b["albumId"]), max(a["albumId"], b["albumId"])) in dismissed:
                continue
            candidates.append({"a": a, "b": b, "score": round(score, 1)})
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return {"minScore": min_score, "candidateCount": len(candidates), "candidates": candidates[:limit]}


@route("POST", "/api/albums/dismiss-duplicate", mutating=True)
def dismiss_duplicate(req):
    a_id, b_id = req.int("aId", required=True), req.int("bId", required=True)
    if a_id == b_id:
        raise ApiError("aId and bId must differ")
    with write_tx() as c:
        c.execute("INSERT INTO duplicate_dismissals (entity_type, entity_id_a, entity_id_b) VALUES ('album', ?, ?) ON CONFLICT DO NOTHING",
                  tuple(sorted((a_id, b_id))))
    return {"dismissed": True}


@route("POST", "/api/albums/assign-mbid", mutating=True)
def assign_mbid(req):
    """Also the "correct a wrongly-identified album" endpoint: title/year are optional, sent
    when the caller picked the right release group via search."""
    album_id = req.int("albumId", required=True)
    submitted = req.str("mbid").lower()
    if not MBID_RE.match(submitted):
        raise ApiError("mbid doesn't look like a MusicBrainz id (expected a UUID)")
    # albums.mbid is always a release-group id; a pasted id is easily a *release* id instead
    # (MB's release page is what usually gets copied). Resolved before the write transaction
    # since it's a network call.
    try:
        mbid = mbcache.resolve_release_group(submitted)
    except requests.RequestException as exc:
        raise ApiError(f"MusicBrainz request failed: {exc}", 502)
    if not mbid:
        raise ApiError("That doesn't resolve to a release or release group on MusicBrainz -- double check the id.", 400, "not_found")
    changes = {"mbid": mbid}
    if req.str("title"):
        changes["title"] = req.str("title")
    if req.body.get("year"):
        changes["year"] = int(req.body["year"])
    with write_tx() as c:
        current = c.execute("SELECT mbid FROM albums WHERE id = ?", (album_id,)).fetchone()
        if not current:
            raise ApiError("album not found", 404)
        if current[0] != mbid:
            # The mbid the cover was fetched under no longer describes this album.
            changes.update({"cover_status": None, "cover_updated_at": None})
        try:
            edit = merge.edit_entity(c, "album", album_id, changes, req.str("reason") or "assigned")
        except merge.MergeError as exc:
            if exc.code == "mbid_conflict":
                raise ApiError(str(exc), 409, "mbid_conflict",
                               conflictingAlbum={"albumId": exc.conflict["id"], "title": exc.conflict["name"], "mbid": mbid})
            raise
        title = c.execute("SELECT title FROM albums WHERE id = ?", (album_id,)).fetchone()[0]
    if current[0] != mbid:
        # A new identity: look the release group up now -- what the vinyl identity / original-year
        # checks read (otherwise they sit at "not checked"). Cache only, so the edit stays cleanly
        # undoable; the cover it cleared is re-fetched by hand (a fetch here would block that undo).
        try:
            mbcache.release_group(mbid)
        except Exception:  # noqa: BLE001 -- best-effort: "Check on MusicBrainz" on the vinyl page redoes it
            pass
    return {"albumId": album_id, "title": title, "mbid": mbid, "resolvedFromRelease": mbid != submitted, "editId": edit["editId"]}


@route("POST", "/api/albums/audit-identity", mutating=True)
def audit_identity(req):
    """Corrects an already-stored mbid that's really a release id to its release group. Only
    ever the mbid -- never title/year, which need a human choosing the release group."""
    album_id = req.int("albumId", required=True)
    with read_conn() as c:
        row = c.execute("SELECT id, mbid FROM albums WHERE id = ?", (album_id,)).fetchone()
    if not row:
        raise ApiError("album not found", 404)
    if not row[1]:
        return {"albumId": album_id, "changed": False, "reason": "no_mbid"}
    resolved = mbcache.resolve_release_group(row[1])
    if not resolved:
        return {"albumId": album_id, "changed": False, "reason": "unresolvable"}
    if resolved == row[1]:
        return {"albumId": album_id, "changed": False, "reason": "already_correct"}
    with write_tx() as c:
        try:
            merge.edit_entity(c, "album", album_id, {"mbid": resolved, "cover_status": None, "cover_updated_at": None}, "audit: release -> release group")
        except merge.MergeError as exc:
            if exc.code == "mbid_conflict":
                return {"albumId": album_id, "changed": False, "reason": "conflict",
                        "message": f'Resolves to a release group already linked to "{exc.conflict["name"]}" -- looks like a duplicate album, needs a human to merge.'}
            raise
    return {"albumId": album_id, "changed": True, "oldMbid": row[1], "newMbid": resolved}


@route("POST", "/api/albums/fetch-cover", mutating=True)
def fetch_cover(req):
    album_id = req.int("albumId", required=True)
    # Not write_tx(): the fetch is a network call, and an IMMEDIATE transaction would hold the
    # write lock for its whole duration. covers only writes (one UPDATE) after the download.
    with read_conn() as c:
        row = c.execute("SELECT id, mbid FROM albums WHERE id = ?", (album_id,)).fetchone()
        if not row:
            raise ApiError("album not found", 404)
        if not row[1]:
            raise ApiError("This album has no MusicBrainz id yet -- assign one first so there's something to look the cover art up by.", 409, "no_mbid")
        found = covers.fetch_from_cover_art_archive(c, album_id, row[1])
        c.commit()
    return {"albumId": album_id, "found": found, "coverStatus": "ok" if found else "none"}


@route("POST", "/api/albums/set-cover", mutating=True)
def set_cover(req):
    album_id, url = req.int("albumId", required=True), req.str("url")
    if not url.startswith(("http://", "https://")):
        raise ApiError("url must start with http:// or https://")
    with read_conn() as c:  # see fetch_cover: no write lock across the download
        if not c.execute("SELECT 1 FROM albums WHERE id = ?", (album_id,)).fetchone():
            raise ApiError("album not found", 404)
        ok, error = covers.fetch_from_url(c, album_id, url)
        c.commit()
    if not ok:
        raise ApiError(error)
    return {"albumId": album_id, "coverStatus": "ok"}


@route("POST", "/api/albums/clear-cover", mutating=True)
def clear_cover(req):
    """The Covers tab's Undo: takes back a cover saved moments ago (see covers.remove_cover)."""
    album_id = req.int("albumId", required=True)
    status = req.body.get("status")
    if status not in (None, "none"):
        raise ApiError("status must be null or 'none'")
    with write_tx() as c:
        if not c.execute("SELECT 1 FROM albums WHERE id = ?", (album_id,)).fetchone():
            raise ApiError("album not found", 404)
        moved = covers.remove_cover(c, album_id, status)
    return {"albumId": album_id, "removed": moved, "coverStatus": status}


# ---- Covers: at least one picture per artist ---------------------------------------------------
# The site's home page shows an artist as the cover of the album of theirs you've played most that has
# one -- so an artist with no covered album at all is a blank square. This queue lists those artists,
# most played first, each with the album whose cover would stand in for them: their most played album
# with a MusicBrainz id that the Cover Art Archive hasn't already come up empty for, and that you
# haven't turned down. The page previews the cover straight from the archive; nothing is saved until
# you say so (fetch-cover / set-cover), and a turned-down cover or a skipped artist is a review mark.
COVER_REJECTED = "cover-rejected"   # on an album: not this cover, propose the next album
COVER_SKIP = "cover-skip"           # on an artist: leave them without a picture
COVERS_PAGE = 24


def _covers_where() -> str:
    return f"""
        FROM (SELECT artist_id, count(*) AS plays FROM scrobbles GROUP BY artist_id) p
        JOIN artists ar ON ar.id = p.artist_id
        WHERE EXISTS (SELECT 1 FROM scrobbles s WHERE s.artist_id = ar.id AND s.album_id IS NOT NULL)
          AND NOT EXISTS (SELECT 1 FROM scrobbles s JOIN albums al ON al.id = s.album_id WHERE s.artist_id = ar.id AND al.cover_status = 'ok')
          AND NOT EXISTS (SELECT 1 FROM review_marks m WHERE m.entity_type = 'artist' AND m.entity_id = ar.id AND m.mark = '{COVER_SKIP}')"""


def covers_queue_rows(c, offset: int = 0, limit: int = COVERS_PAGE) -> tuple[int, list[dict]]:
    total = c.execute(f"SELECT count(*) {_covers_where()}").fetchone()[0]
    items = []
    for artist_id, name, plays in c.execute(f"SELECT ar.id, ar.name, p.plays {_covers_where()} ORDER BY p.plays DESC, ar.name LIMIT ? OFFSET ?",
                                            (limit, offset)).fetchall():
        albums = [{"albumId": r[0], "title": r[1], "mbid": r[2], "coverStatus": r[3], "plays": r[4], "rejected": bool(r[5])} for r in c.execute(
            f"""SELECT al.id, al.title, al.mbid, al.cover_status, count(*) AS n,
                       EXISTS (SELECT 1 FROM review_marks m WHERE m.entity_type = 'album' AND m.entity_id = al.id AND m.mark = '{COVER_REJECTED}')
                FROM scrobbles s JOIN albums al ON al.id = s.album_id WHERE s.artist_id = ?
                GROUP BY al.id ORDER BY n DESC, al.title LIMIT 10""", (artist_id,))]
        pick = next((a for a in albums if a["mbid"] and a["coverStatus"] is None and not a["rejected"]), None)
        items.append({"artistId": artist_id, "artistName": name, "plays": plays, "albums": albums,
                      "candidateId": pick["albumId"] if pick else None})
    return total, items


# The second pass, once an artist has a picture: their other albums, most played first -- every album
# you play a lot gets its own cover. Only albums with an id the archive hasn't come up empty for, and
# that you haven't turned down; albums with no id are fixed in Discography (or the Missing MBID tab).
def _album_covers_where() -> str:
    return f"""
        FROM (SELECT album_id, count(*) AS plays FROM scrobbles WHERE album_id IS NOT NULL GROUP BY album_id) p
        JOIN albums al ON al.id = p.album_id
        JOIN artists ar ON ar.id = al.artist_id
        WHERE al.cover_status IS NULL AND al.mbid IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM review_marks m WHERE m.entity_type = 'album' AND m.entity_id = al.id AND m.mark = '{COVER_REJECTED}')
          -- "has a picture" exactly as the site decides it: a covered album among the artist's played ones
          AND EXISTS (SELECT 1 FROM scrobbles s JOIN albums other ON other.id = s.album_id WHERE s.artist_id = al.artist_id AND other.cover_status = 'ok')"""


def album_covers_rows(c, offset: int = 0, limit: int = COVERS_PAGE) -> tuple[int, list[dict]]:
    total = c.execute(f"SELECT count(*) {_album_covers_where()}").fetchone()[0]
    items = [{"albumId": r[0], "title": r[1], "mbid": r[2], "plays": r[3], "artistId": r[4], "artistName": r[5]} for r in c.execute(
        f"SELECT al.id, al.title, al.mbid, p.plays, ar.id, ar.name {_album_covers_where()} ORDER BY p.plays DESC, al.title LIMIT ? OFFSET ?", (limit, offset))]
    return total, items


# By artist: one band's whole discography at once -- every album credited to them that has no cover
# (including ones with no id yet, or that the archive had nothing for: a link can still be pasted).
def artist_covers_rows(c, artist_id: int) -> dict:
    artist = c.execute("SELECT id, name, mbid FROM artists WHERE id = ?", (artist_id,)).fetchone()
    if not artist:
        raise ApiError("artist not found", 404)
    rows = c.execute(f"""
        SELECT al.id, al.title, al.mbid, al.cover_status, al.year,
               (SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id) AS plays,
               EXISTS (SELECT 1 FROM review_marks m WHERE m.entity_type = 'album' AND m.entity_id = al.id AND m.mark = '{COVER_REJECTED}')
        FROM album_artists aa JOIN albums al ON al.id = aa.album_id
        WHERE aa.artist_id = ? ORDER BY plays DESC, al.year, al.title""", (artist_id,)).fetchall()
    albums = [{"albumId": r[0], "title": r[1], "mbid": r[2], "coverStatus": r[3], "year": r[4], "plays": r[5], "rejected": bool(r[6])} for r in rows]
    return {"artist": {"artistId": artist[0], "name": artist[1], "mbid": artist[2]},
            "items": [a for a in albums if a["coverStatus"] != "ok"], "covered": sum(a["coverStatus"] == "ok" for a in albums)}


def cover_artists(c, limit: int = 100) -> list[dict]:
    """Artists with albums still to cover (an id, not looked up, not turned down), most played first."""
    return [{"artistId": r[0], "name": r[1], "missing": r[2], "plays": r[3]} for r in c.execute(f"""
        SELECT ar.id, ar.name, count(*) AS missing, coalesce(p.plays, 0) AS plays
        FROM album_artists aa JOIN albums al ON al.id = aa.album_id JOIN artists ar ON ar.id = aa.artist_id
        LEFT JOIN (SELECT artist_id, count(*) AS plays FROM scrobbles GROUP BY artist_id) p ON p.artist_id = ar.id
        WHERE al.cover_status IS NULL AND al.mbid IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM review_marks m WHERE m.entity_type = 'album' AND m.entity_id = al.id AND m.mark = '{COVER_REJECTED}')
        GROUP BY ar.id ORDER BY plays DESC, ar.name LIMIT ?""", (limit,))]


@route("GET", "/api/albums/covers-queue")
def covers_queue(req):
    """mode=artists (default): one picture per artist. mode=albums: the most played albums still without one.
    mode=artist: one artist's albums without a cover (artistId; without it, just the list of artists to pick from)."""
    offset, limit = max(0, req.int("offset", 0)), max(1, min(60, req.int("limit", COVERS_PAGE)))
    with read_conn() as c:
        if req.str("mode") == "artist":
            counts = {"artists": c.execute(f"SELECT count(*) {_covers_where()}").fetchone()[0],
                      "albums": c.execute(f"SELECT count(*) {_album_covers_where()}").fetchone()[0]}
            out = {"counts": counts, "artists": cover_artists(c), "marks": {"rejected": COVER_REJECTED, "skip": COVER_SKIP}}
            artist_id = req.int("artistId")
            if artist_id:
                one = artist_covers_rows(c, artist_id)
                out.update(one, count=len(one["items"]))
            else:
                out.update(items=[], count=0, artist=None)
            return out
        if req.str("mode") == "albums":
            total, items = album_covers_rows(c, offset, limit)
            other = c.execute(f"SELECT count(*) {_covers_where()}").fetchone()[0]
            counts = {"artists": other, "albums": total}
        else:
            total, items = covers_queue_rows(c, offset, limit)
            counts = {"artists": total, "albums": c.execute(f"SELECT count(*) {_album_covers_where()}").fetchone()[0]}
    return {"count": total, "counts": counts, "items": items, "marks": {"rejected": COVER_REJECTED, "skip": COVER_SKIP}}


@route("POST", "/api/albums/merge", mutating=True)
def merge_albums(req):
    identity = req.body.get("identity") or None
    with write_tx() as c:
        return merge.merge_albums(c, req.int("absorbedId", required=True), req.int("canonicalId", required=True), identity)


# =============================================================================================
# Redesigned Albums page (Phase 2): artist-level discography review, title-first search,
# richer comparisons, MusicBrainz discography browse, verify.
# =============================================================================================

def _in(ids) -> str:
    return ",".join("?" * len(ids))


def _album_group_key(title: str) -> str:
    return base_key(title) or fold(title)


def album_profiles(c, album_ids: list[int]) -> dict[int, dict]:
    """Everything the review UI shows about a set of albums, in a handful of batched queries."""
    ids = list(dict.fromkeys(int(i) for i in album_ids))
    if not ids:
        return {}
    out = {}
    for aid, title, year, mbid, cover, artist_id, artist_name, artist_mbid in c.execute(
            f"SELECT al.id, al.title, al.year, al.mbid, al.cover_status, ar.id, ar.name, ar.mbid FROM albums al "
            f"JOIN artists ar ON ar.id = al.artist_id WHERE al.id IN ({_in(ids)})", ids):
        base, tags = split_title(title)
        out[aid] = {"albumId": aid, "title": title, "year": year, "mbid": mbid, "coverStatus": cover,
                    "artistId": artist_id, "artistName": artist_name, "artistMbid": artist_mbid,
                    "baseKey": _album_group_key(title), "baseTitle": base, "tags": sorted(tags), "sequel": sequel_marker(title),
                    "scrobbleCount": 0, "firstPlayed": None, "lastPlayed": None, "vinyl": [], "vinylCount": 0,
                    "songCount": 0, "trackKeys": [], "rawTitles": {"lastfm": [], "discogs": []}, "mergedIn": 0, "mb": None}
    for aid, n, first, last in c.execute(
            f"SELECT album_id, count(*), min(played_at), max(played_at) FROM scrobbles WHERE album_id IN ({_in(ids)}) GROUP BY album_id", ids):
        out[aid].update(scrobbleCount=n, firstPlayed=first, lastPlayed=last)
    for aid, vid, rel, fmt, label, cat, raw in c.execute(
            f"SELECT album_id, id, discogs_release_id, format, label, catalog_number, raw_title_text FROM vinyl_holdings WHERE album_id IN ({_in(ids)})", ids):
        out[aid]["vinyl"].append({"vinylId": vid, "discogsReleaseId": rel, "format": fmt, "label": label, "catalogNumber": cat})
        if raw not in out[aid]["rawTitles"]["discogs"]:
            out[aid]["rawTitles"]["discogs"].append(raw)
    for aid, raw in c.execute(
            f"SELECT DISTINCT album_id, raw_album_text FROM scrobbles WHERE album_id IN ({_in(ids)}) AND raw_album_text IS NOT NULL", ids):
        if len(out[aid]["rawTitles"]["lastfm"]) < 8:
            out[aid]["rawTitles"]["lastfm"].append(raw)
    tracks = defaultdict(set)
    for aid, title in c.execute(
            f"""SELECT sc.album_id, s.title FROM scrobbles sc JOIN songs s ON s.id = sc.song_id WHERE sc.album_id IN ({_in(ids)})
                UNION SELECT album_id, title FROM songs WHERE album_id IN ({_in(ids)})""", ids + ids):
        tracks[aid].add(base_key(title))
    for aid, keys in tracks.items():
        out[aid]["trackKeys"] = sorted(keys)
        out[aid]["songCount"] = len(keys)
    for aid, n in c.execute(
            f"SELECT canonical_id, count(*) FROM merge_log WHERE entity_type = 'album' AND undone_at IS NULL AND canonical_id IN ({_in(ids)}) GROUP BY canonical_id", ids):
        out[aid]["mergedIn"] = n
    by_mbid = {a["mbid"]: a for a in out.values() if a["mbid"]}
    if by_mbid:
        for key, payload in c.execute(
                f"SELECT key, payload_json FROM mb_cache WHERE key IN ({_in(by_mbid)})", [f"lookup:release-group:{m}" for m in by_mbid]):
            rg = json.loads(payload)
            if rg:
                by_mbid[key.split(":", 2)[2]]["mb"] = {k: rg.get(k) for k in ("title", "primaryType", "secondaryTypes", "firstReleaseDate", "artistCredit")}
    return out


def _dismissed_pairs(c) -> set[tuple[int, int]]:
    return {(a, b) for a, b in c.execute("SELECT entity_id_a, entity_id_b FROM duplicate_dismissals WHERE entity_type = 'album'")}


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def discography_groups(albums: list[dict], dismissed: set) -> tuple[list[list[int]], dict[int, list[int]]]:
    """-> (groups, similar). groups: albums whose edition-normalised titles are identical ("Ride
    the Lightning" / "Ride The Lightning (Remastered)"), minus pairs a human said aren't the
    same. similar: albumId -> other ungrouped albums with a near-identical base title (fuzzy,
    same sequel number) -- shown as hints, never grouped automatically."""
    by_key = defaultdict(list)
    for a in albums:
        by_key[a["baseKey"]].append(a)
    groups = []
    for members in by_key.values():
        if len(members) < 2:
            continue
        ids = [m["albumId"] for m in members]
        # drop anyone a human has said is different from every other member
        keep = [i for i in ids if any(_pair(i, j) not in dismissed for j in ids if j != i)]
        if len(keep) >= 2:
            groups.append(keep)
    grouped = {i for g in groups for i in g}
    similar = defaultdict(list)
    for a, b in combinations(albums, 2):
        if a["albumId"] in grouped and b["albumId"] in grouped or a["baseKey"] == b["baseKey"]:
            continue
        if a["sequel"] != b["sequel"] or _pair(a["albumId"], b["albumId"]) in dismissed:
            continue
        if min(len(a["baseKey"]), len(b["baseKey"])) >= 4 and fuzz.token_set_ratio(a["baseKey"], b["baseKey"]) >= 92:
            similar[a["albumId"]].append(b["albumId"])
            similar[b["albumId"]].append(a["albumId"])
    return groups, dict(similar)


def default_primary(members: list[dict]) -> int:
    """Pre-selected (never auto-applied) primary: owned on vinyl, then has an mbid, then a plain
    title with no edition suffix, then most played."""
    return max(members, key=lambda m: (m["vinylCount"] > 0, bool(m["mbid"]), not m["tags"], m["scrobbleCount"]))["albumId"]


@route("GET", "/api/albums/artist-queue")
def artist_queue(req):
    """Artists whose discography has albums that look like versions of each other, most-played first."""
    with read_conn() as c:
        rows = c.execute("SELECT al.id, al.title, al.artist_id FROM albums al").fetchall()
        dismissed = _dismissed_pairs(c)
        plays = dict(c.execute("SELECT artist_id, count(*) FROM scrobbles GROUP BY artist_id").fetchall())
        names = dict(c.execute("SELECT id, name FROM artists").fetchall())
        mbids = dict(c.execute("SELECT id, mbid FROM artists").fetchall())
    by_artist = defaultdict(list)
    for aid, title, artist_id in rows:
        by_artist[artist_id].append({"albumId": aid, "baseKey": _album_group_key(title), "sequel": sequel_marker(title)})
    items = []
    for artist_id, albums in by_artist.items():
        if len(albums) < 2:
            continue
        groups, similar = discography_groups(albums, dismissed)
        if groups or similar:
            items.append({"artistId": artist_id, "name": names[artist_id], "mbid": mbids[artist_id], "scrobbleCount": plays.get(artist_id, 0),
                          "albumCount": len(albums), "groups": len(groups), "versions": sum(len(g) for g in groups),
                          "similar": len(similar)})
    items.sort(key=lambda x: (-x["scrobbleCount"], x["name"]))
    return {"count": len(items), "items": items[: req.int("limit", 300)]}


@route("GET", "/api/albums/discography")
def discography(req):
    artist_id = req.int("artistId", required=True)
    with read_conn() as c:
        artist = c.execute("SELECT id, name, mbid FROM artists WHERE id = ?", (artist_id,)).fetchone()
        if not artist:
            raise ApiError("artist not found", 404)
        ids = [r[0] for r in c.execute("SELECT id FROM albums WHERE artist_id = ?", (artist_id,))]
        profiles = album_profiles(c, ids)
        dismissed = _dismissed_pairs(c)
    albums = sorted(profiles.values(), key=lambda a: (a["baseKey"], a["title"]))
    groups, similar = discography_groups(albums, dismissed)
    return {
        "artist": {"artistId": artist[0], "name": artist[1], "mbid": artist[2],
                   "scrobbleCount": sum(a["scrobbleCount"] for a in albums)},
        "albums": albums,
        "groups": [{"albumIds": g, "primary": default_primary([profiles[i] for i in g])} for g in groups],
        "similar": similar,
    }


@route("GET", "/api/albums/search")
def search(req):
    """Title-first: albums ranked on their own title alone (the artist is a column, not part of
    the match), plus artists by name -- either opens that artist's discography."""
    q = req.str("q")
    if not q:
        return {"albums": [], "artists": []}
    with read_conn() as c:
        rows = c.execute(
            """SELECT al.id, al.title, al.year, al.mbid, ar.id, ar.name,
                      (SELECT count(*) FROM scrobbles sc WHERE sc.album_id = al.id)
               FROM albums al JOIN artists ar ON ar.id = al.artist_id""").fetchall()
        artists = c.execute("SELECT id, name, mbid FROM artists").fetchall()
    meta = {r[0]: r for r in rows}
    hits = process.extract(q, {r[0]: r[1] for r in rows}, scorer=fuzz.WRatio, limit=40, score_cutoff=60, processor=default_process)
    # Equal title scores (e.g. every "Greatest Hits") -> most played first.
    hits.sort(key=lambda h: (-round(h[1]), -meta[h[2]][6]))
    artist_hits = process.extract(q, {r[0]: r[1] for r in artists}, scorer=fuzz.WRatio, limit=5, score_cutoff=80, processor=default_process)
    amb = {r[0]: r[2] for r in artists}
    return {
        "albums": [{"albumId": aid, "title": meta[aid][1], "year": meta[aid][2], "mbid": meta[aid][3], "artistId": meta[aid][4],
                    "artistName": meta[aid][5], "scrobbleCount": meta[aid][6], "score": round(score, 1)} for _t, score, aid in hits[:15]],
        "artists": [{"artistId": aid, "name": name, "mbid": amb[aid], "score": round(score, 1)} for name, score, aid in artist_hits],
    }


@route("POST", "/api/albums/merge-group", mutating=True)
def merge_group(req):
    """Several versions into one primary, in ONE transaction (all or nothing). `identity`
    ({title, year, mbid}, optional) is applied to the primary with the last merge."""
    canonical_id = req.int("canonicalId", required=True)
    absorbed = [int(i) for i in (req.body.get("absorbedIds") or [])]
    if not absorbed or canonical_id in absorbed:
        raise ApiError("absorbedIds must be a non-empty list not containing canonicalId")
    identity = req.body.get("identity") or None
    results = []
    with write_tx() as c:
        for i, aid in enumerate(absorbed):
            results.append(merge.merge_albums(c, aid, canonical_id, identity if i == len(absorbed) - 1 else None))
    return {"canonicalId": canonical_id, "canonicalTitle": results[-1]["canonicalTitle"], "logIds": [r["logId"] for r in results],
            "merged": [{"albumId": r["absorbedId"], "title": r["absorbedTitle"], "rowsMoved": r["rowsMoved"]} for r in results]}


@route("POST", "/api/albums/dismiss-group", mutating=True)
def dismiss_group(req):
    """"These aren't the same record" for every pair among the given albums."""
    ids = sorted({int(i) for i in (req.body.get("albumIds") or [])})
    if len(ids) < 2:
        raise ApiError("albumIds needs at least two albums")
    with write_tx() as c:
        for a, b in combinations(ids, 2):
            c.execute("INSERT INTO duplicate_dismissals (entity_type, entity_id_a, entity_id_b) VALUES ('album', ?, ?) ON CONFLICT DO NOTHING", (a, b))
    return {"dismissed": len(ids) * (len(ids) - 1) // 2}


@route("GET", "/api/albums/compare")
def compare(req):
    a_id, b_id = req.int("a", required=True), req.int("b", required=True)
    with read_conn() as c:
        p = album_profiles(c, [a_id, b_id])
    if a_id not in p or b_id not in p:
        raise ApiError("album not found", 404)
    a, b = p[a_id], p[b_id]
    ka, kb = set(a["trackKeys"]), set(b["trackKeys"])
    union = ka | kb
    return {"a": a, "b": b, "overlap": {
        "tracks": len(ka & kb), "trackOverlapPct": round(100 * len(ka & kb) / len(union)) if union else None,
        "sameBaseTitle": a["baseKey"] == b["baseKey"], "sequelMismatch": a["sequel"] != b["sequel"],
        "distinctMbids": bool(a["mbid"] and b["mbid"] and a["mbid"] != b["mbid"]), "sameArtist": a["artistId"] == b["artistId"]}}


@route("GET", "/api/albums/pairs")
def pairs(req):
    """Library-wide likely-duplicate pairs within each artist, ranked on edition-normalised title
    similarity plus shared tracks -- for the fuzzier cases the discography grouping doesn't
    catch on its own (exact-base-title groups are left to the Discography tab)."""
    min_score = float(req.str("minScore") or 85)
    include_distinct = req.str("includeDistinct") == "1"
    with read_conn() as c:
        rows = c.execute("SELECT al.id, al.title, al.mbid, al.artist_id FROM albums al").fetchall()
        dismissed = _dismissed_pairs(c)
        by_artist = defaultdict(list)
        for aid, title, mbid, artist_id in rows:
            by_artist[artist_id].append((aid, _album_group_key(title), sequel_marker(title), mbid))
        found = []
        for albums in by_artist.values():
            for (a, ka, sa, ma), (b, kb, sb, mb_) in combinations(albums, 2):
                if ka == kb or sa != sb or min(len(ka), len(kb)) < 4 or _pair(a, b) in dismissed:
                    continue
                if ma and mb_ and ma != mb_ and not include_distinct:
                    continue
                score = fuzz.token_set_ratio(ka, kb)
                if score >= min_score:
                    found.append((a, b, score))
        profiles = album_profiles(c, [x for a, b, _ in found for x in (a, b)])
    out = []
    for a, b, score in found:
        pa, pb = profiles[a], profiles[b]
        ka, kb = set(pa["trackKeys"]), set(pb["trackKeys"])
        union = ka | kb
        overlap = round(100 * len(ka & kb) / len(union)) if union else 0
        out.append({"a": pa, "b": pb, "score": round(score, 1), "trackOverlapPct": overlap, "sharedTracks": len(ka & kb),
                    "distinctMbids": bool(pa["mbid"] and pb["mbid"] and pa["mbid"] != pb["mbid"]),
                    "rank": score + overlap * 0.5})
    out.sort(key=lambda x: (-x["rank"], -(x["a"]["scrobbleCount"] + x["b"]["scrobbleCount"])))
    for x in out:  # the grid/compare don't need these -- keep the payload light
        for side in ("a", "b"):
            x[side] = {k: v for k, v in x[side].items() if k != "trackKeys"}
    return {"count": len(out), "pairs": out[: req.int("limit", 150)]}


@route("GET", "/api/albums/mb-discography")
def mb_discography(req):
    """The album artist's real MusicBrainz discography, ranked against this album's title --
    for when a search finds nothing. Release groups already linked to a local album say which."""
    album_id = req.int("albumId", required=True)
    with read_conn() as c:
        row = c.execute("SELECT al.title, ar.mbid, ar.name, ar.id FROM albums al JOIN artists ar ON ar.id = al.artist_id WHERE al.id = ?", (album_id,)).fetchone()
        if not row:
            raise ApiError("album not found", 404)
        extra = sorted(artist_mbid_sets(c, [row[3]]).get(row[3], set()) - {row[1]})
    title, artist_mbid, artist_name, _aid = row
    if not artist_mbid:
        raise ApiError(f"{artist_name} has no MusicBrainz id yet -- resolve the artist first.", 409, "no_artist_mbid")
    try:
        groups, seen = [], set()
        for m in [artist_mbid, *extra]:  # plus "also releases as" discographies
            for g in mbcache.artist_release_groups(m) or []:
                if g["mbid"] not in seen:
                    seen.add(g["mbid"])
                    groups.append(g)
    except requests.RequestException as exc:
        raise ApiError(f"MusicBrainz request failed: {exc}", 502)
    key = _album_group_key(title)
    with read_conn() as c:
        linked = {m: (i, t) for i, t, m in c.execute(
            f"SELECT id, title, mbid FROM albums WHERE mbid IN ({_in(groups)})", [g["mbid"] for g in groups])} if groups else {}
    out = []
    for g in groups:
        out.append({**g, "similarity": round(fuzz.token_set_ratio(key, _album_group_key(g["title"] or "")), 1),
                    "linkedTo": {"albumId": linked[g["mbid"]][0], "title": linked[g["mbid"]][1]} if g["mbid"] in linked else None})
    out.sort(key=lambda g: (-g["similarity"], g["firstReleaseDate"] or "9999"))
    return {"artistName": artist_name, "artistMbid": artist_mbid, "count": len(out), "releaseGroups": out}


@route("GET", "/api/albums/near-matches")
def near_matches(req):
    """Other albums by the same artist whose title is close to this one's -- "is this really a
    version of something I already have?"."""
    album_id = req.int("albumId", required=True)
    with read_conn() as c:
        row = c.execute("SELECT title, artist_id FROM albums WHERE id = ?", (album_id,)).fetchone()
        if not row:
            raise ApiError("album not found", 404)
        others = c.execute("SELECT id, title FROM albums WHERE artist_id = ? AND id != ?", (row[1], album_id)).fetchall()
        key = _album_group_key(row[0])
        scored = [(i, fuzz.token_set_ratio(key, _album_group_key(t))) for i, t in others]
        scored = [(i, s) for i, s in scored if s >= 70]
        scored.sort(key=lambda x: -x[1])
        profiles = album_profiles(c, [i for i, _ in scored[:8]])
    return {"matches": [{**{k: v for k, v in profiles[i].items() if k != "trackKeys"}, "similarity": round(s, 1)} for i, s in scored[:8]]}


@route("GET", "/api/albums/verify")
def verify(req):
    """Flags computed from cached release-group lookups (the album-verify sweep fills the cache)."""
    with read_conn() as c:
        rows = c.execute(
            """SELECT al.id, al.title, al.year, al.mbid, ar.name, ar.mbid,
                      (SELECT count(*) FROM scrobbles sc WHERE sc.album_id = al.id) plays,
                      (SELECT count(*) FROM vinyl_holdings vh WHERE vh.album_id = al.id) vinyl
               FROM albums al JOIN artists ar ON ar.id = al.artist_id
               WHERE al.mbid IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM review_marks m WHERE m.entity_type = 'album' AND m.entity_id = al.id AND m.mark = 'verified:album-mbid')""").fetchall()
        cache = {k[len("lookup:release-group:"):]: json.loads(v) for k, v in c.execute(
            "SELECT key, payload_json FROM mb_cache WHERE key LIKE 'lookup:release-group:%'")}
        # album id -> every MusicBrainz artist id its credited artists count as
        artist_ids = dict(c.execute("SELECT id, artist_id FROM albums WHERE mbid IS NOT NULL").fetchall())
        credits = defaultdict(set)
        for alb, art in c.execute("SELECT aa.album_id, aa.artist_id FROM album_artists aa JOIN albums al ON al.id = aa.album_id WHERE al.mbid IS NOT NULL"):
            credits[alb].add(art)
        sets = artist_mbid_sets(c)
        mbid_sets = {alb: set().union(*(sets.get(a, set()) for a in arts)) for alb, arts in credits.items()}
    flagged, checked = [], 0
    for aid, title, year, mbid, artist_name, artist_mbid, plays, vinyl in rows:
        if mbid not in cache:
            continue
        checked += 1
        rg = cache[mbid]
        flags = []
        if rg is None:
            flags.append({"code": "not_a_release_group", "severity": "high", "text": "This id isn't a release group on MusicBrainz (it may be a release id — try “Audit identity”)."})
        else:
            if artist_mbid and not (mbid_sets.get(aid, {artist_mbid}) & set(rg.get("artistMbids") or [])):
                flags.append({"code": "artist_mismatch", "severity": "high",
                              "text": f"MusicBrainz credits this release group to “{rg.get('artistCredit')}”, not {artist_name}.",
                              "credit": {"mbids": rg.get("artistMbids") or [], "name": rg.get("artistCredit"),
                                         "artistId": artist_ids.get(aid), "artistName": artist_name}})
            sim = fuzz.token_set_ratio(_album_group_key(title), _album_group_key(rg.get("title") or ""))
            if sim < 80:
                flags.append({"code": "title_mismatch", "severity": "medium", "text": f"MusicBrainz title is “{rg.get('title')}”."})
            first = (rg.get("firstReleaseDate") or "")[:4]
            if year and first.isdigit() and int(first) != year:
                flags.append({"code": "year_mismatch", "severity": "medium",
                              "text": f"Year here is {year}, but it was first released in {first} (a reissue's year?)."})
            types = [rg.get("primaryType")] + (rg.get("secondaryTypes") or [])
            odd = [t for t in types if t in ("Live", "Compilation", "Single", "Remix", "Soundtrack", "DJ-mix")]
            local_tags = set(split_title(title)[1])
            if odd and not ("live" in local_tags and "Live" in odd) and "live" not in fold(title):
                flags.append({"code": "type_mismatch", "severity": "low", "text": f"MusicBrainz type is {'/'.join(t for t in types if t)} — check it's not a different release."})
        if flags:
            flagged.append({"albumId": aid, "title": title, "year": year, "mbid": mbid, "artistName": artist_name,
                            "scrobbleCount": plays, "vinylCount": vinyl, "mb": rg, "flags": flags})
    sev = {"high": 0, "medium": 1, "low": 2}
    flagged.sort(key=lambda x: (min(sev[f["severity"]] for f in x["flags"]), -(x["vinylCount"] * 50 + x["scrobbleCount"])))
    return {"checked": checked, "unchecked": len(rows) - checked, "flagged": flagged}


@route("GET", "/api/albums/profile")
def profile(req):
    album_id = req.int("id", required=True)
    with read_conn() as c:
        p = album_profiles(c, [album_id])
    if album_id not in p:
        raise ApiError("album not found", 404)
    return p[album_id]


@route("POST", "/api/albums/edit", mutating=True)
def edit(req):
    """Title/year correction on one album (e.g. set the original release year, drop an edition
    suffix) -- old values kept in edit_log, undoable."""
    album_id = req.int("albumId", required=True)
    changes = {}
    if req.str("title"):
        changes["title"] = req.str("title")
    if req.body.get("year") not in (None, ""):
        changes["year"] = int(req.body["year"])
    if not changes:
        raise ApiError("nothing to change -- send title and/or year")
    with write_tx() as c:
        e = merge.edit_entity(c, "album", album_id, changes, req.str("reason") or "edited")
        title = c.execute("SELECT title FROM albums WHERE id = ?", (album_id,)).fetchone()[0]
    return {"albumId": album_id, "title": title, "editId": e["editId"], "changes": e["changes"]}
