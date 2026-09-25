"""Songs > Splits / Tracklists / Wrong album -- three views onto song clean-up that lean on
what the vinyl collection taught us:

  Splits       a track whose live sightings and scrobbles sit on different song rows (setlist.fm's
               plain "War Pigs" has the shows, "War Pigs - 2009 Remaster" the plays). The safest
               merges there are, and the most visible: every live count, dot and stat depends on them.
  Tracklists   each vinyl album checked line by line against its pressing's Discogs tracklist:
               the song rows behind each track (variants to merge), tracks with no song, and
               songs filed under the album that aren't on it.
  Wrong album  plays that arrived under ANOTHER album's name and ended up filed under this one --
               typically an old album merge into the wrong album ("Led Zeppelin (Remaster)" merged
               into Led Zeppelin II). Moving them (merge.move_album_plays) also re-points the import
               alias, so future plays land right. Evidence shown, nothing moves without a click.
Merges go through /api/songs/apply (one transaction, one undo)."""
import json
from collections import defaultdict

from rapidfuzz import fuzz

import merge
from api.core import ApiError, read_conn, route, write_tx
from api.songs import _best_target, _groups_for, _in, song_profiles
from titles import base_key, sequel_marker, split_title, track_key

TRACKLIST_MARK = "tracklist-reviewed"
_NUM_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"}


def _numbered(title: str) -> str | None:
    """sequel_marker, reading number words too ("Pt. One" = "Pt. 1")."""
    import re
    return sequel_marker(re.sub(r"\b(" + "|".join(_NUM_WORDS) + r")\b", lambda m: _NUM_WORDS[m.group(1).lower()], title, flags=re.I))
WRONG_ALBUM_OK = "wrong-album-ok:"  # review mark on the album: "<raw album text>" is fine here


# -- Splits ------------------------------------------------------------------------------------

@route("GET", "/api/songs/splits")
def splits(req):
    with read_conn() as c:
        artists = [r[0] for r in c.execute("SELECT DISTINCT artist_id FROM songs")]
        groups = [g for gs in _groups_for(c, artists).values() for g in gs]
        prof = song_profiles(c, [i for g in groups for i in g["songIds"]])
        names = dict(c.execute("SELECT id, name FROM artists").fetchall())
    out = []
    for g in groups:
        members = [prof[i] for i in g["songIds"]]
        live_only = [m for m in members if m["setlistCount"] and not m["scrobbleCount"]]
        played = [m for m in members if m["scrobbleCount"]]
        if not live_only or not played:
            continue
        primary = _best_target(members)
        absorbed = [m for m in members if m["songId"] != primary["songId"]]
        # safe to pre-tick: every row being folded in is the same recording spelled differently --
        # edition suffixes only, no live / remix / demo / edit variant
        clean = all(not m["tags"] for m in absorbed)
        out.append({
            "artistId": primary["artistId"], "artistName": names.get(primary["artistId"]), "baseTitle": primary["baseTitle"],
            "primary": primary["songId"], "absorbed": [m["songId"] for m in absorbed], "clean": clean,
            "shows": sum(m["setlistCount"] for m in members), "plays": sum(m["scrobbleCount"] for m in members),
            "members": [{k: m[k] for k in ("songId", "title", "scrobbleCount", "setlistCount", "albumTitle", "tags", "mbid")} for m in members],
        })
    out.sort(key=lambda x: (not x["clean"], -x["shows"], -x["plays"]))
    return {"count": len(out), "clean": sum(x["clean"] for x in out), "splits": out}


# -- Tracklist matching ----------------------------------------------------------------------

