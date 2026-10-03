"""Reviewed batches: the one way the workbench and the review queues change data.

A batch is a list of actions a human ticked. Every action goes through merge.py (journal, import
aliases, edit_log) -- this module adds no write logic of its own beyond review metadata.

  POST /api/batch/preview  runs the exact actions in a transaction and ROLLS BACK: what each would
                           move, rename or refuse, plus the integrity verdict. Nothing is kept.
  POST /api/batch/apply    the same, committed as ONE transaction -- all or nothing. Before it
                           commits, the integrity checks run again: if any hard count went up
                           (something now points at nothing) the whole batch is rolled back.
  POST /api/batch/undo     the whole batch, newest action first, again all or nothing.
  GET  /api/batches        recent batches for the activity feed.

Action types (all ids are ints):
  mergeAlbums      {canonicalId, absorbedIds[], identity?: {title, year, mbid}}
  mergeSongs       {canonicalId, absorbedIds[], title?}
  editAlbum        {albumId, title?, year?}
  renameSong       {songId, title}
  setSongAlbum     {songId, albumId}
  assignAlbumMbid  {albumId, mbid (a release-group id), title?, year?, suggestionId?}
  rejectSuggestion {suggestionId}
  splitSong        {songId, rawTitles[], title, mergeLogId?}
  dismiss          {entityType: album|song|artist, ids[]}      "these aren't the same"
  mark             {entityType: album|song|artist, entityId, mark}
  removeAlias      {aliasId}            only an alias that points at a row that no longer exists
  undoMerge        {logId}              reverse one journaled merge (undo of the batch re-merges it)
  moveAlbumToArtist {albumId, toArtistId | newArtist: {name, mbid?}, keepCredit?}   an album an old merge put under
                                        the wrong (same-named) artist, with its songs and plays
  versionTracklist {albumId, label, releaseMbid?, tracks: [{number, title, recordingMbid?, lengthMs?}]}
                                        another-language tracklist for an album whose reference is your
                                        pressing (Carolus Rex: Swedish LP + the English version)
  linkArtists      {artistId, linkedArtistId}   a solo act and its band (Live sets can use the band's recordings)
  unlinkArtists    {artistId, linkedArtistId}
  albumGenres      {albumId, keep: [{name, mbid?, source, votes?}], drop: [genreId], reject: [name]}
                                        one album's reviewed genres (Maintenance > Genres); marks it reviewed
  foldSingle       {singleId, albumId}  a single filed as an album (singles.py): its own copies of
                                        songs merge into their twins on the album, then it merges in
"""
import json
import re

import integrity
import merge
import singles
from api.core import ApiError, read_conn, route
from common import connect as db_connect

MBID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
BIG_BATCH = 25          # an extra named snapshot is taken before applying more than this many actions
MAX_ACTIONS = 500


def _int(it: dict, key: str) -> int:
    try:
        return int(it[key])
    except (KeyError, TypeError, ValueError):
        raise ApiError(f"{it.get('type')}: {key} must be an integer")


def _ints(it: dict, key: str) -> list[int]:
    raw = it.get(key)
    if not isinstance(raw, list) or not raw:
        raise ApiError(f"{it.get('type')}: {key} must be a non-empty list")
    try:
        return [int(x) for x in raw]
    except (TypeError, ValueError):
        raise ApiError(f"{it.get('type')}: {key} must be integers")


def _restore_suggestions(c, entity_type: str, entity_id: int) -> None:
    """A row brought back by undo is live again -- its decided suggestions go back to pending."""
    c.execute("UPDATE suggestions SET status = 'pending', decided_at = NULL WHERE entity_type = ? AND entity_id = ? AND status != 'pending'",
              (entity_type, entity_id))


