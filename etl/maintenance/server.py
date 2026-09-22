"""
Web UI for etl/refresh.py -- a nicer way to trigger a data refresh, read
its report, and then actually decide whether to keep it, than scrolling
terminal output and hoping for the best.

It executes local scripts (which hit your Last.fm/Setlist.fm API keys and
can import an arbitrary CSV from imports/) on request, so it's gated
behind a shared password rather than left open to whoever can reach the
port. Whether it's reachable beyond this machine is controlled by one
thing: set MAINTENANCE_PASSWORD in .env and it binds to the LAN with
every request requiring that password (HTTP Basic Auth -- browsers
prompt once and remember it per-origin); leave it unset and it stays on
127.0.0.1 with no auth check, exactly as before. There's deliberately no
way to end up LAN-reachable without a password required, or vice versa.

Usage:
    python3 etl/maintenance/server.py
    open http://localhost:8643/
"""
import base64
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from collections import defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import combinations
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import numpy as np
import requests
from rapidfuzz import fuzz, process

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "etl"))
from common import connect as db_connect, load_env  # noqa: E402
from musicbrainz import search_artists, search_release_groups  # noqa: E402

load_env()
PASSWORD = os.environ.get("MAINTENANCE_PASSWORD", "")

PAGE_PATH = Path(__file__).resolve().parent / "index.html"
ARTISTS_PAGE_PATH = Path(__file__).resolve().parent / "artists.html"
DUPLICATES_PAGE_PATH = Path(__file__).resolve().parent / "duplicates.html"
ALBUMS_PAGE_PATH = Path(__file__).resolve().parent / "albums.html"
ALBUM_DUPLICATES_PAGE_PATH = Path(__file__).resolve().parent / "album-duplicates.html"
VINYL_PAGE_PATH = Path(__file__).resolve().parent / "vinyl.html"
SHARED_JS_PATH = Path(__file__).resolve().parent / "shared.js"
DATA_DB = ROOT / "data" / "music.sqlite"
BACKUP_DIR = ROOT / "data" / ".refresh_backups"
MERGE_BACKUP_DIR = ROOT / "data" / ".merge_backups"  # shared by artist and album merges
PORT = 8643

MBID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

# Duplicate-name scan tuning. token_set_ratio alone can't tell "Bush" vs
# "Kate Bush" (coincidence) from "Bob Marley" vs "Bob Marley & The
# Wailers" (same act) -- both score 100, since a short name that's a
# subset of a longer one's words always will. In practice every false
# positive we found on the real dataset involved a very short name
# (4-7 chars); requiring the shorter side to clear DUPLICATE_MIN_SHORT_LEN
# cuts that noise from ~22k candidates down to a genuinely reviewable
# list without losing real matches (verified against this project's own
# data -- "Bob Marley"/10 chars, "The Wailers"/11 chars both clear it).
DUPLICATE_SCORER = fuzz.token_set_ratio
DUPLICATE_DEFAULT_MIN_SCORE = 92
DUPLICATE_MIN_SHORT_LEN = 8

# Album duplicate scan tuning. Unlike artists, this is scoped per-artist
# (see _handle_album_duplicate_candidates) -- comparing "Greatest Hits"
# against every other "Greatest Hits" in the whole library would be
# useless noise, but comparing it only against *this artist's own*
# other albums is exactly the "Remastered"/"Special Edition"/"Deluxe
# Edition" case this feature exists for. That scoping already does most of
# the false-positive filtering the artist scan needed DUPLICATE_MIN_SHORT_LEN
# for, so this can run with a shorter floor -- just enough to stop e.g. an
# artist's "Live" and "Live at Wembley" (genuinely different releases)
# from matching on a short common word alone.
ALBUM_DUPLICATE_DEFAULT_MIN_SCORE = 90
ALBUM_DUPLICATE_MIN_SHORT_LEN = 6

# job_id -> {
#   "lines": [str, ...] (append-only),
#   "done": bool, "returncode": int|None,
#   "kind": "refresh" | "build",
#   "backup": Path|None,   -- only for "refresh" jobs; the pre-run copy of
#                              data/music.sqlite, so a rejection has
#                              something to restore
#   "decision": "accepted" | "rejected" | None,
# }
# Lines are append-only + the client tracks its own offset (rather than
# "read and clear" server-side), so a missed poll can never silently
# drop output.
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


