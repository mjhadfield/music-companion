"""The one-off MusicBrainz genre fill (Maintenance > Genres > MusicBrainz fill).

Three parts, kept apart on purpose:
  gather()  the sweep "genre-fill": only warms mb_cache, writes no tags. Four steps, each skipping
            what's already cached (so a cancelled or interrupted run just carries on next time):
              B  release-group genres per artist (browse, 1 request per 100 albums)
              A  albums the browse didn't match: their id is an edition (Last.fm release id) --
                 one lookup gives its release group AND the edition's own genres
              B2 albums whose release group has no genres of its own (5+ plays, on vinyl, or a
                 release group the browse never saw): its editions' genres summed (2 requests)
              C  artist genres (artist?inc=genres), for artists you've played, own or seen
  plan()    what the cache says each untagged album / artist would get -- no network, no writes.
            Album rule (genres.pick_for_fill): the album's own genres (release group, else its
            editions summed, else the one edition it's filed under), ties broken by the artist's
            votes; nothing of its own = stays untagged (never the artist's genres).
  apply()   the plan, written inside the caller's transaction (batch action "genreFill"):
            albums via genres.set_album (journaled, source musicbrainz, not marked reviewed),
            artists into artist_mb_genres (one undo handle with the rows it replaced).

The schedule (Mike: start at 03:30, review in the morning) is a file, data/.scheduled/genre-fill.json,
so a server restart before then doesn't lose it: arm() is called at startup and after scheduling.
"""
import json
import random
import threading
import time
import uuid
from datetime import datetime

import genres as genre_tags
import mbcache
from common import DB_PATH, RELEASE_GENRES_KEY, RESOLVE_KEY, VARIOUS_ARTISTS_MBID, artist_mbid_sets, connect as db_connect

KIND = "genre-fill"
REASON = "MusicBrainz genre fill"
B2_MIN_PLAYS = 5
SCHEDULE_FILE = DB_PATH.parent / ".scheduled" / "genre-fill.json"

# wired by server.py (like api.general.JOBS): the jobs dict/lock and its "is something running" check
JOBS: dict = {}
JOBS_LOCK = None
BUSY = None  # () -> reason str | None


def _payloads(conn, keys: list[str]) -> dict[str, object]:
    out = {}
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        for k, p in conn.execute(f"SELECT key, payload_json FROM mb_cache WHERE key IN ({','.join('?' * len(chunk))})", chunk):
            out[k] = json.loads(p)
    return out


def _untagged_albums(conn) -> list[dict]:
    """Albums with no genres yet and not already reviewed (a reviewed album with nothing kept stays so)."""
    rows = conn.execute(f"""
        SELECT al.id, al.title, al.mbid, al.artist_id, ar.name,
               (SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id) AS plays,
               (SELECT count(*) FROM vinyl_holdings v WHERE v.album_id = al.id) AS vinyl
        FROM albums al JOIN artists ar ON ar.id = al.artist_id
        WHERE NOT EXISTS (SELECT 1 FROM album_genres ag WHERE ag.album_id = al.id)
          AND NOT EXISTS (SELECT 1 FROM review_marks m WHERE m.entity_type = 'album' AND m.entity_id = al.id
                          AND m.mark = '{genre_tags.REVIEWED_MARK}')""").fetchall()
    return [{"albumId": r[0], "title": r[1], "mbid": r[2], "artistId": r[3], "artist": r[4], "plays": r[5], "vinyl": r[6]} for r in rows]


def _artists_in_use(conn) -> list[dict]:
    rows = conn.execute("""
        SELECT ar.id, ar.name, ar.mbid,
               (SELECT count(*) FROM scrobbles s WHERE s.artist_id = ar.id) AS plays,
               (SELECT count(*) FROM vinyl_holdings v JOIN album_artists aa ON aa.album_id = v.album_id WHERE aa.artist_id = ar.id) AS vinyl,
               (SELECT count(*) FROM setlists sl WHERE sl.artist_id = ar.id) AS shows
        FROM artists ar WHERE ar.mbid IS NOT NULL""").fetchall()
    return [{"artistId": r[0], "name": r[1], "mbid": r[2], "plays": r[3], "vinyl": r[4], "shows": r[5]}
            for r in rows if (r[3] or r[4] or r[5]) and r[2] != VARIOUS_ARTISTS_MBID]


