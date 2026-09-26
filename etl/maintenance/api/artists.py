"""Artist endpoints: missing-mbid queue, MusicBrainz/local search, rich detail + side-by-side
compare, duplicate scan, suggestions, verify flags, assign/merge/dismiss."""
import json
from collections import defaultdict

import numpy as np
import requests
from rapidfuzz import fuzz, process
from rapidfuzz.utils import default_process

import mbcache
import merge
from api.core import ApiError, read_conn, route, write_tx
from common import artist_mbid_sets
from suggest import album_matches, local_album_titles, name_similarity
from titles import base_key, normalize_artist_name

MBID_RE_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

# Duplicate-name scan tuning. token_set_ratio alone can't tell "Bush" vs "Kate Bush"
# (coincidence) from "Bob Marley" vs "Bob Marley & The Wailers" (same act) -- both score 100.
# Every false positive on the real dataset involved a very short name, so the shorter side
# must clear DUPLICATE_MIN_SHORT_LEN -- except when the two names normalise to exactly the
# same thing ("The Beatles"/"Beatles"), which is always worth showing.
DUPLICATE_SCORER = fuzz.token_set_ratio
DUPLICATE_DEFAULT_MIN_SCORE = 92
DUPLICATE_MIN_SHORT_LEN = 8


def _valid_mbid(mbid: str) -> bool:
    import re
    return bool(re.match(MBID_RE_PATTERN, mbid or "", re.IGNORECASE))


def _artist_rows(c) -> list[dict]:
    """Every artist with its activity (outer joins: vinyl-only / live-only artists included),
    open suggestions, "not on MusicBrainz" and "MBID verified" marks, and "also releases as" count."""
    rows = c.execute(
        """
        SELECT ar.id, ar.name, ar.mbid, coalesce(sc.n, 0), coalesce(v.n, 0), coalesce(st.n, 0),
               (SELECT count(*) FROM suggestions s WHERE s.entity_type = 'artist' AND s.entity_id = ar.id AND s.status = 'pending'),
               EXISTS (SELECT 1 FROM review_marks m WHERE m.entity_type = 'artist' AND m.entity_id = ar.id AND m.mark = 'no-mbid'),
               EXISTS (SELECT 1 FROM review_marks m WHERE m.entity_type = 'artist' AND m.entity_id = ar.id AND m.mark = 'verified:artist-mbid'),
               (SELECT count(*) FROM artist_mb_aliases x WHERE x.artist_id = ar.id)
        FROM artists ar
        LEFT JOIN (SELECT artist_id, count(*) n FROM scrobbles GROUP BY artist_id) sc ON sc.artist_id = ar.id
        LEFT JOIN (SELECT aa.artist_id, count(*) n FROM vinyl_holdings vh JOIN album_artists aa ON aa.album_id = vh.album_id
                   GROUP BY aa.artist_id) v ON v.artist_id = ar.id
        LEFT JOIN (SELECT artist_id, count(*) n FROM setlists GROUP BY artist_id) st ON st.artist_id = ar.id
        """
    ).fetchall()
    return [{"artistId": r[0], "name": r[1], "mbid": r[2], "scrobbleCount": r[3], "vinylCount": r[4], "setlistCount": r[5],
             "pendingSuggestions": r[6], "markedNoMbid": bool(r[7]), "verified": bool(r[8]), "aliasCount": r[9]} for r in rows]


def _weight(x: dict) -> int:
    """Queue order: an artist on the shelf matters whatever the play count says, then seen live."""
    return x["vinylCount"] * 50 + x["setlistCount"] * 5 + x["scrobbleCount"]


def _name_search(q: str, pool: list[dict]) -> list[dict]:
    by_id = {x["artistId"]: x for x in pool}
    hits = process.extract(q, {x["artistId"]: x["name"] for x in pool}, scorer=fuzz.WRatio, limit=60, score_cutoff=60,
                           processor=default_process)
    items = [{**by_id[aid], "score": round(score, 1)} for _name, score, aid in hits]
    items.sort(key=lambda x: (-round(x["score"] / 5), -_weight(x)))  # near-equal matches: most-owned/played first
    return items


