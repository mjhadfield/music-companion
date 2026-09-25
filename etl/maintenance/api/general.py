"""Cross-cutting endpoints: landing-page counts, the activity feed + undo, suggestion
accept/reject, background sweeps, review marks and publish status."""
import json
import uuid

import merge
import suggest
from api.core import ApiError, read_conn, route, write_tx
from common import DB_OVERRIDDEN, DB_PATH, ROOT

PUBLIC_DB = ROOT / "site" / "public" / "music.sqlite"

# Set by server.py at startup -- the same jobs dict /status/<id> reads from.
JOBS: dict = {}
JOBS_LOCK = None


@route("GET", "/api/overview")
def overview(req):
    with read_conn() as c:
        one = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "artists": {
                "missingMbid": one("SELECT count(DISTINCT artist_id) FROM scrobbles sc JOIN artists ar ON ar.id = sc.artist_id WHERE ar.mbid IS NULL"),
                "pendingSuggestions": one("SELECT count(DISTINCT s.entity_id) FROM suggestions s JOIN artists ar ON ar.id = s.entity_id "
                                         "WHERE s.entity_type = 'artist' AND s.status = 'pending' AND ar.mbid IS NULL"),
                "total": one("SELECT count(*) FROM artists"),
            },
            "vinyl": {
                "total": one("SELECT count(*) FROM vinyl_holdings"),
                "missingAlbumMbid": one("SELECT count(*) FROM vinyl_holdings vh JOIN albums al ON al.id = vh.album_id WHERE al.mbid IS NULL AND NOT EXISTS (SELECT 1 FROM album_parts ap WHERE ap.album_id = al.id)"),
                "missingCover": one("SELECT count(*) FROM vinyl_holdings vh JOIN albums al ON al.id = vh.album_id WHERE coalesce(al.cover_status, '') != 'ok'"),
            },
            "albums": {
                "total": one("SELECT count(*) FROM albums"),
                "missingMbid": one("SELECT count(*) FROM albums al WHERE al.mbid IS NULL AND NOT EXISTS (SELECT 1 FROM album_parts ap WHERE ap.album_id = al.id)"),
                "merged": one("SELECT count(*) FROM merge_log WHERE entity_type = 'album' AND undone_at IS NULL"),
            },
            "imports": {
                "pending": one("SELECT count(*) FROM import_events WHERE reviewed_at IS NULL"),
                "lastRun": one("SELECT max(started_at) FROM import_runs"),
            },
            "songs": {
                "total": one("SELECT count(*) FROM songs"),
                # live songs with no album and no "no-album" mark -- an upper bound on the Live to-do list
                "liveTodo": one("SELECT count(DISTINCT s.id) FROM setlist_songs ss JOIN songs s ON s.id = ss.song_id WHERE s.album_id IS NULL "
                                "AND NOT EXISTS (SELECT 1 FROM review_marks m WHERE m.entity_type = 'song' AND m.entity_id = s.id AND m.mark LIKE 'no-album:%')"),
                "setlistOnly": one("SELECT count(*) FROM songs s WHERE EXISTS (SELECT 1 FROM setlist_songs x WHERE x.song_id = s.id) "
                                   "AND NOT EXISTS (SELECT 1 FROM scrobbles c WHERE c.song_id = s.id)"),
            },
        }


@route("GET", "/api/history")
def history(req):
    limit = min(req.int("limit", 40), 200)
    entity_type = req.str("entityType") or None
    where = "WHERE entity_type = ?" if entity_type else ""
    args = (entity_type,) if entity_type else ()
    with read_conn() as c:
        merges = c.execute(
            f"SELECT id, entity_type, absorbed_name, canonical_name, absorbed_mbid, rows_moved_json, merged_at, undone_at, undo_json IS NOT NULL "
            f"FROM merge_log {where} ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        edits = c.execute(
            f"SELECT id, entity_type, entity_id, entity_name, changes_json, reason, created_at, undone_at FROM edit_log {where} ORDER BY id DESC LIMIT ?",
            (*args, limit)).fetchall()
    items = [{
        "kind": "merge", "id": r[0], "entityType": r[1], "absorbedName": r[2], "canonicalName": r[3], "absorbedMbid": r[4],
        "rowsMoved": json.loads(r[5]), "at": r[6], "undoneAt": r[7], "undoable": bool(r[8]) and not r[7],
    } for r in merges] + [{
        "kind": "edit", "id": r[0], "entityType": r[1], "entityId": r[2], "name": r[3], "changes": json.loads(r[4]),
        "reason": r[5], "at": r[6], "undoneAt": r[7], "undoable": not r[7],
    } for r in edits]
    items.sort(key=lambda i: i["at"], reverse=True)
    return {"items": items[:limit]}