def album_tracklist(c, album_id: int) -> list[dict]:
    """The album's reference tracklist: a pressing you own (Discogs, via the pressing-details
    sweep; with several, the longest -- a deluxe edition lists everything the plain one does), else
    MusicBrainz's original release (the album-tracklists sweep)."""
    best = []
    for (tl,) in c.execute("SELECT d.tracklist FROM vinyl_details d JOIN vinyl_holdings v ON v.id = d.holding_id WHERE v.album_id = ?",
                           (album_id,)):
        tracks = [t for t in json.loads(tl or "[]") if (t.get("type") or "track") == "track" and t.get("title")]
        if len(tracks) > len(best):
            best = tracks
    if best:
        return best
    return [{"position": n, "title": t, "recordingMbid": m,
             "duration": f"{ms // 60000}:{ms // 1000 % 60:02d}" if ms else None}
            for n, t, m, ms in c.execute("SELECT number, title, recording_mbid, length_ms FROM album_tracklists WHERE album_id = ? ORDER BY position",
                                         (album_id,))]


def album_song_rows(c, album_id: int) -> list[int]:
    """Every song filed under the album or scrobbled from it."""
    return [r[0] for r in c.execute(
        "SELECT id FROM songs WHERE album_id = ? UNION SELECT DISTINCT song_id FROM scrobbles WHERE album_id = ? AND song_id IS NOT NULL",
        (album_id, album_id))]


def track_links(c, album_id: int) -> dict[str, int]:
    """Hand-matched tracklist lines: lower-cased track title -> song id (merge.set_track_link)."""
    return {t.lower(): s for t, s in c.execute("SELECT track_title, song_id FROM track_links WHERE album_id = ? ORDER BY id", (album_id,))}


def match_tracklist(tracks: list[dict], songs: list[dict], links: dict[str, int] | None = None) -> tuple[list[dict], list[dict]]:
    """Pressing tracks -> the song rows behind each (every row with the same track title), then,
    for tracks still empty, one clear winner by spacing-blind title, unique prefix ("Wheels of
    Confusion" / "... / The Straightener") or near-identical spelling ("Seperate"). A line matched
    by hand (`links`, from track_links) takes its song first. -> (lines, extra songs not on the
    tracklist). Rows are grouped per title, so one line can hold several rows."""
    by_key = defaultdict(list)
    for s in songs:
        by_key[track_key(s["title"])].append(s)
    by_rec = {s["mbid"]: s for s in songs if s.get("mbid")}
    by_id = {s["songId"]: s for s in songs}
    on_it = {t["title"].strip().lower() for t in tracks}  # a link for a line this tracklist doesn't have is no claim here
    linked = {t: by_id[sid] for t, sid in (links or {}).items() if sid in by_id and t in on_it}
    # a hand-matched song is spoken for: no other line claims it by title
    used, lines = {s["songId"] for s in linked.values()}, []
    for t in tracks:
        k = track_key(t["title"])
        mine = linked.pop(t["title"].strip().lower(), None)
        if mine:  # plus the rows sharing its title (its remaster, a live version...)
            rows = [mine, *[s for s in by_key.get(track_key(mine["title"]), []) if s["songId"] not in used and s is not mine]]
            for s in rows:
                used.add(s["songId"])
            lines.append({"position": t.get("position"), "title": t["title"], "duration": t.get("duration"), "how": "matched by hand", "rows": rows})
            continue
        rows = [s for s in by_key.get(k, []) if s["songId"] not in used]
        how = "title" if rows else None
        exact = by_rec.get(t.get("recordingMbid"))
        if exact and exact["songId"] not in used and exact not in rows:  # the same MusicBrainz recording, whatever it's called
            rows = [exact, *[s for s in by_key.get(track_key(exact["title"]), []) if s["songId"] not in used and s is not exact]]
            how = "recording id"
        if not rows:
            free = {kk: v for kk, v in by_key.items() if not any(s["songId"] in used for s in v)}
            compact = k.replace(" ", "")
            cands = [kk for kk in free if kk.replace(" ", "") == compact]
            if not cands and len(k) >= 4:
                cands = [kk for kk in free if kk.startswith(f"{k} ") or k.startswith(f"{kk} ")]
            if not cands and len(compact) >= 5:
                scored = sorted(((fuzz.ratio(compact, kk.replace(" ", "")), kk) for kk in free), reverse=True)
                scored = [x for x in scored if x[0] >= 85]
                if scored and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 8):
                    cands = [scored[0][1]]
            if len(cands) == 1:
                rows, how = free[cands[0]], "close title"
        for s in rows:
            used.add(s["songId"])
        lines.append({"position": t.get("position"), "title": t["title"], "duration": t.get("duration"), "how": how, "rows": rows})
    extra = [s for s in songs if s["songId"] not in used]
    return lines, extra


