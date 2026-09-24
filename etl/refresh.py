"""
Unified maintenance entry point: pulls fresh data from whichever sources
you ask for, then prints a human-readable "what's new" report so you can
sanity-check it before publishing -- rather than trusting three separate
scripts' scroll-by logs.

Each underlying puller is already safe to re-run (lastfm_pull.py is
incremental, setlistfm_pull.py and discogs_import.py dedupe/refresh by
their source's natural id), so this script's only real job is the
before/after diff. That diff is keyed on each table's *natural* key, not
its internal row id -- setlistfm_pull.py deletes and reinserts every
setlist it touches (even unchanged ones get a new internal id), so
"id > previous max" would wrongly count the whole batch as new.

Usage:
    python etl/refresh.py --lastfm
    python etl/refresh.py --setlistfm
    python etl/refresh.py --discogs imports/new-export.csv
    python etl/refresh.py --lastfm --setlistfm --discogs imports/new-export.csv
"""
import argparse
import difflib
import subprocess
import sys
from pathlib import Path

from common import connect

ROOT = Path(__file__).resolve().parent.parent

# Below this similarity ratio, two artist names are treated as
# unrelated -- no point flagging "Metallica" against "Tenacious D".
DUPLICATE_SIMILARITY_THRESHOLD = 0.87


def snapshot(conn):
    """Capture just enough state, keyed by each table's natural id, to
    tell genuinely new rows apart from ones a full re-pull merely
    touched again."""
    return {
        "max_artist_id": conn.execute("SELECT coalesce(max(id), 0) FROM artists").fetchone()[0],
        "max_album_id": conn.execute("SELECT coalesce(max(id), 0) FROM albums").fetchone()[0],
        "scrobble_count": conn.execute("SELECT count(*) FROM scrobbles").fetchone()[0],
        "setlistfm_ids": {r[0] for r in conn.execute("SELECT setlistfm_id FROM setlists")},
        "discogs_release_ids": {
            r[0] for r in conn.execute(
                "SELECT discogs_release_id FROM vinyl_holdings WHERE discogs_release_id IS NOT NULL"
            )
        },
        "artist_names": {r[0] for r in conn.execute("SELECT name FROM artists")},
        "max_run_id": conn.execute("SELECT coalesce(max(id), 0) FROM import_runs").fetchone()[0],
    }


def run_step(label: str, args: list[str]) -> None:
    print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")
    subprocess.run([sys.executable, *args], cwd=ROOT, check=True)


def find_possible_duplicate(name: str, existing_names: set[str]) -> str | None:
    """Best-effort duplicate check for a newly-created artist: is there
    an existing artist whose name is suspiciously similar (but not
    identical -- exact case-insensitive matches would already have been
    caught by get_or_create_artist)? Flags things like a spelling/
    diacritic mismatch ("Motorhead" vs "Motörhead") for manual review;
    doesn't touch the data itself."""
    best_match, best_ratio = None, 0.0
    lname = name.lower()
    for other in existing_names:
        if other.lower() == lname:
            continue
        ratio = difflib.SequenceMatcher(None, lname, other.lower()).ratio()
        if ratio > best_ratio:
            best_match, best_ratio = other, ratio
    if best_ratio >= DUPLICATE_SIMILARITY_THRESHOLD:
        return best_match
    return None


