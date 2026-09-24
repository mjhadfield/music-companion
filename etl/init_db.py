"""
Build (or rebuild) data/music.sqlite from schema.sql.

Usage:
    python etl/init_db.py [--fresh]

--fresh deletes any existing database file first. Without it, schema.sql
is applied idempotently (everything in it is CREATE TABLE IF NOT EXISTS /
CREATE INDEX IF NOT EXISTS) so re-running is safe.
"""
import argparse
import sqlite3
from pathlib import Path

from common import DB_PATH
from migrations import migrate

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schema.sql"


def build(fresh: bool = False) -> None:
    if fresh and DB_PATH.exists():
        DB_PATH.unlink()
        print(f"Removed existing {DB_PATH}")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = SCHEMA_PATH.read_text()

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(schema_sql)
        conn.commit()
        migrate(conn)  # no-ops on a fresh build (schema.sql already has it all), just records them
    finally:
        conn.close()

    print(f"Schema applied to {DB_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fresh", action="store_true", help="delete existing database before rebuilding"
    )
    args = parser.parse_args()
    build(fresh=args.fresh)