def _row(p: dict) -> dict:
    return {k: p[k] for k in ("songId", "title", "scrobbleCount", "setlistCount", "albumId", "albumTitle", "tags", "mbid", "edition")}


@route("GET", "/api/songs/tracklist-queue")
def tracklist_queue(req):
    """Vinyl albums with a pressing tracklist, and how much each needs: tracks whose plays are
    split across rows, and songs filed under it that aren't on it."""
    show_reviewed = req.str("showReviewed") == "1"
    everything = req.str("source") == "all"
    with read_conn() as c:
        albums = c.execute("""SELECT DISTINCT al.id, al.title, al.year, ar.id, ar.name,
                                     (SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id)
                              FROM vinyl_holdings v JOIN albums al ON al.id = v.album_id JOIN artists ar ON ar.id = al.artist_id
                              JOIN vinyl_details d ON d.holding_id = v.id WHERE d.tracklist IS NOT NULL AND d.tracklist != '[]'""").fetchall()
        if everything:  # plus the albums with MusicBrainz's original tracklist
            albums += c.execute("""SELECT al.id, al.title, al.year, ar.id, ar.name, (SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id)
                                   FROM album_tracklist_sources t JOIN albums al ON al.id = t.album_id JOIN artists ar ON ar.id = al.artist_id
                                   WHERE NOT EXISTS (SELECT 1 FROM vinyl_holdings v WHERE v.album_id = al.id)""").fetchall()
        reviewed = {r[0] for r in c.execute("SELECT entity_id FROM review_marks WHERE entity_type = 'album' AND mark = ?", (TRACKLIST_MARK,))}
        items = []
        for aid, title, year, arid, artist, plays in albums:
            if aid in reviewed and not show_reviewed:
                continue
            prof = song_profiles(c, album_song_rows(c, aid))
            lines, extra = match_tracklist(album_tracklist(c, aid), list(prof.values()), track_links(c, aid))
            to_merge = sum(1 for ln in lines if len(ln["rows"]) > 1)
            extra_played = sum(1 for s in extra if s["scrobbleCount"])
            items.append({"albumId": aid, "title": title, "year": year, "artistId": arid, "artistName": artist, "plays": plays,
                          "vinyl": bool(c.execute("SELECT 1 FROM vinyl_holdings WHERE album_id = ?", (aid,)).fetchone()),
                          "toMerge": to_merge, "extra": extra_played, "empty": sum(1 for ln in lines if not ln["rows"]),
                          "reviewed": aid in reviewed})
    items.sort(key=lambda x: (-(x["toMerge"] + x["extra"]), -x["plays"]))
    return {"count": len(items), "items": items}


@route("GET", "/api/songs/tracklist-status")
def tracklist_status(req):
    with read_conn() as c:
        one = lambda q: c.execute(q).fetchone()[0]  # noqa: E731
        return {"fetched": one("SELECT count(*) FROM album_tracklist_sources"),
                "toFetch": one("""SELECT count(*) FROM albums al WHERE al.mbid IS NOT NULL
                    AND NOT EXISTS (SELECT 1 FROM vinyl_holdings v WHERE v.album_id = al.id)
                    AND NOT EXISTS (SELECT 1 FROM album_tracklist_sources t WHERE t.album_id = al.id)
                    AND NOT EXISTS (SELECT 1 FROM album_releases r WHERE r.release_mbid = al.mbid)
                    AND NOT EXISTS (SELECT 1 FROM scrobble_releases r WHERE r.release_mbid = al.mbid)
                    AND NOT EXISTS (SELECT 1 FROM mb_cache m WHERE m.key = 'browse:tracklist:' || al.mbid AND m.payload_json = 'null')"""),
                "onEditionId": one("""SELECT count(*) FROM albums al WHERE al.mbid IS NOT NULL
                    AND NOT EXISTS (SELECT 1 FROM vinyl_holdings v WHERE v.album_id = al.id)
                    AND (EXISTS (SELECT 1 FROM album_releases r WHERE r.release_mbid = al.mbid)
                         OR EXISTS (SELECT 1 FROM scrobble_releases r WHERE r.release_mbid = al.mbid))""")}


