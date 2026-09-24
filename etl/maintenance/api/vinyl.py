"""Vinyl holdings: the at-a-glance collection view, per-holding detail, the exact
Discogs -> MusicBrainz pressing link, moving a pressing between albums, and reviewed bulk fixes.

Model: a holding is one physical *pressing* (discogs_release_id, and once linked, its exact
MusicBrainz release in mb_release_id). Its album is the *work* -- one album row across every
pressing, identified by the release group (albums.mbid), carrying the ORIGINAL release's year
and title even when the record on the shelf is a 2021 reissue.
"""
import json
import re
from collections import defaultdict
from itertools import combinations

import merge
from api.core import ApiError, read_conn, route, write_tx
from common import artist_mbid_sets
from api.albums import album_profiles, _album_group_key, _dismissed_pairs, _pair
from suggest import link_suggestions_for_holding
from titles import base_key, split_title

# Discogs' format abbreviations -> what they mean. kind drives the badge colour.
FORMAT_TAGS = {
    "RE": ("Reissue", "reissue"), "RP": ("Repress", "reissue"), "RM": ("Remastered", "reissue"),
    "Gat": ("Gatefold", "feature"), "Ltd": ("Limited", "feature"), "Num": ("Numbered", "feature"),
    "180": ("180g", "feature"), "200": ("200g", "feature"), "Emb": ("Embossed", "feature"), "Etch": ("Etched", "feature"),
    "S/Sided": ("Single-sided", "feature"), "RSD": ("Record Store Day", "feature"), "TP": ("Test pressing", "feature"),
    "Promo": ("Promo", "feature"), "Pic": ("Picture disc", "feature"), "Club": ("Club edition", "feature"),
    "Unofficial": ("Unofficial", "warn"), "Dlx": ("Deluxe", "feature"), "Box": ("Box set", "feature"),
    "Comp": ("Compilation", "kind"), "Album": ("Album", "kind"), "EP": ("EP", "kind"), "Single": ("Single", "kind"),
    "Mono": ("Mono", "feature"), "Stereo": ("Stereo", "feature"), "Quad": ("Quadraphonic", "feature"),
    "Red": ("Red", "colour"), "Blu": ("Blue", "colour"), "Yel": ("Yellow", "colour"), "Gre": ("Green", "colour"),
    "Ora": ("Orange", "colour"), "Pur": ("Purple", "colour"), "Whi": ("White", "colour"), "Bla": ("Black", "colour"),
    "Cle": ("Clear", "colour"), "Tra": ("Transparent", "colour"), "Gol": ("Gold", "colour"), "Sil": ("Silver", "colour"),
    "Pin": ("Pink", "colour"), "Ros": ("Rose", "colour"), "Bro": ("Brown", "colour"), "Gry": ("Grey", "colour"),
    "Mar": ("Marbled", "colour"), "Spl": ("Splatter", "colour"),
}
MEDIA_RE = re.compile(r'^(\d+x)?(LP|12"|10"|7"|Vinyl|Box Set|CD|Cass)$', re.IGNORECASE)


def parse_format(fmt: str | None) -> dict:
    """"2xLP, Album, RE, Gat" -> {"media": "2xLP", "tags": [{"code","label","kind"}], "reissue": True}."""
    media, tags = [], []
    for part in re.split(r"\s*\+\s*", fmt or ""):
        for tok in [t.strip() for t in part.split(",") if t.strip()]:
            if MEDIA_RE.match(tok):
                media.append(tok)
                continue
            label, kind = FORMAT_TAGS.get(tok, (tok, "other"))
            if not any(t["code"] == tok for t in tags):
                tags.append({"code": tok, "label": label, "kind": kind})
    return {"media": " + ".join(media) or None, "tags": tags, "reissue": any(t["kind"] == "reissue" for t in tags)}


def _cache(c, keys: set[str]) -> dict:
    keys = [k for k in keys if k]
    if not keys:
        return {}
    out = {}
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        out.update({k: json.loads(v) for k, v in c.execute(
            f"SELECT key, payload_json FROM mb_cache WHERE key IN ({','.join('?' * len(chunk))})", chunk)})
    return out


