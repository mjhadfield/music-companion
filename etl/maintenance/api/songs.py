"""Songs: tie every live performance to its studio song/album, then fold duplicate versions of
the same song together -- artist by artist.

A "song" row is one composition per artist (get_or_create_song keys on artist + title), so
duplicates are *spelling* variants: "Paranoid - 2009 Remaster", a setlist's plain "Paranoid",
"Paranoid - Live at ...". titles.base_key() strips edition/variant suffixes to group them.

Live statuses are per (performer, song): one song row is shared by every show it was played at
(fixing it once fixes it everywhere), but a *cover* depends on who played it -- setlist.fm files
Anthrax's "Antisocial" under Trust, while the version that matters is Anthrax's own recording.
  ok        it has an album (and, for a cover, the band has no recording of its own to prefer)
  marked    a human settled it: no album (solo / jam / intro / medley / live only / ...)
  match     a studio song with the same (or a very close) title -> merge it, or for a cover,
            re-link this band's plays to the band's own recording
  nonsong   the title looks like a solo/jam/intro/medley -> mark it
  album     MusicBrainz: it's on an album you have -> link it (review first)
  newalbum  MusicBrainz: it's on album(s) you don't have yet -> add the album and link it (review
            first; the album stays unplayed until you scrobble it, then imports land on it)
  lookedup  MusicBrainz knows no album with it at all -> confirm "live only" (review first)
  unchecked not looked up yet
Every suggestion can be rejected; rejections are remembered as review marks on the song
("not-match:<songId>", "reject-rg:<mbid>", "cover-original:<performerId>").
"""
import json
import re
from collections import defaultdict
from itertools import combinations

from rapidfuzz import fuzz, process
from rapidfuzz.utils import default_process

import requests

import mbcache
import merge
from api.core import ApiError, read_conn, route, write_tx
from common import artist_mbid_sets
from titles import base_key, sequel_marker, split_title

NONSONG_RULES = [  # checked in order; the first hit names the kind
    ("medley", re.compile(r"\bmedley\b|\s/\s", re.I)),
    ("solo", re.compile(r"\bsolo\b", re.I)),
    ("jam", re.compile(r"\bjam\b|\bimprov|\bdoodle|\bnoodl|\bad[ -]?lib", re.I)),
    ("intro", re.compile(r"\bintro\b|\boutro\b|\btape\b|\binterlude\b|\bprelude\b", re.I)),
    ("snippet", re.compile(r"\btease\b|\bsnippet\b|\breprise\b", re.I)),
    ("speech", re.compile(r"\bspeech\b|\bintroduction\b", re.I)),
]
NO_ALBUM_KINDS = {"solo": "Solo", "jam": "Jam / doodle / improv", "intro": "Intro / tape / interlude", "medley": "Medley",
                  "snippet": "Snippet / tease", "speech": "Speech", "other": "Live only"}
DONE = {"ok", "marked"}
TODO = {"match", "nonsong", "album", "newalbum", "lookedup"}
MAX_CANDIDATES = 12


def _in(ids) -> str:
    return ",".join("?" * len(ids))


def nonsong_kind(title: str) -> str | None:
    for kind, rx in NONSONG_RULES:
        if rx.search(title or ""):
            return kind
    return None


def song_profiles(c, ids) -> dict[int, dict]:
    ids = list(dict.fromkeys(int(i) for i in ids))
    if not ids:
        return {}
    out = {}
    for i in range(0, len(ids), 800):
        chunk = ids[i:i + 800]
        for sid, title, artist_id, artist_name, artist_mbid, mbid, album_id, al_title, al_year, al_cover, al_mbid in c.execute(
                f"""SELECT s.id, s.title, s.artist_id, ar.name, ar.mbid, s.mbid, s.album_id, al.title, al.year, al.cover_status, al.mbid
                    FROM songs s JOIN artists ar ON ar.id = s.artist_id LEFT JOIN albums al ON al.id = s.album_id
                    WHERE s.id IN ({_in(chunk)})""", chunk):
            base, tags = split_title(title)
            out[sid] = {"songId": sid, "title": title, "baseTitle": base, "baseKey": base_key(title), "tags": sorted(tags - {"edition"}),
                        "edition": "edition" in tags, "sequel": sequel_marker(title),
                        "artistId": artist_id, "artistName": artist_name, "artistMbid": artist_mbid, "mbid": mbid,
                        "albumId": album_id, "albumTitle": al_title, "albumYear": al_year, "albumCover": al_cover, "albumMbid": al_mbid,
                        "scrobbleCount": 0, "firstPlayed": None, "lastPlayed": None, "setlistCount": 0, "scrobbleAlbums": [],
                        "mark": None, "marks": set()}
        for sid, n, first, last in c.execute(
                f"SELECT song_id, count(*), min(played_at), max(played_at) FROM scrobbles WHERE song_id IN ({_in(chunk)}) GROUP BY song_id", chunk):
            out[sid].update(scrobbleCount=n, firstPlayed=first, lastPlayed=last)
        for sid, n in c.execute(f"SELECT song_id, count(*) FROM setlist_songs WHERE song_id IN ({_in(chunk)}) GROUP BY song_id", chunk):
            out[sid]["setlistCount"] = n
        for sid, album_id, title, n in c.execute(
                f"""SELECT sc.song_id, sc.album_id, al.title, count(*) FROM scrobbles sc JOIN albums al ON al.id = sc.album_id
                    WHERE sc.song_id IN ({_in(chunk)}) GROUP BY sc.song_id, sc.album_id ORDER BY count(*) DESC""", chunk):
            out[sid]["scrobbleAlbums"].append({"albumId": album_id, "title": title, "count": n})
        for mid, sid, mark in c.execute(f"SELECT id, entity_id, mark FROM review_marks WHERE entity_type = 'song' AND entity_id IN ({_in(chunk)})", chunk):
            out[sid]["marks"].add(mark)
            if mark.startswith("no-album:"):
                out[sid]["mark"] = {"id": mid, "kind": mark.split(":", 1)[1]}
    for v in out.values():
        v["marks"] = sorted(v["marks"])  # JSON-safe; membership tests work the same
    return out


