"""
Web UI for keeping data/music.sqlite clean: pulling fresh data (etl/refresh.py, with a
keep/discard decision), and the review tools for artists, vinyl, albums and songs.

It executes local scripts (which hit your Last.fm/Setlist.fm API keys and can import an
arbitrary CSV from imports/) on request, so it's gated behind a shared password rather than
left open to whoever can reach the port. Whether it's reachable beyond this machine is
controlled by one thing: set MAINTENANCE_PASSWORD in .env and it binds to the LAN with every
request requiring that password (HTTP Basic Auth -- browsers prompt once and remember it
per-origin); leave it unset and it stays on 127.0.0.1 with no auth check. There's deliberately
no way to end up LAN-reachable without a password required, or vice versa.

This file is just the shell: auth, static files, the refresh/publish jobs and dispatch. The
API itself lives in api/*.py (registered into api.core.ROUTES), merges/undo in merge.py,
MusicBrainz access in mbcache.py, background sweeps in suggest.py.

Test instance (never touches the real database or the published site):
    MUSIC_DB_PATH=/tmp/x/music.sqlite MUSIC_COVERS_DIR=/tmp/x/covers MAINTENANCE_PORT=8653 \\
        python3 etl/maintenance/server.py

Usage:
    python3 etl/maintenance/server.py
    open http://localhost:8643/
"""
import base64
import hmac
import json
import os
import shutil
import subprocess
import sys
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PAGE_DIR = Path(__file__).resolve().parent
ROOT = PAGE_DIR.parent.parent
sys.path.insert(0, str(ROOT / "etl"))
sys.path.insert(0, str(PAGE_DIR))
from common import DB_OVERRIDDEN, DB_PATH, connect as db_connect, load_env  # noqa: E402
from migrations import migrate  # noqa: E402
import covers  # noqa: E402
import merge  # noqa: E402
from api import albums as _albums, artists as _artists, editions as _editions, general as _general, imports as _imports, recordings as _recordings, songs as _songs, vinyl as _vinyl  # noqa: E402,F401  (registers routes)
from api.core import ROUTES, ApiError, Req, merge_error_response  # noqa: E402

load_env()
PASSWORD = os.environ.get("MAINTENANCE_PASSWORD", "")
PORT = int(os.environ.get("MAINTENANCE_PORT", "8643"))

PAGES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/artists.html": "artists.html",
    "/albums.html": "albums.html",
    "/vinyl.html": "vinyl.html",
    "/songs.html": "songs.html",
    "/inbox.html": "inbox.html",
}
# Pages folded into a tab of another page -- old bookmarks keep working.
REDIRECTS = {
    "/duplicates.html": "/artists.html#duplicates",
    "/album-duplicates.html": "/albums.html#duplicates",
}
STATIC = {"/shared.js": ("shared.js", "application/javascript; charset=utf-8"),
          "/album-ui.js": ("album-ui.js", "application/javascript; charset=utf-8"),
          "/artist-ui.js": ("artist-ui.js", "application/javascript; charset=utf-8"),
          "/editions-ui.js": ("editions-ui.js", "application/javascript; charset=utf-8"),
          "/recordings-ui.js": ("recordings-ui.js", "application/javascript; charset=utf-8"),
          "/shared.css": ("shared.css", "text/css; charset=utf-8")}
FONT_TYPES = {".woff2": "font/woff2", ".ttf": "font/ttf"}
BACKUP_DIR = DB_PATH.parent / ".refresh_backups"

# job_id -> {"lines": [str] (append-only), "done", "returncode", "kind": "refresh"|"build"|"sweep:*",
#            "backup": Path|None (refresh only -- the pre-run copy a rejection restores),
#            "decision": "accepted"|"rejected"|None, + "progress"/"cancel" for sweeps}.
# Lines are append-only and the client tracks its own offset, so a missed poll never drops output.
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
_general.JOBS, _general.JOBS_LOCK = jobs, jobs_lock


