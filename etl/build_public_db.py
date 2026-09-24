"""
Build the slim, public-facing database that the frontend actually ships to
the browser (site/public/music.sqlite).

data/music.sqlite (the working copy) keeps raw staging_* tables -- full
JSON responses from every API pull, kept for reproducibility so entity
resolution or a schema change can be re-run without re-hitting the APIs.
For ~100k scrobbles that alone is ~80MB, which is fine to have on disk
locally but not something to ask a browser to download. This script
creates a fresh database using only the public subset of schema.sql, then
copies rows in from the working database and VACUUMs it to its real size.

Usage:
    python etl/build_public_db.py
"""
import re
import shutil
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schema.sql"
SOURCE_DB = ROOT / "data" / "music.sqlite"  # deliberately NOT common.DB_PATH: only the real DB is ever published
PUBLIC_DB = ROOT / "site" / "public" / "music.sqlite"
SOURCE_COVERS = ROOT / "data" / "covers"
PUBLIC_COVERS = ROOT / "site" / "public" / "covers"

# Every table except staging_* -- listed explicitly (rather than pattern-
# matched) so a new table added to schema.sql without a decision made here
# fails loudly instead of silently leaking into the public build or
# silently getting dropped from it.
PUBLIC_TABLES = [
    "artists",
    "albums",
    "album_artists",
    "songs",
    "vinyl_holdings",
    "scrobbles",
    "venues",
    "setlists",
    "setlist_songs",
    "notes",
    "alias_overrides",
    "genres",
    "album_genres",
    "vinyl_details",
]
# Views are recreated from schema.sql (no rows to copy) -- also listed explicitly.
PUBLIC_VIEWS = [
    "artist_genres",
]


def public_schema_statements() -> list[str]:
    full_schema = SCHEMA_PATH.read_text()
    # Strip "--" line comments before splitting on ";" -- some of them
    # (e.g. the album_artists FK comment) contain semicolons of their own,
    # which would otherwise fool a naive split into cutting a statement
    # in half.
    without_comments = re.sub(r"--[^\n]*", "", full_schema)
    statements = [s.strip() for s in without_comments.split(";") if s.strip()]
    keep = []
    for stmt in statements:
        match = re.search(r"CREATE (TABLE|INDEX|VIEW) IF NOT EXISTS (\S+)", stmt)
        if not match:
            continue
        kind, name = match.groups()
        if kind == "TABLE":
            if name in PUBLIC_TABLES:
                keep.append(stmt)
        elif kind == "VIEW":
            if name in PUBLIC_VIEWS:
                keep.append(stmt)
        else:  # INDEX -- check what table it's ON, not the index's own name
            on_match = re.search(r"\bON\s+(\w+)", stmt)
            if on_match and on_match.group(1) in PUBLIC_TABLES:
                keep.append(stmt)
    return keep


def sync_covers() -> int:
    """Copy any cover art fetched since the last build into the public, actually-served
    directory -- covers become live on the site only when a build happens, same as every other
    row change (nothing to VACUUM here, they're just files, not database rows)."""
    if not SOURCE_COVERS.is_dir():
        return 0
    PUBLIC_COVERS.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in SOURCE_COVERS.iterdir():
        if not src.is_file():
            continue
        dest = PUBLIC_COVERS / src.name
        if dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime:
            continue
        shutil.copy2(src, dest)
        copied += 1
    return copied


def build(source_db: Path = SOURCE_DB, public_db: Path = PUBLIC_DB, covers: bool = True) -> None:
    SOURCE_DB, PUBLIC_DB = source_db, public_db  # noqa: N806 -- overridable for a scratch test build
    if not SOURCE_DB.exists():
        raise SystemExit(f"{SOURCE_DB} doesn't exist yet -- run the ETL scripts first.")

    PUBLIC_DB.parent.mkdir(parents=True, exist_ok=True)
    if PUBLIC_DB.exists():
        PUBLIC_DB.unlink()

    conn = sqlite3.connect(PUBLIC_DB)
    for stmt in public_schema_statements():
        conn.execute(stmt)

    conn.execute("ATTACH DATABASE ? AS src", (str(SOURCE_DB),))
    total_rows = 0
    for table in PUBLIC_TABLES:
        # Named columns, not SELECT * -- a positional copy silently shuffles values between
        # columns whenever the working database's column order differs from schema.sql's
        # (ALTER TABLE ADD COLUMN always appends, wherever schema.sql declares it).
        cols = ", ".join(r[1] for r in conn.execute(f"PRAGMA main.table_info({table})"))
        cur = conn.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM src.{table}")
        total_rows += cur.rowcount
        print(f"  {table}: {cur.rowcount} rows")
    conn.commit()
    conn.execute("DETACH DATABASE src")
    conn.execute("VACUUM")
    conn.close()

    size_bytes = PUBLIC_DB.stat().st_size
    # The frontend's loading-progress bar needs the real, uncompressed byte
    # count up front. It can't just trust the fetch response's own
    # Content-Length for this: GitHub Pages (and most static hosts) gzip
    # text-heavy assets like this one on the wire, and Content-Length then
    # reports the *compressed* size while the bytes the browser actually
    # hands to a stream reader are the decompressed ones -- so the
    # percentage would drift as loaded-so-far sails past a total that was
    # never the real target. A tiny sidecar file with the true byte count,
    # written at build time, sidesteps relying on any HTTP header at all.
    (PUBLIC_DB.parent / "music.sqlite.size").write_text(str(size_bytes))

    size_mb = size_bytes / (1024 * 1024)
    print(f"\nBuilt {PUBLIC_DB} -- {total_rows} rows total, {size_mb:.1f} MB")

    if covers:
        covers_copied = sync_covers()
        print(f"Synced covers/ -- {covers_copied} new/updated file(s)")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=SOURCE_DB, help="working database to publish from (testing only)")
    parser.add_argument("--dest", type=Path, default=PUBLIC_DB, help="where to write the public database (testing only)")
    args = parser.parse_args()
    # a test build never touches the real site's covers
    build(args.source, args.dest, covers=args.dest.resolve() == PUBLIC_DB.resolve())