@route("GET", "/api/artists/missing-mbid")
def missing_mbid(req):
    """The artist queue. Activity counts come from outer joins, so an artist with no scrobbles
    at all (vinyl-only, or only ever seen live) is still listed -- and vinyl weighs most, since an
    artist on the shelf matters whatever the play count says.

      q=<text>        fuzzy name search across ALL artists (ignores minCount); onlyMissing=0 to
                      include artists that already have an mbid (e.g. to correct one)
      id=<artistId>   just that artist (deep links from other pages)
      withVinyl=1     only artists credited on a vinyl holding
      minCount=<n>    minimum scrobbles (0 allowed)"""
    q = req.str("q")
    only_missing = req.str("onlyMissing", "1") != "0"
    with_vinyl = req.str("withVinyl") == "1"
    show_marked = req.str("showMarked") == "1"
    only_id = req.int("id")
    min_count = req.int("minCount", 2)
    with read_conn() as c:
        items = _artist_rows(c)
        total_missing = c.execute("SELECT count(*) FROM artists WHERE mbid IS NULL").fetchone()[0]

    if only_id:
        items = [x for x in items if x["artistId"] == only_id]
    elif q:
        pool = [x for x in items if not only_missing or not x["mbid"]]
        if with_vinyl:
            pool = [x for x in pool if x["vinylCount"]]
        items = _name_search(q, pool)
    else:
        items = [x for x in items if not x["mbid"] and x["scrobbleCount"] >= min_count
                 and (show_marked or not x["markedNoMbid"]) and (not with_vinyl or x["vinylCount"])]
        items.sort(key=lambda x: -_weight(x))
    return {"minCount": min_count, "totalMissing": total_missing, "queueCount": len(items), "rows": items[:500]}


@route("GET", "/api/artists/mapped")
def mapped(req):
    """Artists that already have an MBID -- to review and correct existing mappings. What
    MusicBrainz calls each one is shown when it's cached (a lookup, the verify sweep); opening
    one fetches it (/api/artists/mb-info).
      q=<text>  fuzzy name search      id=<artistId>  just that one (deep links)
      withVinyl=1  only artists on vinyl      unverified=1  hide ones marked "looks right"
    """
    q, only_id = req.str("q"), req.int("id")
    with read_conn() as c:
        items = [x for x in _artist_rows(c) if x["mbid"]]
        info = {k[len("lookup:artist:"):]: json.loads(v) for k, v in c.execute(
            "SELECT key, payload_json FROM mb_cache WHERE key LIKE 'lookup:artist:%'")}
    total = len(items)
    if only_id:
        items = [x for x in items if x["artistId"] == only_id]
    else:
        if req.str("withVinyl") == "1":
            items = [x for x in items if x["vinylCount"]]
        if req.str("unverified") == "1":
            items = [x for x in items if not x["verified"]]
        items = _name_search(q, items) if q else sorted(items, key=lambda x: -_weight(x))
    count = len(items)
    items = items[:300]
    for x in items:
        if x["mbid"] in info:
            mb = info[x["mbid"]]
            x["mb"] = mb or {"missing": True}  # cached None: no such artist on MusicBrainz (any more)
            if mb:
                x["nameDiffers"] = max(name_similarity(x["name"], mb["name"] or ""), name_similarity(x["name"], mb.get("sortName") or "")) < 80
    return {"total": total, "count": count, "rows": items}


@route("GET", "/api/artists/mb-info")
def mb_info(req):
    """What an MBID is on MusicBrainz (cached lookup) -- null when there's no such artist."""
    mbid = req.str("mbid").lower()
    if not _valid_mbid(mbid):
        raise ApiError("mbid doesn't look like a MusicBrainz id")
    try:
        return {"mbid": mbid, "mb": mbcache.artist(mbid)}
    except requests.RequestException as exc:
        raise ApiError(f"MusicBrainz request failed: {exc}", 502)


