"""The Import inbox: everything an import run created or couldn't place with certainty
(import_events, written by common.get_or_create_* during a Last.fm / setlist.fm pull), for review.

Kinds: new_artist / new_album / new_song, edition_linked (an album edition attached to an album --
only the by-name ones reach the inbox; exact MusicBrainz links are recorded as already reviewed),
unresolved_release, mbid_clash (the source says this is the same thing as another row), suspect
(detail.reason says why). Reviewing only ever sets reviewed_at; fixes go through the normal merge
engine, so they show in the activity feed and undo like any other merge."""
import json
from collections import defaultdict

from rapidfuzz import fuzz, process

import merge
from api.core import ApiError, read_conn, route, write_tx
from titles import base_key, normalize_artist_name

TABLE = {"artist": "artists", "album": "albums", "song": "songs"}
TABS = {
    "artists": "(e.entity_type = 'artist')",
    "albums": "(e.entity_type = 'album')",
    "songs": "(e.entity_type = 'song')",
    "other": "(e.entity_type IS NULL OR e.entity_type NOT IN ('artist', 'album', 'song'))",
}
LIMIT = 300


def _in(ids) -> str:
    return ",".join("?" * len(ids))


def settle_gone(c) -> None:
    """Items whose artist/album/song has since been merged away or deleted are settled -- whatever
    was done to it was the review."""
    for et, table in TABLE.items():
        c.execute(f"UPDATE import_events SET reviewed_at = datetime('now') WHERE reviewed_at IS NULL AND entity_type = ? "
                  f"AND entity_id IS NOT NULL AND entity_id NOT IN (SELECT id FROM {table})", (et,))


def pending_counts(c) -> dict:
    counts = {tab: 0 for tab in TABS}
    for tab, where in TABS.items():
        counts[tab] = c.execute(f"SELECT count(*) FROM import_events e WHERE e.reviewed_at IS NULL AND {where}").fetchone()[0]
    return counts


@route("GET", "/api/imports/summary")
def summary(req):
    with write_tx() as c:
        settle_gone(c)
    with read_conn() as c:
        runs = c.execute("SELECT r.id, r.source, r.started_at, r.finished_at, r.summary_json, "
                         "(SELECT count(*) FROM import_events e WHERE e.run_id = r.id AND e.reviewed_at IS NULL) "
                         "FROM import_runs r ORDER BY r.id DESC LIMIT 30").fetchall()
        return {"pending": pending_counts(c),
                "runs": [{"id": i, "source": s, "startedAt": a, "finishedAt": f, "summary": json.loads(j) if j else None, "pending": p}
                         for i, s, a, f, j, p in runs]}


# -- Enrichment --------------------------------------------------------------------------------

def _plays(c, col: str, ids) -> dict[int, int]:
    ids = list({i for i in ids if i})
    if not ids:
        return {}
    return dict(c.execute(f"SELECT {col}, count(*) FROM scrobbles WHERE {col} IN ({_in(ids)}) GROUP BY {col}", ids).fetchall())


def _artists(c, ids) -> dict[int, dict]:
    ids = list({i for i in ids if i})
    if not ids:
        return {}
    plays = _plays(c, "artist_id", ids)
    return {i: {"artistId": i, "name": n, "mbid": m, "plays": plays.get(i, 0)}
            for i, n, m in c.execute(f"SELECT id, name, mbid FROM artists WHERE id IN ({_in(ids)})", ids)}


def _albums(c, ids) -> dict[int, dict]:
    ids = list({i for i in ids if i})
    if not ids:
        return {}
    plays = _plays(c, "album_id", ids)
    out = {i: {"albumId": i, "title": t, "year": y, "mbid": m, "artistId": a, "artistName": an, "plays": plays.get(i, 0), "releases": []}
           for i, t, y, m, a, an in c.execute(f"SELECT al.id, al.title, al.year, al.mbid, ar.id, ar.name FROM albums al "
                                               f"JOIN artists ar ON ar.id = al.artist_id WHERE al.id IN ({_in(ids)})", ids)}
    for aid, rel in c.execute(f"SELECT album_id, release_mbid FROM album_releases WHERE album_id IN ({_in(ids)}) ORDER BY id", ids):
        out[aid]["releases"].append(rel)
    return out