@route("GET", "/api/songs/tracklist")
def tracklist(req):
    album_id = req.int("albumId", required=True)
    with read_conn() as c:
        al = c.execute("SELECT al.id, al.title, al.year, al.mbid, ar.id, ar.name FROM albums al JOIN artists ar ON ar.id = al.artist_id "
                       "WHERE al.id = ?", (album_id,)).fetchone()
        if not al:
            raise ApiError("album not found", 404)
        prof = song_profiles(c, album_song_rows(c, album_id))
        tracks = album_tracklist(c, album_id)
        lines, extra = match_tracklist(tracks, list(prof.values()), track_links(c, album_id))
        # where might each extra song belong? another of the artist's albums listing it
        homes = _track_homes(c, al[4], exclude=album_id)
        reviewed = c.execute("SELECT 1 FROM review_marks WHERE entity_type = 'album' AND entity_id = ? AND mark = ?",
                             (album_id, TRACKLIST_MARK)).fetchone() is not None
        wrong = [w for w in _wrong_album_groups(c, [album_id])]
    out_lines = []
    for ln in lines:
        rows = [_row(r) for r in ln["rows"]]
        primary = _best_target(ln["rows"])["songId"] if ln["rows"] else None
        out_lines.append({**{k: ln[k] for k in ("position", "title", "duration", "how")}, "rows": rows, "primary": primary,
                          "plays": sum(r["scrobbleCount"] for r in rows), "shows": sum(r["setlistCount"] for r in rows),
                          # safe to pre-tick: the rows are the same recording, edition spellings only
                          "clean": len(rows) > 1 and all(not r["tags"] for r in rows if r["songId"] != primary)})
    out_extra = []
    for s in sorted(extra, key=lambda s: -s["scrobbleCount"]):
        home = homes.get(track_key(s["title"])) or []
        out_extra.append({**_row(s), "homes": home[:3]})
    return {"album": {"albumId": al[0], "title": al[1], "year": al[2], "mbid": al[3], "artistId": al[4], "artistName": al[5],
                      "reviewed": reviewed},
            "hasTracklist": bool(tracks), "lines": out_lines, "extra": out_extra, "wrongAlbum": wrong}


def _track_homes(c, artist_id: int, exclude: int) -> dict[str, list[dict]]:
    """track key -> the artist's other vinyl albums whose pressing tracklist lists it."""
    out = defaultdict(list)
    for aid, title, tl in c.execute("""SELECT al.id, al.title, d.tracklist FROM albums al JOIN vinyl_holdings v ON v.album_id = al.id
                                       JOIN vinyl_details d ON d.holding_id = v.id WHERE al.artist_id = ? AND al.id != ?""",
                                    (artist_id, exclude)):
        for t in json.loads(tl or "[]"):
            if t.get("title"):
                entry = {"albumId": aid, "title": title}
                k = track_key(t["title"])
                if entry not in out[k]:
                    out[k].append(entry)
    return out


# -- Wrong album -------------------------------------------------------------------------------