@route("POST", "/api/history/undo", mutating=True)
def undo(req):
    kind = req.str("kind")
    ids = req.body.get("ids")
    if kind == "merge" and isinstance(ids, list) and ids:
        # A group merge (several albums into one) undone as one action: newest first, since
        # each later merge may depend on the earlier ones' state -- all or nothing.
        results = []
        with write_tx() as c:
            for log_id in sorted((int(i) for i in ids), reverse=True):
                r = merge.undo_merge(c, log_id)
                c.execute("UPDATE suggestions SET status = 'pending', decided_at = NULL WHERE entity_type = ? AND entity_id = ? AND status != 'pending'",
                          (r["entityType"], r["restoredId"]))
                results.append(r)
        return {"undone": results, "restoredName": ", ".join(r["restoredName"] for r in results)}
    if kind == "edit" and isinstance(ids, list) and ids:
        # A reviewed bulk action (e.g. "link these 40 pressings") undone as one, newest first.
        results = []
        with write_tx() as c:
            for edit_id in sorted((int(i) for i in ids), reverse=True):
                r = merge.undo_edit(c, edit_id)
                reason = c.execute("SELECT reason FROM edit_log WHERE id = ?", (edit_id,)).fetchone()[0] or ""
                if reason.startswith("suggestion:"):
                    c.execute("UPDATE suggestions SET status = 'pending', decided_at = NULL WHERE id = ?", (int(reason.split(":")[1]),))
                results.append(r)
        return {"undone": results, "name": plural_names([r["name"] for r in results])}
    if kind == "batch":
        # A mixed reviewed batch (merges + edits + marks, e.g. a whole artist's live songs),
        # undone together, newest first.
        items = req.body.get("items") or []
        with write_tx() as c:
            for it in reversed(items):
                k = it.get("kind")
                i = None if isinstance(it.get("id"), list) else int(it.get("id"))
                if k == "merge":
                    r = merge.undo_merge(c, i)
                    c.execute("UPDATE suggestions SET status = 'pending', decided_at = NULL WHERE entity_type = ? AND entity_id = ? AND status != 'pending'",
                              (r["entityType"], r["restoredId"]))
                elif k == "edit":
                    merge.undo_edit(c, i)
                elif k == "mark":
                    c.execute("DELETE FROM review_marks WHERE id = ?", (i,))
                elif k == "inbox":
                    from api.imports import unreview
                    unreview(c, it["id"] if isinstance(it["id"], list) else [it["id"]])
                else:
                    raise ApiError(f"unknown batch item kind {k!r}")
        return {"undone": len(items), "name": f"{len(items)} change{'' if len(items) == 1 else 's'}"}
    if kind == "remerge":  # undo "un-merge these live versions": fold them back in
        pairs = req.body.get("ids") or []  # [{absorbedId, canonicalId}]
        with write_tx() as c:
            for p in pairs:
                merge.merge_songs(c, int(p["absorbedId"]), int(p["canonicalId"]))
        return {"undone": len(pairs), "name": f"{len(pairs)} song{'' if len(pairs) == 1 else 's'}"}
    if kind == "mbalias":  # undo "also releases as"
        from api.artists import remove_mb_alias
        with write_tx() as c:
            name = remove_mb_alias(c, req.int("id", required=True))
        return {"undone": 1, "name": name or "alias"}
    if kind == "inbox":  # items marked reviewed in the Import inbox go back to pending
        from api.imports import unreview
        with write_tx() as c:
            n = unreview(c, [int(i) for i in (ids or [req.int("id", required=True)])])
        return {"undone": n, "name": f"{n} inbox item{'' if n == 1 else 's'}"}
    if kind == "mark":
        with write_tx() as c:
            row = c.execute("SELECT entity_type, entity_id, mark FROM review_marks WHERE id = ?", (req.int("id", required=True),)).fetchone()
            c.execute("DELETE FROM review_marks WHERE id = ?", (req.int("id"),))
        return {"undone": 1, "name": row[2] if row else "mark"}
    item_id = req.int("id", required=True)
    with write_tx() as c:
        if kind == "merge":
            result = merge.undo_merge(c, item_id)
            # The restored row's decided suggestions go back to pending -- it's live again.
            c.execute("UPDATE suggestions SET status = 'pending', decided_at = NULL WHERE entity_type = ? AND entity_id = ? AND status != 'pending'",
                      (result["entityType"], result["restoredId"]))
        elif kind == "edit":
            result = merge.undo_edit(c, item_id)
            reason = c.execute("SELECT reason FROM edit_log WHERE id = ?", (item_id,)).fetchone()[0] or ""
            if reason.startswith("suggestion:"):
                c.execute("UPDATE suggestions SET status = 'pending', decided_at = NULL WHERE entity_type = ? AND entity_id = ?",
                          (result["entityType"], result["entityId"]))
                # an accepted suggestion's other candidates were auto-rejected -- bring them back too
                c.execute("UPDATE suggestions SET status = 'pending', decided_at = NULL WHERE id = ?", (int(reason.split(":")[1]),))
        else:
            raise ApiError("kind must be 'merge' or 'edit'")
    return result