class _Index:
    """Everything the cache knows that the fill needs, for a set of albums and artists."""

    def __init__(self, conn, albums: list[dict], artists: list[dict]):
        artist_ids = sorted({a["artistId"] for a in albums} | {a["artistId"] for a in artists})
        self.mbids = artist_mbid_sets(conn, artist_ids) if artist_ids else {}
        all_mbids = sorted({m for s in self.mbids.values() for m in s})
        browse = _payloads(conn, [f"browse:rg-genres:{m}" for m in all_mbids])
        self.rg = {}  # release group id -> its genres from a browse
        for groups in browse.values():
            for g in groups or []:
                if g.get("mbid"):
                    self.rg[g["mbid"]] = g.get("genres") or []
        self.artist_genres = {k.split(":", 2)[2]: v for k, v in
                              _payloads(conn, [f"lookup:artist-genres:{m}" for m in all_mbids]).items()}
        ids = sorted({a["mbid"] for a in albums if a["mbid"]})
        self.resolved = {k[len(RESOLVE_KEY.format("")):]: v for k, v in _payloads(conn, [RESOLVE_KEY.format(m) for m in ids]).items()}
        rgs = sorted({self.rg_of(a) for a in albums if a["mbid"]} - {None})
        self.rg_lookup = {k[len("lookup:rg-genres:"):]: v for k, v in _payloads(conn, [f"lookup:rg-genres:{r}" for r in rgs]).items()}
        self.edition = {k[len(RELEASE_GENRES_KEY.format("")):]: v for k, v in _payloads(conn, [RELEASE_GENRES_KEY.format(m) for m in ids]).items()}

    def rg_of(self, a: dict) -> str | None:
        if not a["mbid"]:
            return None
        if a["mbid"] in self.rg:
            return a["mbid"]
        r = self.resolved.get(a["mbid"])
        return r if isinstance(r, str) else None

    def artist_votes(self, artist_id: int) -> list[dict]:
        out: dict[str, dict] = {}
        for m in sorted(self.mbids.get(artist_id, ())):
            for g in self.artist_genres.get(m) or []:
                t = out.setdefault(g["name"], {**g, "count": 0})
                t["count"] = max(t["count"], int(g.get("count") or 0))
        return list(out.values())

    def album(self, a: dict) -> dict:
        """-> {status, from?, genres?: [(name, mbid, votes)], all?: [...]}; status: tagged | none | pending | nomb"""
        if not a["mbid"]:
            return {"status": "nomb"}
        rg = self.rg_of(a)
        known_edition = a["mbid"] in self.resolved
        found, frm = None, None
        if rg and self.rg.get(rg):
            found, frm = self.rg[rg], "album"
        elif rg and self.rg_lookup.get(rg):
            lk = self.rg_lookup[rg]
            found, frm = (lk.get("group"), "album") if lk.get("group") else (lk.get("releases"), "editions")
        if not found and self.edition.get(a["mbid"]):
            found, frm = self.edition[a["mbid"]], "edition"
        if not found:
            # not knowing what the id is (never resolved) is "pending", not "none"
            if rg is None and not known_edition:
                return {"status": "pending"}
            return {"status": "none"}
        picked = genre_tags.pick_for_fill(found, self.artist_votes(a["artistId"]))
        return {"status": "tagged", "from": frm, "genres": picked, "all": genre_tags._ranked(found)}

    def artist(self, a: dict) -> dict:
        if a["mbid"] not in self.artist_genres:
            return {"status": "pending"}
        found = self.artist_genres.get(a["mbid"]) or []
        if not found:
            return {"status": "none"}
        return {"status": "tagged", "genres": genre_tags.pick_for_fill(found), "all": genre_tags._ranked(found)}