def run_action(c, it: dict) -> tuple[list[dict], dict]:
    """Executes one action inside the caller's transaction. -> (undo handles, summary for the preview)."""
    t = it.get("type")
    if t == "mergeAlbums":
        canonical, absorbed = _int(it, "canonicalId"), _ints(it, "absorbedIds")
        if canonical in absorbed:
            raise ApiError("mergeAlbums: the primary can't also be absorbed")
        handles, moved, names = [], {}, []
        for i, aid in enumerate(absorbed):
            r = merge.merge_albums(c, aid, canonical, (it.get("identity") or None) if i == len(absorbed) - 1 else None)
            handles.append({"kind": "merge", "id": r["logId"]})
            names.append(r["absorbedTitle"])
            for k, v in r["rowsMoved"].items():
                moved[k] = moved.get(k, 0) + v
        return handles, {"text": f'Merge {", ".join(names)} into "{r["canonicalTitle"]}"', "rowsMoved": moved}
    if t == "undoMerge":
        log_id = _int(it, "logId")
        row = c.execute("SELECT entity_type, canonical_id FROM merge_log WHERE id = ?", (log_id,)).fetchone()
        if not row:
            raise ApiError("undoMerge: merge not found", 404)
        # how the survivor looks now (a merge may have renamed it) -- re-merging restores exactly that
        identity = None
        if row[0] in ("album", "song"):
            cur = c.execute(f"SELECT title, mbid{', year' if row[0] == 'album' else ''} FROM {row[0]}s WHERE id = ?", (row[1],)).fetchone()
            identity = dict(zip(("title", "mbid", "year"), cur)) if cur else None
        r = merge.undo_merge(c, log_id)
        return ([{"kind": "remerge", "entityType": row[0], "absorbedId": r["restoredId"], "canonicalId": row[1], "identity": identity}],
                {"text": f'Undo merge of "{r["restoredName"]}" into "{r["canonicalName"]}"'})
    if t == "moveAlbumToArtist":
        album_id = _int(it, "albumId")
        handles = []
        if it.get("newArtist"):
            na = it["newArtist"]
            mbid = (na.get("mbid") or "").lower() or None
            if mbid and not MBID_RE.match(mbid):
                raise ApiError("moveAlbumToArtist: newArtist.mbid must be a MusicBrainz artist id")
            to, edit = merge.create_artist(c, na.get("name"), mbid, it.get("reason") or "split out a same-named artist")
            handles.append({"kind": "edit", "id": edit})
        else:
            to = _int(it, "toArtistId")
        r = merge.move_album_to_artist(c, album_id, to, it.get("reason") or "wrong artist", keep_credit=bool(it.get("keepCredit")))
        handles.append({"kind": "edit", "id": r["editId"]})
        return handles, {"text": "Move album to another artist", "rowsMoved": {"songs": r["songs"], "scrobbles": r["scrobbles"]}}
    if t == "versionTracklist":
        album_id, label = _int(it, "albumId"), (it.get("label") or "").strip()
        tracks = it.get("tracks") or []
        if not label or not tracks or not all(isinstance(x, dict) and (x.get("title") or "").strip() for x in tracks):
            raise ApiError("versionTracklist: a label and titled tracks are required")
        if not c.execute("SELECT 1 FROM albums WHERE id = ?", (album_id,)).fetchone():
            raise ApiError("versionTracklist: album not found", 404)
        if not c.execute("SELECT 1 FROM vinyl_holdings WHERE album_id = ?", (album_id,)).fetchone():
            raise ApiError("versionTracklist: only for an album you own on vinyl (its pressing is the main tracklist)")
        prev_rows = [list(r) for r in c.execute(
            "SELECT album_id, position, number, disc, title, recording_mbid, length_ms FROM album_tracklists WHERE album_id = ?", (album_id,))]
        prev_src = c.execute("SELECT album_id, source, release_mbid, release_title, release_date, country, format, fetched_at "
                             "FROM album_tracklist_sources WHERE album_id = ?", (album_id,)).fetchone()
        c.execute("DELETE FROM album_tracklists WHERE album_id = ?", (album_id,))
        c.executemany("INSERT INTO album_tracklists (album_id, position, number, disc, title, recording_mbid, length_ms) VALUES (?, ?, ?, NULL, ?, ?, ?)",
                      [(album_id, i + 1, str(x.get("number") or i + 1), x["title"].strip(), x.get("recordingMbid"), x.get("lengthMs"))
                       for i, x in enumerate(tracks)])
        # source 'version': the site shows it as a second tracklist beside your pressing's
        c.execute("INSERT INTO album_tracklist_sources (album_id, source, release_mbid, release_title) VALUES (?, 'version', ?, ?) "
                  "ON CONFLICT (album_id) DO UPDATE SET source = 'version', release_mbid = excluded.release_mbid, release_title = excluded.release_title, "
                  "release_date = NULL, country = NULL, format = NULL, fetched_at = datetime('now')", (album_id, it.get("releaseMbid"), label))
        return ([{"kind": "tracklist", "albumId": album_id, "rows": prev_rows, "source": list(prev_src) if prev_src else None}],
                {"text": f'Add the "{label}" tracklist ({len(tracks)} tracks)'})
    if t == "linkArtists":
        r = merge.link_artists(c, _int(it, "artistId"), _int(it, "linkedArtistId"))
        return [{"kind": "edit", "id": r["editId"]}], {"text": "Link artists"}
    if t == "unlinkArtists":
        r = merge.unlink_artists(c, _int(it, "artistId"), _int(it, "linkedArtistId"))
        return [{"kind": "edit", "id": r["editId"]}], {"text": "Unlink artists"}
    if t == "albumGenres":
        import genres as genre_tags
        album_id = _int(it, "albumId")
        try:
            edit = genre_tags.set_album(c, album_id, it.get("keep") or [], [int(x) for x in it.get("drop") or []],
                                        [str(x) for x in it.get("reject") or []])
        except ValueError as exc:
            raise ApiError(f"albumGenres: {exc}", 404)
        handles = [{"kind": "edit", "id": edit}] if edit else []
        cur = c.execute("INSERT OR IGNORE INTO review_marks (entity_type, entity_id, mark) VALUES ('album', ?, ?)", (album_id, genre_tags.REVIEWED_MARK))
        if cur.rowcount:
            handles.append({"kind": "mark", "id": cur.lastrowid})
        return handles, {"text": "Save genres"}
    if t == "foldSingle":
        single_id, album_id = _int(it, "singleId"), _int(it, "albumId")
        # re-checked against the data as it is now (an earlier action in this batch may have changed it)
        found = singles.find_singles(c, album_ids=[single_id])
        if not found or found[0]["target"]["albumId"] != album_id:
            raise ApiError("foldSingle: that no longer looks like a single from that album -- reload and check it again", 409)
        handles, moved = [], {}
        for tr in found[0]["tracks"]:
            for sid in tr["ownIds"]:                                   # only the single's own copies
                if sid == tr["twin"]["songId"]:
                    continue
                r = merge.merge_songs(c, sid, tr["twin"]["songId"])
                handles.append({"kind": "merge", "id": r["logId"]})
                for k, v in r["rowsMoved"].items():
                    moved[k] = moved.get(k, 0) + v
        r = merge.merge_albums(c, single_id, album_id)
        handles.append({"kind": "merge", "id": r["logId"]})
        for k, v in r["rowsMoved"].items():
            moved[k] = moved.get(k, 0) + v
        return handles, {"text": f'Fold the single "{r["absorbedTitle"]}" into "{r["canonicalTitle"]}"', "rowsMoved": moved}
    if t == "mergeSongs":
        canonical, absorbed = _int(it, "canonicalId"), _ints(it, "absorbedIds")
        if canonical in absorbed:
            raise ApiError("mergeSongs: the primary can't also be absorbed")
        title = (it.get("title") or "").strip() or None
        handles, moved, names = [], {}, []
        for i, sid in enumerate(absorbed):
            r = merge.merge_songs(c, sid, canonical, {"title": title} if title and i == len(absorbed) - 1 else None)
            handles.append({"kind": "merge", "id": r["logId"]})
            names.append(r["absorbedTitle"])
            for k, v in r["rowsMoved"].items():
                moved[k] = moved.get(k, 0) + v
        return handles, {"text": f'Merge {", ".join(names)} into "{r["canonicalTitle"]}"', "rowsMoved": moved}
    if t == "editAlbum":
        changes = {}
        if (it.get("title") or "").strip():
            changes["title"] = it["title"].strip()
        if it.get("year") not in (None, ""):
            changes["year"] = int(it["year"])
        if not changes:
            raise ApiError("editAlbum: nothing to change")
        e = merge.edit_entity(c, "album", _int(it, "albumId"), changes, it.get("reason") or "edited")
        return ([{"kind": "edit", "id": e["editId"]}] if e["editId"] else []), {"text": "Edit album", "changes": e["changes"]}
    if t == "renameSong":
        title = (it.get("title") or "").strip()
        if not title:
            raise ApiError("renameSong: title is required")
        e = merge.edit_entity(c, "song", _int(it, "songId"), {"title": title}, it.get("reason") or "song rename")
        return ([{"kind": "edit", "id": e["editId"]}] if e["editId"] else []), {"text": f'Rename to "{title}"', "changes": e["changes"]}
    if t == "setSongAlbum":
        song_id, album_id = _int(it, "songId"), _int(it, "albumId")
        song = c.execute("SELECT artist_id, title FROM songs WHERE id = ?", (song_id,)).fetchone()
        album = c.execute("SELECT title FROM albums WHERE id = ?", (album_id,)).fetchone()
        if not song or not album:
            raise ApiError("setSongAlbum: song or album not found", 404)
        credited = {r[0] for r in c.execute("SELECT artist_id FROM album_artists WHERE album_id = ?", (album_id,))}
        if song[0] not in credited:
            raise ApiError(f'setSongAlbum: "{album[0]}" doesn\'t credit this song\'s artist', 409)
        e = merge.edit_entity(c, "song", song_id, {"album_id": album_id}, it.get("reason") or "song album")
        return ([{"kind": "edit", "id": e["editId"]}] if e["editId"] else []), {"text": f'File "{song[1]}" under "{album[0]}"', "changes": e["changes"]}
    if t == "assignAlbumMbid":
        album_id, mbid = _int(it, "albumId"), str(it.get("mbid") or "").lower()
        if not MBID_RE.match(mbid):
            raise ApiError("assignAlbumMbid: mbid must be a MusicBrainz release-group id")
        current = c.execute("SELECT mbid FROM albums WHERE id = ?", (album_id,)).fetchone()
        if not current:
            raise ApiError("assignAlbumMbid: album not found", 404)
        changes = {"mbid": mbid}
        if (it.get("title") or "").strip():
            changes["title"] = it["title"].strip()
        if it.get("year") not in (None, ""):
            changes["year"] = int(it["year"])
        if current[0] != mbid:
            changes.update({"cover_status": None, "cover_updated_at": None})   # art fetched under the old identity
        sid = it.get("suggestionId")
        e = merge.edit_entity(c, "album", album_id, changes, f"suggestion:{int(sid)}" if sid else (it.get("reason") or "assigned"))
        handles = [{"kind": "edit", "id": e["editId"]}] if e["editId"] else []
        if sid:
            sid = int(sid)
            row = c.execute("SELECT status FROM suggestions WHERE id = ? AND entity_type = 'album' AND entity_id = ?", (sid, album_id)).fetchone()
            if not row:
                raise ApiError("assignAlbumMbid: that suggestion isn't for this album", 409)
            others = [r[0] for r in c.execute("SELECT id FROM suggestions WHERE entity_type = 'album' AND entity_id = ? AND status = 'pending' AND id != ?",
                                              (album_id, sid))]
            c.execute("UPDATE suggestions SET status = 'accepted', decided_at = datetime('now') WHERE id = ?", (sid,))
            c.execute(f"UPDATE suggestions SET status = 'rejected', decided_at = datetime('now') WHERE id IN ({','.join('?' * len(others)) or 'NULL'})", others)
            handles.append({"kind": "suggestion", "id": sid, "prev": row[0], "others": others})
        return handles, {"text": "Link album to MusicBrainz", "changes": e["changes"]}
    if t == "rejectSuggestion":
        sid = _int(it, "suggestionId")
        row = c.execute("SELECT status FROM suggestions WHERE id = ?", (sid,)).fetchone()
        if not row:
            raise ApiError("rejectSuggestion: suggestion not found", 404)
        if row[0] != "pending":
            raise ApiError(f"rejectSuggestion: already {row[0]}", 409)
        c.execute("UPDATE suggestions SET status = 'rejected', decided_at = datetime('now') WHERE id = ?", (sid,))
        return [{"kind": "suggestion", "id": sid, "prev": "pending", "others": []}], {"text": "Reject suggestion"}
    if t == "splitSong":
        raw = it.get("rawTitles")
        if not isinstance(raw, list) or not raw:
            raise ApiError("splitSong: rawTitles must be a non-empty list")
        r = merge.split_song(c, _int(it, "songId"), [str(x) for x in raw], str(it.get("title") or ""),
                             int(it["mergeLogId"]) if it.get("mergeLogId") else None)
        return [{"kind": "edit", "id": r["editId"]}], {"text": f'Split out "{it.get("title")}"', "rowsMoved": {"scrobbles": r["scrobbles"], "setlist_songs": r["shows"], "alias_overrides": r["aliases"]}}
    if t == "dismiss":
        et = it.get("entityType")
        if et not in ("album", "song", "artist"):
            raise ApiError("dismiss: entityType must be album, song or artist")
        ids = sorted(set(_ints(it, "ids")))
        if len(ids) < 2:
            raise ApiError("dismiss: needs at least two ids")
        inserted = []
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                cur = c.execute("INSERT INTO duplicate_dismissals (entity_type, entity_id_a, entity_id_b) VALUES (?, ?, ?) ON CONFLICT DO NOTHING", (et, a, b))
                if cur.rowcount:
                    inserted.append(cur.lastrowid)
        return [{"kind": "dismiss", "ids": inserted}], {"text": "Not the same"}
    if t == "mark":
        et, mark = it.get("entityType"), str(it.get("mark") or "").strip()
        if et not in ("album", "song", "artist") or not mark:
            raise ApiError("mark: entityType and mark are required")
        cur = c.execute("INSERT OR IGNORE INTO review_marks (entity_type, entity_id, mark) VALUES (?, ?, ?)", (et, _int(it, "entityId"), mark))
        return ([{"kind": "mark", "id": cur.lastrowid}] if cur.rowcount else []), {"text": f"Mark {mark}"}
    if t == "removeAlias":
        alias_id = _int(it, "aliasId")
        row = c.execute("SELECT id, source, source_key, canonical_type, canonical_id, note FROM alias_overrides WHERE id = ?", (alias_id,)).fetchone()
        if not row:
            raise ApiError("removeAlias: alias not found", 404)
        table = {"artist": "artists", "album": "albums", "song": "songs"}[row[3]]
        if c.execute(f"SELECT 1 FROM {table} WHERE id = ?", (row[4],)).fetchone():
            raise ApiError("removeAlias: that alias still points at a real row -- only dangling ones can be removed here", 409)
        c.execute("DELETE FROM alias_overrides WHERE id = ?", (alias_id,))
        return [{"kind": "alias", "row": list(row)}], {"text": f'Remove dangling alias "{row[2]}"'}
    raise ApiError(f"unknown action type {t!r}")


