"""
Import your Discogs collection into music.sqlite -- from a CSV export, or straight from Discogs (--api).

From Discogs (--api, needs DISCOGS_TOKEN in .env): every copy in your collection, read through the API
(discogs_api.collection_instances) and turned into the same rows a CSV export gives. The API spells formats
out ("Reissue, Limited Edition") where the export abbreviates them ("RE, Ltd"), so an API sync compares only
your copy's own fields (grades, rating, notes, date added) -- format, label and catalog number belong to the
pressing and can't change for a copy you own. A new record's format is written export-style
(csv_style_format). --dry-run reports what would change without writing anything.

Or a CSV export: discogs.com -> Collection -> Export (top right) -> CSV.
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
    python etl/discogs_import.py --api [--dry-run]
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
# What an API sync compares: your copy's own fields (format / label / catalog number belong to the pressing)
COPY_FIELDS = ["media_condition", "sleeve_condition", "rating", "notes", "date_added"]

# Discogs' format descriptions -> the export's abbreviations (None: the export leaves it out)
FORMAT_ABBR = {
    "Album": "Album", "Compilation": "Comp", "Reissue": "RE", "Repress": "RP", "Remastered": "RM", "Limited Edition": "Ltd",
    "Numbered": "Num", "Record Store Day": "RSD", "Mono": "Mono", "EP": "EP", "Single": "Single", "Single Sided": "S/Sided",
    "Etched": "Etch", "Picture Disc": "Pic", "Unofficial Release": "Unofficial", "Promo": "Promo", "Test Pressing": "TP",
    "Deluxe Edition": "Dlx", "Club Edition": "Club", "Special Edition": "S/Edition", "Mini-Album": "MiniAlbum", "Maxi-Single": "Maxi",
    "Mixed": "Mixed", "Sampler": "Smplr", "Quadraphonic": "Quad", "Misprint": "M/Print", "Stereo": None,
}
DISC_SIZES = {"LP", '12"', '10"', '7"', '11"', '16"', "Flexi-disc"}


def csv_style_format(formats: list[dict]) -> str:
    """Discogs' structured formats -> the export's text: [{name: Vinyl, qty: 2, descriptions: [LP, Album,
    Reissue, Stereo], text: "Gatefold"}] -> "2xLP, Album, RE, Gat". Reproduces 231 of the 233 records' export
    text exactly (the other two differ only in tag order); used for a new record's format."""
    parts = []
    for f in formats or []:
        name, qty, desc = f.get("name") or "", str(f.get("qty") or "1"), list(f.get("descriptions") or [])
        toks = []
        size = next((d for d in desc if d in DISC_SIZES), None)
        if size:
            desc.remove(size)
        media = size or ({"Box Set": "Box"}.get(name, name) if name not in ("Vinyl", "All Media") else "")
        if media:
            toks.append((f"{qty}x" if qty not in ("1", "") else "") + media)
        for d in desc:
            if d.endswith(" RPM"):
                continue
            a = FORMAT_ABBR.get(d, d)
            if a:
                toks.append(a)
        text = (f.get("text") or "").split(",")[0].strip()   # the export keeps the first word of the free text, shortened
        if text:
            word = text.split()[0]
            toks.append(word if word.isdigit() else word[:3])
        parts.append(", ".join(toks))
    return " + ".join(p for p in parts if p)


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


def csv_rows(csv_path: Path) -> tuple[list[dict], set[str], list[str]]:
    """A CSV export -> (rows keyed by our field names, the fields it carries, each raw row as JSON)."""
    rows, raws = [], []
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
            raws.append(json.dumps(row))
            out = {field: _csv_value(field, row, columns) for field in COLUMN_ALIASES}
            rid = str(out.get("release_id") or "").strip()
            out["release_id"] = int(rid) if rid.isdigit() else None
            rows.append(out)
    return rows, set(columns), raws


def api_rows() -> tuple[list[dict], set[str], list[str]]:
    """Your collection straight from Discogs (needs DISCOGS_TOKEN) -> the same shape as csv_rows."""
    import discogs_api
    user = discogs_api.identity()
    fields = discogs_api.collection_fields(user)
    by_name = {name.strip().lower(): fid for fid, name in fields.items()}
    print(f"Reading {user}'s Discogs collection...")
    rows, raws = [], []
    for inst in discogs_api.collection_instances(user):
        raws.append(json.dumps(inst))
        b = inst.get("basic_information") or {}
        notes = {int(n.get("field_id", 0)): n.get("value") or "" for n in inst.get("notes") or []}
        field = lambda name: notes.get(by_name.get(name.lower(), -1), "")  # noqa: E731 -- by name, not id
        labels = b.get("labels") or []
        rows.append({
            "artist": ", ".join(a.get("name", "") for a in b.get("artists") or []),
            "title": b.get("title") or "",
            "label": ", ".join(lb.get("name", "") for lb in labels),
            "catalog_number": ", ".join(lb.get("catno", "") for lb in labels),
            "format": csv_style_format(b.get("formats")),
            "rating": inst.get("rating") or None,  # Discogs' 0 = not rated
            "released": str(b.get("year") or ""),
            "release_id": int(inst["id"]) if inst.get("id") else None,
            "date_added": discogs_api.export_time(inst.get("date_added")),
            "media_condition": field("Media Condition"),
            "sleeve_condition": field("Sleeve Condition"),
            "notes": field("Notes"),
        })
    print(f"  {len(rows)} copies")
    return rows, set(COPY_FIELDS), raws


def import_csv(csv_path: Path) -> None:
    import_rows(*csv_rows(csv_path), source="discogs")


def import_api(dry_run: bool = False) -> None:
    import_rows(*api_rows(), source="discogs-api", dry_run=dry_run)