@route("POST", "/api/artists/clear-mbid", mutating=True)
def clear_mbid(req):
    """Take a wrong MBID off an artist (it goes back to the Missing MBID queue). Undoable."""
    artist_id = req.int("artistId", required=True)
    with write_tx() as c:
        edit = merge.edit_entity(c, "artist", artist_id, {"mbid": None}, req.str("reason") or "cleared")
        _drop_verified(c, artist_id)
        name = c.execute("SELECT name FROM artists WHERE id = ?", (artist_id,)).fetchone()[0]
    return {"artistId": artist_id, "name": name, "editId": edit["editId"]}


def _drop_verified(c, artist_id: int) -> None:
    """A "looks right" was about the old MBID -- the new one hasn't been looked at."""
    c.execute("DELETE FROM review_marks WHERE entity_type = 'artist' AND entity_id = ? AND mark = 'verified:artist-mbid'", (artist_id,))


@route("GET", "/api/artists/mb-search")
def mb_search(req):
    q = req.str("q")
    if not q:
        raise ApiError("q is required")
    try:
        candidates = mbcache.search_artists(q, limit=min(req.int("limit", 10), 25))
    except requests.RequestException as exc:
        raise ApiError(f"MusicBrainz request failed: {exc}", 502)
    with read_conn() as c:
        linked = {r[0]: (r[1], r[2]) for r in c.execute(
            f"SELECT mbid, id, name FROM artists WHERE mbid IN ({','.join('?' * len(candidates))})", [x["mbid"] for x in candidates])} if candidates else {}
    for cand in candidates:
        if cand["mbid"] in linked:
            cand["alreadyLinkedTo"] = {"artistId": linked[cand["mbid"]][0], "name": linked[cand["mbid"]][1]}
    return {"candidates": candidates}


@route("GET", "/api/artists/local-search")
def local_search(req):
    q = req.str("q")
    if not q:
        return {"results": []}
    exclude_id = req.int("excludeId")
    with read_conn() as c:
        rows = c.execute(
            "SELECT ar.id, ar.name, ar.mbid, (SELECT count(*) FROM scrobbles sc WHERE sc.artist_id = ar.id) FROM artists ar"
        ).fetchall()
    choices = {r[0]: r[1] for r in rows if r[0] != exclude_id}
    meta = {r[0]: {"mbid": r[2], "scrobbleCount": r[3]} for r in rows}
    matches = process.extract(q, choices, scorer=fuzz.WRatio, limit=min(req.int("limit", 12), 40), score_cutoff=55, processor=default_process)
    return {"results": [{"artistId": aid, "name": name, "score": round(score, 1), **meta[aid]} for name, score, aid in matches]}


def artist_detail(c, artist_id: int) -> dict:
    row = c.execute("SELECT id, name, mbid FROM artists WHERE id = ?", (artist_id,)).fetchone()
    if not row:
        raise ApiError("artist not found", 404)
    sc = c.execute("SELECT count(*), min(played_at), max(played_at) FROM scrobbles WHERE artist_id = ?", (artist_id,)).fetchone()
    vinyl = c.execute(
        "SELECT count(*) FROM vinyl_holdings vh JOIN album_artists aa ON aa.album_id = vh.album_id WHERE aa.artist_id = ?", (artist_id,)).fetchone()[0]
    setlists = c.execute("SELECT count(*) FROM setlists WHERE artist_id = ?", (artist_id,)).fetchone()[0]
    albums = c.execute("SELECT count(*) FROM albums WHERE artist_id = ?", (artist_id,)).fetchone()[0]
    songs = c.execute("SELECT count(*) FROM songs WHERE artist_id = ?", (artist_id,)).fetchone()[0]
    top_songs = c.execute(
        "SELECT s.title, count(*) n FROM scrobbles sc JOIN songs s ON s.id = sc.song_id WHERE sc.artist_id = ? GROUP BY s.id ORDER BY n DESC LIMIT 5",
        (artist_id,)).fetchall()
    top_albums = c.execute(
        """
        SELECT al.id, al.title, al.year, al.mbid, count(sc.id) n,
               (SELECT count(*) FROM vinyl_holdings vh WHERE vh.album_id = al.id) v
        FROM albums al LEFT JOIN scrobbles sc ON sc.album_id = al.id
        WHERE al.artist_id = ? GROUP BY al.id ORDER BY v * 50 + n DESC LIMIT 5
        """, (artist_id,)).fetchall()
    raw = {
        "lastfm": [r[0] for r in c.execute("SELECT DISTINCT raw_artist_text FROM scrobbles WHERE artist_id = ? LIMIT 6", (artist_id,))],
        "setlistfm": [r[0] for r in c.execute("SELECT DISTINCT raw_artist_text FROM setlists WHERE artist_id = ? LIMIT 6", (artist_id,))],
        "discogs": [r[0] for r in c.execute(
            "SELECT DISTINCT vh.raw_artist_text FROM vinyl_holdings vh JOIN album_artists aa ON aa.album_id = vh.album_id WHERE aa.artist_id = ? LIMIT 6",
            (artist_id,))],
    }
    found, mb = mbcache.peek(f"lookup:artist:{row[2]}") if row[2] else (False, None)
    return {
        "artistId": row[0], "name": row[1], "mbid": row[2], "mb": mb if found else None,
        "scrobbleCount": sc[0], "firstPlayed": sc[1], "lastPlayed": sc[2],
        "vinylCount": vinyl, "setlistCount": setlists, "albumCount": albums, "songCount": songs,
        "topSongs": [{"title": t, "plays": n} for t, n in top_songs],
        "topAlbums": [{"albumId": a, "title": t, "year": y, "mbid": m, "plays": n, "vinyl": v} for a, t, y, m, n, v in top_albums],
        "rawNames": raw,
        "aliases": _aliases(c, artist_id),
    }