def _best_target(cands: list[dict]) -> dict:
    """The song a variant should fold into: a plain title (no live/remix/... tags, no edition
    suffix) with an album, then most played."""
    return max(cands, key=lambda x: (not x["tags"], not x["edition"], bool(x["albumId"]), x["setlistCount"] > 0, x["scrobbleCount"]))


def _brief(p: dict) -> dict:
    return {k: p[k] for k in ("songId", "title", "albumId", "albumTitle", "albumYear", "scrobbleCount", "setlistCount", "mbid",
                              "edition", "tags", "baseTitle", "artistId", "artistName")}


def _find_match(s: dict, cands: list[dict]) -> tuple[dict, bool, float] | None:
    """Best same-song candidate: exact once edition/variant words are stripped, else close
    (fuzzy >= 88, same sequel number). -> (candidate, exact, score) or None."""
    k = s["baseKey"]
    cands = [x for x in cands if x["songId"] != s["songId"] and f"not-match:{x['songId']}" not in s["marks"]]
    exact = [x for x in cands if x["baseKey"] == k]
    if exact:
        return _best_target(exact), True, 100.0
    if nonsong_kind(s["title"]):
        return None
    fuzzy = [(fuzz.token_set_ratio(k, x["baseKey"]), x) for x in cands if len(k) >= 4 and len(x["baseKey"]) >= 4 and x["sequel"] == s["sequel"]]
    fuzzy = [(sc, x) for sc, x in fuzzy if sc >= 88]
    if fuzzy:
        sc, x = max(fuzzy, key=lambda p: (p[0], p[1]["scrobbleCount"]))
        return x, False, round(sc, 1)
    return None


