"""
Background sweeps: batches of MusicBrainz lookups that turn into *suggestions* (or warm the
cache that the Verify tabs compute their flags from). A sweep never edits an entity -- it only
ever writes to `suggestions` and `mb_cache`; a human accepts or rejects each result.

One sweep runs at a time (a second start request is refused), always with a cap, always
cancellable, and every request it makes goes through mbcache -> musicbrainz.py's 1 req/s
throttle, so a sweep of N artists costs at most ~2-4 N seconds the first time and ~0 after.
"""
import json
import threading
from collections import defaultdict

from rapidfuzz import fuzz

import mbcache
from common import artist_mbid_sets, connect as db_connect
from titles import base_key, normalize_artist_name

_active_lock = threading.Lock()
_active_job: str | None = None


def name_similarity(a: str, b: str) -> float:
    na, nb = normalize_artist_name(a), normalize_artist_name(b)
    return 100.0 if na == nb else fuzz.ratio(na, nb)


def local_album_titles(conn, artist_id: int, limit: int = 12) -> list[str]:
    """The artist's most-played/owned albums -- what corroborates (or contradicts) an mbid."""
    return [r[0] for r in conn.execute(
        """
        SELECT al.title FROM albums al
        LEFT JOIN (SELECT album_id, count(*) n FROM scrobbles WHERE album_id IS NOT NULL GROUP BY album_id) sc ON sc.album_id = al.id
        LEFT JOIN (SELECT album_id, count(*) n FROM vinyl_holdings GROUP BY album_id) vh ON vh.album_id = al.id
        -- any album they're credited on, not just as primary: "Kiss, Gene Simmons" is Gene's album too
        WHERE al.artist_id = ? OR al.id IN (SELECT album_id FROM album_artists WHERE artist_id = ?)
        ORDER BY coalesce(vh.n, 0) * 50 + coalesce(sc.n, 0) DESC
        LIMIT ?
        """,
        (artist_id, artist_id, limit),
    )]


def album_matches(local_titles: list[str], release_groups: list[dict]) -> list[str]:
    """Which local album titles appear (edition-normalised, fuzzy >= 90) in an MB discography."""
    mb_keys = [base_key(rg["title"] or "") for rg in release_groups or []]
    hits = []
    for title in local_titles:
        k = base_key(title)
        if k and any(k == m or fuzz.ratio(k, m) >= 90 for m in mb_keys):
            hits.append(title)
    return hits


def score_artist_candidate(local_name: str, cand: dict, exact_name_count: int, local_titles: list[str], matched: list[str] | None) -> tuple[float, str]:
    sim = name_similarity(local_name, cand["name"])
    if local_titles and matched is not None:
        ratio = len(matched) / len(local_titles)
        confidence = 0.45 * sim + 0.20 * cand["score"] + 0.35 * ratio * 100
    else:
        ratio = None
        confidence = 0.6 * sim + 0.4 * cand["score"]
    if sim == 100 and ((ratio is not None and ratio >= 0.4) or (ratio is None and exact_name_count == 1 and cand["score"] >= 95)):
        tier = "high"
    elif confidence >= 75 and not (exact_name_count > 1 and not (ratio or 0) > 0):
        tier = "medium"
    else:
        tier = "low"
    return round(confidence, 1), tier


# -- Sweeps ----------------------------------------------------------------------------------