def _wrong_album_groups(c, album_ids: list[int] | None = None) -> list[dict]:
    """Plays filed under album A that arrived under the name of ANOTHER of the same artist's albums
    B (the raw album text is B's title, not A's) -- with the evidence: B's tracklist / songs
    listing those tracks, and the old merge that put them there, if there was one."""
    where = f"AND s.album_id IN ({_in(album_ids)})" if album_ids else ""
    groups = c.execute(f"""
        SELECT s.album_id, s.raw_album_text, count(*), count(DISTINCT s.song_id), al.title, al.artist_id
        FROM scrobbles s JOIN albums al ON al.id = s.album_id
        WHERE s.raw_album_text IS NOT NULL AND s.raw_album_text != '' {where}
        GROUP BY s.album_id, lower(s.raw_album_text)""", album_ids or []).fetchall()
    ok_marks = {(eid, m[len(WRONG_ALBUM_OK):]) for eid, m in c.execute(
        "SELECT entity_id, mark FROM review_marks WHERE entity_type = 'album' AND mark LIKE ?", (WRONG_ALBUM_OK + "%",))}
    split_out = {r[0] for r in c.execute("SELECT entity_id FROM edit_log WHERE changes_json LIKE '{\"_split\"%' AND undone_at IS NULL")}
    by_artist_key = defaultdict(list)
    for aid, title, artist_id in c.execute("SELECT id, title, artist_id FROM albums"):
        by_artist_key[(artist_id, base_key(title))].append((aid, title))
    out = []
    for album_id, raw, n, nsongs, title, artist_id in groups:
        k = base_key(raw)
        if k == base_key(title) or (album_id, raw.lower()) in ok_marks or album_id in split_out:
            continue  # same album / already judged fine / an album deliberately split out of these very plays
        homes = [h for h in by_artist_key.get((artist_id, k), []) if h[0] != album_id]
        if len(homes) != 1:
            # no album of theirs by that name: only worth a look when the name is unlike this
            # album's, or numbered differently ("Pt. 1" filed under "Pt. 2", "The Blueprint" under
            # "The Blueprint 3") -- the fix then is to split them out as their own album
            if not homes and (fuzz.token_set_ratio(k, base_key(title)) < 60 or _numbered(raw) != _numbered(title)):
                sample = [r[0] for r in c.execute("SELECT DISTINCT so.title FROM scrobbles s JOIN songs so ON so.id = s.song_id "
                                                  "WHERE s.album_id = ? AND lower(s.raw_album_text) = lower(?)", (album_id, raw))]
                merged = c.execute("SELECT id, merged_at FROM merge_log WHERE entity_type = 'album' AND canonical_id = ? "
                                   "AND lower(absorbed_name) = lower(?) ORDER BY id DESC LIMIT 1", (album_id, raw)).fetchone()
                out.append({"fromAlbumId": album_id, "fromTitle": title, "toAlbumId": None, "toTitle": split_title(raw)[0].strip(),
                            "rawTitle": raw, "plays": n, "songs": nsongs, "sample": sample[:6], "onTarget": 0, "onHere": None,
                            "hereHasTracklist": False, "mergedIn": {"logId": merged[0], "at": merged[1]} if merged else None,
                            # the album they're filed under is a numbered one they're not ("Pt. 2", "The Blueprint 3")
                            "strong": False, "split": True, "numbered": bool(_numbered(title)) and _numbered(raw) != _numbered(title)})
            continue
        to_id, to_title = homes[0]
        # evidence from tracks: how many of these songs does the other album list / hold?
        song_titles = [r[0] for r in c.execute("SELECT DISTINCT so.title FROM scrobbles s JOIN songs so ON so.id = s.song_id "
                                               "WHERE s.album_id = ? AND lower(s.raw_album_text) = lower(?)", (album_id, raw))]
        target_keys = {track_key(t["title"]) for t in album_tracklist(c, to_id)} | {
            track_key(r[0]) for r in c.execute("SELECT title FROM songs WHERE album_id = ?", (to_id,))}
        here_keys = {track_key(t["title"]) for t in album_tracklist(c, album_id)}
        on_target = sum(1 for t in song_titles if track_key(t) in target_keys)
        on_here = sum(1 for t in song_titles if track_key(t) in here_keys)
        merged = c.execute("SELECT id, merged_at FROM merge_log WHERE entity_type = 'album' AND canonical_id = ? AND lower(absorbed_name) = lower(?) "
                           "ORDER BY id DESC LIMIT 1", (album_id, raw)).fetchone()
        out.append({"fromAlbumId": album_id, "fromTitle": title, "toAlbumId": to_id, "toTitle": to_title, "rawTitle": raw,
                    "plays": n, "songs": nsongs, "sample": song_titles[:6], "onTarget": on_target, "onHere": on_here,
                    "hereHasTracklist": bool(here_keys),
                    "mergedIn": {"logId": merged[0], "at": merged[1]} if merged else None,
                    # strong: most of the songs are the other album's, few are this one's
                    "strong": on_target >= max(1, nsongs * 0.6) and on_here <= nsongs * 0.34, "split": False})
    out.sort(key=lambda w: (not w["strong"], w["split"] and not w.get("numbered"), -w["plays"]))
    return out