def live_statuses(c, pairs: list[tuple[int, dict]], names: dict[int, str] | None = None) -> dict[tuple[int, int], dict]:
    """Status for each (performer id, song profile). Returns {(performer, songId): info}."""
    artist_ids = sorted({s["artistId"] for _p, s in pairs} | {p for p, _s in pairs})
    cands_by_artist = defaultdict(list)
    if artist_ids:
        cand_ids = [r[0] for r in c.execute(f"SELECT id FROM songs WHERE album_id IS NOT NULL AND artist_id IN ({_in(artist_ids)})", artist_ids)]
        for p in song_profiles(c, cand_ids).values():
            cands_by_artist[p["artistId"]].append(p)
    mbid_sets = artist_mbid_sets(c, artist_ids) if artist_ids else {}  # own mbid + "also releases as" aliases
    names = names or dict(c.execute(f"SELECT id, name FROM artists WHERE id IN ({_in(artist_ids)})", artist_ids).fetchall()) if artist_ids else {}

    # Cached MusicBrainz lookups: the band's own recording (covers), and the song artist's.
    keys = set()
    for perf, s in pairs:
        for m in mbid_sets.get(perf, set()) | mbid_sets.get(s["artistId"], set()):
            keys.add(mbcache.recording_release_groups_key(s["title"], m))
    cache = {}
    klist = list(keys)
    for i in range(0, len(klist), 500):
        chunk = klist[i:i + 500]
        cache.update({k: json.loads(v) for k, v in c.execute(f"SELECT key, payload_json FROM mb_cache WHERE key IN ({_in(chunk)})", chunk)})
    local_rg = {}  # rg mbid -> local album + every artist it credits
    rg_list = list({g["mbid"] for v in cache.values() for g in (v or [])})
    for i in range(0, len(rg_list), 500):
        chunk = rg_list[i:i + 500]
        for aid, title, year, mbid, artist_id in c.execute(f"SELECT id, title, year, mbid, artist_id FROM albums WHERE mbid IN ({_in(chunk)})", chunk):
            credited = {r[0] for r in c.execute("SELECT artist_id FROM album_artists WHERE album_id = ?", (aid,))} | {artist_id}
            local_rg[mbid] = {"albumId": aid, "title": title, "year": year, "credited": credited}

    # release-group lookups already cached (from an earlier "Add album" or a Verify sweep) tell
    # who MusicBrainz credits an album to -- used to flag candidates credited to someone else
    rg_credit = {}
    for i in range(0, len(rg_list), 500):
        chunk = [f"lookup:release-group:{m}" for m in rg_list[i:i + 500]]
        for k, v in c.execute(f"SELECT key, payload_json FROM mb_cache WHERE key IN ({_in(chunk)})", chunk):
            v = json.loads(v)
            if v:
                rg_credit[k.split(":", 2)[2]] = v

    # every album each involved artist is credited on, by base title -- an album you have that
    # isn't identified by its release group yet (a legacy edition id, or none) still counts
    local_titles = defaultdict(set)
    if artist_ids:
        for aid, title, year, mbid, credited in c.execute(
                f"SELECT al.id, al.title, al.year, al.mbid, aa.artist_id FROM albums al JOIN album_artists aa ON aa.album_id = al.id "
                f"WHERE aa.artist_id IN ({_in(artist_ids)})", artist_ids):
            local_titles[(credited, base_key(title))].add((aid, title, year, mbid))

    def candidate(g, owner):
        credit = rg_credit.get(g["mbid"])
        ok = None if not credit else bool(set(credit.get("artistMbids") or []) & mbid_sets.get(owner, set()))
        return {"yoursByTitle": len(local_titles.get((owner, base_key(g["title"] or ""))) or ()),
                "rgMbid": g["mbid"], "title": g["title"], "primaryType": g["primaryType"], "secondaryTypes": g.get("secondaryTypes") or [],
                "firstDate": (credit or {}).get("firstReleaseDate") or g.get("firstDate"), "recordingMbid": g.get("recordingMbid"),
                "ownerId": owner, "ownerName": names.get(owner), "creditOk": ok, "credit": (credit or {}).get("artistCredit")}

    def mb_album(title, artist_id, marks):
        """(local album info or None, looked-up?, first MB album, [ranked candidates when the best isn't yours]) for this
        artist's recording -- under any MusicBrainz artist id that counts as this artist."""
        keys = [mbcache.recording_release_groups_key(title, m) for m in sorted(mbid_sets.get(artist_id, set()))]
        looked = [k for k in keys if k in cache]
        if not looked:
            return None, False, None, []
        rgs = [g for k in looked for g in (cache[k] or []) if f"reject-rg:{g['mbid']}" not in marks]
        rgs.sort(key=lambda g: (g["primaryType"] != "Album", bool(g.get("secondaryTypes")), g.get("firstDate") or "9999"))
        seen, ranked = set(), []
        for g in rgs:  # best first: studio albums, then earliest
            if g["mbid"] in seen:
                continue
            seen.add(g["mbid"])
            cand = candidate(g, artist_id)
            loc = local_rg.get(g["mbid"])
            # only an album that credits this artist (MusicBrainz lists Morricone's "The Ecstasy
            # of Gold" on a Metallica live disc -- that isn't a Metallica album song)
            if loc and artist_id in loc["credited"]:
                cand.update(albumId=loc["albumId"], localTitle=loc["title"], localYear=loc["year"], byTitle=False)
            elif not loc:
                hits = local_titles.get((artist_id, base_key(g["title"] or ""))) or set()
                if len(hits) == 1:  # an album of yours with this title, not identified by its release group yet
                    (aid, t, y, _m), = hits
                    cand.update(albumId=aid, localTitle=t, localYear=y, byTitle=True)
            else:
                continue  # in your library, but credited to someone else entirely
            ranked.append(cand)
        ranked.sort(key=lambda x: x["creditOk"] is False)  # credited to someone else: last
        if ranked and ranked[0].get("albumId"):
            top = ranked[0]
            return {"albumId": top["albumId"], "title": top["localTitle"], "year": top["localYear"], "rgTitle": top["title"],
                    "rgMbid": top["rgMbid"], "type": top["primaryType"], "byTitle": top["byTitle"]}, True, rgs[0], []
        return None, True, (rgs[0] if rgs else None), ranked

    out = {}
    for perf, s in pairs:
        info = {"status": None, "target": None, "alt": None, "kind": None, "mbAlbum": None, "mbFound": None, "mbCandidates": None}
        out[(perf, s["songId"])] = info
        cover = s["artistId"] != perf
        own_ok = cover and f"cover-original:{perf}" not in s["marks"]
        # 1. the band's own studio recording (covers only), 2. the song artist's
        own = _find_match(s, cands_by_artist[perf]) if own_ok else None
        orig = None if s["albumId"] else _find_match(s, cands_by_artist[s["artistId"]])
        if own:
            t, exact, score = own
            info["status"] = "match"
            info["target"] = {**_brief(t), "exact": exact, "score": score, "relink": True, "performerName": names.get(perf)}
            if s["albumId"]:
                info["alt"] = {**_brief(s), "keep": True}  # it's already on the original's album -- keeping that is the alternative
            elif orig:
                info["alt"] = {**_brief(orig[0]), "exact": orig[1], "score": orig[2]}
            continue
        if s["albumId"]:
            info["status"] = "ok"
            continue
        if s["mark"]:
            info["status"] = "marked"
            continue
        if orig:
            t, exact, score = orig
            info["status"] = "match"
            info["target"] = {**_brief(t), "exact": exact, "score": score, "relink": False}
            continue
        kind = nonsong_kind(s["title"])
        if kind:
            info["status"], info["kind"] = "nonsong", kind
            continue
        own_album, own_looked, own_first, own_new = mb_album(s["title"], perf, s["marks"]) if own_ok else (None, False, None, [])
        orig_album, orig_looked, orig_first, orig_new = mb_album(s["title"], s["artistId"], s["marks"])
        if own_album:
            info["status"], info["mbAlbum"] = "album", {**own_album, "relinkNew": True, "performerName": names.get(perf)}
        elif orig_album:
            info["status"], info["mbAlbum"] = "album", {**orig_album, "relinkNew": False}
        elif own_new or orig_new:
            # the band's own recording first (covers), then the song artist's
            info["status"], info["mbCandidates"] = "newalbum", (own_new + orig_new)[:MAX_CANDIDATES]
            info["needsDetail"] = info["mbCandidates"][0]["creditOk"] is None
        elif own_looked or orig_looked:
            info["status"] = "lookedup"
            first = own_first or orig_first
            info["mbFound"] = {"rgTitle": first["title"], "rgMbid": first["mbid"], "type": first["primaryType"], "firstDate": first["firstDate"]} if first else None
        else:
            info["status"] = "unchecked"
            info["lookable"] = bool(mbid_sets.get(perf) if cover else False) or bool(mbid_sets.get(s["artistId"]))
    return out


