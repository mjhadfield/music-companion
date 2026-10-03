"""The artist workbench: everything outstanding for one artist on one page, and the queue of
artists that have something outstanding. Read-only -- every change goes through api/batch.py.

What counts as outstanding:
  albums   versions of one record still separate (discography groups), albums with no
           MusicBrainz id, Verify flags on ones that have one (wrong artist / title / year...), and
           singles filed as albums (singles.py) to fold into the album they came from
  songs    duplicate groups and near-matches (dupes.py), and placement: a song filed under an
           album that doesn't credit its artist, or under a compilation when it's also been
           played from a studio album of the artist's
  live     live songs still to settle (the Live sets tab does that work; summarised here)

An artist marked done (review mark "workbench-reviewed:<signature>") leaves the queue until
something new turns up -- the signature is a hash of exactly what was outstanding.
"""
import hashlib
import json
import re
from collections import defaultdict

import dupes
import singles
from api.albums import _dismissed_pairs, album_profiles, default_primary, discography_groups
from api.albums import verify as album_verify
from api.core import ApiError, Req, read_conn, route
from api.songs import DONE, TODO, _in, live_pairs
from titles import split_title

COMPILATION_RE = re.compile(r"\b(greatest hits|best of|the very best|the collection|collection|anthology|essential|definitive|"
                            r"ultimate|gold|singles|hits|retrospective)\b", re.I)
DONE_MARK = "workbench-reviewed:"
PLACEMENT_OK = "placement-ok"


def _compilations(c) -> set[int]:
    """Albums that are compilations: MusicBrainz says so (cached release group), or the title does."""
    comp = set()
    rg_types = {}
    for key, payload in c.execute("SELECT key, payload_json FROM mb_cache WHERE key LIKE 'lookup:release-group:%'"):
        rg = json.loads(payload)
        if rg:
            rg_types[key.split(":", 2)[2]] = rg.get("secondaryTypes") or []
    for aid, title, mbid in c.execute("SELECT id, title, mbid FROM albums"):
        if (mbid and "Compilation" in rg_types.get(mbid, [])) or COMPILATION_RE.search(title or ""):
            comp.add(aid)
    return comp


def placement_issues(c, artist_ids: list[int]) -> dict[int, list[dict]]:
    """artist -> songs filed under the wrong album, each with the album proposed instead."""
    if not artist_ids:
        return {}
    comp = _compilations(c)
    credits = defaultdict(set)
    for album_id, artist_id in c.execute("SELECT album_id, artist_id FROM album_artists"):
        credits[album_id].add(artist_id)
    titles = dict(c.execute("SELECT id, title FROM albums").fetchall())
    ok = {r[0] for r in c.execute("SELECT entity_id FROM review_marks WHERE entity_type = 'song' AND mark = ?", (PLACEMENT_OK,))}
    plays_from = defaultdict(list)
    for sid, album_id, n in c.execute(
            f"""SELECT sc.song_id, sc.album_id, count(*) FROM scrobbles sc JOIN songs s ON s.id = sc.song_id
                WHERE s.artist_id IN ({_in(artist_ids)}) AND sc.album_id IS NOT NULL GROUP BY 1, 2 ORDER BY 3 DESC""", artist_ids):
        plays_from[sid].append((album_id, n))
    out = defaultdict(list)
    for sid, title, artist_id, album_id in c.execute(
            f"SELECT id, title, artist_id, album_id FROM songs WHERE artist_id IN ({_in(artist_ids)}) AND album_id IS NOT NULL", artist_ids):
        if sid in ok:
            continue
        candidates = [(a, n) for a, n in plays_from[sid] if artist_id in credits[a] and a != album_id]
        reason = None
        if artist_id not in credits[album_id]:
            reason = "uncredited"
        elif album_id in comp:
            candidates = [(a, n) for a, n in candidates if a not in comp]
            if candidates:
                reason = "compilation"
        if not reason:
            continue
        best = candidates[0] if candidates else None
        out[artist_id].append({"key": f"pl:{sid}", "songId": sid, "title": title, "reason": reason,
                               "current": {"albumId": album_id, "title": titles.get(album_id)},
                               "suggested": {"albumId": best[0], "title": titles.get(best[0]), "plays": best[1]} if best else None,
                               "plays": sum(n for _a, n in plays_from[sid])})
    return out