def run_job(job_id: str, cmd_args: list[str]) -> None:
    proc = subprocess.Popen(
        [sys.executable, *cmd_args],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    for line in proc.stdout:
        with jobs_lock:
            jobs[job_id]["lines"].append(line)
    proc.wait()
    with jobs_lock:
        jobs[job_id]["done"] = True
        jobs[job_id]["returncode"] = proc.returncode


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # keep the terminal quiet -- the web UI is the point

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str):
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def _authorized(self) -> bool:
        """No password configured -> loopback-only mode (see main()), where auth is moot -- always allow.
        Password configured -> every request needs it, timing-safe compared; username is ignored."""
        if not PASSWORD:
            return True
        given = self.headers.get("Authorization", "")
        if not given.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(given[len("Basic "):]).decode()
        except Exception:
            return False
        _, _, pw = decoded.partition(":")
        return hmac.compare_digest(pw, PASSWORD)

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Music Maintenance"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if not self._require_auth():
            return
        # Used only by the new /api/artists/... routes below -- the
        # pre-existing routes keep parsing self.path themselves, untouched.
        path = urlsplit(self.path).path
        query = parse_qs(urlsplit(self.path).query)

        if self.path == "/":
            self._send_file(PAGE_PATH, "text/html; charset=utf-8")
        elif self.path == "/artists.html":
            self._send_file(ARTISTS_PAGE_PATH, "text/html; charset=utf-8")
        elif self.path == "/duplicates.html":
            self._send_file(DUPLICATES_PAGE_PATH, "text/html; charset=utf-8")
        elif self.path == "/albums.html":
            self._send_file(ALBUMS_PAGE_PATH, "text/html; charset=utf-8")
        elif self.path == "/album-duplicates.html":
            self._send_file(ALBUM_DUPLICATES_PAGE_PATH, "text/html; charset=utf-8")
        elif self.path == "/vinyl.html":
            self._send_file(VINYL_PAGE_PATH, "text/html; charset=utf-8")
        elif self.path == "/shared.js":
            self._send_file(SHARED_JS_PATH, "application/javascript; charset=utf-8")
        elif self.path == "/imports":
            imports_dir = ROOT / "imports"
            csvs = sorted(p.name for p in imports_dir.glob("*.csv")) if imports_dir.exists() else []
            self._send_json({"files": csvs})
        elif self.path.startswith("/status/"):
            job_id, _, jquery = self.path.split("/status/", 1)[1].partition("?")
            since = int(dict(p.split("=") for p in jquery.split("&") if "=" in p).get("since", "0")) if jquery else 0
            with jobs_lock:
                job = jobs.get(job_id)
                if not job:
                    return self._send_json({"error": "unknown job"}, status=404)
                payload = {
                    "lines": job["lines"][since:],
                    "total": len(job["lines"]),
                    "done": job["done"],
                    "returncode": job["returncode"],
                    "kind": job["kind"],
                    "decision": job["decision"],
                }
            self._send_json(payload)
        elif path == "/api/artists/missing-mbid":
            self._handle_missing_mbid(query)
        elif path == "/api/artists/mb-search":
            self._handle_mb_search(query)
        elif path == "/api/artists/local-search":
            self._handle_local_search(query)
        elif path == "/api/artists/detail":
            self._handle_artist_detail(query)
        elif path == "/api/artists/top-songs":
            self._handle_top_songs(query)
        elif path == "/api/artists/duplicate-candidates":
            self._handle_duplicate_candidates(query)
        elif path == "/api/albums/missing-mbid":
            self._handle_albums_missing_mbid(query)
        elif path == "/api/albums/mb-search":
            self._handle_album_mb_search(query)
        elif path == "/api/albums/local-search":
            self._handle_album_local_search(query)
        elif path == "/api/albums/detail":
            self._handle_album_detail(query)
        elif path == "/api/albums/tracklist":
            self._handle_album_tracklist(query)
        elif path == "/api/albums/duplicate-candidates":
            self._handle_album_duplicate_candidates(query)
        elif path == "/api/vinyl/queue":
            self._handle_vinyl_queue(query)
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._require_auth():
            return
        if self.path == "/run":
            return self._handle_run()
        if self.path == "/build":
            return self._handle_build()
        if self.path.startswith("/decision/"):
            return self._handle_decision(self.path.split("/decision/", 1)[1])
        if self.path == "/api/artists/assign-mbid":
            return self._handle_assign_mbid()
        if self.path == "/api/artists/merge":
            return self._handle_merge_artists()
        if self.path == "/api/artists/dismiss-duplicate":
            return self._handle_dismiss_duplicate()
        if self.path == "/api/albums/assign-mbid":
            return self._handle_assign_album_mbid()
        if self.path == "/api/albums/merge":
            return self._handle_merge_albums()
        if self.path == "/api/albums/dismiss-duplicate":
            return self._handle_dismiss_album_duplicate()
        return self.send_error(404)

    def _handle_run(self):
        body = self._read_json_body()

        args = []
        if body.get("lastfm"):
            args.append("--lastfm")
        if body.get("setlistfm"):
            args.append("--setlistfm")
        if body.get("discogsFile"):
            csv_path = ROOT / "imports" / body["discogsFile"]
            if not csv_path.is_file():
                return self._send_json({"error": f"{csv_path} not found"}, status=400)
            args += ["--discogs", str(csv_path)]

        if not args:
            return self._send_json({"error": "Select at least one source to refresh."}, status=400)

        if not DATA_DB.is_file():
            return self._send_json({"error": f"{DATA_DB} doesn't exist -- run the ETL scripts first."}, status=400)

        # Snapshot the working database before touching it, so a rejection
        # has something concrete to restore -- refresh.py's own report is
        # informative, but this is what actually makes "reject" real.
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        job_id = uuid.uuid4().hex
        backup_path = BACKUP_DIR / f"{job_id}.sqlite"
        shutil.copy2(DATA_DB, backup_path)

        with jobs_lock:
            jobs[job_id] = {
                "lines": [], "done": False, "returncode": None,
                "kind": "refresh", "backup": backup_path, "decision": None,
            }
        threading.Thread(target=run_job, args=(job_id, ["etl/refresh.py", *args]), daemon=True).start()
        self._send_json({"jobId": job_id})

    def _handle_build(self):
        job_id = uuid.uuid4().hex
        with jobs_lock:
            jobs[job_id] = {
                "lines": [], "done": False, "returncode": None,
                "kind": "build", "backup": None, "decision": None,
            }
        threading.Thread(target=run_job, args=(job_id, ["etl/build_public_db.py"]), daemon=True).start()
        self._send_json({"jobId": job_id})

    def _handle_decision(self, job_id: str):
        body = self._read_json_body()
        action = body.get("action")
        if action not in ("accept", "reject"):
            return self._send_json({"error": "action must be 'accept' or 'reject'"}, status=400)

        with jobs_lock:
            job = jobs.get(job_id)
            if not job or job["kind"] != "refresh":
                return self._send_json({"error": "unknown refresh job"}, status=404)
            if not job["done"]:
                return self._send_json({"error": "job still running"}, status=409)
            if job["decision"] is not None:
                return self._send_json({"error": f"already {job['decision']}"}, status=409)
            backup_path = job["backup"]

        if action == "reject":
            if not backup_path or not backup_path.is_file():
                return self._send_json({"error": "backup no longer available"}, status=500)
            shutil.copy2(backup_path, DATA_DB)

        # Either way the backup has done its job -- accepted means we're
        # keeping the new state, rejected means we've already restored it.
        if backup_path and backup_path.is_file():
            backup_path.unlink()

        with jobs_lock:
            jobs[job_id]["decision"] = "accepted" if action == "accept" else "rejected"
        self._send_json({"decision": jobs[job_id]["decision"]})

    # -- Artist MBID resolution / merge -----------------------------------
    # Unlike /run and /build above, these are fast synchronous DB
    # operations on data/music.sqlite -- no job/log/poll machinery needed.
    # Each mutating handler opens its own connection (sqlite3.Connection
    # isn't safe to share across ThreadingHTTPServer's request threads)
    # and wraps writes in an explicit transaction; common.connect() already
    # turns PRAGMA foreign_keys = ON, so a merge that forgets to move a FK
    # reference off the absorbed row before deleting it fails loudly
    # instead of leaving orphaned rows.

    def _handle_missing_mbid(self, query):
        try:
            min_count = int((query.get("minCount") or ["5"])[0])
        except ValueError:
            return self._send_json({"error": "minCount must be an integer"}, status=400)

        conn = db_connect()
        try:
            rows = conn.execute(
                """
                SELECT ar.id, ar.name, count(ar.id) AS cnt
                FROM scrobbles s2
                JOIN songs s ON s.id = s2.song_id
                JOIN artists ar ON ar.id = s.artist_id
                WHERE ar.mbid IS NULL
                GROUP BY ar.id, ar.name
                HAVING cnt >= ?
                ORDER BY cnt DESC
                LIMIT 500
                """,
                (min_count,),
            ).fetchall()
            total_missing = conn.execute("SELECT count(*) FROM artists WHERE mbid IS NULL").fetchone()[0]
        finally:
            conn.close()

        self._send_json({
            "minCount": min_count,
            "totalMissing": total_missing,
            "queueCount": len(rows),
            "rows": [{"artistId": r[0], "name": r[1], "scrobbleCount": r[2]} for r in rows],
        })

    def _handle_mb_search(self, query):
        q = (query.get("q") or [""])[0].strip()
        if not q:
            return self._send_json({"error": "q is required"}, status=400)
        try:
            limit = int((query.get("limit") or ["10"])[0])
        except ValueError:
            limit = 10

        try:
            candidates = search_artists(q, limit=limit)
        except requests.RequestException as exc:
            return self._send_json({"error": f"MusicBrainz request failed: {exc}"}, status=502)
        self._send_json({"candidates": candidates})

    def _handle_local_search(self, query):
        q = (query.get("q") or [""])[0].strip()
        if not q:
            return self._send_json({"results": []})
        exclude_id = (query.get("excludeId") or [None])[0]
        try:
            limit = int((query.get("limit") or ["10"])[0])
        except ValueError:
            limit = 10

        conn = db_connect()
        try:
            rows = conn.execute("SELECT id, name FROM artists").fetchall()
        finally:
            conn.close()

        choices = {r[0]: r[1] for r in rows if exclude_id is None or str(r[0]) != str(exclude_id)}
        matches = process.extract(q, choices, scorer=fuzz.WRatio, limit=limit, score_cutoff=55)
        self._send_json({
            "results": [
                {"artistId": artist_id, "name": name, "score": round(score, 1)}
                for name, score, artist_id in matches
            ]
        })

    def _handle_artist_detail(self, query):
        try:
            artist_id = int((query.get("id") or [""])[0])
        except ValueError:
            return self._send_json({"error": "id is required"}, status=400)

        conn = db_connect()
        try:
            row = conn.execute("SELECT id, name, mbid FROM artists WHERE id = ?", (artist_id,)).fetchone()
            if not row:
                return self._send_json({"error": "artist not found"}, status=404)
            scrobble_count = conn.execute(
                "SELECT count(*) FROM scrobbles WHERE artist_id = ?", (artist_id,)
            ).fetchone()[0]
        finally:
            conn.close()

        self._send_json({"artistId": row[0], "name": row[1], "mbid": row[2], "scrobbleCount": scrobble_count})

    def _handle_top_songs(self, query):
        try:
            artist_id = int((query.get("id") or [""])[0])
        except ValueError:
            return self._send_json({"error": "id is required"}, status=400)
        try:
            limit = int((query.get("limit") or ["5"])[0])
        except ValueError:
            limit = 5

        conn = db_connect()
        try:
            rows = conn.execute(
                """
                SELECT s.id, s.title, count(*) AS cnt
                FROM scrobbles sc
                JOIN songs s ON s.id = sc.song_id
                WHERE sc.artist_id = ?
                GROUP BY s.id, s.title
                ORDER BY cnt DESC
                LIMIT ?
                """,
                (artist_id, limit),
            ).fetchall()
        finally:
            conn.close()

        self._send_json({
            "artistId": artist_id,
            "songs": [{"songId": r[0], "title": r[1], "scrobbleCount": r[2]} for r in rows],
        })

    def _handle_duplicate_candidates(self, query):
        try:
            min_score = float((query.get("minScore") or [str(DUPLICATE_DEFAULT_MIN_SCORE)])[0])
        except ValueError:
            min_score = DUPLICATE_DEFAULT_MIN_SCORE
        try:
            limit = int((query.get("limit") or ["100"])[0])
        except ValueError:
            limit = 100

        conn = db_connect()
        try:
            rows = conn.execute(
                """
                SELECT ar.id, ar.name, ar.mbid, count(sc.id) AS cnt
                FROM artists ar LEFT JOIN scrobbles sc ON sc.artist_id = ar.id
                GROUP BY ar.id
                """
            ).fetchall()
            dismissed = conn.execute(
                "SELECT entity_id_a, entity_id_b FROM duplicate_dismissals WHERE entity_type = 'artist'"
            ).fetchall()
        finally:
            conn.close()

        dismissed_set = {(a, b) for a, b in dismissed}
        artists = [{"artistId": r[0], "name": r[1], "mbid": r[2], "scrobbleCount": r[3]} for r in rows]
        names = [a["name"] for a in artists]
        n = len(names)

        candidates = []
        if n >= 2:
            # cdist computes the full n x n similarity matrix in one
            # optimized batch call -- for ~3000 artists this takes well
            # under a second, vs. minutes for a naive Python double loop.
            score_matrix = process.cdist(names, names, scorer=DUPLICATE_SCORER, score_cutoff=min_score, workers=-1)
            lens = np.array([len(name) for name in names])
            short_len_matrix = np.minimum.outer(lens, lens)

            # Upper triangle only (i < j) -- skips self-pairs and each
            # pair's mirror image.
            iu = np.triu_indices(n, k=1)
            scores = score_matrix[iu]
            short_lens = short_len_matrix[iu]
            keep = (scores >= min_score) & (short_lens >= DUPLICATE_MIN_SHORT_LEN)
            idx_i, idx_j, kept_scores = iu[0][keep], iu[1][keep], scores[keep]

            for i, j, score in zip(idx_i.tolist(), idx_j.tolist(), kept_scores.tolist()):
                id_a, id_b = artists[i]["artistId"], artists[j]["artistId"]
                pair = (min(id_a, id_b), max(id_a, id_b))
                if pair in dismissed_set:
                    continue
                candidates.append({"a": artists[i], "b": artists[j], "score": round(score, 1)})
            candidates.sort(key=lambda c: c["score"], reverse=True)

        self._send_json({
            "minScore": min_score,
            "candidateCount": len(candidates),
            "candidates": candidates[:limit],
        })

    def _handle_dismiss_duplicate(self):
        body = self._read_json_body()
        a_id, b_id = body.get("aId"), body.get("bId")
        if not a_id or not b_id or a_id == b_id:
            return self._send_json({"error": "aId and bId (two different artist ids) are required"}, status=400)
        id_a, id_b = sorted((a_id, b_id))

        conn = db_connect()
        try:
            conn.execute(
                """
                INSERT INTO duplicate_dismissals (entity_type, entity_id_a, entity_id_b)
                VALUES ('artist', ?, ?)
                ON CONFLICT (entity_type, entity_id_a, entity_id_b) DO NOTHING
                """,
                (id_a, id_b),
            )
            conn.commit()
        finally:
            conn.close()
        self._send_json({"dismissed": True})

    def _handle_assign_mbid(self):
        body = self._read_json_body()
        artist_id = body.get("artistId")
        mbid = (body.get("mbid") or "").strip()
        if not artist_id or not mbid:
            return self._send_json({"error": "artistId and mbid are required"}, status=400)
        if not MBID_RE.match(mbid):
            return self._send_json({"error": "mbid doesn't look like a MusicBrainz id (expected a UUID)"}, status=400)

        conn = db_connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conflict = conn.execute(
                "SELECT id, name, mbid FROM artists WHERE mbid = ? AND id != ?", (mbid, artist_id)
            ).fetchone()
            if conflict:
                conn.rollback()
                return self._send_json({
                    "error": "mbid_conflict",
                    "message": f"That MusicBrainz id is already linked to \"{conflict[1]}\" in your library.",
                    "conflictingArtist": {"artistId": conflict[0], "name": conflict[1], "mbid": conflict[2]},
                }, status=409)

            row = conn.execute("SELECT id, name FROM artists WHERE id = ?", (artist_id,)).fetchone()
            if not row:
                conn.rollback()
                return self._send_json({"error": "artist not found"}, status=404)

            conn.execute("UPDATE artists SET mbid = ? WHERE id = ?", (mbid, artist_id))
            conn.commit()
            self._send_json({"artistId": row[0], "name": row[1], "mbid": mbid})
        except Exception as exc:
            conn.rollback()
            self._send_json({"error": str(exc)}, status=500)
        finally:
            conn.close()

    def _handle_merge_artists(self):
        body = self._read_json_body()
        absorbed_id = body.get("absorbedId")
        canonical_id = body.get("canonicalId")
        if not absorbed_id or not canonical_id:
            return self._send_json({"error": "absorbedId and canonicalId are required"}, status=400)
        if absorbed_id == canonical_id:
            return self._send_json({"error": "absorbedId and canonicalId must differ"}, status=400)

        conn = db_connect()
        try:
            absorbed = conn.execute("SELECT id, name, mbid FROM artists WHERE id = ?", (absorbed_id,)).fetchone()
            canonical = conn.execute("SELECT id, name FROM artists WHERE id = ?", (canonical_id,)).fetchone()
        finally:
            conn.close()
        if not absorbed or not canonical:
            return self._send_json({"error": "absorbedId and canonicalId must both be existing artists"}, status=404)

        # Safety copy before anything destructive -- this merge is the
        # already-reviewed action (the UI confirms before calling this),
        # so no separate accept/reject dance, just an undo-by-hand path.
        MERGE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup_path = MERGE_BACKUP_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-artist-{absorbed_id}-into-{canonical_id}.sqlite"
        shutil.copy2(DATA_DB, backup_path)

        conn = db_connect()
        rows_moved = {}
        try:
            conn.execute("BEGIN IMMEDIATE")

            # album_artists: PK is (album_id, artist_id) -- drop the
            # absorbed artist's credit wherever the canonical artist is
            # ALREADY credited on the same album (a blind UPDATE would
            # collide on that PK), then reassign what's left.
            rows_moved["album_artists_collisions_dropped"] = conn.execute(
                """
                DELETE FROM album_artists
                WHERE artist_id = ?
                  AND album_id IN (SELECT album_id FROM album_artists WHERE artist_id = ?)
                """,
                (absorbed_id, canonical_id),
            ).rowcount
            rows_moved["album_artists_reassigned"] = conn.execute(
                "UPDATE album_artists SET artist_id = ? WHERE artist_id = ?", (canonical_id, absorbed_id)
            ).rowcount

            for table in ("albums", "songs", "scrobbles", "setlists"):
                rows_moved[table] = conn.execute(
                    f"UPDATE {table} SET artist_id = ? WHERE artist_id = ?", (canonical_id, absorbed_id)
                ).rowcount

            rows_moved["notes"] = conn.execute(
                "UPDATE notes SET entity_id = ? WHERE entity_type = 'artist' AND entity_id = ?",
                (canonical_id, absorbed_id),
            ).rowcount

            # absorbed may itself have previously been a merge *target* --
            # repoint so an older override chain doesn't dangle on a
            # deleted id.
            rows_moved["alias_overrides_repointed"] = conn.execute(
                "UPDATE alias_overrides SET canonical_id = ? WHERE canonical_type = 'artist' AND canonical_id = ?",
                (canonical_id, absorbed_id),
            ).rowcount

            # Close the loop for future imports: one row per known source,
            # keyed on the absorbed artist's own raw name text, so
            # re-encountering that exact string resolves straight to
            # canonical instead of recreating the duplicate.
            absorbed_key = absorbed[1].strip().lower()
            note = f"merged from duplicate artist id {absorbed_id} on {datetime.now():%Y-%m-%d}"
            for source in ("lastfm", "setlistfm", "discogs"):
                conn.execute(
                    """
                    INSERT INTO alias_overrides (source, source_key, canonical_type, canonical_id, note)
                    VALUES (?, ?, 'artist', ?, ?)
                    ON CONFLICT (source, source_key, canonical_type) DO UPDATE SET canonical_id = excluded.canonical_id
                    """,
                    (source, absorbed_key, canonical_id, note),
                )

            conn.execute(
                """
                INSERT INTO merge_log (entity_type, absorbed_id, absorbed_name, absorbed_mbid,
                                        canonical_id, canonical_name, rows_moved_json)
                VALUES ('artist', ?, ?, ?, ?, ?, ?)
                """,
                (absorbed_id, absorbed[1], absorbed[2], canonical_id, canonical[1], json.dumps(rows_moved)),
            )

            conn.execute("DELETE FROM artists WHERE id = ?", (absorbed_id,))  # last -- every FK is moved off by now
            conn.commit()
        except Exception as exc:
            conn.rollback()
            return self._send_json({"error": str(exc), "backup": str(backup_path)}, status=500)
        finally:
            conn.close()

        self._send_json({
            "absorbedId": absorbed_id,
            "absorbedName": absorbed[1],
            "canonicalId": canonical_id,
            "canonicalName": canonical[1],
            "rowsMoved": rows_moved,
            "backup": str(backup_path),
        })

    # -- Album MBID resolution / merge -------------------------------------
    # Same shape as the artist handlers above, adapted for two album-
    # specific wrinkles: "activity" for the missing-mbid queue is scrobbles
    # *and* vinyl holdings combined (a rarely-scrobbled record you actually
    # own matters here just as much as a heavily-scrobbled one), and the
    # duplicate scan is scoped per-artist rather than global -- see
    # ALBUM_DUPLICATE_MIN_SHORT_LEN's comment for why.

    def _handle_vinyl_queue(self, query):
        conn = db_connect()
        try:
            rows = conn.execute(
                """
                SELECT vh.id, vh.album_id, al.title, al.mbid, al.artist_id, ar.name, ar.mbid,
                       vh.format, vh.label, vh.date_added
                FROM vinyl_holdings vh
                JOIN albums al ON al.id = vh.album_id
                JOIN artists ar ON ar.id = al.artist_id
                ORDER BY ar.name, al.title
                """
            ).fetchall()
        finally:
            conn.close()

        self._send_json({
            "totalVinyl": len(rows),
            "rows": [
                {
                    "vinylId": r[0], "albumId": r[1], "albumTitle": r[2], "albumMbid": r[3],
                    "artistId": r[4], "artistName": r[5], "artistMbid": r[6],
                    "format": r[7], "label": r[8], "dateAdded": r[9],
                }
                for r in rows
            ],
        })

    def _handle_albums_missing_mbid(self, query):
        try:
            min_count = int((query.get("minCount") or ["3"])[0])
        except ValueError:
            return self._send_json({"error": "minCount must be an integer"}, status=400)

        conn = db_connect()
        try:
            rows = conn.execute(
                """
                WITH album_stats AS (
                    SELECT al.id AS album_id, al.title,
                           ar.id AS artist_id, ar.name AS artist_name, ar.mbid AS artist_mbid,
                           (SELECT count(*) FROM scrobbles sc WHERE sc.album_id = al.id) AS scrobble_count,
                           (SELECT count(*) FROM vinyl_holdings vh WHERE vh.album_id = al.id) AS vinyl_count
                    FROM albums al
                    JOIN artists ar ON ar.id = al.artist_id
                    WHERE al.mbid IS NULL
                )
                SELECT album_id, title, artist_id, artist_name, artist_mbid, scrobble_count, vinyl_count
                FROM album_stats
                WHERE (scrobble_count + vinyl_count) >= ?
                ORDER BY (scrobble_count + vinyl_count) DESC
                LIMIT 500
                """,
                (min_count,),
            ).fetchall()
            total_missing = conn.execute("SELECT count(*) FROM albums WHERE mbid IS NULL").fetchone()[0]
        finally:
            conn.close()

        self._send_json({
            "minCount": min_count,
            "totalMissing": total_missing,
            "queueCount": len(rows),
            "rows": [
                {
                    "albumId": r[0], "title": r[1],
                    "artistId": r[2], "artistName": r[3], "artistMbid": r[4],
                    "scrobbleCount": r[5], "vinylCount": r[6],
                }
                for r in rows
            ],
        })

    def _handle_album_mb_search(self, query):
        q = (query.get("q") or [""])[0].strip()
        if not q:
            return self._send_json({"error": "q is required"}, status=400)
        try:
            limit = int((query.get("limit") or ["10"])[0])
        except ValueError:
            limit = 10
        artist_name = (query.get("artistName") or [None])[0]
        artist_mbid = (query.get("artistMbid") or [None])[0]

        try:
            candidates = search_release_groups(q, artist_name=artist_name, artist_mbid=artist_mbid, limit=limit)
        except requests.RequestException as exc:
            return self._send_json({"error": f"MusicBrainz request failed: {exc}"}, status=502)
        self._send_json({"candidates": candidates})

    def _handle_album_local_search(self, query):
        q = (query.get("q") or [""])[0].strip()
        if not q:
            return self._send_json({"results": []})
        exclude_id = (query.get("excludeId") or [None])[0]
        try:
            limit = int((query.get("limit") or ["10"])[0])
        except ValueError:
            limit = 10

        conn = db_connect()
        try:
            rows = conn.execute(
                """
                SELECT al.id, al.title, ar.name, al.year, al.mbid,
                       (SELECT count(*) FROM scrobbles sc WHERE sc.album_id = al.id) AS scrobble_count,
                       (SELECT count(*) FROM vinyl_holdings vh WHERE vh.album_id = al.id) AS vinyl_count
                FROM albums al JOIN artists ar ON ar.id = al.artist_id
                """
            ).fetchall()
        finally:
            conn.close()

        # Matched on "Artist — Title" rather than title alone -- otherwise
        # two different artists' "Greatest Hits" are indistinguishable in
        # the results list.
        choices, meta = {}, {}
        for r in rows:
            album_id = r[0]
            if exclude_id is not None and str(album_id) == str(exclude_id):
                continue
            choices[album_id] = f"{r[2]} — {r[1]}"
            meta[album_id] = {
                "title": r[1], "artistName": r[2], "year": r[3], "mbid": r[4],
                "scrobbleCount": r[5], "vinylCount": r[6],
            }

        matches = process.extract(q, choices, scorer=fuzz.WRatio, limit=limit, score_cutoff=55)
        self._send_json({
            "results": [
                {"albumId": album_id, "label": label, "score": round(score, 1), **meta[album_id]}
                for label, score, album_id in matches
            ]
        })

    def _handle_album_detail(self, query):
        try:
            album_id = int((query.get("id") or [""])[0])
        except ValueError:
            return self._send_json({"error": "id is required"}, status=400)

        conn = db_connect()
        try:
            row = conn.execute(
                """
                SELECT al.id, al.title, al.mbid, al.year, ar.id, ar.name
                FROM albums al JOIN artists ar ON ar.id = al.artist_id
                WHERE al.id = ?
                """,
                (album_id,),
            ).fetchone()
            if not row:
                return self._send_json({"error": "album not found"}, status=404)
            scrobble_count = conn.execute(
                "SELECT count(*) FROM scrobbles WHERE album_id = ?", (album_id,)
            ).fetchone()[0]
            vinyl_count = conn.execute(
                "SELECT count(*) FROM vinyl_holdings WHERE album_id = ?", (album_id,)
            ).fetchone()[0]
        finally:
            conn.close()

        self._send_json({
            "albumId": row[0], "title": row[1], "mbid": row[2], "year": row[3],
            "artistId": row[4], "artistName": row[5],
            "scrobbleCount": scrobble_count, "vinylCount": vinyl_count,
        })

    def _handle_album_tracklist(self, query):
        try:
            album_id = int((query.get("id") or [""])[0])
        except ValueError:
            return self._send_json({"error": "id is required"}, status=400)

        conn = db_connect()
        try:
            songs = conn.execute(
                """
                SELECT s.id, s.title, count(sc.id) AS cnt
                FROM songs s LEFT JOIN scrobbles sc ON sc.song_id = s.id
                WHERE s.album_id = ?
                GROUP BY s.id, s.title
                ORDER BY cnt DESC, s.title
                """,
                (album_id,),
            ).fetchall()
            vinyl = conn.execute(
                """
                SELECT id, format, label, catalog_number, media_condition, date_added
                FROM vinyl_holdings WHERE album_id = ?
                ORDER BY date_added
                """,
                (album_id,),
            ).fetchall()
        finally:
            conn.close()

        self._send_json({
            "albumId": album_id,
            "songs": [{"songId": r[0], "title": r[1], "scrobbleCount": r[2]} for r in songs],
            "vinylCopies": [
                {
                    "vinylId": r[0], "format": r[1], "label": r[2],
                    "catalogNumber": r[3], "condition": r[4], "dateAdded": r[5],
                }
                for r in vinyl
            ],
        })

    def _handle_album_duplicate_candidates(self, query):
        try:
            min_score = float((query.get("minScore") or [str(ALBUM_DUPLICATE_DEFAULT_MIN_SCORE)])[0])
        except ValueError:
            min_score = ALBUM_DUPLICATE_DEFAULT_MIN_SCORE
        try:
            limit = int((query.get("limit") or ["100"])[0])
        except ValueError:
            limit = 100

        conn = db_connect()
        try:
            rows = conn.execute(
                """
                SELECT al.id, al.title, al.mbid, al.year, al.artist_id, ar.name,
                       (SELECT count(*) FROM scrobbles sc WHERE sc.album_id = al.id) AS scrobble_count,
                       (SELECT count(*) FROM vinyl_holdings vh WHERE vh.album_id = al.id) AS vinyl_count
                FROM albums al JOIN artists ar ON ar.id = al.artist_id
                ORDER BY al.artist_id
                """
            ).fetchall()
            dismissed = conn.execute(
                "SELECT entity_id_a, entity_id_b FROM duplicate_dismissals WHERE entity_type = 'album'"
            ).fetchall()
        finally:
            conn.close()

        dismissed_set = {(a, b) for a, b in dismissed}

        # Grouped by artist first -- comparisons only ever happen within one
        # artist's own discography (see ALBUM_DUPLICATE_MIN_SHORT_LEN above).
        by_artist = defaultdict(list)
        for r in rows:
            by_artist[r[4]].append({
                "albumId": r[0], "title": r[1], "mbid": r[2], "year": r[3],
                "artistId": r[4], "artistName": r[5], "scrobbleCount": r[6], "vinylCount": r[7],
            })

        candidates = []
        for albums in by_artist.values():
            if len(albums) < 2:
                continue
            for a, b in combinations(albums, 2):
                if min(len(a["title"]), len(b["title"])) < ALBUM_DUPLICATE_MIN_SHORT_LEN:
                    continue
                score = DUPLICATE_SCORER(a["title"], b["title"])
                if score < min_score:
                    continue
                pair = (min(a["albumId"], b["albumId"]), max(a["albumId"], b["albumId"]))
                if pair in dismissed_set:
                    continue
                candidates.append({"a": a, "b": b, "score": round(score, 1)})

        candidates.sort(key=lambda c: c["score"], reverse=True)
        self._send_json({
            "minScore": min_score,
            "candidateCount": len(candidates),
            "candidates": candidates[:limit],
        })

    def _handle_dismiss_album_duplicate(self):
        body = self._read_json_body()
        a_id, b_id = body.get("aId"), body.get("bId")
        if not a_id or not b_id or a_id == b_id:
            return self._send_json({"error": "aId and bId (two different album ids) are required"}, status=400)
        id_a, id_b = sorted((a_id, b_id))

        conn = db_connect()
        try:
            conn.execute(
                """
                INSERT INTO duplicate_dismissals (entity_type, entity_id_a, entity_id_b)
                VALUES ('album', ?, ?)
                ON CONFLICT (entity_type, entity_id_a, entity_id_b) DO NOTHING
                """,
                (id_a, id_b),
            )
            conn.commit()
        finally:
            conn.close()
        self._send_json({"dismissed": True})

    def _handle_assign_album_mbid(self):
        body = self._read_json_body()
        album_id = body.get("albumId")
        mbid = (body.get("mbid") or "").strip()
        if not album_id or not mbid:
            return self._send_json({"error": "albumId and mbid are required"}, status=400)
        if not MBID_RE.match(mbid):
            return self._send_json({"error": "mbid doesn't look like a MusicBrainz id (expected a UUID)"}, status=400)

        conn = db_connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conflict = conn.execute(
                "SELECT id, title, mbid FROM albums WHERE mbid = ? AND id != ?", (mbid, album_id)
            ).fetchone()
            if conflict:
                conn.rollback()
                return self._send_json({
                    "error": "mbid_conflict",
                    "message": f"That MusicBrainz id is already linked to \"{conflict[1]}\" in your library.",
                    "conflictingAlbum": {"albumId": conflict[0], "title": conflict[1], "mbid": conflict[2]},
                }, status=409)

            row = conn.execute("SELECT id, title FROM albums WHERE id = ?", (album_id,)).fetchone()
            if not row:
                conn.rollback()
                return self._send_json({"error": "album not found"}, status=404)

            conn.execute("UPDATE albums SET mbid = ? WHERE id = ?", (mbid, album_id))
            conn.commit()
            self._send_json({"albumId": row[0], "title": row[1], "mbid": mbid})
        except Exception as exc:
            conn.rollback()
            self._send_json({"error": str(exc)}, status=500)
        finally:
            conn.close()

    def _handle_merge_albums(self):
        body = self._read_json_body()
        absorbed_id = body.get("absorbedId")
        canonical_id = body.get("canonicalId")
        if not absorbed_id or not canonical_id:
            return self._send_json({"error": "absorbedId and canonicalId are required"}, status=400)
        if absorbed_id == canonical_id:
            return self._send_json({"error": "absorbedId and canonicalId must differ"}, status=400)

        conn = db_connect()
        try:
            absorbed = conn.execute(
                "SELECT id, title, mbid, artist_id FROM albums WHERE id = ?", (absorbed_id,)
            ).fetchone()
            canonical = conn.execute(
                "SELECT id, title, artist_id FROM albums WHERE id = ?", (canonical_id,)
            ).fetchone()
        finally:
            conn.close()
        if not absorbed or not canonical:
            return self._send_json({"error": "absorbedId and canonicalId must both be existing albums"}, status=404)
        if absorbed[3] != canonical[2]:
            return self._send_json({
                "error": "different_artist",
                "message": "These albums belong to different artists -- merge the artists first if they're really the same act.",
            }, status=409)

        # Safety copy before anything destructive -- same as the artist
        # merge, an undo-by-hand path rather than a separate accept/reject
        # dance (the UI already confirms before calling this).
        MERGE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup_path = MERGE_BACKUP_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-album-{absorbed_id}-into-{canonical_id}.sqlite"
        shutil.copy2(DATA_DB, backup_path)

        conn = db_connect()
        rows_moved = {}
        try:
            conn.execute("BEGIN IMMEDIATE")

            # album_artists: PK is (album_id, artist_id) -- drop the
            # absorbed album's credit wherever the canonical album is
            # ALREADY credited to the same artist (a blind UPDATE would
            # collide on that PK), then reassign what's left.
            rows_moved["album_artists_collisions_dropped"] = conn.execute(
                """
                DELETE FROM album_artists
                WHERE album_id = ?
                  AND artist_id IN (SELECT artist_id FROM album_artists WHERE album_id = ?)
                """,
                (absorbed_id, canonical_id),
            ).rowcount
            rows_moved["album_artists_reassigned"] = conn.execute(
                "UPDATE album_artists SET album_id = ? WHERE album_id = ?", (canonical_id, absorbed_id)
            ).rowcount

            for table in ("songs", "vinyl_holdings", "scrobbles"):
                rows_moved[table] = conn.execute(
                    f"UPDATE {table} SET album_id = ? WHERE album_id = ?", (canonical_id, absorbed_id)
                ).rowcount

            rows_moved["notes"] = conn.execute(
                "UPDATE notes SET entity_id = ? WHERE entity_type = 'album' AND entity_id = ?",
                (canonical_id, absorbed_id),
            ).rowcount

            # absorbed may itself have previously been a merge *target* --
            # repoint so an older override chain doesn't dangle on a
            # deleted id.
            rows_moved["alias_overrides_repointed"] = conn.execute(
                "UPDATE alias_overrides SET canonical_id = ? WHERE canonical_type = 'album' AND canonical_id = ?",
                (canonical_id, absorbed_id),
            ).rowcount

            # Close the loop for future imports, same idea as the artist
            # merge: one alias_overrides row per raw title text this album
            # is actually known by in each source's own table (vinyl_holdings
            # for discogs, scrobbles for lastfm -- both already reassigned to
            # canonical_id above, so read them back from there), keyed on the
            # *absorbed* album's artist id since that's what a future import
            # of that exact text, under that artist, will resolve to (see
            # get_or_create_album's docstring in etl/common.py). Falls back
            # to the absorbed album's own title too, in case it had no rows
            # in either table yet.
            note = f"merged from duplicate album id {absorbed_id} on {datetime.now():%Y-%m-%d}"
            raw_titles_by_source = {
                "discogs": {r[0] for r in conn.execute(
                    "SELECT DISTINCT raw_title_text FROM vinyl_holdings WHERE album_id = ?", (canonical_id,)
                ).fetchall()},
                "lastfm": {r[0] for r in conn.execute(
                    "SELECT DISTINCT raw_album_text FROM scrobbles WHERE album_id = ? AND raw_album_text IS NOT NULL",
                    (canonical_id,),
                ).fetchall()},
            }
            raw_titles_by_source["discogs"].add(absorbed[1])
            raw_titles_by_source["lastfm"].add(absorbed[1])
            for source, raw_titles in raw_titles_by_source.items():
                for raw_title in raw_titles:
                    if not raw_title or not raw_title.strip():
                        continue
                    source_key = f"{absorbed[3]}:{raw_title.strip().lower()}"
                    conn.execute(
                        """
                        INSERT INTO alias_overrides (source, source_key, canonical_type, canonical_id, note)
                        VALUES (?, ?, 'album', ?, ?)
                        ON CONFLICT (source, source_key, canonical_type) DO UPDATE SET canonical_id = excluded.canonical_id
                        """,
                        (source, source_key, canonical_id, note),
                    )

            conn.execute(
                """
                INSERT INTO merge_log (entity_type, absorbed_id, absorbed_name, absorbed_mbid,
                                        canonical_id, canonical_name, rows_moved_json)
                VALUES ('album', ?, ?, ?, ?, ?, ?)
                """,
                (absorbed_id, absorbed[1], absorbed[2], canonical_id, canonical[1], json.dumps(rows_moved)),
            )

            conn.execute("DELETE FROM albums WHERE id = ?", (absorbed_id,))  # last -- every FK is moved off by now
            conn.commit()
        except Exception as exc:
            conn.rollback()
            return self._send_json({"error": str(exc), "backup": str(backup_path)}, status=500)
        finally:
            conn.close()

        self._send_json({
            "absorbedId": absorbed_id,
            "absorbedTitle": absorbed[1],
            "canonicalId": canonical_id,
            "canonicalTitle": canonical[1],
            "rowsMoved": rows_moved,
            "backup": str(backup_path),
        })


def _lan_ip() -> str:
    """Best-effort LAN address for the startup message only -- doesn't send anything."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "this machine's LAN IP"


def main():
    host = "0.0.0.0" if PASSWORD else "127.0.0.1"
    server = ThreadingHTTPServer((host, PORT), Handler)
    if PASSWORD:
        print(f"Maintenance UI: http://{_lan_ip()}:{PORT}/  (password required)")
        print("MAINTENANCE_PASSWORD is set -- reachable from the LAN. Ctrl+C to stop.")
    else:
        print(f"Maintenance UI: http://localhost:{PORT}/")
        print("No MAINTENANCE_PASSWORD set -- staying local-only. Set one in .env for LAN access. Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