def _live_rows(c, performer_ids: list[int] | None = None):
    where = f"WHERE st.artist_id IN ({_in(performer_ids)})" if performer_ids else ""
    return c.execute(
        f"""SELECT st.artist_id, st.id, st.event_date, ss.song_id, ss.position, ss.set_name, ss.is_cover, ss.cover_of_artist_text, ss.raw_song_text
            FROM setlist_songs ss JOIN setlists st ON st.id = ss.setlist_id {where}
            ORDER BY st.event_date, ss.position""", performer_ids or []).fetchall()


def live_pairs(c, performer_ids: list[int] | None = None):
    """-> (rows, profiles, statuses) for the given performers (all when None)."""
    rows = _live_rows(c, performer_ids)
    profiles = song_profiles(c, [r[3] for r in rows if r[3]])
    pairs = list({(r[0], r[3]) for r in rows if r[3] in profiles})
    statuses = live_statuses(c, [(p, profiles[sid]) for p, sid in pairs])
    return rows, profiles, statuses


@route("GET", "/api/songs/live-queue")
def live_queue(req):
    with read_conn() as c:
        rows, profiles, statuses = live_pairs(c)
        names = dict(c.execute("SELECT id, name FROM artists").fetchall())
        plays = dict(c.execute("SELECT artist_id, count(*) FROM scrobbles GROUP BY artist_id").fetchall())
    per = defaultdict(lambda: {"shows": set(), "songs": set()})
    for performer, setlist_id, _d, song_id, *_ in rows:
        per[performer]["shows"].add(setlist_id)
        if song_id in profiles:
            per[performer]["songs"].add(song_id)
    items = []
    for performer, v in per.items():
        st = [statuses[(performer, sid)]["status"] for sid in v["songs"]]
        items.append({"artistId": performer, "name": names.get(performer), "shows": len(v["shows"]), "songs": len(st),
                      "todo": sum(x in TODO for x in st), "done": sum(x in DONE for x in st), "unchecked": st.count("unchecked"),
                      "scrobbleCount": plays.get(performer, 0)})
    items.sort(key=lambda x: (x["todo"] == 0 and x["unchecked"] == 0, -x["shows"], -x["scrobbleCount"]))
    total = {k: sum(x[k] for x in items) for k in ("songs", "todo", "done", "unchecked")}
    return {"count": len(items), "items": items, "total": total}


