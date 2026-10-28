"""What kind of release each album is (Maintenance > Albums > Types) -- so the site's artist page can
show the albums as the discography and fold the singles, compilations and odds and ends away.

albums.release_type is one of KINDS, worked out from MusicBrainz's release-group type:
  album        a studio album                    \
  ep           an EP                              > the discography (MAIN)
  live         a dedicated live album or live EP /  (Motörhead's No Sleep 'til Hammersmith)
  single       a single -- live and remix singles too (Mike: "random live versions" fold away)
  compilation  a best-of / collection, live ones included
  other        soundtracks, remix albums, mixtapes, demos, broadcasts...
NULL means not typed yet: no MusicBrainz id, or the type hasn't been looked up. The site folds those
away with "other" until they're typed.

albums.release_type_source is 'musicbrainz' (the fill) or 'manual' (set by hand). The fill never
touches a manual one, and does correct a MusicBrainz one whose album has changed id since.

Three parts, as with the genre fill:
  sweep()  "album-types": looks up the albums whose type isn't cached yet -- only warms mb_cache.
  plan()   what the cache says each album would get -- no network, no writes.
  apply()  the plan, inside the caller's transaction (batch action "releaseTypeFill"): one
           journaled edit per album changed (merge.edit_entity), so the batch undoes as a whole.
"""
import json

import merge
from common import RELEASE_PARENT_KEY, RESOLVE_KEY, connect as db_connect

KINDS = ("album", "ep", "live", "single", "compilation", "other")
MAIN = ("album", "ep", "live")
REASON = "release type from MusicBrainz"
# More songs than a single or an EP plausibly has: the album's id is most likely its title track's
# single (Tool's Lateralus, System of a Down's Hypnotize) -- fix the id rather than fold the album away.
TOO_MANY = {"single": 5, "ep": 9}


def classify(primary: str | None, secondary: list[str]) -> str:
    """MusicBrainz's primary type + secondary types -> one of KINDS."""
    secondary = set(secondary or [])
    if primary == "Single":
        return "single"
    if "Compilation" in secondary:
        return "compilation"
    if "Live" in secondary:
        return "live" if primary in ("Album", "EP") else "other"
    if secondary:                       # Soundtrack, Remix, Mixtape/Street, DJ-mix, Demo, Spokenword...
        return "other"
    return {"Album": "album", "EP": "ep"}.get(primary, "other")


def _mb_types(conn) -> tuple[dict, dict, dict]:
    """From the cache: release group -> (primary, secondaries); release -> its group's types; release ->
    group id. Only payloads that carry secondary types count -- the genre browse has the primary type
    alone, which can't tell a studio album from a compilation."""
    groups, parents, resolved = {}, {}, {}
    for key, payload in conn.execute(
            "SELECT key, payload_json FROM mb_cache WHERE key LIKE 'lookup:release-group:%' OR key LIKE 'browse:release-groups:%' "
            "OR key LIKE 'lookup:release-parent:%' OR key LIKE 'resolve:release-group:%'"):
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        if key.startswith("resolve:"):
            if isinstance(data, str):
                resolved[key.split(":", 2)[2]] = data
            continue
        for rg in data if isinstance(data, list) else [data]:
            if isinstance(rg, dict) and rg.get("mbid") and "secondaryTypes" in rg:
                t = (rg.get("primaryType"), rg.get("secondaryTypes") or [])
                groups[rg["mbid"]] = t
                if key.startswith("lookup:release-parent:"):
                    parents[key.split(":", 2)[2]] = t
    return groups, parents, resolved


def mb_type(mbid: str | None, groups: dict, parents: dict, resolved: dict) -> tuple | None:
    """An album id's (primary, secondaries): it's a release group, or an edition (a Last.fm release id)
    whose group is known. None = not cached."""
    if not mbid:
        return None
    return groups.get(mbid) or parents.get(mbid) or groups.get(resolved.get(mbid) or "")