@route("GET", "/api/songs/wrong-albums")
def wrong_albums(req):
    with read_conn() as c:
        groups = _wrong_album_groups(c)
        names = dict(c.execute("SELECT al.id, ar.name FROM albums al JOIN artists ar ON ar.id = al.artist_id").fetchall())
    for g in groups:
        g["artistName"] = names.get(g["fromAlbumId"])
    return {"count": len(groups), "strong": sum(g["strong"] for g in groups), "groups": groups}


@route("POST", "/api/songs/move-plays", mutating=True)
def move_plays(req):
    from_album, to_album, raw = req.int("fromAlbumId", required=True), req.int("toAlbumId", required=True), req.str("rawTitle")
    if not raw:
        raise ApiError("rawTitle is required")
    with write_tx() as c:
        r = merge.move_album_plays(c, from_album, to_album, [raw], f"plays filed under the wrong album (as “{raw}”)")
    return {**r, "undo": {"kind": "edit", "id": r["editId"]}}


@route("POST", "/api/songs/split-plays", mutating=True)
def split_plays(req):
    """Plays filed under an album they don't belong to, whose own album isn't in the library:
    split them out as that album (named from the name they arrived under). Undoable."""
    album_id, raw = req.int("albumId", required=True), req.str("rawTitle")
    title = (req.str("title") or split_title(raw)[0]).strip()
    if not raw or not title:
        raise ApiError("rawTitle is required")
    with write_tx() as c:
        merged = c.execute("SELECT id FROM merge_log WHERE entity_type = 'album' AND canonical_id = ? AND lower(absorbed_name) = lower(?) "
                           "AND undone_at IS NULL ORDER BY id DESC LIMIT 1", (album_id, raw)).fetchone()
        r = merge.split_album(c, album_id, [raw], {"title": title}, merge_log_id=merged[0] if merged else None)
    return {**r, "title": title, "undo": {"kind": "edit", "id": r["editId"]}}


@route("POST", "/api/songs/wrong-album-ok", mutating=True)
def wrong_album_ok(req):
    """"These plays are right where they are" -- remembered, never suggested again."""
    album_id, raw = req.int("albumId", required=True), req.str("rawTitle").lower()
    with write_tx() as c:
        cur = c.execute("INSERT OR IGNORE INTO review_marks (entity_type, entity_id, mark) VALUES ('album', ?, ?)", (album_id, WRONG_ALBUM_OK + raw))
    return {"undo": {"kind": "mark", "id": cur.lastrowid} if cur.rowcount else None}


@route("POST", "/api/songs/track-link", mutating=True)
def track_link(req):
    """Put a song on a tracklist line whose title doesn't say so (songId null: take it off again)."""
    album_id, title = req.int("albumId", required=True), req.str("title")
    song_id = req.body.get("songId")
    with write_tx() as c:
        r = merge.set_track_link(c, album_id, title, int(song_id) if song_id is not None else None)
    return {"undo": {"kind": "edit", "id": r["editId"]} if r["editId"] else None}


