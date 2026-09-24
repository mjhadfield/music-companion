import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("MUSIC_DB_PATH", str(Path(tempfile.mkdtemp()) / "unused.sqlite"))
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))
from api.vinyl import parse_format  # noqa: E402


class FormatTests(unittest.TestCase):
    def test_reissue_gatefold(self):
        f = parse_format("2xLP, Album, RE, Gat")
        self.assertEqual(f["media"], "2xLP")
        self.assertEqual([t["label"] for t in f["tags"]], ["Album", "Reissue", "Gatefold"])
        self.assertTrue(f["reissue"])

    def test_original_coloured(self):
        f = parse_format("LP, Album, Ltd, Blu")
        self.assertFalse(f["reissue"])
        self.assertIn("Blue", [t["label"] for t in f["tags"]])

    def test_multi_format_and_unknown(self):
        f = parse_format('LP, Ltd + LP, S/Sided, Etch + RSD, RE, RM')
        self.assertEqual(f["media"], "LP + LP")
        labels = [t["label"] for t in f["tags"]]
        self.assertIn("Single-sided", labels)
        self.assertIn("Record Store Day", labels)
        self.assertEqual(labels.count("Limited"), 1)
        self.assertEqual(parse_format('12", EP')["media"], '12"')
        self.assertEqual(parse_format("LP, Wibble")["tags"][0]["label"], "Wibble")


if __name__ == "__main__":
    unittest.main()