def _one_type(conn, mbid: str) -> tuple | None:
    """mb_type for one id, reading just its own cache entries (the lookups the sweep makes)."""
    def payload(key):
        row = conn.execute("SELECT payload_json FROM mb_cache WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None
    for rg in (payload(f"lookup:release-group:{mbid}"), payload(RELEASE_PARENT_KEY.format(mbid))):
        if isinstance(rg, dict) and "secondaryTypes" in rg:
            return rg.get("primaryType"), rg.get("secondaryTypes") or []
    group = payload(RESOLVE_KEY.format(mbid))
    rg = payload(f"lookup:release-group:{group}") if isinstance(group, str) and group != mbid else None
    return (rg.get("primaryType"), rg.get("secondaryTypes") or []) if isinstance(rg, dict) and "secondaryTypes" in rg else None


def plan(conn, artist_id: int | None = None) -> list[dict]:
    """Every album with its type now and what the fill would make it. status:
      set      gets a type (none yet)        change   MusicBrainz's type differs from the stored one
      same     already right                 manual   set by hand -- left alone
      pending  has an id, type not cached    nomb     no MusicBrainz id
      notfound has an id MusicBrainz doesn't know (neither a group nor a release -- a stale Last.fm id)
      check    MusicBrainz says single/EP but the album has too many songs for one (TOO_MANY) --
               left untyped: its id is probably wrong
    artist_id: only the albums crediting that artist (as the artist page lists them)."""
    groups, parents, resolved = _mb_types(conn)
    # resolve:release-group: caches None only on a 404 for both the group and the release lookup
    gone = {k.split(":", 2)[2] for (k,) in conn.execute(
        "SELECT key FROM mb_cache WHERE key LIKE 'resolve:release-group:%' AND payload_json = 'null'")}
    where = "WHERE al.id IN (SELECT album_id FROM album_artists WHERE artist_id = ?)" if artist_id else ""
    rows = conn.execute(f"""
        SELECT al.id, al.title, al.mbid, al.release_type, al.release_type_source, ar.id, ar.name,
               (SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id),
               (SELECT count(*) FROM vinyl_holdings v WHERE v.album_id = al.id), al.year
        FROM albums al JOIN artists ar ON ar.id = al.artist_id {where}""", (artist_id,) if artist_id else ()).fetchall()
    filed = dict(conn.execute("SELECT album_id, count(*) FROM songs WHERE album_id IS NOT NULL GROUP BY album_id"))
    played = dict(conn.execute("SELECT album_id, count(DISTINCT song_id) FROM scrobbles WHERE album_id IS NOT NULL GROUP BY album_id"))
    out = []
    for aid, title, mbid, cur, source, artist_id, artist, plays, vinyl, year in rows:
        t = mb_type(mbid, groups, parents, resolved)
        kind = classify(*t) if t else None
        songs = max(filed.get(aid, 0), played.get(aid, 0))
        if source == "manual":
            status = "manual"
        elif not mbid:
            status = "nomb"
        elif not kind:
            status = "notfound" if mbid in gone else "pending"
        elif kind in TOO_MANY and songs >= TOO_MANY[kind]:
            status = "check"
        elif cur is None:
            status = "set"
        else:
            status = "same" if cur == kind else "change"
        out.append({"albumId": aid, "title": title, "year": year, "mbid": mbid, "artistId": artist_id, "artist": artist, "plays": plays, "vinyl": vinyl, "songs": songs,
                    "current": cur, "source": source, "kind": kind, "mbTypes": [t[0], *t[1]] if t else None, "status": status})
    return out


def preview(conn) -> dict:
    """The Types tab's dry run: counts, the changes, and the most played albums each kind would get."""
    rows = plan(conn)
    status, kinds, totals = {}, {k: 0 for k in KINDS}, {k: 0 for k in KINDS}
    after = lambda r: r["current"] if r["status"] in ("manual", "check") else r["kind"]  # noqa: E731  (each album's kind once applied)
    for r in rows:
        status[r["status"]] = status.get(r["status"], 0) + 1
        if r["status"] in ("set", "change"):
            kinds[r["kind"]] += 1
        if after(r):
            totals[after(r)] += 1
    by_plays = sorted(rows, key=lambda r: (-r["plays"], -r["vinyl"], r["title"]))
    used = [r for r in by_plays if r["plays"] or r["vinyl"]]
    pick = lambda r: {k: r[k] for k in ("albumId", "title", "year", "artistId", "artist", "plays", "vinyl", "songs", "current", "kind", "mbTypes", "status")}  # noqa: E731
    return {
        "status": status, "kinds": kinds, "totals": totals,
        "pendingUsed": sum(1 for r in used if r["status"] == "pending"),
        "nombUsed": sum(1 for r in used if r["status"] == "nomb"),
        "notfoundUsed": sum(1 for r in used if r["status"] == "notfound"),
        "changes": [pick(r) for r in by_plays if r["status"] == "change"][:200],
        "byKind": {k: [pick(r) for r in used if after(r) == k][:40] for k in KINDS},
        "pending": [pick(r) for r in used if r["status"] == "pending"][:40],
        "notfound": [pick(r) for r in used if r["status"] == "notfound"][:40],
        "check": [pick(r) for r in by_plays if r["status"] == "check"],
    }


def apply(c) -> tuple[list[dict], dict]:
    """Writes the plan inside the caller's transaction -> (undo handles, summary)."""
    handles, counts = [], {}
    for r in plan(c):
        if r["status"] not in ("set", "change"):
            continue
        edit = merge.edit_entity(c, "album", r["albumId"], {"release_type": r["kind"], "release_type_source": "musicbrainz"}, REASON)
        if edit["editId"]:
            handles.append({"kind": "edit", "id": edit["editId"]})
            counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    detail = ", ".join(f"{n} {k}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    return handles, {"text": f"Release types for {len(handles)} album(s)" + (f" ({detail})" if detail else ""), "rowsMoved": {"albums": len(handles)}}


def set_type(c, album_id: int, kind: str | None) -> dict:
    """By hand: a kind, or None for MusicBrainz's again (what the cache says, else untyped)."""
    if kind is not None and kind not in KINDS:
        raise merge.MergeError(f"unknown release type {kind!r}")
    if kind is None:
        row = c.execute("SELECT mbid FROM albums WHERE id = ?", (album_id,)).fetchone()
        t = mb_type(row[0], *_mb_types(c)) if row and row[0] else None
        changes = {"release_type": classify(*t) if t else None, "release_type_source": "musicbrainz" if t else None}
    else:
        changes = {"release_type": kind, "release_type_source": "manual"}
    return merge.edit_entity(c, "album", album_id, changes, "release type set by hand" if kind else "release type back to MusicBrainz's")


def sweep(limit: int, log, progress, cancelled) -> None:
    """Looks up the albums whose type isn't cached -- most played (and vinyl) first. An edition id
    (a Last.fm release id) takes one request that also brings its group's type; a release-group id
    takes one lookup. Only warms the cache; the Types tab's dry run shows what it found."""
    import mbcache
    import releases
    conn = db_connect()
    try:
        editions = {r[0] for r in conn.execute("SELECT release_mbid FROM album_releases UNION SELECT release_mbid FROM scrobble_releases")}
        todo = sorted((r for r in plan(conn) if r["status"] == "pending"), key=lambda r: (-r["vinyl"], -r["plays"]))[:limit]
        log(f"Looking up {len(todo)} album type(s) — most played first (about {len(todo)} seconds).")
        found = 0
        for i, r in enumerate(todo):
            if cancelled():
                log("Cancelled.")
                break
            progress(i, len(todo))
            mbid = r["mbid"]
            try:
                row = conn.execute("SELECT payload_json FROM mb_cache WHERE key = ?", (RESOLVE_KEY.format(mbid),)).fetchone()
                group = json.loads(row[0]) if row else None
                if isinstance(group, str) and group != mbid:   # an edition whose group is known: just the group
                    mbcache.release_group(group)
                elif mbid in editions and row is None:          # an edition: one request brings its group's type
                    if releases.resolve(conn, mbid) == mbid:    # ...it was a release group after all
                        mbcache.release_group(mbid)
                elif mbcache.release_group(mbid) is None and row is None:  # not a group: try it as an edition
                    releases.resolve(conn, mbid)
                ok = _one_type(conn, mbid) is not None
                found += ok
                if not ok:
                    log(f"  · {r['artist']} — {r['title']}: not found on MusicBrainz")
            except Exception as exc:  # one failed lookup shouldn't end the sweep
                log(f"  ! {r['artist']} — {r['title']}: {exc}")
        progress(len(todo), len(todo))
        log(f"Done — {found} typed. Review the dry run on the Types tab.")
    finally:
        conn.close()
