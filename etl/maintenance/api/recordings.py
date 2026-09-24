"""Songs > Recording ids: sorting the song mbids Last.fm gave us before the importer could tell
its two kinds of id apart. songs.mbid should be a MusicBrainz *recording* (the song); Last.fm's
per-track id is sometimes that, and sometimes a *track* id (one slot on one release) -- which
links nowhere and never matches anything recording-based.

A song's mbid came from some of its scrobbles, so the editions those scrobbles were played from
(scrobble_tracks + scrobble_releases) are exactly the tracklists that can sort it. The
"song-recordings" sweep fetches those tracklists (one request per edition, cached for good),
then asks MusicBrainz directly about whatever's left. This lists the outcome for review:
  * convert    -- a track id (or a since-merged recording id) -> its recording (pre-ticked)
  * duplicates -- several of your songs are one recording: merge review
  * invalid    -- MusicBrainz knows neither kind: clear it, or keep it
Track ids are kept (song_tracks) -- only songs.mbid changes. Every change is undoable."""
import json
from collections import defaultdict

import merge
from api.core import ApiError, read_conn, route, write_tx
from common import SORT_ID_KEY, TITLES_KEY, TRACKS_KEY
from titles import fold

KEEP_MARK = "recording-ids:keep"
LIST_LIMIT = 400


def _maps(c, prefix: str, keys=None) -> dict:
    if keys is not None:
        out = {}
        for k in keys:
            row = c.execute("SELECT payload_json FROM mb_cache WHERE key = ?", (prefix + k,)).fetchone()
            if row:
                out[k] = json.loads(row[0])
        return out
    return {k[len(prefix):]: json.loads(v) for k, v in c.execute(
        "SELECT key, payload_json FROM mb_cache WHERE key LIKE ?", (prefix + "%",))}


def classify(x: str, title: str, rels: list, tracks: dict, titles: dict, direct: dict) -> dict:
    """Sorts one song's stored id `x`, from what's been looked up so far:
      1. the tracklist of an edition it was played from lists it as a track (-> its recording), or
         as a recording;
      2. MusicBrainz, asked directly, knows it as a track or a (possibly since-merged) recording;
      3. it isn't on the edition it was played from (Last.fm paired it with another edition's id,
         or it's stale -- MusicBrainz reissues track ids when a tracklist is edited), but that
         edition has exactly one track with the same title -> that recording (kind "title",
         listed separately for review; saves asking MusicBrainz about the id itself).
    -> {state: confirmed|convert|invalid|pending, recording, kind, release, matchedTitle}"""
    for rel in rels:
        m = tracks.get(rel)
        if m is None:
            continue
        if x in m:
            return {"state": "convert", "recording": m[x], "kind": "track", "release": rel}
        if x in set(m.values()):
            return {"state": "confirmed", "recording": x, "kind": "recording", "release": rel}
    d = direct.get(x)
    if d and d.get("recording"):
        return {"state": "confirmed" if d["recording"] == x else "convert", "recording": d["recording"], "kind": d["kind"], "release": None}
    if rels and all(r in tracks for r in rels):
        want = fold(title)
        for rel in rels:
            hits = {rec: t for rec, t in (titles.get(rel) or {}).items() if fold(t) == want}
            if len(hits) == 1:
                (rec, t), = hits.items()
                return {"state": "convert", "recording": rec, "kind": "title", "release": rel, "matchedTitle": t}
    if d is not None:
        return {"state": "invalid", "recording": None, "kind": None, "release": None}
    return {"state": "pending", "recording": None, "kind": None, "release": None}


def song_states(c) -> dict:
    """Every song with an mbid, sorted as far as what's been looked up allows. -> {
      "songs": {song_id: {songId, title, artistId, artistName, plays, mbid, rels, **classify()}},
      "editionsToFetch": {release: [song ids it would sort]}, "directToAsk": [song ids]}"""
    songs = {}
    for sid, title, mbid, artist_id, artist_name, plays in c.execute(f"""
            SELECT so.id, so.title, so.mbid, ar.id, ar.name, (SELECT count(*) FROM scrobbles s WHERE s.song_id = so.id)
            FROM songs so JOIN artists ar ON ar.id = so.artist_id
            WHERE so.mbid IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM review_marks k WHERE k.entity_type = 'song' AND k.entity_id = so.id AND k.mark = '{KEEP_MARK}')"""):
        songs[sid] = {"songId": sid, "title": title, "mbid": mbid, "artistId": artist_id, "artistName": artist_name,
                      "plays": plays, "rels": []}
    for sid, rel in c.execute("""
            SELECT DISTINCT so.id, r.release_mbid FROM songs so
            JOIN scrobble_tracks t ON t.lastfm_mbid = so.mbid JOIN scrobble_releases r ON r.scrobble_id = t.scrobble_id
            WHERE so.mbid IS NOT NULL"""):
        if sid in songs:
            songs[sid]["rels"].append(rel)
    tracks = _maps(c, TRACKS_KEY.format(""))
    titles = _maps(c, TITLES_KEY.format(""))
    direct = _maps(c, SORT_ID_KEY.format(""))
    to_fetch, to_ask = defaultdict(list), []
    for s in songs.values():
        s.update(classify(s["mbid"], s["title"], s["rels"], tracks, titles, direct))
        if s["state"] == "pending":
            unfetched = [r for r in s["rels"] if r not in tracks]
            if unfetched:
                for r in unfetched:
                    to_fetch[r].append(s["songId"])
            else:  # no edition to sort it by (or it's on none of them): ask about the id itself
                to_ask.append(s["songId"])
    return {"songs": songs, "editionsToFetch": to_fetch, "directToAsk": to_ask}