@route("GET", "/api/songs/live")
def live(req):
    performer = req.int("artistId", required=True)
    with read_conn() as c:
        artist = c.execute("SELECT id, name, mbid FROM artists WHERE id = ?", (performer,)).fetchone()
        if not artist:
            raise ApiError("artist not found", 404)
        rows, profiles, statuses = live_pairs(c, [performer])
        shows = {}
        for sid, date, venue, city, tour in c.execute(
                """SELECT st.id, st.event_date, v.name, v.city, st.tour_name FROM setlists st LEFT JOIN venues v ON v.id = st.venue_id
                   WHERE st.artist_id = ? ORDER BY st.event_date DESC""", (performer,)):
            shows[sid] = {"setlistId": sid, "date": date, "venue": venue, "city": city, "tour": tour, "entries": []}
    played_at = defaultdict(list)
    for _p, setlist_id, date, song_id, position, set_name, is_cover, cover_of, raw in rows:
        shows[setlist_id]["entries"].append({"songId": song_id, "position": position, "set": set_name, "cover": bool(is_cover),
                                             "coverOf": cover_of, "raw": raw})
        if song_id:
            played_at[song_id].append(date)
    songs = []
    for sid, s in profiles.items():
        songs.append({**{k: v for k, v in s.items() if k != "marks"}, **statuses[(performer, sid)],
                      "playedAt": sorted(set(played_at[sid]), reverse=True), "cover": s["artistId"] != performer})
    by_id = {s["songId"]: s for s in songs}
    for sh in shows.values():
        st = [by_id[e["songId"]]["status"] for e in sh["entries"] if e["songId"] in by_id]
        sh["done"], sh["todo"], sh["total"] = sum(x in DONE for x in st), sum(x in TODO for x in st), len(st)
    order = {"match": 0, "album": 1, "newalbum": 2, "lookedup": 3, "nonsong": 4, "unchecked": 5, "marked": 6, "ok": 7}
    songs.sort(key=lambda s: (order[s["status"]], -len(s["playedAt"]), s["title"].lower()))
    return {"artist": {"artistId": artist[0], "name": artist[1], "mbid": artist[2]}, "songs": songs,
            "shows": list(shows.values()), "kinds": NO_ALBUM_KINDS}


@route("GET", "/api/songs/search")
def search(req):
    """Songs by one or more artists (artistId=1,2), for pick-the-song typeaheads -- e.g. a cover:
    the original artist's songs AND the band's own."""
    ids = [int(x) for x in req.str("artistId").split(",") if x.strip().isdigit()]
    if not ids:
        raise ApiError("artistId is required")
    q = req.str("q")
    with read_conn() as c:
        rows = c.execute(f"SELECT id, title FROM songs WHERE artist_id IN ({_in(ids)})", ids).fetchall()
        hits = process.extract(q, dict(rows), scorer=fuzz.WRatio, limit=15, score_cutoff=50, processor=default_process) if q else []
        prof = song_profiles(c, [h[2] for h in hits])
    return {"songs": [{**_brief(prof[sid]), "score": round(sc, 1)} for _t, sc, sid in hits if sid in prof]}


# -- Actions ---------------------------------------------------------------------------------

def _mark(c, song_id: int, mark: str) -> dict:
    cur = c.execute("INSERT OR IGNORE INTO review_marks (entity_type, entity_id, mark) VALUES ('song', ?, ?)", (song_id, mark))
    if not cur.rowcount:
        return None
    return {"kind": "mark", "id": cur.lastrowid}


def _apply_item(c, it: dict) -> dict | None:
    """One reviewed action; returns its undo handle {kind, id}."""
    t = it.get("type")
    if t == "merge":
        title = (it.get("title") or "").strip() or None
        r = merge.merge_songs(c, int(it["absorbedId"]), int(it["canonicalId"]), {"title": title} if title else None)
        return {"kind": "merge", "id": r["logId"]}
    if t == "relink":  # this band's plays of a cover -> the band's own recording
        return {"kind": "edit", "id": merge.relink_live(c, int(it["songId"]), int(it["toSongId"]), int(it["performerId"]), "live re-link")}
    if t == "relinkNew":  # the band's own recording isn't in the library yet: create it on their album, then re-link
        performer, album_id = int(it["performerId"]), int(it["albumId"])
        credited = {r[0] for r in c.execute("SELECT artist_id FROM album_artists WHERE album_id = ?", (album_id,))}
        if performer not in credited:
            raise ApiError("That album isn't credited to this band.", 409)
        title = it["title"].strip()
        new_id = c.execute("INSERT INTO songs (artist_id, album_id, title) VALUES (?, ?, ?)", (performer, album_id, title)).lastrowid
        return {"kind": "edit", "id": merge.relink_live(c, int(it["songId"]), new_id, performer, "live re-link (new song)", created_song=True)}
    if t == "albumNew":
        return _album_new(c, it)
    if t == "album":
        e = merge.edit_entity(c, "song", int(it["songId"]), {"album_id": int(it["albumId"])}, "song album")
        return {"kind": "edit", "id": e["editId"]} if e["editId"] else None
    if t == "mark":
        kind = it.get("kind") or "other"
        if kind not in NO_ALBUM_KINDS:
            raise ApiError(f"unknown kind {kind!r}")
        sid = int(it["songId"])
        c.execute("DELETE FROM review_marks WHERE entity_type = 'song' AND entity_id = ? AND mark LIKE 'no-album:%'", (sid,))
        return _mark(c, sid, f"no-album:{kind}")
    if t == "reject":  # remember a "no" so the same suggestion doesn't come back
        what = it.get("what")
        sid = int(it["songId"])
        if what == "match":
            return _mark(c, sid, f"not-match:{int(it['targetId'])}")
        if what == "album":
            return _mark(c, sid, f"reject-rg:{it['rgMbid']}")
        if what == "cover":  # keep the original artist's song for this band's plays
            return _mark(c, sid, f"cover-original:{int(it['performerId'])}")
        raise ApiError("reject needs what = match | album | cover")
    if t == "rename":
        e = merge.edit_entity(c, "song", int(it["songId"]), {"title": it["title"].strip()}, "song rename")
        return {"kind": "edit", "id": e["editId"]} if e["editId"] else None
    raise ApiError(f"unknown action type {t!r}")