# -- gathering (the sweep) -------------------------------------------------------------------

def gather(limit: int, log, progress, cancelled) -> None:
    import releases
    conn = db_connect()
    try:
        def step(name, items, fn, per=1):
            n = min(len(items), limit) * per
            log(f"{name}: {len(items)} to look up (about {n} request(s), ~{max(1, round(n * 1.05 / 60))} min).")
            done = failed = 0
            for i, item in enumerate(items[:limit]):
                if cancelled():
                    log("Cancelled — everything fetched so far is cached; the next run carries on.")
                    return False
                progress(i, len(items))
                try:
                    fn(item)
                    done += 1
                except Exception as exc:  # one failed lookup shouldn't end the run
                    failed += 1
                    log(f"  ! {item if isinstance(item, str) else item.get('title') or item.get('name')}: {exc}")
            progress(len(items), len(items))
            log(f"  done — {done} looked up{f', {failed} failed' if failed else ''}.")
            return True

        albums, artists = _untagged_albums(conn), _artists_in_use(conn)
        log(f"{len(albums)} untagged album(s), {len(artists)} artist(s) you've played, own or seen.")

        # B: one browse per artist of an untagged album (own id + "also releases as")
        need = sorted({a["artistId"] for a in albums if a["mbid"]})
        sets = artist_mbid_sets(conn, need) if need else {}
        plays = {a["artistId"]: a["plays"] for a in artists}
        have = set(_payloads(conn, [f"browse:rg-genres:{m}" for s in sets.values() for m in s]))
        todo_b = []
        for aid in sorted(need, key=lambda x: -plays.get(x, 0)):  # most played first
            for m in sorted(sets.get(aid, ())):
                if f"browse:rg-genres:{m}" not in have and m not in todo_b and m != VARIOUS_ARTISTS_MBID:
                    todo_b.append(m)
        if not step("Step 1/4 — albums by artist (release-group genres)", todo_b, mbcache.artist_release_group_genres):
            return

        # A: ids the browse didn't recognise -> look the edition up (release group + its genres)
        idx = _Index(conn, albums, [])
        todo_a = sorted((a for a in albums if a["mbid"] and a["mbid"] not in idx.rg and a["mbid"] not in idx.resolved),
                        key=lambda a: (-a["vinyl"], -a["plays"]))
        if not step("Step 2/4 — albums filed under one edition", todo_a, lambda a: releases.resolve(conn, a["mbid"])):
            return

        # B2: the release group has no genres of its own -> its editions' genres, summed
        idx = _Index(conn, albums, [])
        todo_b2 = []
        for a in albums:
            rg = idx.rg_of(a)
            if not rg or idx.rg.get(rg) or rg in idx.rg_lookup:
                continue
            if a["plays"] >= B2_MIN_PLAYS or a["vinyl"] or rg not in idx.rg:
                todo_b2.append(a)
        seen, uniq = set(), []
        for a in sorted(todo_b2, key=lambda a: (-a["vinyl"], -a["plays"])):
            rg = idx.rg_of(a)
            if rg not in seen:
                seen.add(rg)
                uniq.append(rg)
        if not step("Step 3/4 — albums with no genres of their own (their editions)", uniq, mbcache.release_group_genres, per=2):
            return

        # C: artist genres
        have = set(_payloads(conn, [f"lookup:artist-genres:{a['mbid']}" for a in artists]))
        todo_c = [a["mbid"] for a in sorted(artists, key=lambda a: (-a["vinyl"], -a["plays"], -a["shows"]))
                  if f"lookup:artist-genres:{a['mbid']}" not in have]
        if not step("Step 4/4 — artist genres", todo_c, mbcache.artist_genres):
            return

        s = summary(conn)
        log(f"Gathered. Albums: {s['albums']['tagged']} would be tagged, {s['albums']['none']} have nothing on MusicBrainz, "
            f"{s['albums']['nomb']} have no MusicBrainz id. Artists: {s['artists']['tagged']} would be tagged. "
            "Nothing is applied yet — review the dry run in Maintenance › Genres.")
    finally:
        conn.close()