def _album_rows(c, artist_ids: list[int]) -> dict[int, list[int]]:
    """artist -> its album ids (as the album's own artist)."""
    out = defaultdict(list)
    for aid, artist_id in c.execute(f"SELECT id, artist_id FROM albums WHERE artist_id IN ({_in(artist_ids)})", artist_ids):
        out[artist_id].append(aid)
    return out


def _missing_mbid(c, artist_ids: list[int]) -> dict[int, list[dict]]:
    out = defaultdict(list)
    for aid, artist_id, title, plays, vinyl in c.execute(
            f"""SELECT al.id, al.artist_id, al.title,
                       (SELECT count(*) FROM scrobbles sc WHERE sc.album_id = al.id),
                       (SELECT count(*) FROM vinyl_holdings vh WHERE vh.album_id = al.id)
                FROM albums al WHERE al.artist_id IN ({_in(artist_ids)}) AND al.mbid IS NULL
                  AND NOT EXISTS (SELECT 1 FROM album_parts ap WHERE ap.album_id = al.id)
                  AND NOT EXISTS (SELECT 1 FROM review_marks m WHERE m.entity_type = 'album' AND m.entity_id = al.id AND m.mark = 'no-mbid')""", artist_ids):
        out[artist_id].append({"key": f"mb:{aid}", "albumId": aid, "title": title, "plays": plays, "vinyl": vinyl})
    for v in out.values():
        v.sort(key=lambda a: -(a["vinyl"] * 50 + a["plays"]))
    return out


def _album_suggestions(c, album_ids: list[int]) -> dict[int, list[dict]]:
    out = defaultdict(list)
    if not album_ids:
        return out
    for sid, aid, mbid, label, conf, tier, ev in c.execute(
            f"""SELECT id, entity_id, mbid, label, confidence, tier, evidence_json FROM suggestions
                WHERE entity_type = 'album' AND status = 'pending' AND entity_id IN ({_in(album_ids)}) ORDER BY confidence DESC""", album_ids):
        out[aid].append({"suggestionId": sid, "mbid": mbid, "label": label, "confidence": conf, "tier": tier, "evidence": json.loads(ev or "{}")})
    return out


def outstanding(c, artist_ids: list[int], *, with_flags: dict | None = None) -> dict[int, dict]:
    """artist -> everything outstanding, as keyed items (the keys make the done-signature)."""
    albums_of = _album_rows(c, artist_ids)
    dismissed = _dismissed_pairs(c)
    profiles = album_profiles(c, [a for ids in albums_of.values() for a in ids])
    song_groups = defaultdict(list)
    for g in dupes.review_groups(c, artist_ids):
        song_groups[g["artistId"]].append(g)
    placement = placement_issues(c, artist_ids)
    missing = _missing_mbid(c, artist_ids)
    singles_of = defaultdict(list)
    for x in singles.find_singles(c, artist_ids):
        singles_of[x["artistId"]].append(x)
    out = {}
    for artist_id in artist_ids:
        albums = [profiles[a] for a in albums_of.get(artist_id, []) if a in profiles]
        groups, similar = discography_groups(albums, dismissed) if len(albums) > 1 else ([], {})
        album_groups = [{"key": "ag:" + ",".join(map(str, sorted(g))), "albumIds": g, "primary": default_primary([profiles[i] for i in g])}
                        for g in groups]
        flags = [f for f in (with_flags or {}).get(artist_id, [])]
        out[artist_id] = {"albumGroups": album_groups, "similar": similar, "missing": missing.get(artist_id, []), "flags": flags,
                          "songGroups": song_groups.get(artist_id, []), "placement": placement.get(artist_id, []),
                          "singles": singles_of.get(artist_id, [])}
    return out


