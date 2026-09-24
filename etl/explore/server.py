"""
Read-only SQL browser for the Music Companion database -- view and query
data/music.sqlite from a web UI, with schema-aware autocomplete for table
and column names (including aliases: "FROM artists a" then typing "a."
suggests artists' own columns).

Read-only in two independent layers, so a bug in one doesn't matter:
  1. The database is opened with SQLite's own `mode=ro` URI flag -- the
     engine itself refuses any write, whatever SQL is submitted.
  2. A submitted query is rejected up front unless it starts with SELECT
     or WITH (a CTE). This also catches a second stacked statement
     ("SELECT 1; DROP TABLE x") before it ever reaches the database,
     even though layer 1 would have refused the DROP anyway.

Every query also runs under a row cap (wrapped as an outer
`SELECT * FROM (<query>) LIMIT n`, so it applies even if the query has its
own ORDER BY/GROUP BY/UNION) and a wall-clock timeout (via sqlite3's
progress handler, since sqlite3 has no per-call timeout of its own) --
this is a browsing tool, not a place to run a query that scans the whole
98k-row scrobbles table in a cross join.

Binds to all interfaces (matches the main site on :8642): unlike
etl/maintenance/server.py this never mutates data or calls out to a paid
API, so there's no reason to keep it to localhost.

Usage:
    python3 etl/explore/server.py
    open http://localhost:8644/
"""
import json
import re
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent.parent
PAGE_DIR = Path(__file__).resolve().parent
DATA_DB = ROOT / "data" / "music.sqlite"
PORT = 8644

QUERY_TIMEOUT_S = 10          # a query still running after this long is aborted
DEFAULT_LIMIT = 500
MAX_LIMIT = 500_000            # the biggest table here is ~100k rows, so this is really "no cap" — the real
                                # limits are QUERY_TIMEOUT_S above (server) and the browser's own table-rendering
                                # cost (client, see app.js's renderResults) for whoever picks the 500,000 option

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}
FONT_TYPES = {".woff2": "font/woff2", ".ttf": "font/ttf"}


# ---- read-only query engine -----------------------------------------------------------------------------------

ALLOWED_START = re.compile(r"^\s*(?:SELECT|WITH)\b", re.IGNORECASE)
STRING_LITERAL = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")  # so a ';' *inside* a quoted value isn't mistaken for a second statement


def validate(sql: str) -> str | None:
    """None if `sql` looks safe to run; otherwise the reason it was refused."""
    stripped = sql.strip()
    if not stripped:
        return "Enter a query."
    if not ALLOWED_START.match(stripped):
        return "Only SELECT (or WITH ... SELECT) queries are allowed here -- this is a read-only tool."
    body = stripped[:-1] if stripped.endswith(";") else stripped
    if ";" in STRING_LITERAL.sub("''", body):
        return "One query at a time, please (no semicolons inside the query)."
    return None


def jsonable(value):
    """A cell value sqlite3 might hand back that json.dumps can't take as-is."""
    return f"<{len(value)} bytes>" if isinstance(value, (bytes, bytearray)) else value


def run_query(sql: str, limit: int) -> dict:
    con = sqlite3.connect(f"file:{DATA_DB}?mode=ro", uri=True)
    con.row_factory = None
    deadline = time.monotonic() + QUERY_TIMEOUT_S
    con.set_progress_handler(lambda: time.monotonic() > deadline, 2000)
    try:
        started = time.monotonic()
        # Wrapped so the cap applies to the *final* result regardless of what the query itself does
        # (its own ORDER BY / GROUP BY / UNION all run first, exactly as written, inside the subquery).
        cur = con.execute(f"SELECT * FROM (\n{sql}\n) LIMIT ?", (limit + 1,))
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description] if cur.description else []
        truncated = len(rows) > limit
        rows = rows[:limit]
        return {
            "columns": columns,
            "rows": [[jsonable(v) for v in r] for r in rows],
            "truncated": truncated,
            "row_count": len(rows),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }
    except sqlite3.OperationalError as exc:
        if time.monotonic() > deadline:
            raise TimeoutError(f"query took longer than {QUERY_TIMEOUT_S}s and was stopped -- try narrowing it (a WHERE clause, a smaller table, an explicit LIMIT)") from exc
        raise
    finally:
        con.close()


def qi(name: str) -> str:
    """Quote a SQL identifier. Only ever called with names sqlite itself just gave us (from sqlite_master /
    PRAGMA table_info), never with anything from a request, but quoted properly regardless."""
    return '"' + name.replace('"', '""') + '"'


def get_schema() -> dict:
    con = sqlite3.connect(f"file:{DATA_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        tables = [r["name"] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        out = {}
        for t in tables:
            columns = [{"name": r["name"], "type": r["type"] or "", "pk": bool(r["pk"]), "notnull": bool(r["notnull"])}
                       for r in con.execute(f"PRAGMA table_info({qi(t)})")]
            foreign_keys = [{"from": r["from"], "table": r["table"], "to": r["to"]}
                            for r in con.execute(f"PRAGMA foreign_key_list({qi(t)})")]
            row_count = con.execute(f"SELECT COUNT(*) FROM {qi(t)}").fetchone()[0]
            out[t] = {"columns": columns, "foreign_keys": foreign_keys, "row_count": row_count}
        return out
    finally:
        con.close()


# ---- HTTP -------------------------------------------------------------------------------------------------------

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
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        path = urlsplit(self.path).path

        if path in STATIC:
            name, ctype = STATIC[path]
            return self._send_file(PAGE_DIR / name, ctype)

        if path.startswith("/fonts/") and "/" not in path[len("/fonts/"):]:
            fp = PAGE_DIR / "fonts" / path[len("/fonts/"):]
            if fp.is_file() and fp.suffix in FONT_TYPES:
                return self._send_file(fp, FONT_TYPES[fp.suffix])
            return self.send_error(404)

        if path == "/api/schema":
            if not DATA_DB.is_file():
                return self._send_json({"error": f"{DATA_DB} not found -- run the ETL scripts first."}, status=404)
            try:
                return self._send_json({"tables": get_schema(), "db_bytes": DATA_DB.stat().st_size})
            except sqlite3.Error as exc:
                return self._send_json({"error": str(exc)}, status=500)

        self.send_error(404)

    def do_POST(self):
        if urlsplit(self.path).path != "/api/query":
            return self.send_error(404)

        body = self._read_json_body()
        sql = str(body.get("sql") or "")
        try:
            limit = max(1, min(MAX_LIMIT, int(body.get("limit", DEFAULT_LIMIT))))
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT

        if (error := validate(sql)) is not None:
            return self._send_json({"error": error}, status=400)
        if not DATA_DB.is_file():
            return self._send_json({"error": f"{DATA_DB} not found -- run the ETL scripts first."}, status=404)

        query_body = sql.strip()
        query_body = query_body[:-1] if query_body.endswith(";") else query_body
        try:
            self._send_json(run_query(query_body, limit))
        except TimeoutError as exc:
            self._send_json({"error": str(exc)}, status=504)
        except sqlite3.Error as exc:
            self._send_json({"error": str(exc)}, status=400)


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Music DB explorer (read-only): http://localhost:{PORT}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