def _year(date: str | None) -> int | None:
    return int(date[:4]) if date and date[:4].isdigit() else None


def holdings(c, ids: list[int] | None = None) -> list[dict]:
    """Every holding (or just `ids`) with everything the grid, drawer and checks need."""
    where = f"WHERE vh.id IN ({','.join('?' * len(ids))})" if ids else ""
    rows = c.execute(
        f"""
        SELECT vh.id, vh.album_id, vh.discogs_release_id, vh.catalog_number, vh.label, vh.format, vh.media_condition,
               vh.sleeve_condition, vh.date_added, vh.rating, vh.notes, vh.raw_artist_text, vh.raw_title_text, vh.mb_release_id,
               al.title, al.year, al.mbid, al.cover_status, al.artist_id
        FROM vinyl_holdings vh JOIN albums al ON al.id = vh.album_id {where}
        """, ids or []).fetchall()
    if not rows:
        return []
    album_ids = sorted({r[1] for r in rows})
    ain = ",".join("?" * len(album_ids))

    credits = defaultdict(list)
    for album_id, artist_id, name, mbid in c.execute(
            f"SELECT aa.album_id, ar.id, ar.name, ar.mbid FROM album_artists aa JOIN artists ar ON ar.id = aa.artist_id "
            f"WHERE aa.album_id IN ({ain}) ORDER BY aa.album_id, aa.position", album_ids):
        credits[album_id].append({"artistId": artist_id, "name": name, "mbid": mbid})
    credited_ids = sorted({x["artistId"] for v in credits.values() for x in v})
    mbid_sets = artist_mbid_sets(c, credited_ids) if credited_ids else {}
    primary = {r[0]: {"artistId": r[1], "name": r[2], "mbid": r[3]} for r in c.execute(
        f"SELECT al.id, ar.id, ar.name, ar.mbid FROM albums al JOIN artists ar ON ar.id = al.artist_id WHERE al.id IN ({ain})", album_ids)}
    plays = dict(c.execute(f"SELECT album_id, count(*) FROM scrobbles WHERE album_id IN ({ain}) GROUP BY album_id", album_ids).fetchall())
    pressings = defaultdict(list)
    for album_id, vid in c.execute(f"SELECT album_id, id FROM vinyl_holdings WHERE album_id IN ({ain})", album_ids):
        pressings[album_id].append(vid)
    released = {}
    for rel, raw in c.execute(
            "SELECT json_extract(raw_json, '$.release_id'), json_extract(raw_json, '$.Released') FROM staging_discogs_rows"):
        m = re.search(r"\d{4}", str(raw or ""))
        if rel and m:
            released[str(rel)] = int(m.group())
    pending = {}
    for sid, eid, mbid, label, ev in c.execute(
            "SELECT id, entity_id, mbid, label, evidence_json FROM suggestions WHERE entity_type = 'vinyl' AND status = 'pending'"):
        pending.setdefault(eid, []).append({"id": sid, "releaseMbid": mbid, "label": label, "evidence": json.loads(ev)})
    # A suggestion's relation to the album (confirms / sets / differs) was worked out when it was
    # made; the album may have changed since (a merge, an identity fix) -- recompute it below.
    album_mbid_of_holding = {r[0]: r[16] for r in rows}
    for vid, sugs in pending.items():
        am = album_mbid_of_holding.get(vid)
        for sg in sugs:
            rgm = (sg["evidence"].get("rg") or {}).get("mbid")
            sg["evidence"]["relation"] = "confirms" if rgm and rgm == am else "sets" if not am else "differs"

    # Other versions of the same album (same edition-normalised title, same artist, not dismissed).
    artist_ids = sorted({r[18] for r in rows})
    by_artist = defaultdict(list)
    for aid, title, artist_id in c.execute(
            f"SELECT id, title, artist_id FROM albums WHERE artist_id IN ({','.join('?' * len(artist_ids))})", artist_ids):
        by_artist[artist_id].append((aid, _album_group_key(title)))
    dismissed = _dismissed_pairs(c)
    merged_in = dict(c.execute(
        f"SELECT canonical_id, count(*) FROM merge_log WHERE entity_type = 'album' AND undone_at IS NULL AND canonical_id IN ({ain}) GROUP BY canonical_id",
        album_ids).fetchall())
    reviewed = {r[0] for r in c.execute("SELECT entity_id FROM review_marks WHERE entity_type = 'album' AND mark = 'verified:album-mbid'")}

    cache = _cache(c, {f"lookup:release-group:{r[16]}" for r in rows if r[16]}
                   | {f"lookup:release-parent:{r[13]}" for r in rows if r[13]}
                   | {f"lookup:discogs-release:{r[2]}" for r in rows if r[2]}
                   | {f"lookup:discogs-api:{r[2]}" for r in rows if r[2]})

    out = []
    for (vid, album_id, rel_id, cat, label, fmt, media_c, sleeve_c, added, rating, notes, raw_artist, raw_title, mb_rel,
         title, year, mbid, cover, artist_id) in rows:
        artist = primary[album_id]
        creds = credits.get(album_id) or [artist]
        key = _album_group_key(title)
        versions = [a for a, k in by_artist[artist_id] if k == key and a != album_id and _pair(a, album_id) not in dismissed]
        rg = cache.get(f"lookup:release-group:{mbid}") if mbid else None
        rg_checked = bool(mbid) and f"lookup:release-group:{mbid}" in cache
        pressing_rg = cache.get(f"lookup:release-parent:{mb_rel}") if mb_rel else None
        discogs_key = f"lookup:discogs-release:{rel_id}"
        discogs = cache.get(discogs_key) if rel_id else None
        fmt_info = parse_format(fmt)
        pressing_year = released.get(str(rel_id)) if rel_id else None
        # The album's own release group decides the original year; the pressing's release group
        # only stands in when it IS the album's (a mis-linked pressing must not suggest a year).
        year_source = rg or (pressing_rg if pressing_rg and pressing_rg.get("mbid") == mbid else None)
        original_year = _year(year_source.get("firstReleaseDate")) if year_source else None
        h = {
            "vinylId": vid, "discogsReleaseId": rel_id, "catalogNumber": cat, "label": label, "format": fmt, "formatInfo": fmt_info,
            "pressingYear": pressing_year, "mediaCondition": media_c, "sleeveCondition": sleeve_c, "dateAdded": added,
            "rating": rating, "notes": notes, "rawArtist": raw_artist, "rawTitle": raw_title, "mbReleaseId": mb_rel,
            # This physical record's own title when it isn't just the album's -- e.g. the 1968 UK
            # "Electric Ladyland Part 1" / "Part 2" single LPs, two pressings of one album.
            "pressingTitle": raw_title if raw_title and base_key(raw_title) != base_key(title) else None,
            "album": {"albumId": album_id, "title": title, "year": year, "mbid": mbid, "coverStatus": cover,
                      "baseTitle": split_title(title)[0], "editionTags": sorted(split_title(title)[1]),
                      "scrobbleCount": plays.get(album_id, 0), "mergedIn": merged_in.get(album_id, 0)},
            "artist": artist, "credits": creds,
            "artistMbidSet": sorted({m for x in creds for m in mbid_sets.get(x["artistId"], set())}),
            "otherPressings": [p for p in pressings[album_id] if p != vid],
            "versions": versions,
            "rg": rg, "pressingRg": pressing_rg, "originalYear": original_year,
            "discogsChecked": bool(rel_id) and discogs_key in cache, "discogsLinks": discogs or [],
            "discogs": cache.get(f"lookup:discogs-api:{rel_id}") if rel_id else None,
            "discogsDetailsChecked": bool(rel_id) and f"lookup:discogs-api:{rel_id}" in cache,
            "suggestions": [s for s in pending.get(vid, [])],
        }
        h["checks"] = _checks(h, rg_checked, album_id in reviewed)
        h["score"] = sum({"ok": 0, "na": 0, "unknown": 1, "warn": 2, "bad": 4}[x["status"]] for x in h["checks"])
        out.append(h)
    return out