@route("GET", "/api/artists/detail")
def detail(req):
    with read_conn() as c:
        return artist_detail(c, req.int("id", required=True))


@route("GET", "/api/artists/compare")
def compare(req):
    a_id, b_id = req.int("a", required=True), req.int("b", required=True)
    with read_conn() as c:
        a, b = artist_detail(c, a_id), artist_detail(c, b_id)
        songs = {aid: {base_key(r[0]): r[0] for r in c.execute("SELECT title FROM songs WHERE artist_id = ?", (aid,))} for aid in (a_id, b_id)}
        albums = {aid: {base_key(r[0]): r[0] for r in c.execute("SELECT title FROM albums WHERE artist_id = ?", (aid,))} for aid in (a_id, b_id)}
    shared_songs = sorted(set(songs[a_id]) & set(songs[b_id]))
    shared_albums = sorted(set(albums[a_id]) & set(albums[b_id]))
    return {
        "a": a, "b": b,
        "overlap": {
            "songs": len(shared_songs), "songTitles": [songs[a_id][k] for k in shared_songs[:12]],
            "albums": len(shared_albums), "albumTitles": [albums[a_id][k] for k in shared_albums[:12]],
            "sameNormalizedName": normalize_artist_name(a["name"]) == normalize_artist_name(b["name"]),
            "distinctMbids": bool(a["mbid"] and b["mbid"] and a["mbid"] != b["mbid"]),
        },
    }


@route("GET", "/api/artists/scrobbles")
def scrobbles(req):
    """The artist's actual scrobbles, newest first, as the source sent them -- for low-volume
    artists, what was played is often the fastest way to tell who they really are."""
    artist_id = req.int("id", required=True)
    limit = min(req.int("limit", 50), 200)
    with read_conn() as c:
        total = c.execute("SELECT count(*) FROM scrobbles WHERE artist_id = ?", (artist_id,)).fetchone()[0]
        rows = c.execute(
            "SELECT played_at, raw_track_text, raw_album_text FROM scrobbles WHERE artist_id = ? ORDER BY played_at DESC LIMIT ?",
            (artist_id, limit)).fetchall()
    return {"artistId": artist_id, "total": total,
            "scrobbles": [{"playedAt": r[0], "track": r[1], "album": r[2]} for r in rows]}