def report(conn, before: dict) -> None:
    print(f"\n{'=' * 60}\nWhat's new\n{'=' * 60}")

    after_scrobbles = conn.execute("SELECT count(*) FROM scrobbles").fetchone()[0]
    new_scrobbles = after_scrobbles - before["scrobble_count"]
    print(f"\nScrobbles: +{new_scrobbles:,} (was {before['scrobble_count']:,}, now {after_scrobbles:,})")
    if new_scrobbles > 0:
        # One row per newly-added scrobble (no SQL-level aggregation --
        # tallied in Python below), most recent first, capped at exactly
        # how many scrobbles this run actually added.
        new_scrobble_artists = conn.execute(f"""
            SELECT ar.name
            FROM scrobbles s JOIN artists ar ON ar.id = s.artist_id
            ORDER BY s.id DESC LIMIT {new_scrobbles}
        """).fetchall()
        from collections import Counter
        tally = Counter(row[0] for row in new_scrobble_artists)
        for name, count in tally.most_common(8):
            print(f"    {count:>4}  {name}")

    new_setlist_rows = conn.execute("""
        SELECT sl.setlistfm_id, sl.event_date, ar.name, ven.name, ven.city
        FROM setlists sl
        JOIN artists ar ON ar.id = sl.artist_id
        LEFT JOIN venues ven ON ven.id = sl.venue_id
    """).fetchall()
    new_setlists = [r for r in new_setlist_rows if r[0] not in before["setlistfm_ids"]]
    print(f"\nSetlists: +{len(new_setlists)}")
    for setlistfm_id, event_date, artist_name, venue_name, city in new_setlists:
        where = f"{venue_name or 'Unknown venue'}" + (f", {city}" if city else "")
        print(f"    {event_date}  {artist_name} @ {where}")

    new_vinyl_rows = conn.execute("""
        SELECT v.discogs_release_id, al.title, ar.name, v.format
        FROM vinyl_holdings v
        JOIN albums al ON al.id = v.album_id
        JOIN artists ar ON ar.id = al.artist_id
    """).fetchall()
    new_vinyl = [r for r in new_vinyl_rows if r[0] not in before["discogs_release_ids"]]
    print(f"\nVinyl holdings: +{len(new_vinyl)}")
    for _, title, artist_name, fmt in new_vinyl:
        print(f"    {artist_name} -- {title} ({fmt})")

    new_artist_rows = conn.execute(
        "SELECT id, name, mbid FROM artists WHERE id > ? ORDER BY id", (before["max_artist_id"],)
    ).fetchall()
    print(f"\nNew artists discovered: {len(new_artist_rows)}")
    flagged = []
    for artist_id, name, mbid in new_artist_rows:
        dup = find_possible_duplicate(name, before["artist_names"])
        marker = " [MBID]" if mbid else ""
        print(f"    {name}{marker}")
        if dup:
            flagged.append((name, dup))

    if flagged:
        print(f"\n⚠  {len(flagged)} possible duplicate(s) -- review before publishing:")
        for new_name, existing_name in flagged:
            print(f"    \"{new_name}\" looks similar to existing \"{existing_name}\"")
        print(
            "    If these are really the same artist under a different spelling, fix it via\n"
            "    alias_overrides (see schema.sql) or by editing the artist name directly --\n"
            "    don't just ignore it, or stats for that artist will be split across two rows."
        )

    # Soft nudge, not a gate: this run's pull already happened and nothing
    # here blocks accept/reject -- it's just a pointer to the maintenance
    # tool so new artists don't quietly pile onto the mbid backlog.
    missing_mbid = [name for _, name, mbid in new_artist_rows if not mbid]
    if missing_mbid:
        print(f"\n{len(missing_mbid)} new artist(s) still missing a MusicBrainz ID:")
        for name in missing_mbid[:8]:
            print(f"    {name}")
        if len(missing_mbid) > 8:
            print(f"    …and {len(missing_mbid) - 8} more")
        print("    Resolve at http://localhost:8643/artists.html when convenient -- doesn't block this pull.")

    new_albums = conn.execute(
        "SELECT count(*) FROM albums WHERE id > ?", (before["max_album_id"],)
    ).fetchone()[0]
    print(f"\nNew albums discovered: {new_albums}")

    # The import inbox: everything this run created or couldn't place with certainty.
    rows = conn.execute("""
        SELECT kind, count(*), sum(reviewed_at IS NULL) FROM import_events
        WHERE run_id > ? GROUP BY kind ORDER BY kind
    """, (before["max_run_id"],)).fetchall()
    linked = sum(n for kind, n, _ in rows if kind == "edition_linked")
    pending = {kind: todo for kind, _, todo in rows if todo}
    if linked:
        print(f"\nAlbum editions matched to an album: {linked}")
    print(f"\nImport inbox: {sum(pending.values())} to review"
          + (" (" + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in pending.items()) + ")" if pending else ""))
    if pending:
        print("    Review at http://localhost:8643/inbox.html")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lastfm", action="store_true", help="incremental Last.fm scrobble pull")
    parser.add_argument("--setlistfm", action="store_true", help="full Setlist.fm re-pull")
    parser.add_argument("--discogs", metavar="CSV_PATH", help="import a fresh Discogs collection export")
    args = parser.parse_args()

    if not (args.lastfm or args.setlistfm or args.discogs):
        parser.error("nothing to do -- pass at least one of --lastfm, --setlistfm, --discogs PATH")

    conn = connect()
    before = snapshot(conn)
    conn.close()

    if args.discogs:
        run_step("Discogs import", ["etl/discogs_import.py", args.discogs])
    if args.lastfm:
        run_step("Last.fm pull (incremental)", ["etl/lastfm_pull.py"])
    if args.setlistfm:
        run_step("Setlist.fm pull (full refresh)", ["etl/setlistfm_pull.py"])

    conn = connect()
    report(conn, before)
    conn.close()

    print(f"\n{'=' * 60}")
    print(
        "Review the report above. If it looks right:\n"
        "    python3 etl/build_public_db.py   # refresh site/public/music.sqlite\n"
        "    git add -A && git commit ...      # your call, whenever you're ready\n"
    )


if __name__ == "__main__":
    main()
