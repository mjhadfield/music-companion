"""Album years from MusicBrainz (Maintenance > Albums > Types > Years, and the workbench's Suggested
fixes): an album with a MusicBrainz id but no year gets its release group's first-release year.

albums.year is the original release year (album = the work, not a pressing). The fill only ever
fills a gap: an album that has a year keeps it, whatever MusicBrainz says.

Three parts, as with the release types (release_types.py):
  sweep()  "album-years": one browse per artist (mbcache.artist_release_groups -- every release group
           with its first-release date and its types, so it warms the Types fill too), then a group
           lookup for the albums the browse didn't cover. Only warms mb_cache.
  plan()   what the cache says each album would get -- no network, no writes.
  apply()  the plan, inside the caller's transaction (batch action "yearFill"): one journaled edit
           per album (merge.edit_entity), so the batch undoes as a whole.
"""
import json
from collections import defaultdict

import merge
from common import RESOLVE_KEY, connect as db_connect

REASON = "year from MusicBrainz"


def _in(ids) -> str:
    return ",".join("?" * len(ids))


def mb_years(c, mbids) -> dict[str, int]:
    """Release group id -> MusicBrainz's first-release year, from the cache only (the group lookups
    and the artist discography browses -- no network). An edition id is followed to its group."""
    want = set(filter(None, mbids))
    if not want:
        return {}
    resolved = {k.split(":", 2)[2]: json.loads(p) for k, p in c.execute(
        f"SELECT key, payload_json FROM mb_cache WHERE key IN ({_in(want)})", [RESOLVE_KEY.format(m) for m in want])}
    groups = {m: (resolved.get(m) if isinstance(resolved.get(m), str) else m) for m in want}
    targets = set(groups.values())
    dates = {}
    for _key, payload in c.execute("SELECT key, payload_json FROM mb_cache WHERE key LIKE 'lookup:release-group:%' OR key LIKE 'browse:release-groups:%'"):
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        for rg in data if isinstance(data, list) else [data]:
            if isinstance(rg, dict) and rg.get("mbid") in targets and (rg.get("firstReleaseDate") or "")[:4].isdigit():
                dates[rg["mbid"]] = int(rg["firstReleaseDate"][:4])
    return {m: dates[g] for m, g in groups.items() if g in dates}


def _yearless(conn, artist_id: int | None = None) -> list[tuple]:
    """(album id, title, mbid, artist id, artist, artist mbid, plays, vinyl) for albums with an id and no year."""
    where = "AND al.id IN (SELECT album_id FROM album_artists WHERE artist_id = ?)" if artist_id else ""
    return conn.execute(f"""
        SELECT al.id, al.title, al.mbid, ar.id, ar.name, ar.mbid,
               (SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id),
               (SELECT count(*) FROM vinyl_holdings v WHERE v.album_id = al.id)
        FROM albums al JOIN artists ar ON ar.id = al.artist_id
        WHERE al.mbid IS NOT NULL AND al.year IS NULL {where}""", (artist_id,) if artist_id else ()).fetchall()


def plan(conn, artist_id: int | None = None) -> list[dict]:
    """Every album with an id and no year that the cache has a year for, most played first."""
    rows = _yearless(conn, artist_id)
    years = mb_years(conn, [r[2] for r in rows])
    out = [{"albumId": aid, "title": title, "mbid": mbid, "artistId": arid, "artist": artist, "plays": plays, "vinyl": vinyl, "year": years[mbid]}
           for aid, title, mbid, arid, artist, _ambid, plays, vinyl in rows if mbid in years]
    out.sort(key=lambda r: (-r["vinyl"], -r["plays"], r["title"]))
    return out


def preview(conn) -> dict:
    """The Years dry run: how many albums would get a year, how many are still to look up, the most played."""
    rows = _yearless(conn)
    planned = plan(conn)
    have = {r["albumId"] for r in planned}
    browsed = {k.split(":", 2)[2] for (k,) in conn.execute("SELECT key FROM mb_cache WHERE key LIKE 'browse:release-groups:%'")}
    to_browse = {r[5] for r in rows if r[0] not in have and r[5] and r[5] not in browsed}
    return {"yearless": len(rows), "count": len(planned), "used": sum(1 for r in planned if r["plays"] or r["vinyl"]),
            "artistsToLookUp": len(to_browse), "sample": planned[:60]}


def apply(c) -> tuple[list[dict], dict]:
    """Writes the plan inside the caller's transaction -> (undo handles, summary)."""
    handles = []
    for r in plan(c):
        edit = merge.edit_entity(c, "album", r["albumId"], {"year": r["year"]}, REASON)
        if edit["editId"]:
            handles.append({"kind": "edit", "id": edit["editId"]})
    return handles, {"text": f"Years for {len(handles)} album(s) from MusicBrainz", "rowsMoved": {"albums": len(handles)}}


def lookup_artists(conn, artist_id: int | None = None) -> list[tuple[str, str]]:
    """(artist mbid, name) whose discography browse would answer albums still without a year -- most
    played first."""
    rows = _yearless(conn, artist_id)
    have = set(mb_years(conn, [r[2] for r in rows]))
    browsed = {k.split(":", 2)[2] for (k,) in conn.execute("SELECT key FROM mb_cache WHERE key LIKE 'browse:release-groups:%'")}
    weight = defaultdict(int)
    names = {}
    for _aid, _title, mbid, _arid, artist, artist_mbid, plays, vinyl in rows:
        if mbid not in have and artist_mbid and artist_mbid not in browsed:
            weight[artist_mbid] += plays + 50 * vinyl
            names[artist_mbid] = artist
    return [(m, names[m]) for m in sorted(weight, key=lambda m: -weight[m])]


def sweep(limit: int, log, progress, cancelled, artist_id: int | None = None) -> None:
    """Browses the discography of up to `limit` artists with albums still missing a year (one request
    each), then looks up the groups those browses didn't cover (an album credited to another artist
    first). Only warms the cache; the Years dry run shows what it found."""
    import mbcache
    conn = db_connect()
    try:
        artists = lookup_artists(conn, artist_id)[:limit]
        log(f"Browsing the discography of {len(artists)} artist(s) on MusicBrainz — about {len(artists)} seconds.")
        for i, (mbid, name) in enumerate(artists):
            if cancelled():
                log("Cancelled.")
                return
            progress(i, len(artists))
            try:
                mbcache.artist_release_groups(mbid)
            except Exception as exc:  # one failed browse shouldn't end the sweep
                log(f"  ! {name}: {exc}")
        progress(len(artists), len(artists))
        if artist_id:   # one artist: the odd album the browse missed (another artist's credit) gets its own lookup
            rows = _yearless(conn, artist_id)
            have = set(mb_years(conn, [r[2] for r in rows]))
            for r in [r for r in rows if r[2] not in have][:20]:
                if cancelled():
                    return
                try:
                    mbcache.release_group(r[2])
                except Exception as exc:
                    log(f"  ! {r[1]}: {exc}")
        log(f"Done — {len(plan(conn, artist_id))} album(s) now have a year to fill. Review the dry run.")
    finally:
        conn.close()
