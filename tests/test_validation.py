import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from creator_archive.links import classify
from creator_archive.validation import AdapterFailure, Item, Page, Store, SyntheticAdapter, scan


class FixedAdapter:
    version = "synthetic-v1"

    def __init__(self, page):
        self.result = page

    def page(self, author_id, cursor):
        return self.result


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.store = Store(self.path)
        self.run_id = self.store.create("wechat", "demo-a", SyntheticAdapter.version)

    def test_four_pages_deduplicate_both_platforms(self):
        for platform in ("wechat", "xiaohongshu"):
            run = self.store.create(platform, "demo-a", SyntheticAdapter.version)
            row = scan(self.store, run, SyntheticAdapter())
            self.assertEqual((row["pages"], row["item_count"]), (4, 60))
            self.assertEqual(row["coverage"], "complete_for_accessible_scope")
            self.assertEqual(row["media_state"], "not_tested")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM items").fetchone()[0], 120)
            self.assertEqual(db.execute("SELECT count(*) FROM page_evidence").fetchone()[0], 8)

    def test_process_crash_then_new_process_resume(self):
        # Real OS process exit immediately after a committed page; only source data is synthetic.
        program = """
import os, sys
from pathlib import Path
from creator_archive.validation import Store, SyntheticAdapter
s=Store(Path(sys.argv[1])); a=SyntheticAdapter()
s.commit_page(1,None,a.page('demo-a',None))
os._exit(23)
"""
        result = subprocess.run([sys.executable, "-c", program, str(self.path)], capture_output=True)
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(self.store.get(1)["cursor"], "1")
        resume = """
import sys
from pathlib import Path
from creator_archive.validation import Store,SyntheticAdapter,scan
r=scan(Store(Path(sys.argv[1])),1,SyntheticAdapter())
assert r['pages']==4 and r['item_count']==60 and r['state']=='succeeded', r
"""
        result = subprocess.run([sys.executable, "-c", resume, str(self.path)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_login_failure_requires_explicit_resume(self):
        row = scan(self.store, 1, SyntheticAdapter(2, "needs_login"))
        self.assertEqual((row["pages"], row["cursor"], row["state"]), (2, "2", "needs_login"))
        self.assertEqual(scan(self.store, 1, SyntheticAdapter())["pages"], 2)
        row = scan(Store(self.path), 1, SyntheticAdapter(), login_confirmed=True)
        self.assertEqual((row["pages"], row["item_count"]), (4, 60))

    def test_rate_limit_persists_and_click_cannot_bypass(self):
        scan(self.store, 1, SyntheticAdapter(2, "rate_limited"), now=100)
        self.assertEqual(scan(Store(self.path), 1, SyntheticAdapter(), now=104)["pages"], 2)
        self.assertEqual(scan(Store(self.path), 1, SyntheticAdapter(), now=105)["pages"], 4)

    def test_timeout_retries_only_failed_page(self):
        scan(self.store, 1, SyntheticAdapter(2), now=100)
        row = scan(Store(self.path), 1, SyntheticAdapter())
        self.assertEqual((row["pages"], row["item_count"]), (4, 60))

    def test_page_budget_is_partial(self):
        row = scan(self.store, 1, SyntheticAdapter(), max_pages=3)
        self.assertEqual((row["pages"], row["coverage"], row["reason"]), (3, "partial", "page_budget_reached"))
        self.assertEqual(scan(self.store, 1, SyntheticAdapter())["pages"], 4)

    def test_invalid_pages_do_not_advance_checkpoint(self):
        item = Item("one", "demo-a", "2026-08-01")
        bad = [
            (Page((), "1", True), "empty_nonterminal_page"),
            (Page((item,), None, True), "missing_cursor"),
            (Page((item,), None, False), "missing_terminal_evidence"),
            (Page((item,), "1", False, "end"), "missing_terminal_evidence"),
            (Page((item,), "1", "false"), "invalid_page"),
        ]
        for page, reason in bad:
            with self.subTest(reason=reason):
                row = scan(self.store, 1, FixedAdapter(page))
                self.assertEqual((row["pages"], row["item_count"], row["reason"]), (0, 0, reason))

    def test_empty_terminal_requires_explicit_evidence(self):
        row = scan(self.store, 1, FixedAdapter(Page((), None, False, "synthetic empty account")))
        self.assertEqual((row["pages"], row["state"]), (1, "succeeded"))

    def test_repeated_cursor_and_multi_page_cycle(self):
        scan(self.store, 1, SyntheticAdapter(), max_pages=2)
        for cursor in ("2", "1"):
            row = scan(self.store, 1, FixedAdapter(Page((Item("new", "demo-a", "2026-08-01"),), cursor, True)))
            self.assertEqual((row["pages"], row["reason"]), (2, "repeated_cursor"))

    def test_transaction_rolls_back_partial_page_on_wrong_author(self):
        page = Page((Item("ok", "demo-a", "2026-08-01"), Item("wrong", "other", "2026-08-01")), None, False, "end")
        row = scan(self.store, 1, FixedAdapter(page))
        self.assertEqual((row["item_count"], row["pages"], row["reason"]), (0, 0, "identity_mismatch"))

    def test_item_id_cannot_move_between_authors(self):
        scan(self.store, 1, SyntheticAdapter())
        run = self.store.create("wechat", "other", SyntheticAdapter.version)
        page = Page((Item("demo-a-000", "other", "2026-08-01"),), None, False, "end")
        row = scan(self.store, run, FixedAdapter(page))
        self.assertEqual(row["reason"], "identity_mismatch")
        self.assertEqual(self.store.get(1)["item_count"], 60)

    def test_version_change_cannot_reuse_cursor(self):
        scan(self.store, 1, SyntheticAdapter(), max_pages=1)
        adapter = SyntheticAdapter()
        adapter.version = "changed"
        row = scan(self.store, 1, adapter)
        self.assertEqual((row["pages"], row["reason"]), (1, "adapter_version_changed"))

    def test_repeated_click_reuses_completed_run(self):
        scan(self.store, 1, SyntheticAdapter())
        self.assertEqual(self.store.create("wechat", "demo-a", SyntheticAdapter.version), 1)
        self.assertEqual(scan(self.store, 1, SyntheticAdapter())["pages"], 4)

    def test_unknown_exception_is_not_persisted(self):
        class Broken(SyntheticAdapter):
            def page(self, author_id, cursor):
                raise RuntimeError("sensitive-example-must-not-persist")
        self.assertEqual(scan(self.store, 1, Broken())["reason"], "unexpected_adapter_error")
        self.assertNotIn(b"sensitive-example-must-not-persist", self.path.read_bytes())


class LinkTests(unittest.TestCase):
    def test_known_links_are_candidates_not_confirmed_identity(self):
        samples = [
            ("分享 https://www.xiaohongshu.com/user/profile/" + "a"*24, "xiaohongshu", "profile"),
            ("https://www.xiaohongshu.com/explore/" + "b"*24, "xiaohongshu", "item"),
            ("https://xhslink.com/a/example", "xiaohongshu", "short_link"),
            ("https://mp.weixin.qq.com/s/example", "wechat", "item"),
            ("https://mp.weixin.qq.com/mp/profile_ext?__biz=EXAMPLE==", "wechat", "profile"),
        ]
        for text, platform, kind in samples:
            row = classify(text)
            self.assertEqual((row["platform"], row["kind"]), (platform, kind))
            self.assertFalse(row["can_subscribe"])
            self.assertFalse(row["identity_verified"])

    def test_unsupported_or_ambiguous_links_rejected(self):
        for text in ["https://127.0.0.1/private", "https://mp.weixin.qq.com.evil.example/s/a",
                     "https://user:pass@mp.weixin.qq.com/s/a", "https://mp.weixin.qq.com:444/s/a",
                     "https://mp.weixin.qq.com/unknown", "https://[invalid/s/a", "no link",
                     "https://xhslink.com/a https://xhslink.com/b"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                classify(text)

    def test_query_secrets_not_returned(self):
        row = classify("https://www.xiaohongshu.com/explore/" + "a"*24 + "?xsec_token=private-example")
        self.assertNotIn("private-example", str(row))


if __name__ == "__main__":
    unittest.main()
