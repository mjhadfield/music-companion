"""
Route table + tiny helpers shared by every api/*.py module.

A handler is a plain function `fn(req) -> payload | (payload, status)`, registered with
@route(method, path). `req.query` is the query string flattened to first values, `req.body`
the parsed JSON body (POST). Raising ApiError (or merge.MergeError) turns into a JSON error
response -- handlers never write to the socket themselves.

`mutating=True` routes get a session snapshot taken before they run (merge.ensure_snapshot).
"""
from contextlib import contextmanager

from common import connect as db_connect
from merge import MergeError

ROUTES: dict[tuple[str, str], tuple] = {}


def route(method: str, path: str, mutating: bool = False):
    def deco(fn):
        ROUTES[(method, path)] = (fn, mutating)
        return fn
    return deco


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, code: str | None = None, **extra):
        super().__init__(message)
        self.status, self.code, self.extra = status, code, extra


class Req:
    def __init__(self, query: dict, body: dict):
        self.query, self.body = query, body

    def int(self, name: str, default=None, required: bool = False) -> int | None:
        raw = self.query.get(name, self.body.get(name) if isinstance(self.body, dict) else None)
        if raw in (None, ""):
            if required:
                raise ApiError(f"{name} is required")
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise ApiError(f"{name} must be an integer")

    def str(self, name: str, default: str = "") -> str:
        raw = self.query.get(name, self.body.get(name) if isinstance(self.body, dict) else None)
        return default if raw is None else str(raw).strip()


_MERGE_STATUS = {"not_found": 404, "merge_error": 400}


def merge_error_response(exc: MergeError) -> tuple[dict, int]:
    payload = {"error": exc.code, "message": str(exc)}
    if getattr(exc, "conflict", None):
        payload["conflict"] = exc.conflict
    return payload, _MERGE_STATUS.get(exc.code, 409)


@contextmanager
def read_conn():
    conn = db_connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_tx():
    """One IMMEDIATE transaction: commits on success, rolls back on any exception."""
    conn = db_connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def rows_to_dicts(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]