def signature(items: dict) -> str:
    keys = sorted([g["key"] for g in items["albumGroups"]] + [m["key"] for m in items["missing"]] +
                  [f"fl:{f['albumId']}:{','.join(sorted(x['code'] for x in f['flags']))}" for f in items["flags"]] +
                  [g["key"] for g in items["songGroups"]] + [p["key"] for p in items["placement"]] +
                  [f"{x['key']}:{x['target']['albumId']}" for x in items["singles"]])
    return hashlib.sha1("|".join(keys).encode()).hexdigest()[:16]


def _flags_by_artist(c) -> dict[int, list[dict]]:
    """Verify flags (Albums > Verify) grouped by the album's artist."""
    flagged = album_verify(Req({}, {}))["flagged"]
    owner = dict(c.execute("SELECT id, artist_id FROM albums WHERE mbid IS NOT NULL").fetchall())
    out = defaultdict(list)
    for f in flagged:
        if f["albumId"] in owner:
            out[owner[f["albumId"]]].append({"albumId": f["albumId"], "title": f["title"], "mbid": f["mbid"], "flags": f["flags"], "mb": f["mb"]})
    return out


def _counts(items: dict) -> dict:
    return {"albumGroups": len(items["albumGroups"]), "missing": len(items["missing"]), "flags": len(items["flags"]),
            "songEdition": sum(g["kind"] == "edition" for g in items["songGroups"]),
            "songVariant": sum(g["kind"] == "variant" for g in items["songGroups"]),
            "songPossible": sum(g["kind"] == "possible" for g in items["songGroups"]),
            "placement": len(items["placement"]), "singles": len(items["singles"])}


@route("GET", "/api/workbench")
def workbench(req):
    artist_id = req.int("artistId", required=True)
    with read_conn() as c:
        artist = c.execute("SELECT id, name, mbid FROM artists WHERE id = ?", (artist_id,)).fetchone()
        if not artist:
            raise ApiError("artist not found", 404)
        flags = _flags_by_artist(c)
        items = outstanding(c, [artist_id], with_flags=flags)[artist_id]
        album_ids = sorted({i for g in items["albumGroups"] for i in g["albumIds"]} | {i for s in items["similar"].values() for i in s}
                           | set(map(int, items["similar"])) | {m["albumId"] for m in items["missing"]} | {f["albumId"] for f in items["flags"]})
        profiles = album_profiles(c, album_ids)
        for p in profiles.values():
            p.pop("trackKeys", None)
        suggestions = _album_suggestions(c, [m["albumId"] for m in items["missing"]])
        for m in items["missing"]:
            m["suggestions"] = suggestions.get(m["albumId"], [])
        rows, _prof, statuses = live_pairs(c, [artist_id])
        st = [v["status"] for (p, _sid), v in statuses.items() if p == artist_id]
        plays = c.execute("SELECT count(*) FROM scrobbles WHERE artist_id = ?", (artist_id,)).fetchone()[0]
        sig = signature(items)
        done = c.execute("SELECT 1 FROM review_marks WHERE entity_type = 'artist' AND entity_id = ? AND mark = ?",
                         (artist_id, DONE_MARK + sig)).fetchone() is not None
        batches = [{"batchId": r[0], "label": r[1], "at": r[2], "undoneAt": r[3], **json.loads(r[4] or "{}")} for r in c.execute(
            "SELECT id, label, created_at, undone_at, summary_json FROM batches WHERE json_extract(summary_json, '$.artistId') = ? ORDER BY id DESC LIMIT 15",
            (artist_id,))]
    return {"artist": {"artistId": artist[0], "name": artist[1], "mbid": artist[2], "plays": plays},
            "items": items, "albums": profiles, "counts": _counts(items), "signature": sig, "done": done, "doneMark": DONE_MARK + sig,
            "live": {"songs": len(st), "todo": sum(x in TODO for x in st), "done": sum(x in DONE for x in st), "unchecked": st.count("unchecked")},
            "batches": batches}


