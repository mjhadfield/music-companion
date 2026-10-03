"""Housekeeping (api/housekeeping.py): only the tools' own copies are listed, the newest stay
protected, and a delete removes exactly what was ticked -- or nothing at all."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("MUSIC_DB_PATH", str(Path(tempfile.mkdtemp()) / "test.sqlite"))  # before common is imported

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))
from api import general, housekeeping  # noqa: E402
from api.core import ApiError, Req  # noqa: E402
from common import DB_PATH  # noqa: E402


class HousekeepingTests(unittest.TestCase):
    def setUp(self):
        self.root = DB_PATH.parent
        self.made = []
        old = time.time() - 10 * 86400
        for i, (folder, name) in enumerate([(".snapshots", "auto-20260901-000000.sqlite"), (".snapshots", "auto-20260902-000000.sqlite"),
                                            (".snapshots", "auto-20260903-000000.sqlite"), (".snapshots", "pre-old.sqlite"), (".snapshots", "pre-new.sqlite"), (".snapshots", "batch-20260904-x.sqlite"),
                                            (".merge_backups", "m1.sqlite"), (".refresh_backups", "r-old.sqlite"), (".refresh_backups", "r-pending.sqlite"),
                                            (".refresh_backups", "r-new.sqlite")]):
            p = self.root / folder / name
            p.parent.mkdir(exist_ok=True)
            p.write_bytes(b"x" * (i + 1))
            t = time.time() if name == "r-new.sqlite" else old + i * 3600
            os.utime(p, (t, t))
            self.made.append(p)
        (self.root / "keep.tar.gz").write_bytes(b"archive")
        DB_PATH.write_bytes(b"live")
        general.JOBS.clear()
        general.JOBS["j"] = {"kind": "refresh", "decision": None, "backup": self.root / ".refresh_backups" / "r-pending.sqlite"}

    def tearDown(self):
        general.JOBS.clear()

    def test_listing_and_protection(self):
        out = housekeeping.listing(Req({}, {}))
        files = {f["path"]: f for g in out["folders"] for f in g["files"]}
        self.assertNotIn("keep.tar.gz", " ".join(files))
        self.assertIsNone(files[".snapshots/auto-20260901-000000.sqlite"]["keep"])          # oldest auto: deletable
        self.assertTrue(files[".snapshots/auto-20260902-000000.sqlite"]["keep"])            # two newest autos kept
        self.assertTrue(files[".snapshots/auto-20260903-000000.sqlite"]["keep"])
        self.assertTrue(files[".snapshots/pre-new.sqlite"]["keep"])                         # newest manual kept,
        self.assertTrue(files[".snapshots/batch-20260904-x.sqlite"]["keep"])                # even with a newer pre-batch one
        self.assertIsNone(files[".snapshots/pre-old.sqlite"]["keep"])
        self.assertTrue(files[".refresh_backups/r-pending.sqlite"]["keep"])                 # a decision still needs it
        self.assertTrue(files[".refresh_backups/r-new.sqlite"]["keep"])                     # under a day old
        self.assertIsNone(files[".refresh_backups/r-old.sqlite"]["keep"])

    def test_delete_is_exact_or_nothing(self):
        with self.assertRaises(ApiError):
            housekeeping.delete(Req({}, {"paths": [".snapshots/pre-old.sqlite", ".refresh_backups/r-pending.sqlite"]}))
        self.assertTrue((self.root / ".snapshots" / "pre-old.sqlite").exists())               # nothing went
        for bad in ["../test.sqlite", ".snapshots/../test.sqlite", "keep.tar.gz"]:
            with self.assertRaises(ApiError):
                housekeeping.delete(Req({}, {"paths": [bad]}))
        out = housekeeping.delete(Req({}, {"paths": [".snapshots/pre-old.sqlite", ".merge_backups/m1.sqlite"]}))
        self.assertEqual(out["deleted"], 2)
        self.assertFalse((self.root / ".snapshots" / "pre-old.sqlite").exists())
        self.assertTrue(DB_PATH.exists() and (self.root / "keep.tar.gz").exists())


if __name__ == "__main__":
    unittest.main()