def _local_artists_for(c, mbids) -> list[int]:
    """Local artists behind MusicBrainz artist ids (own mbid, or an "also releases as" alias)."""
    out = []
    for m in mbids:
        row = c.execute("SELECT id FROM artists WHERE mbid = ?", (m,)).fetchone() or \
            c.execute("SELECT artist_id FROM artist_mb_aliases WHERE mbid = ?", (m,)).fetchone()
        if row and row[0] not in out:
            out.append(row[0])
    return out


def _same_title_albums(c, artist_id: int, title: str) -> list[int]:
    """Albums this artist is credited on with this (base) title."""
    key = base_key(title or "")
    return sorted({aid for aid, t in c.execute("SELECT al.id, al.title FROM albums al JOIN album_artists aa ON aa.album_id = al.id "
                                               "WHERE aa.artist_id = ?", (artist_id,)) if base_key(t) == key})


def _album_new(c, it) -> list[dict]:
    """A song heard live, on a MusicBrainz album you don't have yet: add that album (identified by
    its release group, original year, credited as MusicBrainz credits it) and link the song -- or
    for a cover, the band's own recording on it. Refuses an album MusicBrainz credits to someone
    else. Undo handles: the album first, so a batch undo removes the song link, then the album."""
    rg, sid = it["_rg"], int(it["songId"])
    owner = int(it["ownerId"])
    performer = int(it.get("performerId") or owner)
    song = c.execute("SELECT artist_id, title, mbid FROM songs WHERE id = ?", (sid,)).fetchone()
    if not song:
        raise ApiError("That song no longer exists — reload.", 409)
    owner_name = c.execute("SELECT name FROM artists WHERE id = ?", (owner,)).fetchone()[0]
    handles = []
    row = c.execute("SELECT id FROM albums WHERE mbid = ?", (rg["mbid"],)).fetchone()
    if row:  # added meanwhile (another song from the same album, earlier in this batch)
        album_id = row[0]
        if not c.execute("SELECT 1 FROM album_artists WHERE album_id = ? AND artist_id = ?", (album_id, owner)).fetchone():
            raise ApiError(f"“{rg['title']}” is in your library already, credited to someone else.", 409)
    elif same := _same_title_albums(c, owner, rg["title"]):
        # you have it already, just not identified by its release group yet -- link, never duplicate
        if len(same) > 1:
            raise ApiError(f"You already have {len(same)} {owner_name} albums called “{rg['title']}” — link the song to one of them "
                           f"(Other… › album search), or merge them first (Albums › Discography).", 409, "ambiguous_title")
        album_id = same[0]
    else:
        credited = _local_artists_for(c, rg.get("artistMbids") or [])
        if owner not in credited:
            raise ApiError(f"MusicBrainz credits “{rg['title']}” to {rg.get('artistCredit') or 'another artist'}, not {owner_name} "
                           f"— it isn't {owner_name}'s album.", 409, "credited_elsewhere")
        year = int(rg["firstReleaseDate"][:4]) if (rg.get("firstReleaseDate") or "")[:4].isdigit() else None
        album_id, edit_id = merge.create_album(c, [owner, *[a for a in credited if a != owner]], rg["title"], year, rg["mbid"],
                                               "album from MusicBrainz (song heard live)")
        handles.append({"kind": "edit", "id": edit_id})
    rec = it.get("recordingMbid") or None
    rec_free = rec and not c.execute("SELECT 1 FROM songs WHERE mbid = ?", (rec,)).fetchone()
    if owner == song[0]:
        changes = {"album_id": album_id}
        if rec_free and not song[2]:
            changes["mbid"] = rec
        e = merge.edit_entity(c, "song", sid, changes, "song album (added from MusicBrainz)")
        if e["editId"]:
            handles.append({"kind": "edit", "id": e["editId"]})
        return handles
    # a cover: the band's own recording, on the band's album; only this band's live plays move to it
    title = (it.get("title") or song[1]).strip()
    existing = c.execute("SELECT id, album_id FROM songs WHERE artist_id = ? AND lower(title) = lower(?)", (owner, title)).fetchone()
    if existing:
        if not existing[1]:
            e = merge.edit_entity(c, "song", existing[0], {"album_id": album_id}, "song album (added from MusicBrainz)")
            if e["editId"]:
                handles.append({"kind": "edit", "id": e["editId"]})
        handles.append({"kind": "edit", "id": merge.relink_live(c, sid, existing[0], performer, "live re-link (album added)")})
    else:
        new_id = c.execute("INSERT INTO songs (artist_id, album_id, title, mbid) VALUES (?, ?, ?, ?)",
                           (owner, album_id, title, rec if rec_free else None)).lastrowid
        handles.append({"kind": "edit", "id": merge.relink_live(c, sid, new_id, performer, "live re-link (album added, new song)", created_song=True)})
    return handles