def _songs(c, ids) -> dict[int, dict]:
    ids = list({i for i in ids if i})
    if not ids:
        return {}
    plays = _plays(c, "song_id", ids)
    return {i: {"songId": i, "title": t, "mbid": m, "artistId": a, "artistName": an, "albumId": al, "albumTitle": alt, "plays": plays.get(i, 0)}
            for i, t, m, a, an, al, alt in c.execute(
                f"SELECT s.id, s.title, s.mbid, ar.id, ar.name, al.id, al.title FROM songs s JOIN artists ar ON ar.id = s.artist_id "
                f"LEFT JOIN albums al ON al.id = s.album_id WHERE s.id IN ({_in(ids)})", ids)}


PROFILE = {"artist": _artists, "album": _albums, "song": _songs}


def _artist_hints(c, items) -> None:
    """Existing artists with a near-identical name (a new "Motorhead" beside "Motörhead")."""
    new = [it for it in items if it["kind"] == "new_artist" and it["entity"]]
    if not new:
        return
    rows = c.execute("SELECT id, name FROM artists").fetchall()
    ids, names = [r[0] for r in rows], [normalize_artist_name(r[1]) for r in rows]
    hint_ids = defaultdict(list)
    for it in new:
        me = it["entityId"]
        for _, score, idx in process.extract(normalize_artist_name(it["entity"]["name"]), names, scorer=fuzz.ratio, score_cutoff=87, limit=6):
            if ids[idx] != me:
                hint_ids[me].append((ids[idx], round(score)))
    prof = _artists(c, [i for v in hint_ids.values() for i, _ in v])
    for it in new:
        it["hints"] = [{**prof[i], "score": s} for i, s in hint_ids.get(it["entityId"], []) if i in prof]


def _title_hints(c, items, kind, table) -> None:
    """Same artist, same base title (a new "Vol 4 (Remaster)" beside "Vol. 4")."""
    new = [it for it in items if it["kind"] == kind and it["entity"]]
    by_artist = defaultdict(list)
    for it in new:
        by_artist[it["entity"]["artistId"]].append(it)
    hint_ids = {}
    for artist_id, its in by_artist.items():
        rows = c.execute(f"SELECT id, title FROM {table} WHERE artist_id = ?", (artist_id,)).fetchall()
        keyed = defaultdict(list)
        for i, t in rows:
            keyed[base_key(t)].append(i)
        for it in its:
            hint_ids[it["entityId"]] = [i for i in keyed.get(base_key(it["entity"]["title"]), []) if i != it["entityId"]]
    prof = PROFILE[kind.split("_")[1]](c, [i for v in hint_ids.values() for i in v])
    for it in new:
        it["hints"] = [prof[i] for i in hint_ids.get(it["entityId"], []) if i in prof]


def _flag_doubtful_artists(c, items) -> None:
    """A new artist whose imported MBID MusicBrainz itself contradicts (an edition of theirs is
    credited to a different artist) -- Last.fm's artist ids are occasionally just wrong."""
    new = {it["entityId"]: it for it in items if it["kind"] == "new_artist" and it["entity"]}
    if not new:
        return
    for artist_id, detail in c.execute(
            f"SELECT json_extract(detail_json, '$.artistId'), detail_json FROM import_events WHERE kind = 'suspect' AND reviewed_at IS NULL "
            f"AND json_extract(detail_json, '$.reason') = 'release-credited-elsewhere' AND json_extract(detail_json, '$.artistId') IN ({_in(new)})",
            list(new)):
        rg = json.loads(detail).get("releaseGroup") or {}
        new[artist_id]["warning"] = (f"MusicBrainz credits one of their editions to “{rg.get('artistCredit') or 'another artist'}” — "
                                     f"the MBID Last.fm gave this artist may be wrong (see the Check item).")


def _is_clean(it) -> bool:
    """Pre-ticked for bulk accept: nothing here suggests it's anything but what it says."""
    if it["kind"] in ("new_artist", "new_album", "new_song"):
        return not it["hints"] and not it.get("warning")
    if it["kind"] == "edition_linked":
        e = it["entity"]
        return bool(e and e["mbid"] and e["mbid"] == it["detail"].get("releaseGroup"))
    return False