def _checks(h: dict, rg_checked: bool, reviewed: bool) -> list[dict]:
    a = h["album"]
    checks = []

    def add(key, label, status, text, fix=None):
        checks.append({"key": key, "label": label, "status": status, "text": text, "fix": fix})

    missing_artists = [x["name"] for x in h["credits"] if not x["mbid"]]
    add("artist", "Artist MBID", "bad" if missing_artists else "ok",
        f"No MBID for {', '.join(missing_artists)}" if missing_artists else "Every credited artist has an MBID")

    sets = [x for x in h["suggestions"] if x["evidence"].get("relation") == "sets"]
    add("album", "Album MBID", "ok" if a["mbid"] else "bad",
        "Album is linked to a MusicBrainz release group" if a["mbid"]
        else "Album has no MusicBrainz release group yet" + (" — an exact match is waiting for review" if sets else ""),
        None if a["mbid"] else "suggestion" if sets else "identity")

    release_suggestions = [x for x in h["suggestions"] if x["evidence"].get("kind") != "master"]
    if h["mbReleaseId"]:
        add("pressing", "Pressing linked", "ok", "This exact pressing is linked to its MusicBrainz release")
    elif release_suggestions:
        add("pressing", "Pressing linked", "warn", "MusicBrainz links this Discogs release to a release — review it", "suggestion")
    elif h["discogsChecked"] and not h["discogsLinks"]:
        # Nothing a human can do about it here -- counts as done ("na"), so a record can still be complete.
        add("pressing", "Pressing linked", "na", "MusicBrainz doesn't list this exact pressing yet — nothing to link (the album is identified separately)")
    elif h["discogsChecked"]:
        add("pressing", "Pressing linked", "unknown", "The MusicBrainz link for this pressing was rejected")
    else:
        add("pressing", "Pressing linked", "unknown", "Not checked against MusicBrainz yet", "lookup")

    prg, rg = h["pressingRg"], h["rg"]
    if a["mbid"] and prg:
        if prg.get("mbid") == a["mbid"]:
            add("identity", "Identity verified", "ok", "This pressing belongs to the album's release group")
        else:
            add("identity", "Identity verified", "bad", f"This pressing belongs to “{prg.get('title')}” ({prg.get('firstReleaseDate') or '?'}), "
                "not the album's release group — the album's MBID or the pressing's album is wrong", "identity")
    elif a["mbid"] and any(x["evidence"].get("relation") == "differs" for x in h["suggestions"]):
        add("identity", "Identity verified", "bad", "Discogs/MusicBrainz point this record at a different release group — review the link", "suggestion")
    elif a["mbid"] and rg_checked:
        artist_mbids = set(h["artistMbidSet"])  # incl. "also releases as" credits
        if rg is None:
            add("identity", "Identity verified", "bad", "The album's MBID isn't a release group on MusicBrainz", "identity")
        elif artist_mbids and not artist_mbids & set(rg.get("artistMbids") or []):
            add("identity", "Identity verified", "bad", f"MusicBrainz credits that release group to “{rg.get('artistCredit')}”", "credit")
            checks[-1]["credit"] = {"mbids": rg.get("artistMbids") or [], "name": rg.get("artistCredit"), "artistId": h["artist"]["artistId"],
                                    "artistName": h["artist"]["name"]}
        else:
            add("identity", "Identity verified", "ok", "Release group is credited to this artist")
    elif a["mbid"] and reviewed:
        add("identity", "Identity verified", "ok", "Confirmed (Discogs master or by hand)")
    elif a["mbid"]:
        add("identity", "Identity verified", "unknown", "Not checked against MusicBrainz yet", "lookup")
    else:
        add("identity", "Identity verified", "unknown", "Needs an album MBID first")

    if not a["year"]:
        add("year", "Original year", "bad", "Album has no year" + (f" — originally {h['originalYear']}" if h["originalYear"] else ""),
            "year" if h["originalYear"] else None)
    elif h["originalYear"] and a["year"] != h["originalYear"]:
        add("year", "Original year", "warn", f"Album says {a['year']}, but it was first released in {h['originalYear']}"
            + (f" (this pressing is from {h['pressingYear']})" if h["pressingYear"] else ""), "year")
    elif h["originalYear"]:
        add("year", "Original year", "ok", f"{a['year']} matches the original release")
    else:
        add("year", "Original year", "unknown", f"{a['year']} — not confirmed against MusicBrainz yet")

    add("title", "Clean title", "warn" if a["editionTags"] else "ok",
        f"Title carries edition words ({', '.join(a['editionTags'])}) — the album should be named after the original"
        if a["editionTags"] else "Title has no edition suffix", "title" if a["editionTags"] else None)

    cover = a["coverStatus"]
    add("cover", "Cover art", "ok" if cover == "ok" else "bad" if cover == "none" else "warn",
        {"ok": "Has cover art", "none": "Cover Art Archive had nothing — paste an image URL"}.get(cover, "No cover fetched yet"),
        None if cover == "ok" else "cover")

    n = len(h["versions"])
    add("versions", "No duplicates", "warn" if n else "ok",
        f"{n} other version{'s' if n != 1 else ''} of this album in your library" if n else "No other versions of this album", "versions" if n else None)
    return checks