@route("GET", "/api/artists/album-match")
def album_match(req):
    """Does this MusicBrainz artist's discography contain the albums we have for this local
    artist? One cached browse request per candidate mbid -- the same corroboration the
    suggestion sweep uses, on demand for one search result."""
    artist_id, mbid = req.int("artistId", required=True), req.str("mbid").lower()
    if not _valid_mbid(mbid):
        raise ApiError("mbid doesn't look like a MusicBrainz id")
    with read_conn() as c:
        titles = local_album_titles(c, artist_id)
    if not titles:
        return {"albumsChecked": 0, "albumsMatched": [], "releaseGroupCount": None}
    try:
        groups = mbcache.artist_release_groups(mbid)
    except requests.RequestException as exc:
        raise ApiError(f"MusicBrainz request failed: {exc}", 502)
    return {"albumsChecked": len(titles), "albumsMatched": album_matches(titles, groups), "releaseGroupCount": len(groups or [])}


@route("GET", "/api/artists/top-songs")
def top_songs(req):
    artist_id = req.int("id", required=True)
    with read_conn() as c:
        rows = c.execute(
            "SELECT s.id, s.title, count(*) AS cnt FROM scrobbles sc JOIN songs s ON s.id = sc.song_id WHERE sc.artist_id = ? "
            "GROUP BY s.id ORDER BY cnt DESC LIMIT ?", (artist_id, req.int("limit", 5))).fetchall()
    return {"artistId": artist_id, "songs": [{"songId": r[0], "title": r[1], "scrobbleCount": r[2]} for r in rows]}


@route("GET", "/api/artists/duplicate-candidates")
def duplicate_candidates(req):
    try:
        min_score = float(req.str("minScore") or DUPLICATE_DEFAULT_MIN_SCORE)
    except ValueError:
        min_score = DUPLICATE_DEFAULT_MIN_SCORE
    limit = min(req.int("limit", 100), 1000)
    include_distinct = req.str("includeDistinct") == "1"
    with read_conn() as c:
        rows = c.execute(
            "SELECT ar.id, ar.name, ar.mbid, count(sc.id) FROM artists ar LEFT JOIN scrobbles sc ON sc.artist_id = ar.id GROUP BY ar.id").fetchall()
        dismissed = {(a, b) for a, b in c.execute("SELECT entity_id_a, entity_id_b FROM duplicate_dismissals WHERE entity_type = 'artist'")}
    artists = [{"artistId": r[0], "name": r[1], "mbid": r[2], "scrobbleCount": r[3]} for r in rows]
    names = [a["name"] for a in artists]
    n = len(names)

    pairs: dict[tuple, float] = {}
    if n >= 2:
        # cdist computes the full n x n matrix in one optimised batch call -- well under a
        # second for ~3000 artists, vs minutes for a Python double loop.
        matrix = process.cdist(names, names, scorer=DUPLICATE_SCORER, score_cutoff=min_score, workers=-1, processor=default_process)
        lens = np.array([len(x) for x in names])
        short = np.minimum.outer(lens, lens)
        iu = np.triu_indices(n, k=1)
        keep = (matrix[iu] >= min_score) & (short[iu] >= DUPLICATE_MIN_SHORT_LEN)
        for i, j, s in zip(iu[0][keep].tolist(), iu[1][keep].tolist(), matrix[iu][keep].tolist()):
            pairs[(i, j)] = s
    by_norm = defaultdict(list)
    for i, name in enumerate(names):
        by_norm[normalize_artist_name(name)].append(i)
    for idxs in by_norm.values():
        for x in range(len(idxs)):
            for y in range(x + 1, len(idxs)):
                pairs[(idxs[x], idxs[y])] = 100.0

    candidates = []
    for (i, j), score in pairs.items():
        a, b = artists[i], artists[j]
        if (min(a["artistId"], b["artistId"]), max(a["artistId"], b["artistId"])) in dismissed:
            continue
        distinct = bool(a["mbid"] and b["mbid"] and a["mbid"] != b["mbid"])
        if distinct and not include_distinct:
            continue
        same_norm = normalize_artist_name(a["name"]) == normalize_artist_name(b["name"])
        candidates.append({"a": a, "b": b, "score": round(score, 1), "distinctMbids": distinct, "sameNormalizedName": same_norm})
    candidates.sort(key=lambda x: (not x["sameNormalizedName"], x["distinctMbids"], -x["score"], -(x["a"]["scrobbleCount"] + x["b"]["scrobbleCount"])))
    total = len(candidates)
    candidates = candidates[:limit]

    # Song-title overlap: the strongest "same act" signal there is -- but only computed for the
    # pairs actually being returned.
    ids = {x["a"]["artistId"] for x in candidates} | {x["b"]["artistId"] for x in candidates}
    if ids:
        with read_conn() as c:
            song_keys = defaultdict(set)
            for aid, title in c.execute(f"SELECT artist_id, title FROM songs WHERE artist_id IN ({','.join('?' * len(ids))})", list(ids)):
                song_keys[aid].add(base_key(title))
        for x in candidates:
            x["songOverlap"] = len(song_keys[x["a"]["artistId"]] & song_keys[x["b"]["artistId"]])
    return {"minScore": min_score, "candidateCount": total, "candidates": candidates}