def run_job(job_id: str, cmd_args: list[str]) -> None:
    proc = subprocess.Popen([sys.executable, *cmd_args], cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
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
        self.send_header("Cache-Control", "no-store")
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

    def _dispatch(self, method: str, path: str, query: dict, body: dict) -> bool:
        entry = ROUTES.get((method, path))
        if not entry:
            return False
        fn, mutating = entry
        try:
            if mutating:
                merge.ensure_snapshot()
            result = fn(Req(query, body))
            payload, status = result if isinstance(result, tuple) else (result, 200)
            self._send_json(payload, status)
        except ApiError as exc:
            self._send_json({"error": exc.code or str(exc), "message": str(exc), **exc.extra}, exc.status)
        except merge.MergeError as exc:
            self._send_json(*merge_error_response(exc))
        except Exception as exc:
            traceback.print_exc()
            self._send_json({"error": str(exc), "message": f"Server error: {exc}"}, 500)
        return True

    def do_GET(self):
        if not self._require_auth():
            return
        parts = urlsplit(self.path)
        path = parts.path
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}

        if path in PAGES:
            return self._send_file(PAGE_DIR / PAGES[path], "text/html; charset=utf-8")
        if path in REDIRECTS:
            self.send_response(301)
            self.send_header("Location", REDIRECTS[path])
            self.send_header("Content-Length", "0")
            return self.end_headers()
        if path in STATIC:
            name, ctype = STATIC[path]
            return self._send_file(PAGE_DIR / name, ctype)
        if path.startswith("/fonts/") and "/" not in path[len("/fonts/"):]:
            fp = PAGE_DIR / "fonts" / path[len("/fonts/"):]
            if fp.is_file() and fp.suffix in FONT_TYPES:
                return self._send_file(fp, FONT_TYPES[fp.suffix])
            return self.send_error(404)
        if path.startswith("/covers/") and "/" not in path[len("/covers/"):]:
            # Preview of a locally-cached cover -- the private working copy (data/covers/), not
            # the published one, so a just-fetched image shows before any publish has run.
            fp = covers.COVERS_DIR / path[len("/covers/"):]
            if fp.is_file() and fp.suffix == ".jpg":
                return self._send_file(fp, "image/jpeg")
            return self.send_error(404)
        if path == "/imports":
            imports_dir = ROOT / "imports"
            return self._send_json({"files": sorted(p.name for p in imports_dir.glob("*.csv")) if imports_dir.exists() else []})
        if path.startswith("/status/"):
            job_id = path[len("/status/"):]
            since = int(query.get("since", "0") or 0)
            with jobs_lock:
                job = jobs.get(job_id)
                if not job:
                    return self._send_json({"error": "unknown job"}, status=404)
                payload = {"lines": job["lines"][since:], "total": len(job["lines"]), "done": job["done"],
                           "returncode": job["returncode"], "kind": job["kind"], "decision": job["decision"],
                           "progress": job.get("progress")}
            return self._send_json(payload)
        if not self._dispatch("GET", path, query, {}):
            self.send_error(404)

    def do_POST(self):
        if not self._require_auth():
            return
        path = urlsplit(self.path).path
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            return self._send_json({"error": "body must be JSON"}, 400)
        if path == "/run":
            return self._handle_run(body)
        if path == "/build":
            return self._handle_build()
        if path.startswith("/decision/"):
            return self._handle_decision(path[len("/decision/"):], body)
        if not self._dispatch("POST", path, {}, body):
            self.send_error(404)

    def _handle_run(self, body):
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
        if not DB_PATH.is_file():
            return self._send_json({"error": f"{DB_PATH} doesn't exist -- run the ETL scripts first."}, status=400)

        # Snapshot before touching it, so a rejection has something concrete to restore.
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        job_id = uuid.uuid4().hex
        backup_path = BACKUP_DIR / f"{job_id}.sqlite"
        shutil.copy2(DB_PATH, backup_path)
        with jobs_lock:
            jobs[job_id] = {"lines": [], "done": False, "returncode": None, "kind": "refresh", "backup": backup_path, "decision": None}
        threading.Thread(target=run_job, args=(job_id, ["etl/refresh.py", *args]), daemon=True).start()
        self._send_json({"jobId": job_id})

    def _handle_build(self):
        if DB_OVERRIDDEN:
            return self._send_json({"error": "Publishing is disabled on a test instance (MUSIC_DB_PATH is overridden)."}, status=409)
        job_id = uuid.uuid4().hex
        with jobs_lock:
            jobs[job_id] = {"lines": [], "done": False, "returncode": None, "kind": "build", "backup": None, "decision": None}
        threading.Thread(target=run_job, args=(job_id, ["etl/build_public_db.py"]), daemon=True).start()
        self._send_json({"jobId": job_id})

    def _handle_decision(self, job_id: str, body):
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
            shutil.copy2(backup_path, DB_PATH)
        # Either way the backup has done its job.
        if backup_path and backup_path.is_file():
            backup_path.unlink()
        with jobs_lock:
            jobs[job_id]["decision"] = "accepted" if action == "accept" else "rejected"
        self._send_json({"decision": jobs[job_id]["decision"]})


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
    conn = db_connect()
    try:
        applied = migrate(conn)
    finally:
        conn.close()
    if applied:
        print(f"Applied migrations: {', '.join(applied)}")
    if DB_OVERRIDDEN:
        print(f"TEST INSTANCE -- database {DB_PATH}, publishing disabled.")

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