def undo_handle(c, h: dict) -> None:
    k = h.get("kind")
    # a merge or edit already undone on its own (Activity's per-item Undo) needs nothing more --
    # the rest of the batch can still be undone around it
    if k in ("merge", "edit"):
        done = c.execute(f"SELECT undone_at FROM {'merge_log' if k == 'merge' else 'edit_log'} WHERE id = ?", (int(h["id"]),)).fetchone()
        if done and done[0]:
            return
    if k == "merge":
        r = merge.undo_merge(c, int(h["id"]))
        _restore_suggestions(c, r["entityType"], r["restoredId"])
    elif k == "edit":
        merge.undo_edit(c, int(h["id"]))
    elif k == "tracklist":                                             # put back exactly what was there
        c.execute("DELETE FROM album_tracklists WHERE album_id = ?", (int(h["albumId"]),))
        c.execute("DELETE FROM album_tracklist_sources WHERE album_id = ?", (int(h["albumId"]),))
        c.executemany("INSERT INTO album_tracklists (album_id, position, number, disc, title, recording_mbid, length_ms) VALUES (?, ?, ?, ?, ?, ?, ?)", h["rows"])
        if h.get("source"):
            c.execute("INSERT INTO album_tracklist_sources (album_id, source, release_mbid, release_title, release_date, country, format, fetched_at) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", h["source"])
    elif k == "remerge":                                               # undoing an undoMerge: merge it again
        fn = {"artist": merge.merge_artists, "album": merge.merge_albums, "song": merge.merge_songs}[h["entityType"]]
        if h["entityType"] == "artist":
            fn(c, int(h["absorbedId"]), int(h["canonicalId"]))
        else:
            fn(c, int(h["absorbedId"]), int(h["canonicalId"]), {k: v for k, v in (h.get("identity") or {}).items() if v is not None} or None)
    elif k == "suggestion":
        c.execute("UPDATE suggestions SET status = ?, decided_at = NULL WHERE id = ?", (h["prev"], int(h["id"])))
        if h.get("others"):
            c.execute(f"UPDATE suggestions SET status = 'pending', decided_at = NULL WHERE id IN ({','.join('?' * len(h['others']))})", h["others"])
    elif k == "dismiss":
        if h.get("ids"):
            c.execute(f"DELETE FROM duplicate_dismissals WHERE id IN ({','.join('?' * len(h['ids']))})", h["ids"])
    elif k == "mark":
        c.execute("DELETE FROM review_marks WHERE id = ?", (int(h["id"]),))
    elif k == "alias":
        c.execute("INSERT INTO alias_overrides (id, source, source_key, canonical_type, canonical_id, note) VALUES (?, ?, ?, ?, ?, ?)", h["row"])
    else:
        raise ApiError(f"unknown undo handle {k!r}")