@route("POST", "/api/artists/dismiss-duplicate", mutating=True)
def dismiss_duplicate(req):
    a_id, b_id = req.int("aId", required=True), req.int("bId", required=True)
    if a_id == b_id:
        raise ApiError("aId and bId must differ")
    with write_tx() as c:
        c.execute("INSERT INTO duplicate_dismissals (entity_type, entity_id_a, entity_id_b) VALUES ('artist', ?, ?) "
                  "ON CONFLICT DO NOTHING", tuple(sorted((a_id, b_id))))
    return {"dismissed": True}


@route("POST", "/api/artists/assign-mbid", mutating=True)
def assign_mbid(req):
    artist_id, mbid = req.int("artistId", required=True), req.str("mbid").lower()
    if not _valid_mbid(mbid):
        raise ApiError("mbid doesn't look like a MusicBrainz id (expected a UUID)")
    with write_tx() as c:
        try:
            edit = merge.edit_entity(c, "artist", artist_id, {"mbid": mbid}, req.str("reason") or "assigned")
        except merge.MergeError as exc:
            if exc.code == "mbid_conflict":
                cid = exc.conflict["id"]
                raise ApiError(str(exc), 409, "mbid_conflict",
                               conflictingArtist={"artistId": cid, "name": exc.conflict["name"], "mbid": mbid})
            raise
        c.execute("UPDATE suggestions SET status = CASE WHEN mbid = ? THEN 'accepted' ELSE 'rejected' END, decided_at = datetime('now') "
                  "WHERE entity_type = 'artist' AND entity_id = ? AND status = 'pending'", (mbid, artist_id))
        if edit["editId"]:
            _drop_verified(c, artist_id)
        name = c.execute("SELECT name FROM artists WHERE id = ?", (artist_id,)).fetchone()[0]
    return {"artistId": artist_id, "name": name, "mbid": mbid, "editId": edit["editId"]}


@route("POST", "/api/artists/merge", mutating=True)
def merge_artists(req):
    absorbed_id, canonical_id = req.int("absorbedId", required=True), req.int("canonicalId", required=True)
    with write_tx() as c:
        result = merge.merge_artists(c, absorbed_id, canonical_id)
        # The absorbed artist is gone -- settle its open suggestions (the one naming the
        # survivor's mbid was effectively accepted by this merge).
        canonical_mbid = c.execute("SELECT mbid FROM artists WHERE id = ?", (canonical_id,)).fetchone()[0]
        c.execute("UPDATE suggestions SET status = CASE WHEN mbid = ? THEN 'accepted' ELSE 'rejected' END, decided_at = datetime('now') "
                  "WHERE entity_type = 'artist' AND entity_id = ? AND status = 'pending'", (canonical_mbid or "", absorbed_id))
    return result