@route("GET", "/api/workbench/queue")
def queue(req):
    """Artists with something outstanding, most-played first -- minus ones marked done whose
    outstanding set hasn't changed since."""
    with read_conn() as c:
        plays = dict(c.execute("SELECT artist_id, count(*) FROM scrobbles GROUP BY artist_id").fetchall())
        names = dict(c.execute("SELECT id, name FROM artists").fetchall())
        artist_ids = [a for a in names if plays.get(a) or c.execute("SELECT 1 FROM albums WHERE artist_id = ? LIMIT 1", (a,)).fetchone()]
        flags = _flags_by_artist(c)
        all_items = outstanding(c, artist_ids, with_flags=flags)
        marks = {(r[0], r[1]) for r in c.execute("SELECT entity_id, mark FROM review_marks WHERE entity_type = 'artist' AND mark LIKE ?", (DONE_MARK + "%",))}
    rows = []
    for artist_id, items in all_items.items():
        counts = _counts(items)
        total = sum(counts.values())
        if not total or (artist_id, DONE_MARK + signature(items)) in marks:
            continue
        rows.append({"artistId": artist_id, "name": names.get(artist_id), "plays": plays.get(artist_id, 0), "counts": counts, "total": total})
    rows.sort(key=lambda r: (-r["plays"], r["name"] or ""))
    return {"count": len(rows), "items": rows[: req.int("limit", 400)],
            "totals": {k: sum(r["counts"][k] for r in rows) for k in ("albumGroups", "singles", "missing", "flags", "songEdition", "songVariant", "songPossible", "placement")}}


@route("GET", "/api/search")
def search(req):
    """One box for everything: artists, albums and songs by name -- each opens in the workbench."""
    from rapidfuzz import fuzz, process
    from rapidfuzz.utils import default_process
    q = req.str("q")
    if len(q) < 2:
        return {"artists": [], "albums": [], "songs": []}
    with read_conn() as c:
        artists = dict(c.execute("SELECT id, name FROM artists").fetchall())
        albums = {r[0]: r for r in c.execute("SELECT al.id, al.title, al.artist_id FROM albums al")}
        hit_artists = process.extract(q, artists, scorer=fuzz.WRatio, limit=6, score_cutoff=75, processor=default_process)
        hit_albums = process.extract(q, {k: v[1] for k, v in albums.items()}, scorer=fuzz.WRatio, limit=8, score_cutoff=80, processor=default_process)
        songs = {r[0]: r for r in c.execute("SELECT id, title, artist_id FROM songs WHERE title LIKE ? LIMIT 4000", (f"%{q.split()[0]}%",))}
        hit_songs = process.extract(q, {k: v[1] for k, v in songs.items()}, scorer=fuzz.WRatio, limit=8, score_cutoff=80, processor=default_process)
    return {"artists": [{"artistId": i, "name": n, "score": round(s)} for n, s, i in hit_artists],
            "albums": [{"albumId": i, "title": t, "artistId": albums[i][2], "artistName": artists.get(albums[i][2]), "score": round(s)} for t, s, i in hit_albums],
            "songs": [{"songId": i, "title": t, "artistId": songs[i][2], "artistName": artists.get(songs[i][2]), "score": round(s)} for t, s, i in hit_songs]}


