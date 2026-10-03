"""
Import a Discogs collection CSV export into music.sqlite.

Get the export from: discogs.com -> Collection -> Export (top right) -> CSV.
Typical columns: Catalog#, Artist, Title, Label, Format, Rating, Released,
release_id, CollectionFolder, Date Added, Collection Media Condition,
Collection Sleeve Condition, Collection Notes
(Discogs has tweaked these column names over the years, so lookups below
are case/whitespace-insensitive and tolerant of a few known aliases.)

This script:
  1. Stores every raw CSV row verbatim in staging_discogs_rows (as JSON),
     so re-imports are reproducible even if this script's logic changes.
  2. Adds the records that are new (by Discogs release id), filing each under an
     artist/album by the usual matcher (alias overrides from merges, then exact
     name) -- new artists and albums go to the Import inbox like any other
     import's, and so does each new record, saying which album it was filed under.
  3. Compares every record you already have with its row in the export: the fields
     Discogs owns (grades, rating, notes, catalog number, label, format, date added)
     are never edited in maintenance, so a difference means you changed it on Discogs.
     Each changed record becomes an Inbox item ("Changed on Discogs") to apply or keep
     -- nothing is overwritten here. A change you kept isn't raised again.
  4. Flags records you have that aren't in the export any more (sold? moved to
     another folder that wasn't exported?) -- for review; nothing is deleted.

Usage:
    python etl/discogs_import.py path/to/discogs_export.csv
"""
import argparse
import csv
import json
import re
from pathlib import Path

from common import connect, finish_import_run, get_or_create_album, get_or_create_artist, record_event, start_import_run

# Maps our internal field name -> possible column headers in the export,
# tried in order, case-insensitive.
COLUMN_ALIASES = {
    "catalog_number": ["Catalog#", "Catalog #", "CatNo"],
    "artist": ["Artist"],
    "title": ["Title"],
    "label": ["Label"],
    "format": ["Format"],
    "rating": ["Rating"],
    "released": ["Released"],
    "release_id": ["release_id", "Release ID"],
    "date_added": ["Date Added", "DateAdded"],
    "media_condition": ["Collection Media Condition", "Media Condition"],
    "sleeve_condition": ["Collection Sleeve Condition", "Sleeve Condition"],
    "notes": ["Collection Notes", "Notes"],
}

# Discogs disambiguates artists that collide with an existing name in their
# database by appending " (2)", "(3)", etc. right after the name -- this can
# land at the end of the whole Artist field ("Ghost (32)") or mid-string when
# multiple artists are joined by commas ("John Williams, Dio (2), Gojira (2)"),
# so strip it after any name, not just at the very end.
DISAMBIGUATION_SUFFIX = re.compile(r"\s*\(\d+\)")


def build_column_map(fieldnames: list[str]) -> dict[str, str]:
    lower_to_actual = {name.strip().lower(): name for name in fieldnames}
    resolved = {}
    for internal_name, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            match = lower_to_actual.get(alias.strip().lower())
            if match:
                resolved[internal_name] = match
                break
    return resolved


def clean_artist_name(raw: str) -> str:
    return DISAMBIGUATION_SUFFIX.sub("", raw).strip()


def split_artist_credits(raw_artist: str) -> list[str]:
    """
    Discogs joins multiple credited artists in the collection export's
    Artist column with ", " (e.g. "John Williams (4), London Symphony
    Orchestra", "Kiss, Ace Frehley"). Artist names themselves don't contain
    literal commas in Discogs' data, so a plain split is safe.
    """
    parts = [clean_artist_name(p) for p in raw_artist.split(",")]
    return [p for p in parts if p]


def parse_year(released: str):
    if not released:
        return None
    match = re.search(r"\d{4}", released)
    return int(match.group()) if match else None


# The holding fields the export owns: compared on every import, changed only through the Inbox.
DISCOGS_FIELDS = ["media_condition", "sleeve_condition", "rating", "notes", "catalog_number", "label", "format", "date_added"]


def _norm(value) -> str:
    return "" if value is None else str(value).replace("\r\n", "\n").strip()