@route("GET", "/api/artists/suggestions")
def suggestions(req):
    with read_conn() as c:
        rows = c.execute(
            """
            SELECT s.id, s.entity_id, ar.name, s.mbid, s.label, s.confidence, s.tier, s.evidence_json,
                   (SELECT count(*) FROM scrobbles sc WHERE sc.artist_id = ar.id) AS plays
            FROM suggestions s JOIN artists ar ON ar.id = s.entity_id
            WHERE s.entity_type = 'artist' AND s.status = 'pending' AND ar.mbid IS NULL  -- resolved some other way since
            ORDER BY plays DESC, s.confidence DESC
            """
        ).fetchall()
    grouped: dict[int, dict] = {}
    for sid, aid, name, mbid, label, conf, tier, ev, plays in rows:
        g = grouped.setdefault(aid, {"artistId": aid, "name": name, "scrobbleCount": plays, "candidates": []})
        g["candidates"].append({"id": sid, "mbid": mbid, "label": label, "confidence": conf, "tier": tier, "evidence": json.loads(ev)})
    items = list(grouped.values())
    rank = {"high": 0, "medium": 1, "low": 2}
    for g in items:
        g["candidates"].sort(key=lambda x: (rank[x["tier"]], -x["confidence"]))
        g["bestTier"] = g["candidates"][0]["tier"]
    tier = req.str("tier")
    if tier:
        items = [g for g in items if g["bestTier"] == tier]
    return {"count": len(items), "items": items}


@route("GET", "/api/artists/verify")
def verify(req):
    """Flags computed live from whatever the verify sweep has cached -- nothing here fetches."""
    with read_conn() as c:
        artists = c.execute(
            """
            SELECT ar.id, ar.name, ar.mbid, (SELECT count(*) FROM scrobbles sc WHERE sc.artist_id = ar.id) plays
            FROM artists ar WHERE ar.mbid IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM review_marks m WHERE m.entity_type = 'artist' AND m.entity_id = ar.id AND m.mark = 'verified:artist-mbid')
            """).fetchall()
        lookups = {k[len("lookup:artist:"):]: json.loads(v) for k, v in c.execute(
            "SELECT key, payload_json FROM mb_cache WHERE key LIKE 'lookup:artist:%'")}
        browses = {k[len("browse:release-groups:"):]: json.loads(v) for k, v in c.execute(
            "SELECT key, payload_json FROM mb_cache WHERE key LIKE 'browse:release-groups:%'")}
        checked_ids = [a[0] for a in artists if a[2] in lookups]
        alias_sets = artist_mbid_sets(c, checked_ids) if checked_ids else {}
        titles = defaultdict(list)
        if checked_ids:
            for aid, title in c.execute(
                    # every credited album (album_artists includes the primary), not just primary ones
                    f"""SELECT aa.artist_id, al.title FROM album_artists aa JOIN albums al ON al.id = aa.album_id
                        WHERE aa.artist_id IN ({','.join('?' * len(checked_ids))})
                          AND (EXISTS (SELECT 1 FROM vinyl_holdings v WHERE v.album_id = al.id)
                               OR (SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id) >= 5)""", checked_ids):
                titles[aid].append(title)
    flagged, checked = [], 0
    for aid, name, mbid, plays in artists:
        if mbid not in lookups:
            continue
        checked += 1
        info, rgs = lookups[mbid], browses.get(mbid)
        for alias in alias_sets.get(aid, set()) - {mbid}:  # "also releases as" discographies count too
            if browses.get(alias):
                rgs = (rgs or []) + browses[alias]
        flags = []
        if info is None:
            flags.append({"code": "not_found", "severity": "high", "text": "This mbid doesn't exist on MusicBrainz (deleted or merged there)."})
        else:
            sim = max(name_similarity(name, info["name"]), name_similarity(name, info.get("sortName") or ""))
            if sim < 80:
                flags.append({"code": "name_mismatch", "severity": "medium", "text": f'MusicBrainz calls this artist "{info["name"]}".'})
            local = titles.get(aid, [])
            # A 300-group list is browse_release_groups' page cap -- possibly truncated, so an
            # absence from it proves nothing for a prolific artist.
            if rgs is not None and len(local) >= 2 and len(rgs) < 300:
                hits = album_matches(local, rgs)
                if not hits:
                    flags.append({"code": "no_album_match", "severity": "high",
                                  "text": f"None of your {len(local)} albums by them appear in this artist's MusicBrainz discography ({len(rgs)} release groups)."})
        if flags:
            flagged.append({"artistId": aid, "name": name, "mbid": mbid, "scrobbleCount": plays, "mb": info, "flags": flags,
                            "localAlbums": titles.get(aid, [])[:6]})
    flagged.sort(key=lambda x: (-sum(f["severity"] == "high" for f in x["flags"]), -x["scrobbleCount"]))
    return {"checked": checked, "unchecked": len(artists) - checked, "flagged": flagged}