@route("GET", "/api/vinyl/holdings")
def list_holdings(req):
    with read_conn() as c:
        items = holdings(c)
    summary = defaultdict(lambda: defaultdict(int))
    for h in items:
        for x in h["checks"]:
            summary[x["key"]][x["status"]] += 1
    return {"count": len(items), "holdings": items, "summary": summary}


@route("GET", "/api/vinyl/holding")
def one_holding(req):
    vid = req.int("id", required=True)
    with read_conn() as c:
        found = holdings(c, [vid])
        if not found:
            raise ApiError("holding not found", 404)
        h = found[0]
        others = holdings(c, h["otherPressings"]) if h["otherPressings"] else []
        profiles = album_profiles(c, [h["album"]["albumId"], *h["versions"]])
    h["otherPressingDetails"] = [{k: o[k] for k in ("vinylId", "discogsReleaseId", "catalogNumber", "label", "format", "formatInfo",
                                                    "pressingYear", "mbReleaseId", "dateAdded", "rawTitle", "pressingTitle")} for o in others]
    h["albumProfile"] = profiles.get(h["album"]["albumId"])
    h["versionProfiles"] = [profiles[i] for i in h["versions"] if i in profiles]
    return h


@route("POST", "/api/vinyl/discogs-lookup", mutating=True)
def discogs_lookup(req):
    """One holding's exact Discogs -> MusicBrainz lookup, on demand (the sweep does this in bulk)."""
    vid = req.int("holdingId", required=True)
    made = link_suggestions_for_holding(vid)
    with read_conn() as c:
        h = holdings(c, [vid])[0]
    return {"suggestionsMade": made, "holding": h}


