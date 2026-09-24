"""Albums > Editions: converting albums that are still identified by one *edition* (a Last.fm
release id, stored as albums.mbid before the import learned to resolve them) to their release
group -- the album. The edition itself is kept (album_releases), so nothing is lost.

The "album-editions" sweep resolves them (cached for good); this lists the outcome as reviewed
batches:
  * convert    -- the release group is free: albums.mbid becomes it (pre-ticked unless a check fails)
  * duplicates -- several albums turn out to be one release group: merge review
  * invalid    -- MusicBrainz doesn't know the id: clear it, or keep it
Every change is an edit or merge in the activity feed, undoable."""
import json
from collections import defaultdict

from rapidfuzz import fuzz

import merge
from api.core import ApiError, read_conn, route, write_tx
from common import RELEASE_PARENT_KEY, RESOLVE_KEY, credit_mismatch
from titles import base_key

KEEP_MARK = "editions:keep"
LIST_LIMIT = 400

# Albums whose mbid is known to be an edition (a release some scrobble or edition list names),
# minus the ones already confirmed to be a release group, or left as they are on purpose.
CANDIDATES_SQL = f"""
    SELECT al.id, al.title, al.year, al.mbid, al.artist_id, ar.name AS artist_name,
           (SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id) AS plays,
           (SELECT count(*) FROM vinyl_holdings v WHERE v.album_id = al.id) AS vinyl
    FROM albums al JOIN artists ar ON ar.id = al.artist_id
    WHERE al.mbid IS NOT NULL
      AND (EXISTS (SELECT 1 FROM album_releases r WHERE r.release_mbid = al.mbid)
           OR EXISTS (SELECT 1 FROM scrobble_releases r WHERE r.release_mbid = al.mbid))
      AND NOT EXISTS (SELECT 1 FROM mb_cache m WHERE m.key = 'resolve:release-group:' || al.mbid AND m.payload_json = json_quote(al.mbid))
      AND NOT EXISTS (SELECT 1 FROM mb_cache m WHERE m.key = 'lookup:release-group:' || al.mbid AND m.payload_json != 'null')
      AND NOT EXISTS (SELECT 1 FROM review_marks k WHERE k.entity_type = 'album' AND k.entity_id = al.id AND k.mark = '{KEEP_MARK}')
"""


def _cached(c, key):
    row = c.execute("SELECT payload_json FROM mb_cache WHERE key = ?", (key,)).fetchone()
    return (True, json.loads(row[0])) if row else (False, None)


def _album(row) -> dict:
    aid, title, year, mbid, artist_id, artist_name, plays, vinyl = row
    return {"albumId": aid, "title": title, "year": year, "mbid": mbid, "artistId": artist_id, "artistName": artist_name,
            "plays": plays, "vinyl": vinyl}