def _actions(req) -> list[dict]:
    actions = req.body.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ApiError("actions must be a non-empty list")
    if len(actions) > MAX_ACTIONS:
        raise ApiError(f"at most {MAX_ACTIONS} actions per batch")
    if not all(isinstance(a, dict) for a in actions):
        raise ApiError("each action must be an object")
    return actions


def _error_text(exc: Exception) -> str:
    return str(exc) or exc.__class__.__name__


@route("POST", "/api/batch/preview")
def preview(req):
    """Every action run for real, then rolled back -- including ones after a failed one, so you see
    every problem at once. Not a mutating route: nothing it does is ever committed."""
    actions = _actions(req)
    c = db_connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        before = integrity.counts(c, hard_only=True)
        results = []
        for i, it in enumerate(actions):
            c.execute("SAVEPOINT act")
            try:
                _handles, summary = run_action(c, it)
                c.execute("RELEASE act")
                results.append({"index": i, "ok": True, **summary})
            except (ApiError, merge.MergeError) as exc:
                c.execute("ROLLBACK TO act")
                c.execute("RELEASE act")
                results.append({"index": i, "ok": False, "error": _error_text(exc), "code": getattr(exc, "code", None)})
        after = integrity.counts(c, hard_only=True)
    finally:
        c.rollback()
        c.close()
    bad = integrity.worsened(before, after)
    return {"results": results, "ok": all(r["ok"] for r in results) and not bad,
            "integrity": {"worsened": [{"check": k, "description": integrity.CHECKS[k][1], "before": before[k], "after": after[k]} for k in bad]}}