def plural_names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{len(names)} items"


# -- Suggestions -----------------------------------------------------------------------------

@route("POST", "/api/suggestions/decide", mutating=True)
def decide_suggestion(req):
    sid, action = req.int("id", required=True), req.str("action")
    if action not in ("accept", "reject"):
        raise ApiError("action must be 'accept' or 'reject'")
    with write_tx() as c:
        row = c.execute("SELECT entity_type, entity_id, mbid, status FROM suggestions WHERE id = ?", (sid,)).fetchone()
        if not row:
            raise ApiError("suggestion not found", 404)
        entity_type, entity_id, mbid, status = row
        if status != "pending":
            raise ApiError(f"already {status}", 409)
        if entity_type == "vinyl" and action == "accept":
            raise ApiError("Vinyl pressing links are accepted via /api/vinyl/link-release.")
        if action == "reject":
            c.execute("UPDATE suggestions SET status = 'rejected', decided_at = datetime('now') WHERE id = ?", (sid,))
            return {"id": sid, "status": "rejected"}
        try:
            edit = merge.edit_entity(c, entity_type, entity_id, {"mbid": mbid}, f"suggestion:{sid}")
        except merge.MergeError as exc:
            if exc.code == "mbid_conflict":
                raise ApiError(str(exc), 409, "mbid_conflict", conflict=exc.conflict, entityId=entity_id)
            raise
        c.execute("UPDATE suggestions SET status = 'accepted', decided_at = datetime('now') WHERE id = ?", (sid,))
        c.execute("UPDATE suggestions SET status = 'rejected', decided_at = datetime('now') "
                  "WHERE entity_type = ? AND entity_id = ? AND status = 'pending'", (entity_type, entity_id))
    return {"id": sid, "status": "accepted", "editId": edit["editId"]}


# -- Sweeps ----------------------------------------------------------------------------------

@route("POST", "/api/sweeps/start")
def start_sweep(req):
    kind = req.str("kind")
    # edition lookups are one request each and cache for good -- a bigger batch is fine there
    limit = max(1, min(req.int("limit", 50), 1000 if kind in ("album-editions", "song-recordings", "album-tracklists") else 300 if kind == "pressings" else 200))
    job_id = uuid.uuid4().hex
    params = {}
    if req.body.get("artistId"):
        params["artist_id"] = int(req.body["artistId"])
    err = suggest.start(kind, limit, JOBS, JOBS_LOCK, job_id, params)
    if err:
        raise ApiError(err, 409)
    return {"jobId": job_id}


@route("POST", "/api/sweeps/cancel")
def cancel_sweep(req):
    job_id = req.str("jobId")
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job or "cancel" not in job:
            raise ApiError("unknown sweep", 404)
        job["cancel"] = True
    return {"cancelled": True}


# -- Review marks ----------------------------------------------------------------------------

@route("POST", "/api/review/mark", mutating=True)
def review_mark(req):
    entity_type, entity_id, mark = req.str("entityType"), req.int("entityId", required=True), req.str("mark")
    if entity_type not in ("artist", "album", "song") or not mark:
        raise ApiError("entityType (artist|album|song) and mark are required")
    with write_tx() as c:
        if req.body.get("remove"):
            c.execute("DELETE FROM review_marks WHERE entity_type = ? AND entity_id = ? AND mark = ?", (entity_type, entity_id, mark))
        else:
            c.execute("INSERT OR IGNORE INTO review_marks (entity_type, entity_id, mark) VALUES (?, ?, ?)", (entity_type, entity_id, mark))
    return {"ok": True}


# -- Publish ---------------------------------------------------------------------------------

@route("GET", "/api/publish-status")
def publish_status(req):
    working = DB_PATH.stat().st_mtime
    public = PUBLIC_DB.stat().st_mtime if PUBLIC_DB.exists() else 0
    return {"unpublished": working > public + 1, "disabled": DB_OVERRIDDEN,
            "reason": "test instance (MUSIC_DB_PATH override) -- publishing disabled" if DB_OVERRIDDEN else None}