@route("GET", "/api/songs/mb-discography")
def mb_discography(req):
    """An artist's MusicBrainz discography (own id + "also releases as"), for picking the album a
    live-only song is on by hand. Release groups already in the library say which album."""
    artist_id = req.int("artistId", required=True)
    with read_conn() as c:
        row = c.execute("SELECT name FROM artists WHERE id = ?", (artist_id,)).fetchone()
        if not row:
            raise ApiError("artist not found", 404)
        mbids = sorted(artist_mbid_sets(c, [artist_id]).get(artist_id, set()))
    if not mbids:
        raise ApiError(f"{row[0]} has no MusicBrainz id yet — resolve the artist first.", 409, "no_artist_mbid")
    try:
        groups, seen = [], set()
        for m in mbids:
            for g in mbcache.artist_release_groups(m) or []:
                if g["mbid"] not in seen:
                    seen.add(g["mbid"])
                    groups.append(g)
    except requests.RequestException as exc:
        raise ApiError(f"MusicBrainz request failed: {exc}", 502)
    with read_conn() as c:
        linked = dict(c.execute(f"SELECT mbid, id FROM albums WHERE mbid IN ({_in(groups)})", [g["mbid"] for g in groups]).fetchall()) if groups else {}
        for g in groups:
            if g["mbid"] not in linked and len(same := _same_title_albums(c, artist_id, g["title"])) == 1:
                linked[g["mbid"]] = same[0]
    rank = lambda g: (g.get("primaryType") != "Album", bool(g.get("secondaryTypes")), g.get("firstReleaseDate") or "9999")  # noqa: E731
    return {"artistName": row[0], "releaseGroups": [{**g, "albumId": linked.get(g["mbid"])} for g in sorted(groups, key=rank)]}


@route("POST", "/api/songs/apply", mutating=True)
def apply(req):
    """Apply a reviewed list of actions in ONE transaction. Returns undo handles for all of
    them (undo as a single batch)."""
    items = req.body.get("items") or []
    if not items:
        raise ApiError("nothing to apply")
    # Albums to add from MusicBrainz: fetched (cached) BEFORE the write transaction opens
    for it in items:
        if it.get("type") == "albumNew":
            try:
                it["_rg"] = mbcache.release_group(str(it.get("rgMbid") or ""))
            except requests.RequestException as exc:
                raise ApiError(f"MusicBrainz request failed: {exc}", 502)
            if not it["_rg"]:
                raise ApiError("MusicBrainz doesn't know that album (release group).", 404)
    undo = []
    with write_tx() as c:
        for it in items:
            h = _apply_item(c, it)
            if isinstance(h, list):
                undo.extend(h)
            elif h:
                undo.append(h)
    return {"applied": len(items), "undo": undo}


@route("POST", "/api/songs/merge-group", mutating=True)
def merge_group(req):
    canonical = req.int("canonicalId", required=True)
    absorbed = [int(i) for i in (req.body.get("absorbedIds") or [])]
    if not absorbed or canonical in absorbed:
        raise ApiError("absorbedIds must be a non-empty list not containing canonicalId")
    title = req.str("title") or None
    undo = []
    with write_tx() as c:
        for i, sid in enumerate(absorbed):
            r = merge.merge_songs(c, sid, canonical, {"title": title} if title and i == len(absorbed) - 1 else None)
            undo.append({"kind": "merge", "id": r["logId"]})
        final = c.execute("SELECT title FROM songs WHERE id = ?", (canonical,)).fetchone()[0]
    return {"canonicalId": canonical, "canonicalTitle": final, "merged": len(absorbed), "undo": undo}


@route("POST", "/api/songs/dismiss-group", mutating=True)
def dismiss_group(req):
    ids = sorted({int(i) for i in (req.body.get("songIds") or [])})
    if len(ids) < 2:
        raise ApiError("songIds needs at least two songs")
    with write_tx() as c:
        for a, b in combinations(ids, 2):
            c.execute("INSERT INTO duplicate_dismissals (entity_type, entity_id_a, entity_id_b) VALUES ('song', ?, ?) ON CONFLICT DO NOTHING", (a, b))
    return {"dismissed": True}


# -- Duplicates, album by album ---------------------------------------------------------------