@route("POST", "/api/batch/apply", mutating=True)
def apply(req):
    actions = _actions(req)
    label = str(req.body.get("label") or "")[:120]
    artist_id = int(req.body["artistId"]) if str(req.body.get("artistId") or "").isdigit() else None
    snapshot = merge.labelled_snapshot(label or "batch") if len(actions) > BIG_BATCH else None
    c = db_connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        before = integrity.counts(c, hard_only=True)
        handles, summaries = [], []
        for i, it in enumerate(actions):
            try:
                h, summary = run_action(c, it)
            except (ApiError, merge.MergeError) as exc:
                raise ApiError(f"Nothing was changed -- action {i + 1} ({it.get('type')}) failed: {_error_text(exc)}", 409, "batch_failed", index=i)
            handles.extend(h)
            summaries.append(summary)
        after = integrity.counts(c, hard_only=True)
        bad = integrity.worsened(before, after)
        if bad:
            raise ApiError("Nothing was changed -- this batch would have left " +
                           "; ".join(f"{integrity.CHECKS[k][1].lower()} ({before[k]} -> {after[k]})" for k in bad), 409, "integrity", checks=bad)
        batch_id = c.execute("INSERT INTO batches (label, actions_json, handles_json, summary_json) VALUES (?, ?, ?, ?)",
                             (label, json.dumps(actions), json.dumps(handles), json.dumps({"actions": len(actions), "snapshot": snapshot, "integrityBefore": before, "artistId": artist_id}))).lastrowid
        merges = [h["id"] for h in handles if h["kind"] == "merge"]
        edits = [h["id"] for h in handles if h["kind"] == "edit"]
        if merges:
            c.execute(f"UPDATE merge_log SET batch_id = ? WHERE id IN ({','.join('?' * len(merges))})", (batch_id, *merges))
        if edits:
            c.execute(f"UPDATE edit_log SET batch_id = ? WHERE id IN ({','.join('?' * len(edits))})", (batch_id, *edits))
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()
    return {"batchId": batch_id, "applied": len(actions), "changes": len(handles), "summaries": summaries, "snapshot": snapshot,
            "undo": {"kind": "batchv2", "id": batch_id}}