def _csv_value(field: str, row: dict, columns: dict):
    raw = row.get(columns.get(field, ""), "")
    if field == "rating":
        return int(raw) if str(raw).strip().isdigit() and int(raw) > 0 else None
    return raw if raw is not None else ""


def _flag_once(conn, kind: str, holding_id: int, key: str, detail: dict) -> bool:
    """Record a vinyl Inbox item unless this exact one was raised before (reviewed or not) -- a change
    you chose to keep isn't raised on every import. An older, different item for the same record that's
    still open is settled: the newest export supersedes it."""
    if conn.execute("SELECT 1 FROM import_events WHERE kind = ? AND entity_type = 'vinyl' AND entity_id = ? "
                    "AND json_extract(detail_json, '$.key') = ?", (kind, holding_id, key)).fetchone():
        return False
    conn.execute("UPDATE import_events SET reviewed_at = datetime('now') WHERE kind = ? AND entity_type = 'vinyl' AND entity_id = ? "
                 "AND reviewed_at IS NULL", (kind, holding_id))
    record_event(conn, kind, "vinyl", holding_id, {**detail, "key": key})
    return True


def import_csv(csv_path: Path) -> None:
    conn = connect()
    run_id = start_import_run(conn, "discogs")

    artist_cache: dict[str, int] = {}
    album_cache: dict[tuple, int] = {}
    max_album = conn.execute("SELECT coalesce(max(id), 0) FROM albums").fetchone()[0]
    seen_release_ids: set[int] = set()

    imported = 0
    skipped = 0
    flagged_changes = 0

    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise SystemExit("CSV has no header row -- is this a real Discogs export?")
        columns = build_column_map(reader.fieldnames)

        missing_required = [c for c in ("artist", "title") if c not in columns]
        if missing_required:
            raise SystemExit(
                f"Couldn't find required column(s) {missing_required} in CSV header: "
                f"{reader.fieldnames}"
            )

        for row in reader:
            # Always keep the raw row, regardless of whether we can parse it.
            conn.execute(
                "INSERT INTO staging_discogs_rows (raw_json) VALUES (?)",
                (json.dumps(row),),
            )

            raw_artist = (row.get(columns.get("artist", ""), "") or "").strip()
            raw_title = (row.get(columns.get("title", ""), "") or "").strip()
            if not raw_artist or not raw_title:
                skipped += 1
                continue

            release_id_raw = row.get(columns.get("release_id", ""), "")
            release_id = int(release_id_raw) if str(release_id_raw).strip().isdigit() else None

            if release_id is not None:
                if release_id in seen_release_ids:
                    # a second copy of the same pressing: holdings are keyed by release id, so it can't be added
                    print(f"  ! {raw_artist} -- {raw_title}: a second copy of release {release_id} -- not imported (one row per pressing)")
                    skipped += 1
                    continue
                seen_release_ids.add(release_id)
                cols = ", ".join(DISCOGS_FIELDS)
                existing = conn.execute(
                    f"SELECT id, raw_artist_text, raw_title_text, {cols} FROM vinyl_holdings WHERE discogs_release_id = ?",
                    (release_id,),
                ).fetchone()
                if existing:
                    current = dict(zip(DISCOGS_FIELDS, existing[3:]))
                    changes = {f: [current[f], _csv_value(f, row, columns)] for f in DISCOGS_FIELDS
                               if f in columns and _norm(current[f]) != _norm(_csv_value(f, row, columns))}
                    if changes and _flag_once(conn, "holding_changed", existing[0], json.dumps({f: _norm(v[1]) for f, v in changes.items()}, sort_keys=True),
                                              {"changes": changes, "artist": existing[1], "title": existing[2], "releaseId": release_id}):
                        flagged_changes += 1
                    elif not changes:  # the export agrees with your copy again: an open item for it settles itself
                        conn.execute("UPDATE import_events SET reviewed_at = datetime('now') WHERE kind = 'holding_changed' AND entity_type = 'vinyl' "
                                     "AND entity_id = ? AND reviewed_at IS NULL", (existing[0],))
                    skipped += 1
                    continue

            artist_names = split_artist_credits(raw_artist)
            if not artist_names:
                skipped += 1
                continue
            artist_ids = [get_or_create_artist(conn, artist_cache, name, source="discogs") for name in artist_names]
            year = parse_year(row.get(columns.get("released", ""), ""))  # THIS pressing's year
            fmt_tokens = {t.strip() for part in (row.get(columns.get("format", ""), "") or "").split("+") for t in part.split(",")}
            # A reissue's date is not the album's: a new album gets no year rather than the wrong one
            # (maintenance > Vinyl fills in the original year from MusicBrainz, reviewed).
            album_year = None if fmt_tokens & {"RE", "RP", "RM"} else year
            album_id = get_or_create_album(conn, album_cache, artist_ids, raw_title, album_year, source="discogs")

            rating = _csv_value("rating", row, columns)  # Discogs' 0 = not rated

            conn.execute(
                """
                INSERT INTO vinyl_holdings (
                    album_id, discogs_release_id, catalog_number, label, format,
                    media_condition, sleeve_condition, date_added, rating, notes,
                    raw_artist_text, raw_title_text, pressing_year
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    album_id,
                    release_id,
                    row.get(columns.get("catalog_number", ""), ""),
                    row.get(columns.get("label", ""), ""),
                    row.get(columns.get("format", ""), ""),
                    row.get(columns.get("media_condition", ""), ""),
                    row.get(columns.get("sleeve_condition", ""), ""),
                    row.get(columns.get("date_added", ""), ""),
                    rating,
                    row.get(columns.get("notes", ""), ""),
                    raw_artist,
                    raw_title,
                    year,  # this pressing's year (the album's original year is reviewed in maintenance)
                ),
            )
            holding_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            album_title = conn.execute("SELECT title FROM albums WHERE id = ?", (album_id,)).fetchone()[0]
            record_event(conn, "new_holding", "vinyl", holding_id, {"artist": raw_artist, "title": raw_title, "releaseId": release_id,
                                                                    "albumId": album_id, "albumTitle": album_title, "newAlbum": album_id > max_album})
            imported += 1

    # Records you have that the export no longer lists. Only when the export carries release ids at all
    # (an export without them would make everything look missing).
    missing = 0
    if seen_release_ids:
        for hid, rid, artist, title in conn.execute(
                "SELECT id, discogs_release_id, raw_artist_text, raw_title_text FROM vinyl_holdings WHERE discogs_release_id IS NOT NULL").fetchall():
            if rid not in seen_release_ids:
                missing += _flag_once(conn, "holding_missing", hid, "missing", {"artist": artist, "title": title, "releaseId": rid})
            else:  # back in the export: an open "missing" item settles itself
                conn.execute("UPDATE import_events SET reviewed_at = datetime('now') WHERE kind = 'holding_missing' AND entity_type = 'vinyl' "
                             "AND entity_id = ? AND reviewed_at IS NULL", (hid,))

    summary = {"imported": imported, "duplicates": skipped - flagged_changes, "changedOnDiscogs": flagged_changes, "missingFromExport": missing}
    if run_id is not None:
        summary["events"] = dict(conn.execute("SELECT kind, count(*) FROM import_events WHERE run_id = ? GROUP BY kind", (run_id,)).fetchall())
    conn.commit()
    finish_import_run(conn, summary)
    conn.close()
    print(f"Imported {imported} vinyl holdings, skipped {skipped} (already in the collection / blank rows).")
    if flagged_changes:
        print(f"{flagged_changes} record(s) changed on Discogs (grades, notes, rating...) -- review in maintenance > Inbox > Vinyl.")
    if missing:
        print(f"{missing} record(s) you have aren't in this export -- review in maintenance > Inbox > Vinyl (nothing was deleted).")
    if summary.get("events"):
        print("For review (maintenance > Inbox): " + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(summary["events"].items())))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path, help="path to Discogs collection CSV export")
    args = parser.parse_args()
    import_csv(args.csv_path)