def _groups_for(c, artist_ids: list[int]) -> dict[int, list[dict]]:
    """artist -> groups of 2+ songs sharing an edition/variant-stripped title (minus human
    "not the same" dismissals). Each: {songIds, albumId (common album, or None = across albums)}."""
    if not artist_ids:
        return {}
    songs = defaultdict(list)
    for sid, title, artist_id, album_id in c.execute(
            f"SELECT id, title, artist_id, album_id FROM songs WHERE artist_id IN ({_in(artist_ids)})", artist_ids):
        songs[artist_id].append((sid, base_key(title) or title.lower(), album_id))
    all_ids = [s[0] for v in songs.values() for s in v]
    albums_of = defaultdict(set)
    for sid, album_id in c.execute(
            f"SELECT DISTINCT sc.song_id, sc.album_id FROM scrobbles sc JOIN songs s ON s.id = sc.song_id "
            f"WHERE s.artist_id IN ({_in(artist_ids)}) AND sc.album_id IS NOT NULL", artist_ids):
        albums_of[sid].add(album_id)
    dismissed = {(a, b) for a, b in c.execute("SELECT entity_id_a, entity_id_b FROM duplicate_dismissals WHERE entity_type = 'song'")}
    out = {}
    for artist_id, rows in songs.items():
        by_key = defaultdict(list)
        for sid, k, album_id in rows:
            by_key[k].append((sid, album_id))
        groups = []
        for members in by_key.values():
            if len(members) < 2:
                continue
            ids = [m[0] for m in members]
            keep = [i for i in ids if any((min(i, j), max(i, j)) not in dismissed for j in ids if j != i)]
            if len(keep) < 2:
                continue
            sets = [(albums_of[i] | ({a} if a else set())) for i, a in members if i in keep]
            common = set.intersection(*sets) if all(sets) else set()
            groups.append({"songIds": keep, "albumId": min(common) if common else None, "common": sorted(common)})
        if groups:
            out[artist_id] = groups
    return out


@route("GET", "/api/songs/dupe-queue")
def dupe_queue(req):
    show_reviewed = req.str("showReviewed") == "1"
    with read_conn() as c:
        artists = [r[0] for r in c.execute("SELECT DISTINCT artist_id FROM songs")]
        groups = _groups_for(c, artists)
        names = dict(c.execute("SELECT id, name FROM artists").fetchall())
        plays = dict(c.execute("SELECT artist_id, count(*) FROM scrobbles GROUP BY artist_id").fetchall())
        reviewed = {r[0] for r in c.execute("SELECT entity_id FROM review_marks WHERE entity_type = 'artist' AND mark = 'songs-reviewed'")}
    items = [{"artistId": a, "name": names.get(a), "scrobbleCount": plays.get(a, 0), "groups": len(g),
              "albumGroups": sum(1 for x in g if x["albumId"]), "crossGroups": sum(1 for x in g if not x["albumId"]),
              "reviewed": a in reviewed}
             for a, g in groups.items() if show_reviewed or a not in reviewed]
    items.sort(key=lambda x: (-x["scrobbleCount"], x["name"] or ""))
    return {"count": len(items), "items": items, "totalGroups": sum(x["groups"] for x in items)}


@route("GET", "/api/songs/dupes")
def dupes(req):
    artist_id = req.int("artistId", required=True)
    with read_conn() as c:
        artist = c.execute("SELECT id, name, mbid FROM artists WHERE id = ?", (artist_id,)).fetchone()
        if not artist:
            raise ApiError("artist not found", 404)
        groups = _groups_for(c, [artist_id]).get(artist_id, [])
        prof = song_profiles(c, [i for g in groups for i in g["songIds"]])
        album_ids = sorted({g["albumId"] for g in groups if g["albumId"]})
        albums = {}
        if album_ids:
            for aid, title, year, mbid, cover in c.execute(
                    f"SELECT id, title, year, mbid, cover_status FROM albums WHERE id IN ({_in(album_ids)})", album_ids):
                albums[aid] = {"albumId": aid, "title": title, "year": year, "mbid": mbid, "coverStatus": cover, "groups": []}
            for aid, n in c.execute(f"SELECT canonical_id, count(*) FROM merge_log WHERE entity_type = 'album' AND undone_at IS NULL "
                                    f"AND canonical_id IN ({_in(album_ids)}) GROUP BY canonical_id", album_ids):
                albums[aid]["mergedIn"] = n
            for aid, n in c.execute(f"SELECT album_id, count(*) FROM scrobbles WHERE album_id IN ({_in(album_ids)}) GROUP BY album_id", album_ids):
                albums[aid]["scrobbleCount"] = n
        reviewed = c.execute("SELECT 1 FROM review_marks WHERE entity_type = 'artist' AND entity_id = ? AND mark = 'songs-reviewed'",
                             (artist_id,)).fetchone() is not None
    cross = []
    for g in groups:
        members = [prof[i] for i in g["songIds"]]
        primary = _best_target(members)
        # Pre-ticked only on an album, and only for plain edition variants (remasters, mono,
        # single versions...); live/remix/demo/edit variants are shown but left for a human.
        include = [m["songId"] for m in members if g["albumId"] and not m["tags"] and m["songId"] != primary["songId"]]
        block = {"songIds": g["songIds"], "primary": primary["songId"], "include": include, "baseTitle": primary["baseTitle"]}
        if g["albumId"]:
            albums[g["albumId"]]["groups"].append(block)
        else:
            cross.append(block)
    album_list = sorted(albums.values(), key=lambda a: (-a.get("mergedIn", 0), -a.get("scrobbleCount", 0)))
    return {"artist": {"artistId": artist[0], "name": artist[1], "mbid": artist[2], "reviewed": reviewed},
            "albums": album_list, "cross": cross, "songs": prof}