@route("POST", "/api/batch/undo", mutating=True)
def undo(req):
    return undo_batch(req.int("batchId", required=True))


def undo_batch(batch_id: int) -> dict:
    """The whole batch, newest action first -- all or nothing (also reached from a toast's Undo)."""
    c = db_connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT handles_json, undone_at, label, summary_json FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if not row:
            raise ApiError("batch not found", 404)
        if row[1]:
            raise ApiError("This batch was already undone.", 409, "already_undone")
        handles = json.loads(row[0])
        before = integrity.counts(c, hard_only=True)
        for i, h in enumerate(reversed(handles)):
            try:
                undo_handle(c, h)
            except merge.MergeError as exc:
                raise ApiError(f"Nothing was undone -- {_error_text(exc)}", 409, "undo_blocked", index=len(handles) - 1 - i)
        # Undo may return to exactly how things were before the batch (e.g. a removed dangling alias
        # comes back), never further than that.
        recorded = json.loads(row[3] or "{}").get("integrityBefore") or {}
        allowed = {k: max(v, recorded.get(k, 0)) for k, v in before.items()}
        bad = integrity.worsened(allowed, integrity.counts(c, hard_only=True))
        if bad:
            raise ApiError("Nothing was undone -- undoing would leave " + ", ".join(integrity.CHECKS[k][1].lower() for k in bad), 409, "integrity")
        c.execute("UPDATE batches SET undone_at = datetime('now') WHERE id = ?", (batch_id,))
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()
    return {"batchId": batch_id, "undone": len(handles), "name": row[2] or f"batch #{batch_id}"}


@route("GET", "/api/batches")
def batches(req):
    with read_conn() as c:
        rows = c.execute("SELECT id, label, summary_json, created_at, undone_at FROM batches ORDER BY id DESC LIMIT ?",
                         (min(req.int("limit", 30), 200),)).fetchall()
    return {"items": [{"batchId": r[0], "label": r[1], **json.loads(r[2] or "{}"), "at": r[3], "undoneAt": r[4]} for r in rows]}


@route("GET", "/api/integrity")
def integrity_report(req):
    with read_conn() as c:
        return integrity.report(c, full=req.str("full") == "1")
