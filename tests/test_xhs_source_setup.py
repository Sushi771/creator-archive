"""The one-time source selector must preserve existing private settings."""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from creator_archive.xhs_source_setup import configure_native_source


class XhsSourceSetupTests(unittest.TestCase):
    def test_configures_once_and_preserves_other_sources(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sqlite3.connect(root / "archive.sqlite3").close()
            private = root / "private"
            private.mkdir()
            cookie = private / "session.cookie"
            cookie.write_text("synthetic", encoding="ascii")
            source = root / "sources.json"
            original = {"wechat/a": {"url": "https://example.test/feed"}}
            source.write_text(json.dumps(original), encoding="utf-8")
            configure_native_source(root, cookie)
            configured = json.loads(source.read_text(encoding="utf-8"))
            self.assertEqual(configured["wechat/a"], original["wechat/a"])
            self.assertEqual(configured["xiaohongshu/*"]["cookie_file"], str(cookie.resolve(strict=True)))
            self.assertEqual(len(list((root / "backups").glob("sources-before-xhs-*.json"))), 1)
            configure_native_source(root, cookie)
            self.assertEqual(len(list((root / "backups").glob("sources-before-xhs-*.json"))), 1)

    def test_conflicting_global_source_is_preserved(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sqlite3.connect(root / "archive.sqlite3").close()
            private = root / "private"
            private.mkdir()
            cookie = private / "session.cookie"
            cookie.write_text("synthetic", encoding="ascii")
            source = root / "sources.json"
            source.write_text('{"xiaohongshu/*":{"kind":"another_source"}}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "existing_xhs_source"):
                configure_native_source(root, cookie)
            self.assertEqual(source.read_text(encoding="utf-8"), '{"xiaohongshu/*":{"kind":"another_source"}}')


if __name__ == "__main__":
    unittest.main()