@route("GET", "/api/imports/inbox")
def inbox(req):
    tab = req.str("tab") or "artists"
    if tab not in TABS:
        raise ApiError("unknown tab", 404)
    reviewed = req.str("reviewed") == "1"
    with write_tx() as c:
        settle_gone(c)
    with read_conn() as c:
        rows = c.execute(
            f"SELECT e.id, e.run_id, r.source, e.kind, e.entity_type, e.entity_id, e.detail_json, e.created_at, e.reviewed_at "
            f"FROM import_events e JOIN import_runs r ON r.id = e.run_id "
            f"WHERE {TABS[tab]} AND e.reviewed_at IS {'NOT' if reviewed else ''} NULL ORDER BY e.id DESC LIMIT ?", (LIMIT,)).fetchall()
        items = [{"id": i, "runId": run, "source": src, "kind": k, "entityType": et, "entityId": eid, "detail": json.loads(d or "{}"),
                  "at": at, "reviewedAt": rv, "hints": []} for i, run, src, k, et, eid, d, at, rv in rows]
        # everything each item mentions, profiled in one query per type
        want = defaultdict(set)
        for it in items:
            if it["entityType"] in PROFILE and it["entityId"]:
                want[it["entityType"]].add(it["entityId"])
            d = it["detail"]
            if it["kind"] == "mbid_clash" and d.get("holderId"):
                want[it["entityType"]].add(d["holderId"])
            if d.get("artistId"):
                want["artist"].add(d["artistId"])
            if d.get("albumId"):
                want["album"].add(d["albumId"])
        prof = {t: PROFILE[t](c, ids) for t, ids in want.items()}
        for it in items:
            it["entity"] = prof.get(it["entityType"], {}).get(it["entityId"])
            d = it["detail"]
            if it["kind"] == "mbid_clash":
                it["holder"] = prof.get(it["entityType"], {}).get(d.get("holderId"))
            if d.get("artistId"):
                it["artist"] = prof["artist"].get(d["artistId"])
        _artist_hints(c, items)
        _flag_doubtful_artists(c, items)
        _title_hints(c, items, "new_album", "albums")
        _title_hints(c, items, "new_song", "songs")
        for it in items:
            it["clean"] = _is_clean(it)
        items.sort(key=lambda it: (it["clean"], -it["id"]))  # the ones needing a decision first; bulk-tickable after
        total = c.execute(f"SELECT count(*) FROM import_events e WHERE {TABS[tab]} AND e.reviewed_at IS {'NOT' if reviewed else ''} NULL").fetchone()[0]
        return {"items": items, "total": total, "pending": pending_counts(c)}


# -- Actions -----------------------------------------------------------------------------------

@route("POST", "/api/imports/review", mutating=True)
def review(req):
    ids = [int(i) for i in (req.body.get("ids") or [])]
    if not ids:
        raise ApiError("ids is required")
    with write_tx() as c:
        n = c.execute(f"UPDATE import_events SET reviewed_at = datetime('now') WHERE reviewed_at IS NULL AND id IN ({_in(ids)})", ids).rowcount
    return {"reviewed": n, "undo": {"kind": "inbox", "id": ids}}


def unreview(c, ids) -> int:
    return c.execute(f"UPDATE import_events SET reviewed_at = NULL WHERE id IN ({_in(ids)})", list(ids)).rowcount


@route("POST", "/api/imports/merge", mutating=True)
def merge_into(req):
    """Merge the item's artist/album/song into an existing one (a hint, or an mbid clash's holder),
    and settle the item. Undoable as one batch from the toast."""
    event_id, keep_id = req.int("eventId", required=True), req.int("keepId", required=True)
    with write_tx() as c:
        row = c.execute("SELECT entity_type, entity_id FROM import_events WHERE id = ?", (event_id,)).fetchone()
        if not row or row[0] not in TABLE or not row[1]:
            raise ApiError("that item has nothing to merge", 404)
        entity_type, absorbed_id = row
        if absorbed_id == keep_id:
            raise ApiError("can't merge something into itself")
        fn = {"artist": merge.merge_artists, "album": merge.merge_albums, "song": merge.merge_songs}[entity_type]
        result = fn(c, absorbed_id, keep_id)
        settled = [r[0] for r in c.execute("SELECT id FROM import_events WHERE reviewed_at IS NULL AND entity_type = ? AND entity_id = ?",
                                           (entity_type, absorbed_id))]
        c.execute(f"UPDATE import_events SET reviewed_at = datetime('now') WHERE id IN ({_in(settled)})", settled)
    return {**result, "undo": {"kind": "batch", "id": [{"kind": "merge", "id": result["logId"]}, {"kind": "inbox", "id": settled}]}}