def _artist_suggest(limit: int, log, progress, cancelled) -> None:
    conn = db_connect()
    try:
        targets = conn.execute(
            """
            SELECT ar.id, ar.name, count(sc.id) AS plays
            FROM artists ar JOIN scrobbles sc ON sc.artist_id = ar.id
            WHERE ar.mbid IS NULL
              AND NOT EXISTS (SELECT 1 FROM suggestions s WHERE s.entity_type = 'artist' AND s.entity_id = ar.id AND s.status = 'pending')
              AND NOT EXISTS (SELECT 1 FROM review_marks m WHERE m.entity_type = 'artist' AND m.entity_id = ar.id AND m.mark = 'no-mbid')
            GROUP BY ar.id ORDER BY plays DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        taken = dict(conn.execute("SELECT mbid, name FROM artists WHERE mbid IS NOT NULL").fetchall())
        rejected = {(r[0], r[1]) for r in conn.execute("SELECT entity_id, mbid FROM suggestions WHERE entity_type = 'artist' AND status = 'rejected'")}
    finally:
        conn.close()

    log(f"Looking up {len(targets)} artist(s) with no MusicBrainz id, most-played first.")
    made = 0
    for i, (artist_id, name, plays) in enumerate(targets):
        if cancelled():
            log("Cancelled.")
            break
        progress(i, len(targets))
        try:
            cands = [c for c in (mbcache.search_artists(name, 5) or []) if c.get("mbid") and c["score"] >= 60]
        except Exception as exc:  # one failed lookup shouldn't end the sweep
            log(f"  ! {name}: {exc}")
            continue
        cands = [c for c in cands if (artist_id, c["mbid"]) not in rejected]
        if not cands:
            log(f"  - {name}: no plausible candidates")
            continue
        exact = sum(1 for c in cands if name_similarity(name, c["name"]) == 100)
        conn = db_connect()
        try:
            titles = local_album_titles(conn, artist_id)
        finally:
            conn.close()

        scored = []
        # Discography corroboration costs 1-3 extra requests per candidate, so only for the
        # two best name matches.
        for c in sorted(cands, key=lambda c: (-name_similarity(name, c["name"]), -c["score"]))[:2]:
            matched = None
            if titles and name_similarity(name, c["name"]) >= 85:
                try:
                    matched = album_matches(titles, mbcache.artist_release_groups(c["mbid"]))
                except Exception as exc:
                    log(f"  ! {name}: discography lookup failed ({exc})")
            confidence, tier = score_artist_candidate(name, c, exact, titles, matched)
            evidence = {
                "mbName": c["name"], "disambiguation": c.get("disambiguation"), "type": c.get("type"),
                "country": c.get("country"), "beginDate": c.get("beginDate"), "mbScore": c["score"],
                "nameSimilarity": name_similarity(name, c["name"]), "sameNameCandidates": exact,
                "albumsChecked": len(titles) if matched is not None else 0,
                "albumsMatched": matched or [],
                "alreadyLinkedTo": taken.get(c["mbid"]),
            }
            scored.append((confidence, tier, c, evidence))

        conn = db_connect()
        try:
            for confidence, tier, c, evidence in scored:
                label = c["name"] + (f" ({c['disambiguation']})" if c.get("disambiguation") else "")
                conn.execute(
                    """
                    INSERT INTO suggestions (entity_type, entity_id, mbid, label, confidence, tier, source, evidence_json)
                    VALUES ('artist', ?, ?, ?, ?, ?, 'mb-search', ?)
                    ON CONFLICT (entity_type, entity_id, mbid) DO UPDATE SET
                        confidence = excluded.confidence, tier = excluded.tier, evidence_json = excluded.evidence_json, label = excluded.label
                    WHERE suggestions.status = 'pending'
                    """,
                    (artist_id, c["mbid"], label, confidence, tier, json.dumps(evidence)),
                )
            conn.commit()
        finally:
            conn.close()
        made += 1
        best = max(scored, key=lambda s: s[0])
        log(f"  + {name} ({plays} plays): best guess {best[2]['name']} — {best[1]} ({best[0]})")
    progress(len(targets), len(targets))
    log(f"Done — suggestions for {made} artist(s). MusicBrainz requests made: {mbcache.stats['misses']} (session total).")


def _artist_verify(limit: int, log, progress, cancelled) -> None:
    """Warms the cache the Verify tab reads (artist lookup + discography) for the most-played
    artists that have an mbid but haven't been checked or marked as fine yet."""
    conn = db_connect()
    try:
        targets = conn.execute(
            """
            SELECT ar.id, ar.name, ar.mbid, count(sc.id) AS plays
            FROM artists ar LEFT JOIN scrobbles sc ON sc.artist_id = ar.id
            WHERE ar.mbid IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM mb_cache c WHERE c.key = 'browse:release-groups:' || ar.mbid)
              AND NOT EXISTS (SELECT 1 FROM review_marks m WHERE m.entity_type = 'artist' AND m.entity_id = ar.id AND m.mark = 'verified:artist-mbid')
            GROUP BY ar.id ORDER BY plays DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    log(f"Checking {len(targets)} artist mbid(s), most-played first.")
    for i, (artist_id, name, mbid, plays) in enumerate(targets):
        if cancelled():
            log("Cancelled.")
            break
        progress(i, len(targets))
        try:
            info = mbcache.artist(mbid)
            if info:
                mbcache.artist_release_groups(mbid)
                conn2 = db_connect()
                try:
                    extra = artist_mbid_sets(conn2, [artist_id]).get(artist_id, set()) - {mbid}
                finally:
                    conn2.close()
                for alias in extra:  # "also releases as" discographies count too
                    mbcache.artist_release_groups(alias)
            log(f"  · {name}: {'ok' if info else 'NOT FOUND on MusicBrainz'}")
        except Exception as exc:
            log(f"  ! {name}: {exc}")
    progress(len(targets), len(targets))
    log(f"Done. MusicBrainz requests made: {mbcache.stats['misses']} (session total). Open the Verify tab to review flags.")


def _album_verify(limit: int, log, progress, cancelled) -> None:
    """Warms the release-group cache the Albums > Verify tab computes its flags from -- owned
    vinyl first, then most played."""
    conn = db_connect()
    try:
        targets = conn.execute(
            """
            SELECT al.id, al.title, al.mbid, ar.name,
                   (SELECT count(*) FROM vinyl_holdings v WHERE v.album_id = al.id) * 50
                   + (SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id) AS weight
            FROM albums al JOIN artists ar ON ar.id = al.artist_id
            WHERE al.mbid IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM mb_cache c WHERE c.key = 'lookup:release-group:' || al.mbid)
              AND NOT EXISTS (SELECT 1 FROM review_marks m WHERE m.entity_type = 'album' AND m.entity_id = al.id AND m.mark = 'verified:album-mbid')
            ORDER BY weight DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    log(f"Checking {len(targets)} album mbid(s) — vinyl first, then most played.")
    for i, (album_id, title, mbid, artist, _weight) in enumerate(targets):
        if cancelled():
            log("Cancelled.")
            break
        progress(i, len(targets))
        try:
            rg = mbcache.release_group(mbid)
            log(f"  · {artist} — {title}: {'ok' if rg else 'NOT a release group'}")
        except Exception as exc:
            log(f"  ! {artist} — {title}: {exc}")
    progress(len(targets), len(targets))
    log(f"Done. MusicBrainz requests made: {mbcache.stats['misses']} (session total). Review flags in the Verify tab.")


def _write_vinyl_suggestion(vinyl_id: int, mbid: str, label: str, credited: bool, evidence: dict) -> None:
    conn = db_connect()
    try:
        conn.execute(
            """
            INSERT INTO suggestions (entity_type, entity_id, mbid, label, confidence, tier, source, evidence_json)
            VALUES ('vinyl', ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (entity_type, entity_id, mbid) DO UPDATE SET evidence_json = excluded.evidence_json, label = excluded.label
            WHERE suggestions.status = 'pending'
            """,
            (vinyl_id, mbid, label, 100 if credited else 70, "high" if credited else "medium",
             "discogs-master" if evidence.get("kind") == "master" else "discogs-link", json.dumps(evidence)),
        )
        conn.commit()
    finally:
        conn.close()


def link_suggestions_for_holding(vinyl_id: int) -> int:
    """Exact, curated mappings only -- never a fuzzy guess:

      1. MusicBrainz's URL relationships say which MB *release* a Discogs release page is (the
         exact pressing). Its release group is the album.
      2. If MB doesn't know this pressing, Discogs' own API gives the release's *master* (every
         pressing of the album), and MB links masters to release groups -- the album identity,
         just not the specific pressing.

    Each becomes a pending 'vinyl' suggestion for a human to accept. Also warms the album's own
    release-group lookup (verification + original year). Returns suggestions made."""
    conn = db_connect()
    try:
        row = conn.execute(
            "SELECT vh.discogs_release_id, vh.mb_release_id, al.mbid, al.id FROM vinyl_holdings vh JOIN albums al ON al.id = vh.album_id WHERE vh.id = ?",
            (vinyl_id,)).fetchone()
        if not row:
            return 0
        rel_id, linked, album_mbid, album_id = row
        credited = [r[0] for r in conn.execute("SELECT artist_id FROM album_artists WHERE album_id = ?", (album_id,))]
        artist_mbids = {m for v in artist_mbid_sets(conn, credited).values() for m in v} if credited else set()
        decided = {r[0] for r in conn.execute(
            "SELECT mbid FROM suggestions WHERE entity_type = 'vinyl' AND entity_id = ? AND status != 'pending'", (vinyl_id,))}
        verified = conn.execute("SELECT 1 FROM review_marks WHERE entity_type = 'album' AND entity_id = ? AND mark = 'verified:album-mbid'",
                                (album_id,)).fetchone() is not None
    finally:
        conn.close()
    if album_mbid:
        mbcache.release_group(album_mbid)
    if not rel_id:
        return 0

    def relation(rg_mbid):
        return "confirms" if rg_mbid == album_mbid else "sets" if not album_mbid else "differs"

    made = 0
    release_links = mbcache.discogs_release(rel_id) or []
    for link in release_links[:3]:
        release = link["releaseMbid"]
        if release == linked or release in decided:
            continue
        rg = mbcache.release_parent_group(release)
        if not rg:
            continue
        credited = not artist_mbids or bool(artist_mbids & set(rg.get("artistMbids") or []))
        year = (rg.get("firstReleaseDate") or "")[:4]
        _write_vinyl_suggestion(vinyl_id, release, f"{rg.get('title')}{f' ({year})' if year else ''}", credited, {
            "kind": "release", "releaseMbid": release, "releaseTitle": link.get("releaseTitle"), "rg": rg,
            "relation": relation(rg["mbid"]), "artistMatches": credited})
        made += 1

    # Discogs' own details for the pressing (country, master) -- always useful, cached.
    details = mbcache.discogs_release_details(rel_id)
    if release_links or linked or not details or not details.get("masterId"):
        return made
    for rg_id in (mbcache.discogs_master_groups(details["masterId"]) or [])[:2]:
        if rg_id in decided or (rg_id == album_mbid and verified):
            continue
        rg = mbcache.release_group(rg_id)
        if not rg:
            continue
        credited = not artist_mbids or bool(artist_mbids & set(rg.get("artistMbids") or []))
        year = (rg.get("firstReleaseDate") or "")[:4]
        _write_vinyl_suggestion(vinyl_id, rg_id, f"{rg.get('title')}{f' ({year})' if year else ''}", credited, {
            "kind": "master", "masterId": details["masterId"], "rg": rg, "relation": relation(rg_id), "artistMatches": credited})
        made += 1
    return made


def _vinyl_check(limit: int, log, progress, cancelled) -> None:
    """Exact Discogs -> MusicBrainz pressing links + album release-group checks for the holdings
    that need it most: no album mbid first, then pressings not yet linked, then unverified."""
    conn = db_connect()
    try:
        targets = conn.execute(
            """
            SELECT vh.id, vh.raw_artist_text, vh.raw_title_text
            FROM vinyl_holdings vh JOIN albums al ON al.id = vh.album_id
            -- skip anything already fully checked: its pressing looked up (and, where MB didn't know the
            -- pressing, its Discogs details/master too) and its album's release group cached
            WHERE NOT (
                (vh.mb_release_id IS NOT NULL
                 OR (EXISTS (SELECT 1 FROM mb_cache c WHERE c.key = 'lookup:discogs-release:' || vh.discogs_release_id)
                     AND EXISTS (SELECT 1 FROM mb_cache c WHERE c.key = 'lookup:discogs-api:' || vh.discogs_release_id)))
                AND (al.mbid IS NULL OR EXISTS (SELECT 1 FROM mb_cache c WHERE c.key = 'lookup:release-group:' || al.mbid))
            )
              AND vh.discogs_release_id IS NOT NULL
            ORDER BY al.mbid IS NOT NULL, vh.mb_release_id IS NOT NULL, vh.date_added DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    log(f"Checking {len(targets)} holding(s) — albums with no MBID first. MusicBrainz 1 req/s, Discogs ~23/min, all cached.")
    found = 0
    for i, (vid, artist, title) in enumerate(targets):
        if cancelled():
            log("Cancelled.")
            break
        progress(i, len(targets))
        try:
            n = link_suggestions_for_holding(vid)
            found += n
            log(f"  {'+' if n else '·'} {artist} — {title}: {'link found — review it' if n else 'nothing new'}")
        except Exception as exc:
            log(f"  ! {artist} — {title}: {exc}")
    progress(len(targets), len(targets))
    log(f"Done — {found} link(s) to review. Requests made (MusicBrainz + Discogs): {mbcache.stats['misses']} (session total).")


def _live_albums(limit: int, log, progress, cancelled, artist_id: int | None = None) -> None:
    """For live songs the Songs page shows as "not looked up" (no album, no mark, no close title
    in the library), ask MusicBrainz which albums the recording is on: for a cover, the band's
    OWN recording first (Anthrax's "Antisocial", not Trust's), then the song artist's. Uses the
    page's own status logic to pick targets, so the cap is never spent on songs that already
    have a match. Results only ever become suggestions -- nothing is applied. Cached."""
    from api.core import read_conn
    from api.songs import live_pairs

    with read_conn() as c:
        rows, profiles, statuses = live_pairs(c, [artist_id] if artist_id else None)
        mbid_sets = artist_mbid_sets(c)  # own mbid + "also releases as" aliases
        names = dict(c.execute("SELECT id, name FROM artists").fetchall())
    shows = defaultdict(set)
    for perf, setlist_id, _d, song_id, *_ in rows:
        shows[(perf, song_id)].add(setlist_id)
    # also songs already found on an album you don't have, whose album details (true original
    # year, who MusicBrainz credits it to) haven't been fetched yet -- one request each
    targets = sorted(((perf, profiles[sid], st) for (perf, sid), st in statuses.items()
                      if (st["status"] == "unchecked" and st.get("lookable")) or (st["status"] == "newalbum" and st.get("needsDetail"))),
                     key=lambda t: -len(shows[(t[0], t[1]["songId"])]))[:limit]
    log(f"Looking up {len(targets)} live song(s) on MusicBrainz (1 request/second, cached). Results are suggestions for you to review.")
    found = detailed = 0
    for i, (perf, s, st) in enumerate(targets):
        if cancelled():
            log("Cancelled.")
            break
        progress(i, len(targets))
        try:
            if st["status"] == "newalbum":
                g = st["mbCandidates"][0]
                d = mbcache.release_group(g["rgMbid"]) or {}
                log(f"  i {s['artistName']} — {s['title']}: {d.get('title')} ({(d.get('firstReleaseDate') or '?')[:4]}), credited to {d.get('artistCredit')}")
                detailed += 1
                continue
            hits = []
            if s["artistId"] != perf:  # a cover: the band's own recording first
                for m in sorted(mbid_sets.get(perf, set())):
                    hits += [f"{names.get(perf)}'s: {g['title']}" for g in (mbcache.recording_release_groups(s["title"], m) or [])[:1]]
            firsts = []
            for m in sorted(mbid_sets.get(s["artistId"], set())):
                gs = (mbcache.recording_release_groups(s["title"], m) or [])[:1]
                firsts += gs
                hits += [g["title"] for g in gs]
            if s["artistId"] != perf:
                for m in sorted(mbid_sets.get(perf, set())):
                    firsts += (mbcache.recording_release_groups(s["title"], m) or [])[:1]  # cached just above
            for g in firsts[:1]:  # the likeliest album's details: original year + credit, for review
                mbcache.release_group(g["mbid"])
            found += bool(hits)
            log(f"  {'+' if hits else '·'} {s['artistName']} — {s['title']}: {' / '.join(hits) if hits else 'no album on MusicBrainz'}")
        except Exception as exc:
            log(f"  ! {s['artistName']} — {s['title']}: {exc}")
    progress(len(targets), len(targets))
    log(f"Done — {found} found on a MusicBrainz album{f', {detailed} album(s) checked' if detailed else ''}. Review them on the page. MusicBrainz requests made: {mbcache.stats['misses']} (session total).")


def _album_editions(limit: int, log, progress, cancelled) -> None:
    """Resolves the albums still identified by one edition (a Last.fm release id) to their
    release groups -- most played first. Only warms the cache; Albums > Editions lists what to do."""
    import releases
    from api.editions import CANDIDATES_SQL
    conn = db_connect()
    try:
        targets = conn.execute(
            f"SELECT c.id, c.title, c.mbid, c.artist_name FROM ({CANDIDATES_SQL}) c "
            "WHERE NOT EXISTS (SELECT 1 FROM mb_cache m WHERE m.key = 'resolve:release-group:' || c.mbid) "
            "ORDER BY c.vinyl DESC, c.plays DESC LIMIT ?", (limit,)).fetchall()
        log(f"Looking up {len(targets)} album edition(s) — most played first (about {len(targets)} seconds).")
        found = 0
        for i, (album_id, title, mbid, artist) in enumerate(targets):
            if cancelled():
                log("Cancelled.")
                break
            progress(i, len(targets))
            try:
                rg = releases.resolve(conn, mbid)
                found += bool(rg)
                if not rg:
                    log(f"  · {artist} — {title}: not on MusicBrainz")
            except Exception as exc:  # one failed lookup shouldn't end the sweep
                log(f"  ! {artist} — {title}: {exc}")
        progress(len(targets), len(targets))
        log(f"Done — {found} resolved. Review them on the page.")
    finally:
        conn.close()


def _song_recordings(limit: int, log, progress, cancelled) -> None:
    """Sorts Last.fm song ids into recordings vs track ids -- editions' tracklists first (one
    request sorts every song played from that edition; the ones covering the most-played songs go
    first), then direct lookups for songs no edition can sort. Only warms the cache; Songs >
    Recording ids lists what to do."""
    import releases
    from api.recordings import song_states
    conn = db_connect()
    try:
        st = song_states(conn)
        plays = {sid: s["plays"] for sid, s in st["songs"].items()}
        editions = sorted(st["editionsToFetch"].items(), key=lambda kv: -sum(plays[s] for s in kv[1]))
        asks = sorted(st["directToAsk"], key=lambda sid: -plays[sid])
        plan = [("edition", rel) for rel, _ in editions][:limit]
        plan += [("direct", st["songs"][sid]["mbid"]) for sid in asks][:max(0, limit - len(plan))]
        log(f"{len(editions)} edition tracklist(s) and {len(asks)} direct lookup(s) outstanding — doing {len(plan)} now, most-played songs first.")
        sorted_songs = 0
        for i, (kind, key) in enumerate(plan):
            if cancelled():
                log("Cancelled.")
                break
            progress(i, len(plan))
            try:
                if kind == "edition":
                    releases.resolve(conn, key)
                    sorted_songs += len(st["editionsToFetch"][key])
                else:
                    r = releases.sort_id(conn, key)
                    sorted_songs += 1
                    if not r.get("recording"):
                        log(f"  · {key}: MusicBrainz knows no recording or track with this id")
            except Exception as exc:  # one failed lookup shouldn't end the sweep
                log(f"  ! {key}: {exc}")
        progress(len(plan), len(plan))
        log(f"Done — up to {sorted_songs} song id(s) sorted. Review them on the page.")
    finally:
        conn.close()


SWEEPS = {
    "song-recordings": _song_recordings,
    "album-editions": _album_editions,
    "live-albums": _live_albums,
    "artist-suggest": _artist_suggest,
    "artist-verify": _artist_verify,
    "album-verify": _album_verify,
    "vinyl-check": _vinyl_check,
}


def start(kind: str, limit: int, jobs: dict, jobs_lock, job_id: str, params: dict | None = None) -> str | None:
    """Starts a sweep on its own thread, reporting through the server's jobs dict (same
    lines/done/returncode shape the refresh log uses, plus progress/cancel). Returns an error
    string instead if one is already running or the kind is unknown."""
    global _active_job
    if kind not in SWEEPS:
        return f"unknown sweep {kind!r}"
    with _active_lock:
        if _active_job and not jobs.get(_active_job, {}).get("done", True):
            return "Another sweep is already running — wait for it or cancel it first."
        _active_job = job_id
    with jobs_lock:
        jobs[job_id] = {"lines": [], "done": False, "returncode": None, "kind": f"sweep:{kind}",
                        "backup": None, "decision": None, "progress": [0, limit], "cancel": False}

    def log(line):
        with jobs_lock:
            jobs[job_id]["lines"].append(line + "\n")

    def progress(done, total):
        with jobs_lock:
            jobs[job_id]["progress"] = [done, total]

    def cancelled():
        with jobs_lock:
            return jobs[job_id]["cancel"]

    def run():
        code = 0
        try:
            SWEEPS[kind](limit, log, progress, cancelled, **(params or {}))
        except Exception as exc:
            log(f"Sweep failed: {exc}")
            code = 1
        with jobs_lock:
            jobs[job_id]["done"] = True
            jobs[job_id]["returncode"] = code

    threading.Thread(target=run, daemon=True).start()
    return None