def _link(c, vid: int, release_mbid: str, suggestion_id: int | None, reason: str) -> int | None:
    edit = merge.edit_entity(c, "vinyl", vid, {"mb_release_id": release_mbid}, reason)
    if suggestion_id:
        c.execute("UPDATE suggestions SET status = 'accepted', decided_at = datetime('now') WHERE id = ?", (suggestion_id,))
        c.execute("UPDATE suggestions SET status = 'rejected', decided_at = datetime('now') "
                  "WHERE entity_type = 'vinyl' AND entity_id = ? AND status = 'pending' AND id != ?", (vid, suggestion_id))
    return edit["editId"]


@route("POST", "/api/vinyl/link-release", mutating=True)
def link_release(req):
    """Accept MusicBrainz's link for this pressing. albumAction decides what happens to the album:
      none        -- just link the pressing (its release group already is the album's)
      identity    -- give the album this release group (+ original year; title if useTitle)
      move        -- the pressing is on the wrong album: move it to moveToAlbumId
    An mbid conflict (another album already has that release group) comes back as a 409 so the
    UI can offer the compare-and-merge instead."""
    vid, sid = req.int("holdingId", required=True), req.int("suggestionId", required=True)
    action = req.str("albumAction") or "none"
    with write_tx() as c:
        row = c.execute("SELECT entity_id, mbid, evidence_json, status FROM suggestions WHERE id = ? AND entity_type = 'vinyl'", (sid,)).fetchone()
        if not row or row[0] != vid:
            raise ApiError("suggestion not found for this holding", 404)
        if row[3] != "pending":
            raise ApiError(f"already {row[3]}", 409)
        ev = json.loads(row[2])
        rg = ev.get("rg") or {}
        is_master = ev.get("kind") == "master"
        edit_ids = []
        if action == "identity":
            album_id = c.execute("SELECT album_id FROM vinyl_holdings WHERE id = ?", (vid,)).fetchone()[0]
            changes = {"mbid": rg["mbid"]}
            if _year(rg.get("firstReleaseDate")):
                changes["year"] = _year(rg.get("firstReleaseDate"))
            if req.body.get("useTitle") and rg.get("title"):
                changes["title"] = rg["title"]
            current = c.execute("SELECT mbid FROM albums WHERE id = ?", (album_id,)).fetchone()[0]
            if current != rg["mbid"]:
                changes.update({"cover_status": None, "cover_updated_at": None})
            try:
                e = merge.edit_entity(c, "album", album_id, changes, f"suggestion:{sid}")
            except merge.MergeError as exc:
                if exc.code == "mbid_conflict":
                    raise ApiError(str(exc), 409, "mbid_conflict", conflictingAlbum={"albumId": exc.conflict["id"], "title": exc.conflict["name"]},
                                   identity={"mbid": rg["mbid"], "title": rg.get("title"), "year": _year(rg.get("firstReleaseDate"))})
                raise
            edit_ids.append(e["editId"])
        elif action == "move":
            target = req.int("moveToAlbumId", required=True)
            edit_ids.append(_move(c, vid, target, f"suggestion:{sid}"))
        elif action != "none":
            raise ApiError("albumAction must be none, identity or move")
        if is_master:
            # A master link identifies the album, not this pressing: nothing to store on the
            # holding. Accepting a *confirming* one records that a human checked the identity;
            # when it set/changed the identity instead, that edit is the record (and its undo
            # must not leave a stale "verified" mark behind).
            if action == "none":
                album_id = c.execute("SELECT album_id FROM vinyl_holdings WHERE id = ?", (vid,)).fetchone()[0]
                c.execute("INSERT OR IGNORE INTO review_marks (entity_type, entity_id, mark) VALUES ('album', ?, 'verified:album-mbid')", (album_id,))
            c.execute("UPDATE suggestions SET status = 'accepted', decided_at = datetime('now') WHERE id = ?", (sid,))
            c.execute("UPDATE suggestions SET status = 'rejected', decided_at = datetime('now') "
                      "WHERE entity_type = 'vinyl' AND entity_id = ? AND status = 'pending' AND id != ?", (vid, sid))
        else:
            edit_ids.append(_link(c, vid, row[1], sid, f"suggestion:{sid}"))
    return {"holdingId": vid, "editIds": [e for e in edit_ids if e]}


