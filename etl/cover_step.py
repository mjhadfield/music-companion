"""
Covers for the albums you've just played -- a step of the Last.fm import (refresh.py --lastfm, so the
scheduled import runs it too).

For each album played since --since-scrobble that has a MusicBrainz id and has never been looked up
(cover_status NULL), not turned down before (review mark cover-rejected) and not already waiting:
fetch its front cover from the Cover Art Archive into data/covers/.pending/ and raise a new_cover
item in the Inbox's Covers tab. Nothing reaches the site until it's accepted there (and published).
The archive having nothing marks the album 'none', as the manual fetch does.

Space: an album with fewer than covers.FULL_SIZE_PLAYS plays (and no vinyl copy) gets the 250px
thumbnail, about a third of the 500px one's size -- one-offs only show small on the site. When an
album crosses that many plays in this import, its 250px cover (accepted or pending) is swapped for
the 500px one: the same image, so no new review.

Usage:
    python etl/cover_step.py --since-scrobble 123456 [--after-run 95] [--limit 40]
"""
import argparse
import json
import time

import covers
from common import connect

REJECTED = "cover-rejected"   # the Albums > Covers tab's mark: not this cover
PAUSE = 0.3                   # between albums -- the archive has no published limit; be polite anyway


def played_albums(conn, since_scrobble: int) -> list[dict]:
    rows = conn.execute("""
        SELECT al.id, al.mbid, al.cover_status,
               (SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id),
               (SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id AND s.id <= ?),
               EXISTS (SELECT 1 FROM vinyl_holdings v WHERE v.album_id = al.id),
               EXISTS (SELECT 1 FROM review_marks m WHERE m.entity_type = 'album' AND m.entity_id = al.id AND m.mark = ?)
        FROM albums al
        WHERE al.id IN (SELECT DISTINCT album_id FROM scrobbles WHERE id > ? AND album_id IS NOT NULL)
    """, (since_scrobble, REJECTED, since_scrobble)).fetchall()
    return [{"id": i, "mbid": m, "status": st, "plays": n, "playsBefore": nb, "vinyl": bool(v), "rejected": bool(r)}
            for i, m, st, n, nb, v, r in rows]


def import_run(conn, after_run: int | None) -> int:
    """The Last.fm run this step belongs to (its new_cover items sit with that run's other finds),
    or a run of its own when run by hand."""
    if after_run is not None:
        row = conn.execute("SELECT id FROM import_runs WHERE source = 'lastfm' AND id > ? ORDER BY id DESC LIMIT 1", (after_run,)).fetchone()
        if row:
            return row[0]
    run_id = conn.execute("INSERT INTO import_runs (source, finished_at) VALUES ('covers', datetime('now'))").lastrowid
    conn.commit()
    return run_id


def run(conn, since_scrobble: int, after_run: int | None = None, limit: int = 40) -> dict:
    albums = played_albums(conn, since_scrobble)
    open_items = {r[0] for r in conn.execute("SELECT entity_id FROM import_events WHERE kind = 'new_cover' AND reviewed_at IS NULL")}

    fetch = [a for a in albums if a["mbid"] and a["status"] is None and not a["rejected"]
             and a["id"] not in open_items and not covers.pending_path(a["id"]).is_file()]
    fetch.sort(key=lambda a: -a["plays"])
    deferred = max(0, len(fetch) - limit)
    fetch = fetch[:limit]

    # crossed the full-size line in this import, and holding a small cover (published or waiting)
    def small_file(a):
        for path in (covers.pending_path(a["id"]), covers.COVERS_DIR / f"{a['id']}.jpg"):
            if path.is_file():
                width = covers.jpeg_width(path)
                return path if width is not None and width <= covers.SMALL else None
        return None
    upgrade = [(a, p) for a in albums if a["mbid"] and a["playsBefore"] < covers.FULL_SIZE_PLAYS <= a["plays"] and not a["vinyl"]
               for p in [small_file(a)] if p]

    result = {"found": 0, "notOnArchive": 0, "upgraded": 0, "deferred": deferred, "bytes": 0}
    if not fetch and not upgrade:
        return result

    run_id = import_run(conn, after_run)
    for a in fetch:
        size = covers.wanted_size(a["plays"], a["vinyl"])
        content = covers.caa_front(a["mbid"], size)
        if content:
            covers.save_pending(a["id"], content)
            conn.execute("INSERT INTO import_events (run_id, kind, entity_type, entity_id, detail_json) VALUES (?, 'new_cover', 'cover', ?, ?)",
                         (run_id, a["id"], json.dumps({"mbid": a["mbid"], "size": size, "bytes": len(content)})))
            result["found"] += 1
            result["bytes"] += len(content)
        else:
            conn.execute("UPDATE albums SET cover_status = 'none', cover_updated_at = datetime('now') WHERE id = ? AND cover_status IS NULL", (a["id"],))
            result["notOnArchive"] += 1
        conn.commit()
        time.sleep(PAUSE)
    for a, path in upgrade:
        content = covers.caa_front(a["mbid"], covers.FULL)
        if content and len(content) > path.stat().st_size:
            path.write_bytes(content)
            if path.parent == covers.COVERS_DIR:
                conn.execute("UPDATE albums SET cover_updated_at = datetime('now') WHERE id = ?", (a["id"],))
                conn.commit()
            result["upgraded"] += 1
        time.sleep(PAUSE)

    # on the run's own summary, so the Inbox's Runs table shows it beside the scrobbles
    row = conn.execute("SELECT summary_json FROM import_runs WHERE id = ?", (run_id,)).fetchone()
    summary = json.loads(row[0] or "{}") if row else {}
    summary["covers"] = result
    if result["found"]:
        events = summary.setdefault("events", {})
        events["new_cover"] = events.get("new_cover", 0) + result["found"]
    conn.execute("UPDATE import_runs SET summary_json = ? WHERE id = ?", (json.dumps(summary), run_id))
    conn.commit()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since-scrobble", type=int, required=True, help="albums played in scrobbles after this id")
    parser.add_argument("--after-run", type=int, default=None, help="attach to the newest Last.fm import run after this id")
    parser.add_argument("--limit", type=int, default=40, help="max covers fetched this run (the rest wait for their next play)")
    args = parser.parse_args()

    conn = connect()
    r = run(conn, args.since_scrobble, args.after_run, args.limit)
    conn.close()
    if not any((r["found"], r["notOnArchive"], r["upgraded"])):
        print("No new covers to look for.")
        return
    print(f"Covers: {r['found']} found ({r['bytes'] / 1024:.0f} KB, waiting in Inbox › Covers), {r['notOnArchive']} not on the archive"
          + (f", {r['upgraded']} swapped for the {covers.FULL}px version" if r["upgraded"] else "")
          + (f", {r['deferred']} left for their next play (limit {args.limit})" if r["deferred"] else "") + ".")


if __name__ == "__main__":
    main()