@route("POST", "/api/songs/tracklist-reviewed", mutating=True)
def tracklist_reviewed(req):
    album_id = req.int("albumId", required=True)
    with write_tx() as c:
        cur = c.execute("INSERT OR IGNORE INTO review_marks (entity_type, entity_id, mark) VALUES ('album', ?, ?)", (album_id, TRACKLIST_MARK))
    return {"undo": {"kind": "mark", "id": cur.lastrowid} if cur.rowcount else None}


# --- live / remix / demo recordings merged into the studio song ---------------------------------
# A different recording, not another edition: folding one into the plain song hides it (and its
# plays) inside the studio track. "edit"/"version"/"session" are left out -- too often the same take.
VARIANT_TAGS = ("live", "remix", "demo", "acoustic", "instrumental", "rerecording")


def _variant_merges(c) -> list[dict]:
    rows = c.execute(
        "SELECT m.id, m.absorbed_name, m.canonical_id, m.canonical_name, m.merged_at, m.undo_json, "
        "       so.album_id, al.title, ar.name "
        "FROM merge_log m LEFT JOIN songs so ON so.id = m.canonical_id "
        "LEFT JOIN albums al ON al.id = so.album_id LEFT JOIN artists ar ON ar.id = so.artist_id "
        "WHERE m.entity_type = 'song' AND m.undone_at IS NULL AND m.undo_json IS NOT NULL ORDER BY m.id DESC").fetchall()
    out = []
    for log_id, absorbed, canon_id, canon_name, merged_at, undo_json, canon_album, canon_album_title, artist in rows:
        tags = set(split_title(absorbed)[1]) & set(VARIANT_TAGS)
        tags -= split_title(canon_name)[1]
        if not tags:
            continue
        j = json.loads(undo_json)
        own_album = (j.get("absorbed_row") or {}).get("album_id")
        album = c.execute("SELECT title FROM albums WHERE id = ?", (own_album,)).fetchone() if own_album else None
        out.append({
            "logId": log_id, "absorbed": absorbed, "canonicalId": canon_id, "canonical": canon_name,
            "artist": artist, "tags": sorted(tags), "mergedAt": merged_at,
            "plays": len(j["moved"].get("scrobbles.song_id") or []), "shows": len(j["moved"].get("setlist_songs.song_id") or []),
            "album": album[0] if album else None, "canonicalAlbum": canon_album_title,
            "gone": canon_album is None,  # the studio song has since been merged away itself
        })
    return out


@route("GET", "/api/songs/variant-merges")
def variant_merges(req):
    with read_conn() as c:
        items = _variant_merges(c)
    return {"count": len(items), "items": items}


@route("POST", "/api/songs/undo-variant-merges", mutating=True)
def undo_variant_merges(req):
    """Un-merge the ticked ones, newest first, each on its own: one that can't be undone (a later
    action depends on it) is reported and the rest still go through."""
    ids = sorted({int(i) for i in (req.body.get("ids") or [])}, reverse=True)
    if not ids:
        raise ApiError("ids is required")
    undone, failed = [], []
    with write_tx() as c:
        allowed = {m["logId"] for m in _variant_merges(c)}
        for log_id in ids:
            if log_id not in allowed:
                failed.append({"logId": log_id, "error": "not a live/remix/demo merge (or already undone)"})
                continue
            c.execute("SAVEPOINT unmerge")
            try:
                r = merge.undo_merge(c, log_id)
                c.execute("RELEASE unmerge")
                undone.append({"logId": log_id, "name": r["restoredName"], "absorbedId": r["restoredId"],
                               "canonicalId": c.execute("SELECT canonical_id FROM merge_log WHERE id = ?", (log_id,)).fetchone()[0]})
            except merge.MergeError as e:
                c.execute("ROLLBACK TO unmerge")
                c.execute("RELEASE unmerge")
                failed.append({"logId": log_id, "error": str(e)})
    return {"undone": undone, "failed": failed,
            "undo": {"kind": "remerge", "id": [{"absorbedId": u["absorbedId"], "canonicalId": u["canonicalId"]} for u in undone]} if undone else None}