# -- the plan (dry run) and applying it ------------------------------------------------------

def plan(conn) -> tuple[list[dict], list[dict]]:
    albums, artists = _untagged_albums(conn), _artists_in_use(conn)
    idx = _Index(conn, albums, artists)
    for a in albums:
        a.update(idx.album(a))
    for a in artists:
        a.update(idx.artist(a))
    return albums, artists


def _counts(rows: list[dict]) -> dict:
    out = {"tagged": 0, "strong": 0, "weak": 0, "none": 0, "pending": 0, "nomb": 0}
    for r in rows:
        out[r["status"]] = out.get(r["status"], 0) + 1
        if r["status"] == "tagged":
            out["strong" if r["genres"][0][2] >= genre_tags.MB_MIN_VOTES else "weak"] += 1
    return out


def summary(conn) -> dict:
    albums, artists = plan(conn)
    return {"albums": _counts(albums), "artists": _counts(artists)}


def _row(r: dict) -> dict:
    out = {k: r[k] for k in ("status", "plays") if k in r}
    out.update({k: r.get(k) for k in ("albumId", "title", "artist", "artistId", "name", "vinyl", "from")})
    if r.get("genres"):
        out["genres"] = [{"name": n, "votes": v} for n, _m, v in r["genres"]]
        out["all"] = [{"name": n, "votes": v} for n, _m, v in r["all"][:6]]
    return out


def preview(conn, seed: int | None = None) -> dict:
    albums, artists = plan(conn)
    rnd = random.Random(seed)
    tagged_al = [a for a in albums if a["status"] == "tagged"]
    tagged_ar = [a for a in artists if a["status"] == "tagged"]
    others = [a for a in albums if a["status"] in ("none", "pending") and a["mbid"]]
    by_genre: dict[str, int] = {}
    for a in tagged_al:
        for n, _m, _v in a["genres"]:
            by_genre[n] = by_genre.get(n, 0) + 1
    return {
        "albums": _counts(albums), "artists": _counts(artists),
        "albumPlays": sum(a["plays"] for a in tagged_al),
        "topGenres": sorted(by_genre.items(), key=lambda kv: -kv[1])[:20],
        "sampleAlbums": [_row(a) for a in rnd.sample(tagged_al, min(40, len(tagged_al)))],
        "sampleArtists": [_row(a) for a in rnd.sample(tagged_ar, min(20, len(tagged_ar)))],
        "sampleUntagged": [_row(a) for a in rnd.sample(others, min(10, len(others)))],
        "mostPlayed": [_row(a) for a in sorted(tagged_al, key=lambda a: -a["plays"])[:15]],
    }


def apply(c) -> tuple[list[dict], dict]:
    """Writes the plan inside the caller's transaction -> (undo handles, summary)."""
    albums, artists = plan(c)
    handles, n_al = [], 0
    for a in albums:
        if a["status"] != "tagged":
            continue
        keep = [{"name": n, "mbid": m, "source": "musicbrainz", "votes": v} for n, m, v in a["genres"]]
        edit = genre_tags.set_album(c, a["albumId"], keep, reason=REASON)
        if edit:
            handles.append({"kind": "edit", "id": edit})
            n_al += 1
    replaced, inserted = [], []
    for a in artists:
        if a["status"] != "tagged":
            continue
        prev = c.execute("SELECT artist_id, genre_id, votes, fetched_at FROM artist_mb_genres WHERE artist_id = ?", (a["artistId"],)).fetchall()
        replaced += [list(r) for r in prev]
        c.execute("DELETE FROM artist_mb_genres WHERE artist_id = ?", (a["artistId"],))
        for n, m, v in a["genres"]:
            gid = genre_tags.genre_id(c, n, m)
            if gid is not None and c.execute("INSERT OR IGNORE INTO artist_mb_genres (artist_id, genre_id, votes) VALUES (?, ?, ?)",
                                             (a["artistId"], gid, v or None)).rowcount:
                inserted.append([a["artistId"], gid])
    if inserted or replaced:
        handles.append({"kind": "artistMbGenres", "inserted": inserted, "replaced": replaced})
    n_ar = len({r[0] for r in inserted})
    return handles, {"text": f"MusicBrainz genres for {n_al} album(s) and {n_ar} artist(s)", "rowsMoved": {"albums": n_al, "artists": n_ar}}