@route("GET", "/api/albums/editions")
def editions(req):
    with read_conn() as c:
        cands = [_album(r) for r in c.execute(CANDIDATES_SQL)]
        todo, invalid, by_rg = 0, [], defaultdict(list)
        for a in cands:
            found, rg = _cached(c, RESOLVE_KEY.format(a["mbid"]))
            if not found:
                todo += 1
            elif not rg:
                invalid.append(a)
            else:
                a["release"], a["rg"] = a["mbid"], rg
                by_rg[rg].append(a)
        # who already holds each release group
        holders = {}
        rgs = list(by_rg)
        for i in range(0, len(rgs), 500):
            chunk = rgs[i:i + 500]
            for row in c.execute(f"SELECT al.id, al.title, al.year, al.mbid, al.artist_id, ar.name, "
                                 f"(SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id), "
                                 f"(SELECT count(*) FROM vinyl_holdings v WHERE v.album_id = al.id) "
                                 f"FROM albums al JOIN artists ar ON ar.id = al.artist_id WHERE al.mbid IN ({','.join('?' * len(chunk))})", chunk):
                holders[row[3]] = _album(row)

        convert, duplicates = [], []
        for rg, members in by_rg.items():
            _, parent = _cached(c, RELEASE_PARENT_KEY.format(members[0]["release"]))
            parent = parent or {}
            info = {"rg": rg, "rgTitle": parent.get("title"), "rgType": parent.get("primaryType"),
                    "rgSecondary": parent.get("secondaryTypes") or [], "rgFirst": parent.get("firstReleaseDate"),
                    "rgCredit": parent.get("artistCredit")}
            holder = holders.get(rg)
            if holder or len(members) > 1:
                group = ([{**holder, "role": "holder"}] if holder else []) + [{**m, "role": "edition"} for m in members]
                duplicates.append({**info, "members": group, "sameArtist": len({m["artistId"] for m in group}) == 1,
                                   "plays": sum(m["plays"] for m in group)})
                continue
            a = members[0]
            flags = []
            other = credit_mismatch(c, a["artistId"], a["release"])
            if other:
                flags.append(f"MusicBrainz credits it to {other.get('artistCredit') or 'another artist'}")
            if info["rgTitle"] and fuzz.token_set_ratio(base_key(a["title"]), base_key(info["rgTitle"])) < 70:
                flags.append("the titles differ")
            convert.append({**a, **info, "flags": flags, "clean": not flags})
        convert.sort(key=lambda a: (a["clean"], -a["vinyl"], -a["plays"]))
        duplicates.sort(key=lambda g: (not g["sameArtist"], -g["plays"]))
        invalid.sort(key=lambda a: (-a["vinyl"], -a["plays"]))
        return {"counts": {"candidates": len(cands), "toLookUp": todo, "convert": len(convert),
                           "duplicates": len(duplicates), "invalid": len(invalid)},
                "convert": convert[:LIST_LIMIT], "duplicates": duplicates[:LIST_LIMIT // 2], "invalid": invalid[:LIST_LIMIT]}


def _remember_edition(c, album_id: int, release: str) -> None:
    """The edition stays known -- to this album -- before its id is replaced. Written first, so a
    merge moves it with the album (and an undo moves it back)."""
    c.execute("INSERT OR IGNORE INTO album_releases (release_mbid, album_id, source) VALUES (?, ?, 'lastfm')", (release, album_id))


@route("POST", "/api/albums/editions/convert", mutating=True)
def convert(req):
    ids = [int(i) for i in (req.body.get("albumIds") or [])]
    if not ids:
        raise ApiError("albumIds is required")
    edit_ids, skipped = [], []
    with write_tx() as c:
        for aid in ids:
            row = c.execute("SELECT mbid, title FROM albums WHERE id = ?", (aid,)).fetchone()
            if not row or not row[0]:
                skipped.append({"albumId": aid, "reason": "gone or has no MBID now"})
                continue
            found, rg = _cached(c, RESOLVE_KEY.format(row[0]))
            if not found or not rg or rg == row[0]:
                skipped.append({"albumId": aid, "title": row[1], "reason": "nothing to convert"})
                continue
            _remember_edition(c, aid, row[0])
            try:
                edit = merge.edit_entity(c, "album", aid, {"mbid": rg}, "editions: release -> release group")
            except merge.MergeError as exc:
                if exc.code != "mbid_conflict":
                    raise
                skipped.append({"albumId": aid, "title": row[1], "reason": "another album has that release group now — see Duplicates"})
                continue
            if edit["editId"]:
                edit_ids.append(edit["editId"])
    return {"converted": len(edit_ids), "skipped": skipped, "undo": {"kind": "edit", "id": edit_ids} if edit_ids else None}


@route("POST", "/api/albums/editions/merge", mutating=True)
def merge_group(req):
    """One release group, several albums: merge into the chosen primary, which ends up with the
    release group as its mbid; every edition stays listed under it."""
    canonical_id = req.int("canonicalId", required=True)
    absorbed = [int(i) for i in (req.body.get("absorbedIds") or [])]
    rg = req.str("rg")
    if not absorbed or canonical_id in absorbed or not rg:
        raise ApiError("canonicalId, absorbedIds (not containing it) and rg are required")
    log_ids = []
    with write_tx() as c:
        for aid in [canonical_id, *absorbed]:
            row = c.execute("SELECT mbid FROM albums WHERE id = ?", (aid,)).fetchone()
            if not row:
                raise ApiError(f"album {aid} no longer exists — reload", 409)
            if row[0] and row[0] != rg and _cached(c, RESOLVE_KEY.format(row[0]))[1] == rg:
                _remember_edition(c, aid, row[0])
        for i, aid in enumerate(absorbed):
            r = merge.merge_albums(c, aid, canonical_id, {"mbid": rg} if i == len(absorbed) - 1 else None)
            log_ids.append(r["logId"])
        title = c.execute("SELECT title FROM albums WHERE id = ?", (canonical_id,)).fetchone()[0]
    return {"canonicalId": canonical_id, "canonicalTitle": title, "merged": len(absorbed), "undo": {"kind": "merge", "id": log_ids}}


@route("POST", "/api/albums/editions/keep", mutating=True)
def keep(req):
    """Leave these albums as they are (drops them from the Editions lists)."""
    ids = [int(i) for i in (req.body.get("albumIds") or [])]
    if not ids:
        raise ApiError("albumIds is required")
    mark_ids = []
    with write_tx() as c:
        for aid in ids:
            cur = c.execute("INSERT OR IGNORE INTO review_marks (entity_type, entity_id, mark) VALUES ('album', ?, ?)", (aid, KEEP_MARK))
            if cur.rowcount:
                mark_ids.append(cur.lastrowid)
    return {"kept": len(mark_ids), "undo": {"kind": "batch", "id": [{"kind": "mark", "id": i} for i in mark_ids]}}


@route("POST", "/api/albums/editions/clear", mutating=True)
def clear(req):
    """An id MusicBrainz doesn't know: drop it, so the album shows under Missing MBID."""
    ids = [int(i) for i in (req.body.get("albumIds") or [])]
    edit_ids = []
    with write_tx() as c:
        for aid in ids:
            row = c.execute("SELECT mbid FROM albums WHERE id = ?", (aid,)).fetchone()
            if not row or not row[0] or _cached(c, RESOLVE_KEY.format(row[0])) != (True, None):
                continue
            _remember_edition(c, aid, row[0])
            edit = merge.edit_entity(c, "album", aid, {"mbid": None}, "editions: not on MusicBrainz")
            if edit["editId"]:
                edit_ids.append(edit["editId"])
    return {"cleared": len(edit_ids), "undo": {"kind": "edit", "id": edit_ids} if edit_ids else None}