@route("GET", "/api/songs/review-queue")
def song_review_queue(req):
    """Every open song duplicate group across the library (dupes.review_groups), most played first,
    filtered by kind (edition / variant / possible) and optionally to the ones that split a song's
    live shows from its plays. Paged: offset + limit."""
    kind = req.str("kind") or "all"
    if kind not in ("all", "edition", "variant", "possible"):
        raise ApiError("kind must be all, edition, variant or possible")
    split_only = req.str("split") == "1"
    q = req.str("q").lower()
    offset, limit = max(0, req.int("offset", 0)), min(200, max(1, req.int("limit", 40)))
    with read_conn() as c:
        names = dict(c.execute("SELECT id, name FROM artists").fetchall())
        groups = dupes.review_groups(c)
    if q:
        groups = [g for g in groups if q in (names.get(g["artistId"]) or "").lower()]
    counts = {k: sum(g["kind"] == k for g in groups) for k in ("edition", "variant", "possible")}
    counts["split"] = sum(g["split"] for g in groups if kind == "all" or g["kind"] == kind)   # within the kind shown
    picked = [g for g in groups if (kind == "all" or g["kind"] == kind) and (not split_only or g["split"])]
    for g in picked[offset:offset + limit]:
        g["artistName"] = names.get(g["artistId"])
    return {"counts": counts, "total": len(picked), "offset": offset, "items": picked[offset:offset + limit]}


@route("GET", "/api/albums/suggestions")
def album_suggestions(req):
    """Albums with album-id suggestions waiting (the album-suggest sweep), best tier first, then
    owned and most played. Filter by the best suggestion's tier."""
    tier = req.str("tier")
    with read_conn() as c:
        rows = c.execute("""
            SELECT al.id, al.title, al.artist_id, ar.name, ar.mbid,
                   (SELECT count(*) FROM scrobbles sc WHERE sc.album_id = al.id),
                   (SELECT count(*) FROM vinyl_holdings vh WHERE vh.album_id = al.id)
            FROM albums al JOIN artists ar ON ar.id = al.artist_id
            WHERE al.mbid IS NULL AND EXISTS (SELECT 1 FROM suggestions s WHERE s.entity_type = 'album' AND s.entity_id = al.id AND s.status = 'pending')
              AND NOT EXISTS (SELECT 1 FROM review_marks m WHERE m.entity_type = 'album' AND m.entity_id = al.id AND m.mark = 'no-mbid')""").fetchall()
        sugg = _album_suggestions(c, [r[0] for r in rows])
    rank = {"high": 0, "medium": 1, "low": 2}
    items = []
    for aid, title, artist_id, artist, artist_mbid, plays, vinyl in rows:
        s = sorted(sugg.get(aid, []), key=lambda x: (rank[x["tier"]], -x["confidence"]))
        if not s:
            continue
        items.append({"albumId": aid, "title": title, "baseTitle": split_title(title)[0], "artistId": artist_id, "artistName": artist, "artistMbid": artist_mbid,
                      "plays": plays, "vinyl": vinyl, "best": s[0]["tier"], "suggestions": s})
    counts = {t: sum(i["best"] == t for i in items) for t in rank}
    if tier in rank:
        items = [i for i in items if i["best"] == tier]
    items.sort(key=lambda i: (rank[i["best"]], -(i["vinyl"] * 50 + i["plays"])))
    return {"counts": counts, "count": len(items), "items": items[:500]}


@route("GET", "/api/albums/singles")
def album_singles(req):
    """Singles filed as albums, across the library (singles.py): high first, then most played."""
    tier = req.str("tier")
    with read_conn() as c:
        names = dict(c.execute("SELECT id, name FROM artists").fetchall())
        items = singles.find_singles(c)
    counts = {t: sum(x["tier"] == t for x in items) for t in ("high", "review")}
    if tier in counts:
        items = [x for x in items if x["tier"] == tier]
    for x in items:
        x["artistName"] = names.get(x["artistId"])
    return {"counts": counts, "count": len(items), "items": items}


@route("GET", "/api/artists/links")
def artist_links(req):
    """The artists linked to one artist (merge.link_artists), for the Workbench / Live sets header."""
    import merge
    artist_id = req.int("artistId", required=True)
    with read_conn() as c:
        ids = merge.linked_artists(c, artist_id)
        names = dict(c.execute(f"SELECT id, name FROM artists WHERE id IN ({_in(ids)})", ids).fetchall()) if ids else {}
    return {"links": [{"artistId": a, "name": names.get(a)} for a in ids]}