def _move(c, vid: int, album_id: int, reason: str) -> int | None:
    h = c.execute("SELECT vh.album_id, al.artist_id FROM vinyl_holdings vh JOIN albums al ON al.id = vh.album_id WHERE vh.id = ?", (vid,)).fetchone()
    target = c.execute("SELECT id, artist_id FROM albums WHERE id = ?", (album_id,)).fetchone()
    if not h or not target:
        raise ApiError("holding or album not found", 404)
    credited = {r[0] for r in c.execute("SELECT artist_id FROM album_artists WHERE album_id = ?", (h[0],))} | {h[1]}
    if target[1] not in credited:
        raise ApiError("That album is by a different artist — a pressing can only move between albums by the same artist.", 409, "different_artist")
    return merge.edit_entity(c, "vinyl", vid, {"album_id": album_id}, reason)["editId"]


@route("POST", "/api/vinyl/move", mutating=True)
def move(req):
    vid, album_id = req.int("holdingId", required=True), req.int("albumId", required=True)
    with write_tx() as c:
        edit_id = _move(c, vid, album_id, "moved pressing")
        title = c.execute("SELECT title FROM albums WHERE id = ?", (album_id,)).fetchone()[0]
    return {"holdingId": vid, "albumId": album_id, "albumTitle": title, "editId": edit_id}