def import_rows(rows: list[dict], compared: set[str], raws: list[str], source: str = "discogs", dry_run: bool = False) -> None:
    """The import itself, whichever way the rows came. `compared`: the fields this source can speak for
    (a CSV: every column it has; the API: your copy's own fields). A dry run reports and writes nothing."""
    conn = connect()
    run_id = None if dry_run else start_import_run(conn, source)

    artist_cache: dict[str, int] = {}
    album_cache: dict[tuple, int] = {}
    max_album = conn.execute("SELECT coalesce(max(id), 0) FROM albums").fetchone()[0]
    seen_release_ids: set[int] = set()
    report = {"new": [], "changed": [], "missing": []}

    imported = 0
    skipped = 0
    flagged_changes = 0

    for row, raw in zip(rows, raws):
        # Always keep the raw row, regardless of whether we can parse it.
        conn.execute("INSERT INTO staging_discogs_rows (raw_json) VALUES (?)", (raw,))

        raw_artist = (row.get("artist") or "").strip()
        raw_title = (row.get("title") or "").strip()
        if not raw_artist or not raw_title:
            skipped += 1
            continue

        release_id = row.get("release_id")

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
                changes = {f: [current[f], row.get(f)] for f in DISCOGS_FIELDS
                           if f in compared and _norm(current[f]) != _norm(row.get(f))}
                raised = bool(changes) and _flag_once(conn, "holding_changed", existing[0], json.dumps({f: _norm(v[1]) for f, v in changes.items()}, sort_keys=True),
                                                      {"changes": changes, "artist": existing[1], "title": existing[2], "releaseId": release_id})
                if changes:
                    report["changed"].append((existing[1], existing[2], changes, raised))
                if raised:
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
        year = parse_year(row.get("released") or "")  # THIS pressing's year
        fmt_tokens = {t.strip() for part in (row.get("format") or "").split("+") for t in part.split(",")}
        # A reissue's date is not the album's: a new album gets no year rather than the wrong one
        # (maintenance > Vinyl fills in the original year from MusicBrainz, reviewed).
        album_year = None if fmt_tokens & {"RE", "RP", "RM"} else year
        album_id = get_or_create_album(conn, album_cache, artist_ids, raw_title, album_year, source="discogs")

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
                row.get("catalog_number") or "",
                row.get("label") or "",
                row.get("format") or "",
                row.get("media_condition") or "",
                row.get("sleeve_condition") or "",
                row.get("date_added") or "",
                row.get("rating"),
                row.get("notes") or "",
                raw_artist,
                raw_title,
                year,  # this pressing's year (the album's original year is reviewed in maintenance)
            ),
        )
        holding_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        album_title = conn.execute("SELECT title FROM albums WHERE id = ?", (album_id,)).fetchone()[0]
        record_event(conn, "new_holding", "vinyl", holding_id, {"artist": raw_artist, "title": raw_title, "releaseId": release_id,
                                                                "albumId": album_id, "albumTitle": album_title, "newAlbum": album_id > max_album,
                                                                "format": row.get("format") or ""})
        report["new"].append((raw_artist, raw_title, row.get("format") or ""))
        imported += 1

    # Records you have that the export no longer lists. Only when the export carries release ids at all
    # (an export without them would make everything look missing).
    missing = 0
    if seen_release_ids:
        for hid, rid, artist, title in conn.execute(
                "SELECT id, discogs_release_id, raw_artist_text, raw_title_text FROM vinyl_holdings WHERE discogs_release_id IS NOT NULL").fetchall():
            if rid not in seen_release_ids:
                report["missing"].append((artist, title))
                missing += _flag_once(conn, "holding_missing", hid, "missing", {"artist": artist, "title": title, "releaseId": rid})
            else:  # back in the export: an open "missing" item settles itself
                conn.execute("UPDATE import_events SET reviewed_at = datetime('now') WHERE kind = 'holding_missing' AND entity_type = 'vinyl' "
                             "AND entity_id = ? AND reviewed_at IS NULL", (hid,))

    if dry_run:
        conn.rollback()
        conn.close()
        to_review = [c for c in report["changed"] if c[3]]
        print(f"Dry run -- nothing written. {len(report['new'])} new, {len(to_review)} changed to review, {len(report['missing'])} missing"
              f" ({len(report['changed']) - len(to_review)} more differ but you've already decided on them in the Inbox).")
        for artist, title, fmt in report["new"]:
            print(f"  + new: {artist} -- {title} [{fmt}]")
        for artist, title, changes, raised in report["changed"]:
            print(f"  {'~ changed' if raised else '  decided'}: {artist} -- {title}: " + "; ".join(f"{f} {_norm(a)!r} -> {_norm(b)!r}" for f, (a, b) in changes.items()))
        for artist, title in report["missing"]:
            print(f"  - not in the collection: {artist} -- {title}")
        return

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
        print(f"{missing} record(s) you have aren't in your Discogs collection -- review in maintenance > Inbox > Vinyl (nothing was deleted).")
    if summary.get("events"):
        print("For review (maintenance > Inbox): " + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(summary["events"].items())))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path, nargs="?", help="path to Discogs collection CSV export")
    parser.add_argument("--api", action="store_true", help="read your collection from Discogs (needs DISCOGS_TOKEN)")
    parser.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    args = parser.parse_args()
    if args.api == bool(args.csv_path):
        parser.error("give a CSV path, or --api")
    if args.api:
        import discogs_api
        try:
            import_api(dry_run=args.dry_run)
        except discogs_api.DiscogsError as e:
            raise SystemExit(f"Discogs sync stopped: {e}")
    elif args.dry_run:
        import_rows(*csv_rows(args.csv_path), source="discogs", dry_run=True)
    else:
        import_csv(args.csv_path)