# -- "Also releases as": extra MusicBrainz artist ids that count as this local artist ------------

def _aliases(c, artist_id: int) -> list[dict]:
    return [{"id": r[0], "mbid": r[1], "name": r[2]} for r in c.execute(
        "SELECT id, mbid, name FROM artist_mb_aliases WHERE artist_id = ? ORDER BY name", (artist_id,))]


@route("GET", "/api/artists/mb-aliases")
def mb_aliases(req):
    with read_conn() as c:
        return {"aliases": _aliases(c, req.int("artistId", required=True))}


@route("POST", "/api/artists/mb-alias", mutating=True)
def add_mb_alias(req):
    """"<local artist> also releases as <MusicBrainz artist>" -- e.g. Jimi Hendrix also owns
    The Jimi Hendrix Experience. From then on identity checks, album searches/browsing, song
    lookups and imports treat that credit as this artist. The name is routed for imports too
    (alias_overrides for each source), removed again with the alias."""
    artist_id, mbid = req.int("artistId", required=True), req.str("mbid").lower()
    if not _valid_mbid(mbid):
        raise ApiError("mbid doesn't look like a MusicBrainz id (expected a UUID)")
    name = req.str("name") or None
    if not name:
        try:
            info = mbcache.artist(mbid)
        except requests.RequestException as exc:
            raise ApiError(f"MusicBrainz request failed: {exc}", 502)
        if not info:
            raise ApiError("MusicBrainz has no artist with that id.", 404)
        name = info["name"]
    with write_tx() as c:
        me = c.execute("SELECT name, mbid FROM artists WHERE id = ?", (artist_id,)).fetchone()
        if not me:
            raise ApiError("artist not found", 404)
        if me[1] == mbid:
            raise ApiError(f"That's {me[0]}'s own MBID already.", 409)
        other = c.execute("SELECT id, name FROM artists WHERE mbid = ?", (mbid,)).fetchone()
        if other:
            raise ApiError(f"“{other[1]}” in your library already has that MBID — merge the two artists instead.", 409, "mbid_conflict",
                           conflictingArtist={"artistId": other[0], "name": other[1]})
        taken = c.execute("SELECT a.name FROM artist_mb_aliases x JOIN artists a ON a.id = x.artist_id WHERE x.mbid = ?", (mbid,)).fetchone()
        if taken:
            raise ApiError(f"That MusicBrainz artist already counts as “{taken[0]}”.", 409)
        overrides = []
        for source in ("lastfm", "setlistfm", "discogs"):
            key = name.strip().lower()
            if c.execute("SELECT 1 FROM alias_overrides WHERE source = ? AND source_key = ? AND canonical_type = 'artist'", (source, key)).fetchone():
                continue
            overrides.append(c.execute(
                "INSERT INTO alias_overrides (source, source_key, canonical_type, canonical_id, note) VALUES (?, ?, 'artist', ?, ?)",
                (source, key, artist_id, f"also releases as {name}")).lastrowid)
        alias_id = c.execute("INSERT INTO artist_mb_aliases (artist_id, mbid, name, override_ids) VALUES (?, ?, ?, ?)",
                             (artist_id, mbid, name, json.dumps(overrides))).lastrowid
    return {"id": alias_id, "artistId": artist_id, "artistName": me[0], "mbid": mbid, "name": name}


def remove_mb_alias(c, alias_id: int) -> str | None:
    row = c.execute("SELECT name, override_ids FROM artist_mb_aliases WHERE id = ?", (alias_id,)).fetchone()
    if not row:
        return None
    for oid in json.loads(row[1] or "[]"):
        c.execute("DELETE FROM alias_overrides WHERE id = ?", (oid,))
    c.execute("DELETE FROM artist_mb_aliases WHERE id = ?", (alias_id,))
    return row[0]


@route("POST", "/api/artists/mb-alias-remove", mutating=True)
def mb_alias_remove(req):
    with write_tx() as c:
        name = remove_mb_alias(c, req.int("id", required=True))
    if name is None:
        raise ApiError("alias not found", 404)
    return {"removed": True, "name": name}