@route("GET", "/api/vinyl/bulk-preview")
def bulk_preview(req):
    """What a bulk action WOULD do, row by row, so it can be reviewed before anything is applied."""
    action = req.str("action")
    with read_conn() as c:
        items = holdings(c)
    rows = []
    if action == "link-confirmations":
        for h in items:
            for s in h["suggestions"]:
                rg = s["evidence"].get("rg") or {}
                if h["album"]["mbid"] and rg.get("mbid") == h["album"]["mbid"] and not h["mbReleaseId"]:
                    master = s["evidence"].get("kind") == "master"
                    rows.append({"holdingId": h["vinylId"], "suggestionId": s["id"], "artist": h["artist"]["name"], "title": h["album"]["title"],
                                 "detail": "identity confirmed via Discogs master" if master else f"link pressing → {s['evidence'].get('releaseTitle') or s['label']}",
                                 "releaseMbid": None if master else s["releaseMbid"], "format": h["format"], "pressingYear": h["pressingYear"]})
                    break
    elif action == "original-years":
        seen = set()
        for h in items:
            check = next(x for x in h["checks"] if x["key"] == "year")
            ident = next(x for x in h["checks"] if x["key"] == "identity")
            a = h["album"]
            if check["fix"] == "year" and ident["status"] == "ok" and a["albumId"] not in seen:
                seen.add(a["albumId"])
                rows.append({"holdingId": h["vinylId"], "albumId": a["albumId"], "artist": h["artist"]["name"], "title": a["title"],
                             "detail": f"{a['year'] or '—'} → {h['originalYear']}", "year": h["originalYear"],
                             "pressingYear": h["pressingYear"], "format": h["format"]})
    else:
        raise ApiError("unknown action")
    return {"action": action, "rows": rows}


@route("POST", "/api/vinyl/bulk", mutating=True)
def bulk(req):
    """Apply a reviewed bulk action to exactly the rows the human left ticked -- one transaction."""
    action = req.str("action")
    rows = req.body.get("rows") or []
    edit_ids = []
    confirmed = 0  # master-link confirmations: a review mark, not an edit
    with write_tx() as c:
        if action == "link-confirmations":
            for r in rows:
                sid, vid = int(r["suggestionId"]), int(r["holdingId"])
                s = c.execute("SELECT mbid, status, entity_id, evidence_json FROM suggestions WHERE id = ?", (sid,)).fetchone()
                if not s or s[1] != "pending" or s[2] != vid:
                    continue
                if json.loads(s[3]).get("kind") == "master":
                    album_id = c.execute("SELECT album_id FROM vinyl_holdings WHERE id = ?", (vid,)).fetchone()[0]
                    c.execute("INSERT OR IGNORE INTO review_marks (entity_type, entity_id, mark) VALUES ('album', ?, 'verified:album-mbid')", (album_id,))
                    c.execute("UPDATE suggestions SET status = 'accepted', decided_at = datetime('now') WHERE id = ?", (sid,))
                    confirmed += 1
                    continue
                edit_ids.append(_link(c, vid, s[0], sid, f"suggestion:{sid}"))
        elif action == "original-years":
            for r in rows:
                e = merge.edit_entity(c, "album", int(r["albumId"]), {"year": int(r["year"])}, "original year from MusicBrainz")
                edit_ids.append(e["editId"])
        else:
            raise ApiError("unknown action")
    edit_ids = [e for e in edit_ids if e]
    return {"applied": len(edit_ids) + confirmed, "editIds": edit_ids}
