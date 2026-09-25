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
    # "keep this title": edition words you've decided belong (tied to the exact title -- a rename re-checks it)
    titles_kept = {(r[0], r[1]) for r in c.execute("SELECT entity_id, mark FROM review_marks WHERE entity_type = 'album' AND mark LIKE 'title-ok:%'")}

    cache = _cache(c, {f"lookup:release-group:{r[16]}" for r in rows if r[16]}
                   | {f"rg-first-release:{r[16]}" for r in rows if r[16]}
                   | {f"lookup:release-parent:{r[13]}" for r in rows if r[13]}
                   | {f"lookup:discogs-release:{r[2]}" for r in rows if r[2]}
                   | {f"lookup:discogs-api:{r[2]}" for r in rows if r[2]})

    look_cols = {r[1] for r in c.execute("PRAGMA table_info(vinyl_holdings)")}
    looks = {r[0]: r[1:] for r in c.execute("SELECT id, cover_file, display_title, release_year FROM vinyl_holdings")} if "cover_file" in look_cols else {}
    parts_of = defaultdict(list)  # a set's albums (migration 011)
    for set_id, pid, ptitle, pyear, pmbid in c.execute(
            "SELECT ap.album_id, al.id, al.title, al.year, al.mbid FROM album_parts ap JOIN albums al ON al.id = ap.part_album_id ORDER BY ap.position"):
        parts_of[set_id].append({"albumId": pid, "title": ptitle, "year": pyear, "mbid": pmbid})
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
        if original_year is None and mbid:  # the genres sweep saw this release group's first-release date
            original_year = _year(cache.get(f"rg-first-release:{mbid}"))
        h = {
            "vinylId": vid, "discogsReleaseId": rel_id, "catalogNumber": cat, "label": label, "format": fmt, "formatInfo": fmt_info,
            "pressingYear": pressing_year, "mediaCondition": media_c, "sleeveCondition": sleeve_c, "dateAdded": added,
            "rating": rating, "notes": notes, "rawArtist": raw_artist, "rawTitle": raw_title, "mbReleaseId": mb_rel,
            # This physical record's own title when it isn't just the album's -- e.g. the 1968 UK
            # "Electric Ladyland Part 1" / "Part 2" single LPs, two pressings of one album.
            "pressingTitle": raw_title if raw_title and base_key(raw_title) != base_key(title) else None,
            "album": {"albumId": album_id, "title": title, "year": year, "mbid": mbid, "coverStatus": cover,
                      "baseTitle": split_title(title)[0], "editionTags": sorted(split_title(title)[1]),
                      "scrobbleCount": plays.get(album_id, 0), "mergedIn": merged_in.get(album_id, 0),
                      "parts": parts_of.get(album_id, []), "titleKept": (album_id, TITLE_OK + title.lower()) in titles_kept},
            "artist": artist, "credits": creds,
            "artistMbidSet": sorted({m for x in creds for m in mbid_sets.get(x["artistId"], set())}),
            "otherPressings": [p for p in pressings[album_id] if p != vid],
            "versions": versions,
            "rg": rg, "pressingRg": pressing_rg, "originalYear": original_year,
            # the genres sweep found this release group browsing the artist's own discography
            "inArtistDiscography": bool(mbid) and f"rg-first-release:{mbid}" in cache,
            "discogsChecked": bool(rel_id) and discogs_key in cache, "discogsLinks": discogs or [],
            "discogs": cache.get(f"lookup:discogs-api:{rel_id}") if rel_id else None,
            "discogsDetailsChecked": bool(rel_id) and f"lookup:discogs-api:{rel_id}" in cache,
            "suggestions": [s for s in pending.get(vid, [])],
            # this copy's own look on the site (NULL = the album's): see migration 010
            "look": dict(zip(("coverFile", "displayTitle", "releaseYear"), looks.get(vid, (None, None, None)))),
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

    is_set = not a["mbid"] and bool(a["parts"])
    if is_set:
        # A set of albums (a 2-on-1 with no release group of its own): identified by its parts --
        # nothing on MusicBrainz to link, verify or date, so those count as done.
        names = " & ".join(f"“{p['title']}”" for p in a["parts"])
        add("album", "Album MBID", "na", f"A set of {names} — not on MusicBrainz as one release; identified by its albums")
        add("pressing", "Pressing linked", "na", "A set — MusicBrainz has no release of it to link")
        unlinked = [p["title"] for p in a["parts"] if not p["mbid"]]
        add("identity", "Identity verified", "warn" if unlinked else "na",
            f"{', '.join(unlinked)} {'has' if len(unlinked) == 1 else 'have'} no MusicBrainz id yet" if unlinked else "Its albums are each on MusicBrainz",
            "parts" if unlinked else None)
        add("year", "Original year", "na" if a["year"] else "warn", f"{a['year']} — the set's own year" if a["year"] else "The set has no year", None if a["year"] else "parts")
        cover = a["coverStatus"]
        add("cover", "Cover art", "ok" if cover == "ok" else "warn", "Has cover art" if cover == "ok" else "No cover — paste the set's sleeve as an image link",
            None if cover == "ok" else "cover")
    else:
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
        elif a["mbid"] and h["inArtistDiscography"]:
            add("identity", "Identity verified", "ok", "In this artist's own MusicBrainz discography")
        elif a["mbid"] and reviewed:
            add("identity", "Identity verified", "ok", "Confirmed (Discogs master or by hand)")
        elif a["mbid"]:
            # its own button: the pressing lookup can't help when MusicBrainz doesn't list the pressing
            add("identity", "Identity verified", "unknown", "Not checked against MusicBrainz yet", "check")
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
        elif h["formatInfo"]["reissue"] and h["pressingYear"] and a["year"] >= h["pressingYear"]:
            # no MusicBrainz date needed to know this one's wrong: a reissue can't predate the album
            add("year", "Original year", "bad", f"{a['year']} is this reissue's own pressing year, not the album's — look it up to get the original",
                "lookup")
        else:
            add("year", "Original year", "unknown", f"{a['year']} — not confirmed against MusicBrainz yet")

    if a["editionTags"] and a["titleKept"]:
        add("title", "Clean title", "ok", f"Kept as it is — its edition words ({', '.join(a['editionTags'])}) are your call")
    else:
        add("title", "Clean title", "warn" if a["editionTags"] else "ok",
            f"Title carries edition words ({', '.join(a['editionTags'])}) — the album should be named after the original"
            if a["editionTags"] else "Title has no edition suffix", "title" if a["editionTags"] else None)

    if not is_set:  # a set's cover was checked above: there's no MusicBrainz art to fetch for it
        cover = a["coverStatus"]
        add("cover", "Cover art", "ok" if cover == "ok" else "bad" if cover == "none" else "warn",
            {"ok": "Has cover art", "none": "Cover Art Archive had nothing — paste an image URL"}.get(cover, "No cover fetched yet"),
            None if cover == "ok" else "cover")

    # Several copies of one album: a copy that's really its own release (its own title on Discogs --
    # "Electric Ladyland Part 1" -- or a picture disc) should look like itself on the site.
    if h["otherPressings"]:
        look = h["look"]
        distinct = h["pressingTitle"] or ("Pic" in {t["code"] for t in h["formatInfo"]["tags"]})
        if look["coverFile"] or look["displayTitle"] or look["releaseYear"]:
            add("look", "Own look", "ok", "Has its own " + " & ".join(k for k, v in (("cover", look["coverFile"]), ("title", look["displayTitle"]),
                                                                                     ("year", look["releaseYear"])) if v))
        elif distinct:
            add("look", "Own look", "warn", f"{'“' + h['pressingTitle'] + '”' if h['pressingTitle'] else 'A picture disc'} — shows the album's cover & title "
                f"like your {len(h['otherPressings'])} other cop{'y' if len(h['otherPressings']) == 1 else 'ies'}", "look")
        else:
            add("look", "Own look", "ok", f"Shares the album's cover with your other cop{'y' if len(h['otherPressings']) == 1 else 'ies'}")

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
    with read_conn() as c:
        row = c.execute("SELECT v.disc_colour, d.format_text FROM vinyl_holdings v LEFT JOIN vinyl_details d ON d.holding_id = v.id "
                        "WHERE v.id = ?", (vid,)).fetchone()
    h["discColour"] = json.loads(row[0]) if row and row[0] else None
    h["formatText"] = row[1] if row else None  # Discogs' own colour wording, once pressing details are fetched
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


DISC_EFFECTS = {"solid", "translucent", "marbled", "splatter", "split", "swirl"}


@route("POST", "/api/vinyl/colour", mutating=True)
def disc_colour(req):
    """The record's colour set by hand (when the format text doesn't say, or says it wrong).
    colours: up to 3 names; effect: solid|translucent|marbled|splatter|split|swirl. clear=true
    goes back to what's detected. Undoable (an edit)."""
    vid = req.int("vinylId", required=True)
    if req.body.get("clear"):
        value = None
    else:
        colours = [str(x).strip().lower() for x in (req.body.get("colours") or []) if str(x).strip()][:3]
        effect = req.str("effect") or "solid"
        if not colours or effect not in DISC_EFFECTS:
            raise ApiError("pick at least one colour, and an effect")
        value = json.dumps({"colours": colours, "effect": effect})
    with write_tx() as c:
        e = merge.edit_entity(c, "vinyl", vid, {"disc_colour": value}, "record colour")
    return {"vinylId": vid, "discColour": json.loads(value) if value else None, "editId": e["editId"]}


@route("GET", "/api/vinyl/set-candidates")
def set_candidates(req):
    """The albums a set could contain: its credited artists' other albums (not sets themselves)."""
    vid = req.int("vinylId", required=True)
    with read_conn() as c:
        row = c.execute("SELECT album_id FROM vinyl_holdings WHERE id = ?", (vid,)).fetchone()
        if not row:
            raise ApiError("holding not found", 404)
        rows = c.execute(
            "SELECT DISTINCT al.id, al.title, al.year, al.mbid, (SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id) "
            "FROM albums al JOIN album_artists aa ON aa.album_id = al.id "
            "WHERE aa.artist_id IN (SELECT artist_id FROM album_artists WHERE album_id = ?) AND al.id != ? "
            "  AND NOT EXISTS (SELECT 1 FROM album_parts ap WHERE ap.album_id = al.id) "
            "ORDER BY coalesce(al.year, 9999), al.title", (row[0], row[0])).fetchall()
    return {"albumId": row[0], "albums": [{"albumId": r[0], "title": r[1], "year": r[2], "mbid": r[3], "plays": r[4]} for r in rows]}


@route("POST", "/api/vinyl/set-parts", mutating=True)
def set_parts(req):
    """This record's album is a set of these albums ([] = it isn't). Undoable (an edit)."""
    vid = req.int("vinylId", required=True)
    part_ids = [int(x) for x in (req.body.get("partIds") or [])]
    if len(part_ids) == 1:
        raise ApiError("A set is two or more albums -- for one album, fix the identity instead.")
    with write_tx() as c:
        row = c.execute("SELECT album_id FROM vinyl_holdings WHERE id = ?", (vid,)).fetchone()
        if not row:
            raise ApiError("holding not found", 404)
        try:
            r = merge.set_album_parts(c, row[0], part_ids)
        except merge.MergeError as exc:
            raise ApiError(str(exc))
    return {"vinylId": vid, **r}


TITLE_OK = "title-ok:"


@route("POST", "/api/vinyl/keep-title", mutating=True)
def keep_title(req):
    """This album's title stays as it is, edition words and all (e.g. "Star Trek (Original
    Soundtrack) (30th Anniversary)" -- the anniversary IS the album). Undoable (a mark)."""
    vid = req.int("vinylId", required=True)
    with write_tx() as c:
        row = c.execute("SELECT al.id, al.title FROM vinyl_holdings v JOIN albums al ON al.id = v.album_id WHERE v.id = ?", (vid,)).fetchone()
        if not row:
            raise ApiError("holding not found", 404)
        cur = c.execute("INSERT OR IGNORE INTO review_marks (entity_type, entity_id, mark) VALUES ('album', ?, ?)", (row[0], TITLE_OK + row[1].lower()))
    return {"albumId": row[0], "title": row[1], "undo": {"kind": "mark", "id": cur.lastrowid} if cur.rowcount else None}


@route("POST", "/api/vinyl/check-album", mutating=True)
def check_album(req):
    """Look the album's release group up on MusicBrainz (one request, cached) -- what the identity
    and original-year checks are worked out from. Needed on its own when MusicBrainz doesn't list
    the pressing (so the pressing lookup has nothing to go on), e.g. after an identity fix."""
    import mbcache
    vid = req.int("vinylId", required=True)
    with read_conn() as c:
        row = c.execute("SELECT al.mbid FROM vinyl_holdings v JOIN albums al ON al.id = v.album_id WHERE v.id = ?", (vid,)).fetchone()
    if not row:
        raise ApiError("holding not found", 404)
    if not row[0]:
        raise ApiError("The album has no MusicBrainz id yet -- set its identity first.", 409)
    try:
        rg = mbcache.release_group(row[0])
    except Exception as exc:  # noqa: BLE001
        raise ApiError(f"MusicBrainz request failed: {exc}", 502)
    return {"vinylId": vid, "found": bool(rg), "title": (rg or {}).get("title"), "firstReleaseDate": (rg or {}).get("firstReleaseDate")}


# -- A copy's own look (migration 010) ---------------------------------------------------------------

def _holding_look_row(c, vid: int):
    row = c.execute("SELECT v.id, v.discogs_release_id, v.mb_release_id, v.cover_file FROM vinyl_holdings v WHERE v.id = ?", (vid,)).fetchone()
    if not row:
        raise ApiError("holding not found", 404)
    return row


@route("GET", "/api/vinyl/copy-images")
def copy_images(req):
    """Cover choices for one copy: its MusicBrainz release on the Cover Art Archive, and its own
    Discogs release's photos (one throttled Discogs request the first time, cached after)."""
    import mbcache
    vid = req.int("vinylId", required=True)
    with read_conn() as c:
        _, rel_id, mb_rel, _ = _holding_look_row(c, vid)
    out = []
    if mb_rel:
        out.append({"source": "caa", "label": "Cover Art Archive — this pressing's MusicBrainz release",
                    "url": f"https://coverartarchive.org/release/{mb_rel}/front-500", "thumb": f"https://coverartarchive.org/release/{mb_rel}/front-250"})
    if rel_id:
        try:
            images = mbcache.discogs_release_images(rel_id)
        except Exception as exc:  # noqa: BLE001 -- Discogs down / rate-limited: the other options still work
            raise ApiError(f"Couldn't reach Discogs: {exc}", 502)
        for i, im in enumerate(images):
            out.append({"source": "discogs", "label": f"Discogs photo {i + 1}{' (the main one)' if im.get('type') == 'primary' else ''}",
                        "url": im["uri"], "thumb": im.get("uri150") or im["uri"], "width": im.get("width"), "height": im.get("height")})
    return {"vinylId": vid, "images": out}


@route("POST", "/api/vinyl/copy-cover", mutating=True)
def copy_cover(req):
    """Give one copy its own cover: url (a choice above, or pasted), or data (a base64 image the
    browser uploaded), or clear=true to go back to the album's. Undoable (an edit)."""
    import base64
    import covers
    vid = req.int("vinylId", required=True)
    with read_conn() as c:
        _holding_look_row(c, vid)
    if req.body.get("clear"):
        name = None
    else:
        if req.body.get("data"):
            try:
                content = base64.b64decode(str(req.body["data"]).split(",")[-1], validate=True)
            except ValueError:
                raise ApiError("That upload isn't a valid image")
            if not covers.sniff_image(content):
                raise ApiError("That file isn't a JPEG, PNG or WebP image")
            if len(content) > covers.MAX_COVER_BYTES:
                raise ApiError("That image is over 5 MB")
        else:
            url = req.str("url")
            if not url.startswith(("http://", "https://")):
                raise ApiError("url must start with http:// or https://")
            content, error = covers.download(url)  # outside any transaction: a network call
            if error:
                raise ApiError(error)
        name = covers.save_holding_cover(vid, content)
    with write_tx() as c:
        e = merge.edit_entity(c, "vinyl", vid, {"cover_file": name}, "this copy's cover")
    return {"vinylId": vid, "coverFile": name, "editId": e["editId"]}


@route("POST", "/api/vinyl/copy-identity", mutating=True)
def copy_identity(req):
    """This copy's own title / first-release year on the site; blank = the album's. Undoable."""
    vid = req.int("vinylId", required=True)
    title = (req.str("displayTitle") or "").strip() or None
    year = req.body.get("releaseYear")
    year = int(year) if str(year or "").strip().isdigit() else None
    if year is not None and not 1900 <= year <= 2100:
        raise ApiError("That doesn't look like a year")
    with write_tx() as c:
        album_title = c.execute("SELECT al.title FROM vinyl_holdings v JOIN albums al ON al.id = v.album_id WHERE v.id = ?", (vid,)).fetchone()
        if not album_title:
            raise ApiError("holding not found", 404)
        if title == album_title[0]:
            title = None  # the same as the album's is no override at all
        e = merge.edit_entity(c, "vinyl", vid, {"display_title": title, "release_year": year}, "this copy's title & year")
    return {"vinylId": vid, "displayTitle": title, "releaseYear": year, "editId": e["editId"]}


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
