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
  2. Does a *naive* first pass at creating canonical artists/albums by
     exact (case-insensitive) name match -- good enough to get data in the
     door. Cross-source entity resolution (matching these against
     Last.fm/Setlist.fm via MusicBrainz IDs) is a separate later pass and
     will refine these, not redo them from scratch.

Usage:
    python etl/discogs_import.py path/to/discogs_export.csv
"""
import argparse
import csv
import json
import re
from pathlib import Path

from common import connect, get_or_create_album, get_or_create_artist

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


def import_csv(csv_path: Path) -> None:
    conn = connect()

    artist_cache: dict[str, int] = {}
    album_cache: dict[tuple, int] = {}

    imported = 0
    skipped = 0

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
                existing = conn.execute(
                    "SELECT id FROM vinyl_holdings WHERE discogs_release_id = ?",
                    (release_id,),
                ).fetchone()
                if existing:
                    skipped += 1
                    continue

            artist_names = split_artist_credits(raw_artist)
            if not artist_names:
                skipped += 1
                continue
            artist_ids = [get_or_create_artist(conn, artist_cache, name, source="discogs") for name in artist_names]
            year = parse_year(row.get(columns.get("released", ""), ""))
            album_id = get_or_create_album(conn, album_cache, artist_ids, raw_title, year, source="discogs")

            rating_raw = row.get(columns.get("rating", ""), "")
            rating = int(rating_raw) if str(rating_raw).strip().isdigit() else None

            conn.execute(
                """
                INSERT INTO vinyl_holdings (
                    album_id, discogs_release_id, catalog_number, label, format,
                    media_condition, sleeve_condition, date_added, rating, notes,
                    raw_artist_text, raw_title_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            imported += 1

    conn.commit()
    conn.close()
    print(f"Imported {imported} vinyl holdings, skipped {skipped} (duplicates/blank rows).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path, help="path to Discogs collection CSV export")
    args = parser.parse_args()
    import_csv(args.csv_path)