@route("GET", "/api/songs/recording-ids")
def recording_ids(req):
    with read_conn() as c:
        st = song_states(c)
        songs = st["songs"]
        holders = {}  # recording id -> song already carrying it
        for s in songs.values():
            if s["state"] == "confirmed":
                holders[s["recording"]] = s
        by_rec = defaultdict(list)
        for s in songs.values():
            if s["state"] == "convert":
                by_rec[s["recording"]].append(s)
        # a recording held by a song with no mbid problem at all (e.g. set by hand)
        missing = [r for r in by_rec if r not in holders]
        for i in range(0, len(missing), 500):
            chunk = missing[i:i + 500]
            for sid, mbid in c.execute(f"SELECT id, mbid FROM songs WHERE mbid IN ({','.join('?' * len(chunk))})", chunk):
                if sid in songs:
                    holders[mbid] = songs[sid]
                else:
                    t, aid, an = c.execute("SELECT so.title, ar.id, ar.name FROM songs so JOIN artists ar ON ar.id = so.artist_id "
                                           "WHERE so.id = ?", (sid,)).fetchone()
                    holders[mbid] = {"songId": sid, "title": t, "mbid": mbid, "artistId": aid, "artistName": an,
                                     "plays": c.execute("SELECT count(*) FROM scrobbles WHERE song_id = ?", (sid,)).fetchone()[0]}

        public = lambda s, role: {k: s.get(k) for k in ("songId", "title", "mbid", "artistId", "artistName", "plays", "kind", "release", "matchedTitle")} | {"role": role}  # noqa: E731
        convert, duplicates = [], []
        for rec, members in by_rec.items():
            holder = holders.get(rec)
            if holder or len(members) > 1:
                group = ([public(holder, "holder")] if holder else []) + [public(m, "convert") for m in members]
                duplicates.append({"recording": rec, "members": group, "sameArtist": len({m["artistId"] for m in group}) == 1,
                                   "plays": sum(m["plays"] for m in group)})
            else:
                convert.append(public(members[0], "convert") | {"recording": rec})
        invalid = [public(s, "invalid") for s in songs.values() if s["state"] == "invalid"]
        convert.sort(key=lambda s: (s["kind"] == "title", -s["plays"]))
        duplicates.sort(key=lambda g: (not g["sameArtist"], -g["plays"]))
        invalid.sort(key=lambda s: -s["plays"])
        counts = {"songs": len(songs), "confirmed": sum(s["state"] == "confirmed" for s in songs.values()),
                  "pending": sum(s["state"] == "pending" for s in songs.values()),
                  "editionsToFetch": len(st["editionsToFetch"]), "directToAsk": len(st["directToAsk"]),
                  "convert": sum(s["kind"] != "title" for s in convert), "byTitle": sum(s["kind"] == "title" for s in convert), "duplicates": len(duplicates), "invalid": len(invalid)}
        exact = [s for s in convert if s["kind"] != "title"]
        by_title = [s for s in convert if s["kind"] == "title"]
        return {"counts": counts, "convert": exact[:LIST_LIMIT], "byTitle": by_title[:LIST_LIMIT], "duplicates": duplicates[:LIST_LIMIT // 2], "invalid": invalid[:LIST_LIMIT]}


def _sorted_now(c, song_id: int) -> dict | None:
    """classify() for one song, re-derived at apply time (plus its current mbid as "mbid")."""
    row = c.execute("SELECT mbid, title FROM songs WHERE id = ?", (song_id,)).fetchone()
    if not row or not row[0]:
        return None
    x, title = row
    rels = [r for (r,) in c.execute("SELECT DISTINCT r.release_mbid FROM scrobble_tracks t JOIN scrobble_releases r "
                                    "ON r.scrobble_id = t.scrobble_id WHERE t.lastfm_mbid = ?", (x,))]
    return {"mbid": x, **classify(x, title, rels, _maps(c, TRACKS_KEY.format(""), rels), _maps(c, TITLES_KEY.format(""), rels),
                                  _maps(c, SORT_ID_KEY.format(""), [x]))}


def _remember_track(c, song_id: int, track: str, release: str | None) -> None:
    """The track id stays known -- to this song -- before the song's mbid is replaced. Written
    first, so a merge moves it with the song (and an undo moves it back)."""
    c.execute("INSERT OR IGNORE INTO song_tracks (track_mbid, song_id, release_mbid, source) VALUES (?, ?, ?, 'lastfm')",
              (track, song_id, release))


@route("POST", "/api/songs/recording-ids/convert", mutating=True)
def convert(req):
    ids = [int(i) for i in (req.body.get("songIds") or [])]
    if not ids:
        raise ApiError("songIds is required")
    edit_ids, skipped = [], []
    with write_tx() as c:
        for sid in ids:
            now = _sorted_now(c, sid)
            if not now or now["state"] != "convert":
                skipped.append({"songId": sid, "reason": "nothing to convert"})
                continue
            if now["kind"] == "track":
                _remember_track(c, sid, now["mbid"], now["release"])
            why = {"track": "track -> recording", "recording": "merged recording", "title": "recording by title on the edition played"}
            try:
                edit = merge.edit_entity(c, "song", sid, {"mbid": now["recording"]}, "recording ids: " + why[now["kind"]])
            except merge.MergeError as exc:
                if exc.code != "mbid_conflict":
                    raise
                skipped.append({"songId": sid, "reason": "another song has that recording now — see Duplicates"})
                continue
            if edit["editId"]:
                edit_ids.append(edit["editId"])
    return {"converted": len(edit_ids), "skipped": skipped, "undo": {"kind": "edit", "id": edit_ids} if edit_ids else None}


@route("POST", "/api/songs/recording-ids/merge", mutating=True)
def merge_group(req):
    """One recording, several songs: merge into the chosen one, which ends up with the recording
    id; every track id stays listed under it."""
    canonical_id = req.int("canonicalId", required=True)
    absorbed = [int(i) for i in (req.body.get("absorbedIds") or [])]
    rec = req.str("recording")
    if not absorbed or canonical_id in absorbed or not rec:
        raise ApiError("canonicalId, absorbedIds (not containing it) and recording are required")
    log_ids = []
    with write_tx() as c:
        for sid in [canonical_id, *absorbed]:
            now = _sorted_now(c, sid)
            if now is None and not c.execute("SELECT 1 FROM songs WHERE id = ?", (sid,)).fetchone():
                raise ApiError(f"song {sid} no longer exists — reload", 409)
            if now and now["kind"] == "track" and now["recording"] == rec:
                _remember_track(c, sid, now["mbid"], now["release"])
        for i, sid in enumerate(absorbed):
            r = merge.merge_songs(c, sid, canonical_id, {"mbid": rec} if i == len(absorbed) - 1 else None)
            log_ids.append(r["logId"])
        title = c.execute("SELECT title FROM songs WHERE id = ?", (canonical_id,)).fetchone()[0]
    return {"canonicalId": canonical_id, "canonicalTitle": title, "merged": len(absorbed), "undo": {"kind": "merge", "id": log_ids}}


@route("POST", "/api/songs/recording-ids/keep", mutating=True)
def keep(req):
    ids = [int(i) for i in (req.body.get("songIds") or [])]
    if not ids:
        raise ApiError("songIds is required")
    mark_ids = []
    with write_tx() as c:
        for sid in ids:
            cur = c.execute("INSERT OR IGNORE INTO review_marks (entity_type, entity_id, mark) VALUES ('song', ?, ?)", (sid, KEEP_MARK))
            if cur.rowcount:
                mark_ids.append(cur.lastrowid)
    return {"kept": len(mark_ids), "undo": {"kind": "batch", "id": [{"kind": "mark", "id": i} for i in mark_ids]}}


@route("POST", "/api/songs/recording-ids/clear", mutating=True)
def clear(req):
    """An id MusicBrainz knows as neither a recording nor a track: drop it from the song (the
    scrobbles still keep it, in scrobble_tracks)."""
    ids = [int(i) for i in (req.body.get("songIds") or [])]
    edit_ids = []
    with write_tx() as c:
        for sid in ids:
            now = _sorted_now(c, sid)
            if not now or now["state"] != "invalid":
                continue  # only once MusicBrainz has actually been asked, and knows it as neither
            edit = merge.edit_entity(c, "song", sid, {"mbid": None}, "recording ids: not on MusicBrainz")
            if edit["editId"]:
                edit_ids.append(edit["editId"])
    return {"cleared": len(edit_ids), "undo": {"kind": "edit", "id": edit_ids} if edit_ids else None}
