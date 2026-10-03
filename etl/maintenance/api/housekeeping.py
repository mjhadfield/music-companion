"""Housekeeping: the safety copies the tools leave behind (session / batch snapshots, per-merge
backups, refresh backups) listed with their sizes, deleted only when ticked. Opt-in and narrow:

  - only these three folders, only *.sqlite files directly inside them -- never the live database,
    never anything else (your own .tar.gz archives aren't listed);
  - always kept: the two newest automatic snapshots, the newest pre-batch and manual ones, every refresh backup
    from the last day, and any refresh backup a pending Accept / Reject still needs.
"""
import time

from api import general
from api.core import ApiError, route
from common import DB_PATH

FOLDERS = {
    ".snapshots": "Snapshots — automatic ones before a session's first change, and the ones taken before big batches or by hand",
    ".merge_backups": "Per-merge copies from before the undo journal existed",
    ".refresh_backups": "Copies taken before an import refresh (normally removed once you accept or reject it)",
}
DAY = 24 * 3600


def _files() -> list[dict]:
    out = []
    pending = set()
    with general.JOBS_LOCK or _NoLock():
        for job in general.JOBS.values():
            if job.get("kind") == "refresh" and job.get("decision") is None and job.get("backup"):
                pending.add(str(job["backup"]))
    now = time.time()
    for folder, what in FOLDERS.items():
        d = DB_PATH.parent / folder
        if not d.is_dir():
            continue
        files = sorted((p for p in d.glob("*.sqlite") if p.is_file() and not p.is_symlink()), key=lambda p: p.stat().st_mtime, reverse=True)
        autos = [p for p in files if p.name.startswith("auto-")]
        batches = [p for p in files if p.name.startswith("batch-")]          # merge.labelled_snapshot, before a big batch
        manual = [p for p in files if folder == ".snapshots" and not p.name.startswith(("auto-", "batch-"))]
        for p in files:
            st = p.stat()
            keep = None
            if folder == ".snapshots" and p in autos[:2]:
                keep = "one of the two newest automatic snapshots"
            elif folder == ".snapshots" and batches and p == batches[0]:
                keep = "the newest snapshot from before a big batch"
            elif folder == ".snapshots" and manual and p == manual[0]:
                keep = "the newest manual snapshot"
            elif folder == ".refresh_backups" and str(p) in pending:
                keep = "a refresh is waiting for Accept / Reject"
            elif folder == ".refresh_backups" and now - st.st_mtime < DAY:
                keep = "less than a day old"
            out.append({"path": f"{folder}/{p.name}", "folder": folder, "name": p.name, "bytes": st.st_size,
                        "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)), "keep": keep})
    return out


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@route("GET", "/api/housekeeping")
def listing(req):
    files = _files()
    return {"folders": [{"folder": f, "description": d, "files": [x for x in files if x["folder"] == f],
                         "bytes": sum(x["bytes"] for x in files if x["folder"] == f)} for f, d in FOLDERS.items()],
            "bytes": sum(x["bytes"] for x in files)}


@route("POST", "/api/housekeeping/delete")
def delete(req):
    """Deletes exactly the ticked files -- each must be in the current listing and not kept."""
    paths = req.body.get("paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        raise ApiError("paths must be a non-empty list")
    files = {f["path"]: f for f in _files()}
    for p in paths:
        f = files.get(p)
        if not f:
            raise ApiError(f"{p} isn't one of the listed files (nothing was deleted)", 409)
        if f["keep"]:
            raise ApiError(f"{p} is kept: {f['keep']} (nothing was deleted)", 409)
    freed = 0
    for p in paths:
        f = files[p]
        target = DB_PATH.parent / f["folder"] / f["name"]
        if target.resolve().parent != (DB_PATH.parent / f["folder"]).resolve() or target.resolve() == DB_PATH.resolve():
            raise ApiError(f"{p}: refusing a path outside its folder", 409)
        freed += f["bytes"]
        target.unlink()
    return {"deleted": len(paths), "bytes": freed}