def undo_artist_rows(c, h: dict) -> None:
    for aid, gid in h.get("inserted") or []:
        c.execute("DELETE FROM artist_mb_genres WHERE artist_id = ? AND genre_id = ?", (aid, gid))
    c.executemany("INSERT OR REPLACE INTO artist_mb_genres (artist_id, genre_id, votes, fetched_at) VALUES (?, ?, ?, ?)",
                  [tuple(r) for r in h.get("replaced") or []])


# -- the schedule ----------------------------------------------------------------------------

_arm_lock = threading.Lock()


def scheduled() -> dict | None:
    try:
        return json.loads(SCHEDULE_FILE.read_text())
    except (OSError, ValueError):
        return None


def schedule(at: datetime) -> dict:
    SCHEDULE_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {"at": at.isoformat(timespec="minutes"), "token": uuid.uuid4().hex, "createdAt": datetime.now().isoformat(timespec="seconds")}
    tmp = SCHEDULE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry))
    tmp.replace(SCHEDULE_FILE)
    arm()
    return entry


def cancel_schedule() -> bool:
    try:
        SCHEDULE_FILE.unlink()
        return True
    except FileNotFoundError:
        return False


def running_job() -> tuple[str, dict] | None:
    if JOBS_LOCK is None:
        return None
    with JOBS_LOCK:
        for jid, job in JOBS.items():
            if job.get("kind") == f"sweep:{KIND}":
                if not job["done"]:
                    return jid, {"progress": job.get("progress"), "lines": len(job["lines"]), "done": False}
        last = [(jid, j) for jid, j in JOBS.items() if j.get("kind") == f"sweep:{KIND}"]
    if last:
        jid, j = last[-1]
        return jid, {"progress": j.get("progress"), "lines": len(j["lines"]), "done": True, "returncode": j.get("returncode"),
                     "cancelled": bool(j.get("cancel"))}
    return None


def arm() -> None:
    """Starts (once per scheduled entry) the thread that waits for the time, then for anything
    running to finish, then starts the sweep. A cancelled or replaced entry ends its thread."""
    entry = scheduled()
    if not entry:
        return
    with _arm_lock:
        if entry["token"] in _armed:
            return
        _armed.add(entry["token"])
    threading.Thread(target=_wait_and_start, args=(entry,), daemon=True, name="genre-fill-schedule").start()


_armed: set[str] = set()


def _wait_and_start(entry: dict) -> None:
    import suggest
    at = datetime.fromisoformat(entry["at"])
    waiting_logged = False
    while True:
        cur = scheduled()
        if not cur or cur.get("token") != entry["token"]:
            return  # cancelled or replaced
        if datetime.now() >= at:
            busy = BUSY() if BUSY else None
            if not busy:
                job_id = uuid.uuid4().hex
                err = suggest.start(KIND, 1_000_000, JOBS, JOBS_LOCK, job_id)
                if not err:
                    cancel_schedule()
                    with JOBS_LOCK:
                        JOBS[job_id]["lines"].insert(0, f"Scheduled run (set for {entry['at']}), started {datetime.now():%H:%M}.\n")
                    print(f"genre fill: scheduled run started (job {job_id})", flush=True)
                    return
                busy = err
            if not waiting_logged:
                print(f"genre fill: due, waiting — {busy}", flush=True)
                waiting_logged = True
        time.sleep(30)
