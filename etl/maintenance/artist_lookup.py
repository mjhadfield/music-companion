"""Everything MusicBrainz can tell the workbench about one artist, fetched when their page opens
(sweep "artist-lookup"): only warms mb_cache -- the page's Suggested fixes and To sort out then read
the cache, and nothing changes until Mike queues and applies it.

What it asks, in order (about 1 request a second, cached after, so a second visit asks nothing):
  1. the artist's discography browse -- every release group with its first-release date and types
     (years and release types for most of their albums in one request)
  2. a group lookup for each album whose year or type the browse didn't answer (credited to someone else)
  3. the recording search for each possible single's title track (singles.lookup_candidates): which
     albums MusicBrainz has that song on, bonus tracks included
  4. genres for albums with none: the release group's genres, and the artist's as a stand-in

todo() is the list of cache keys still missing -- the page's lookupPending is its length, and
run() works through it, so the two always agree.
"""
import album_years
import mbcache
import release_types
import singles
from common import connect as db_connect


def _cached(conn, keys: list[str]) -> set[str]:
    if not keys:
        return set()
    return {k for (k,) in conn.execute(f"SELECT key FROM mb_cache WHERE key IN ({','.join('?' * len(keys))})", keys)}


def todo(conn, artist_id: int) -> list[tuple[str, tuple]]:
    """[(cache key, (what, args...))] still to fetch for this artist. Later steps lean on earlier ones,
    so after the discography browse run() asks again."""
    row = conn.execute("SELECT mbid FROM artists WHERE id = ?", (artist_id,)).fetchone()
    artist_mbid = row[0] if row else None
    out = []
    yearless = album_years.plan(conn, artist_id)
    have_year = {r["albumId"] for r in yearless}
    types = {r["albumId"]: r for r in release_types.plan(conn, artist_id)}
    albums = conn.execute("""SELECT al.id, al.mbid, al.year, EXISTS (SELECT 1 FROM album_genres g WHERE g.album_id = al.id)
                             FROM albums al JOIN album_artists aa ON aa.album_id = al.id WHERE aa.artist_id = ? AND al.mbid IS NOT NULL""",
                          (artist_id,)).fetchall()
    unanswered = [(aid, mbid) for aid, mbid, year, _g in albums
                  if (not year and aid not in have_year) or types.get(aid, {}).get("status") == "pending"]
    browse = f"browse:release-groups:{artist_mbid}"
    if artist_mbid and unanswered:
        out.append((browse, ("browse", artist_mbid)))
    if not artist_mbid or browse in _cached(conn, [browse]):   # once browsed, what it didn't answer gets a lookup each
        keys = [f"lookup:release-group:{mbid}" for _aid, mbid in unanswered]
        done = _cached(conn, keys)
        out += [(k, ("group", k.split(":", 2)[2])) for k in keys if k not in done]
    for c in singles.lookup_candidates(conn, [artist_id]):
        out.append((f"{singles.RECORDING_RGS}{c['artistMbid']}:{c['title'].strip().lower()}", ("recording", c["title"], c["artistMbid"])))
    no_genres = [mbid for _aid, mbid, _y, has in albums if not has]
    keys = [f"lookup:rg-genres:{m}" for m in no_genres] + ([f"lookup:artist-genres:{artist_mbid}"] if no_genres and artist_mbid else [])
    done = _cached(conn, keys)
    for k in keys:
        if k not in done:
            out.append((k, ("artist-genres", artist_mbid) if k.startswith("lookup:artist-genres:") else ("genres", k.split(":", 2)[2])))
    return list(dict.fromkeys(out))


def _fetch(what: tuple) -> None:
    kind, *args = what
    if kind == "browse":
        mbcache.artist_release_groups(*args)
    elif kind == "group":
        mbcache.release_group(*args)
    elif kind == "recording":
        mbcache.recording_release_groups(*args)
    elif kind == "genres":
        mbcache.release_group_genres(*args)
    elif kind == "artist-genres":
        mbcache.artist_genres(*args)


def run(limit: int, log, progress, cancelled, artist_id: int | None = None) -> None:
    if not artist_id:
        log("artist-lookup needs an artistId.")
        return
    conn = db_connect()
    try:
        name = (conn.execute("SELECT name FROM artists WHERE id = ?", (artist_id,)).fetchone() or ["?"])[0]
        done, asked = 0, set()
        log(f"Looking up {name} on MusicBrainz (up to {limit} requests, about one a second).")
        while done < limit and not cancelled():
            pending = [t for t in todo(conn, artist_id) if t[0] not in asked]
            if not pending:
                break
            progress(done, min(limit, done + len(pending)))
            key, what = pending[0]
            asked.add(key)       # once per run, whatever happens -- a failure doesn't loop
            try:
                _fetch(what)
            except Exception as exc:  # one failed lookup shouldn't end the rest
                log(f"  ! {what[0]} {what[1]}: {exc}")
            done += 1
        progress(done, done)
        log(f"Done — {done} request(s).")
    finally:
        conn.close()


def pending(conn, artist_id: int) -> int:
    return len(todo(conn, artist_id))

