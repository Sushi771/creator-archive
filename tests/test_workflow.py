"""Synthetic workflow regressions; these do not establish live platform coverage."""

import base64
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import quote

from creator_archive.validation import AdapterFailure, Item, Page, SyntheticAdapter
from creator_archive.workflow import ArchiveWorkflow


class FailOneAuthor(SyntheticAdapter):
    def __init__(self):
        super().__init__()
        self.requests = []

    def page(self, author_id, cursor):
        self.requests.append((author_id, cursor))
        if author_id == "blocked" and cursor == "2":
            raise AdapterFailure("timeout")
        return super().page(author_id, cursor)


class MissingTerminalAdapter:
    version = "synthetic-missing-terminal"

    def page(self, author_id, cursor):
        item = Item(f"{author_id}-one", author_id, "2026-08-01")
        return Page((item,), None, False, None)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workflow = ArchiveWorkflow(self.root)

    def subscribe(self, platform, author_id):
        self.workflow.subscribe(platform, author_id, author_id, verified=True,
                                evidence="synthetic test identity")

    def test_verified_subscriptions_and_four_page_evidence_for_both_platforms(self):
        with self.assertRaisesRegex(ValueError, "verified_identity_required"):
            self.workflow.subscribe("wechat", "unverified", "Unknown", verified=False,
                                    evidence="synthetic claim")
        for platform in ("wechat", "xiaohongshu"):
            for author_id in ("author-a", "author-b"):
                self.subscribe(platform, author_id)
        self.workflow.set_enabled("wechat", "author-b", False)
        result = self.workflow.run_all({p: SyntheticAdapter() for p in ("wechat", "xiaohongshu")},
                                       mode="archive")
        self.assertEqual(len(result["runs"]), 4)  # Manual full archive includes paused authors.
        for run in result["runs"]:
            self.assertEqual((run["state"], run["pages"], run["item_count"]),
                             ("succeeded", 4, 60))
            self.assertEqual(run["coverage"], "complete_for_accessible_scope")
            self.assertTrue(run["terminal_evidence"])
            with self.workflow.connect() as db:
                pages = [dict(row) for row in db.execute(
                    "SELECT * FROM pages WHERE run_id=? ORDER BY page_number", (run["id"],))]
            self.assertEqual([page["request_cursor"] for page in pages], ["", "1", "2", "3"])
            self.assertEqual([page["next_cursor"] for page in pages], ["1", "2", "3", None])
            self.assertEqual([len(json.loads(page["item_ids"])) for page in pages], [15, 16, 16, 16])
            self.assertIsNone(pages[-1]["next_cursor"])
            self.assertTrue(pages[-1]["terminal_evidence"])
        self.assertEqual(len(result["export"]["authors"]), 4)

    def test_new_process_continues_fixed_batch_from_committed_cursor(self):
        self.subscribe("wechat", "author-a")
        first = self.workflow.run_all({"wechat": SyntheticAdapter()}, max_pages=2)
        run = first["runs"][0]
        self.assertEqual((run["pages"], run["cursor"], run["state"]), (2, "2", "partial"))
        script = """
from pathlib import Path
import sys
from creator_archive.validation import SyntheticAdapter
from creator_archive.workflow import ArchiveWorkflow
w = ArchiveWorkflow(Path(sys.argv[1]))
r = w.run_all({'wechat': SyntheticAdapter()}, batch_id=int(sys.argv[2]))
assert len(r['runs']) == 1 and r['runs'][0]['pages'] == 4, r
"""
        completed = subprocess.run([sys.executable, "-c", script, str(self.root),
                                    str(first["batch_id"])], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        resumed = ArchiveWorkflow(self.root).status(first["batch_id"])["runs"][0]
        self.assertEqual((resumed["pages"], resumed["item_count"], resumed["state"]),
                         (4, 60, "succeeded"))
        repeated = self.workflow.run_all({"wechat": SyntheticAdapter()}, batch_id=first["batch_id"])
        self.assertEqual(repeated["runs"][0]["pages"], 4)
        with self.workflow.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM pages").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT count(*) FROM items").fetchone()[0], 60)

    def test_completed_author_stays_complete_when_adapter_later_unavailable(self):
        self.subscribe("wechat", "author-a")
        first = self.workflow.run_all({"wechat": SyntheticAdapter()})
        self.assertEqual(first["runs"][0]["state"], "succeeded")
        second = self.workflow.run_all({}, batch_id=first["batch_id"])
        self.assertEqual((second["runs"][0]["state"], second["runs"][0]["pages"]),
                         ("succeeded", 4))

    def test_padded_wechat_biz_can_be_preserved_as_stable_identity(self):
        # Public profile links use __biz values with base64 padding.
        biz = "EXAMPLE+/=="
        self.workflow.subscribe("wechat", biz, "公众号", verified=True,
                                evidence="synthetic test profile link")
        self.assertEqual(self.workflow.subscriptions()[0]["author_id"], biz)
        output = self.workflow.export_all()
        self.assertEqual(output["authors"][0]["author_id"], biz)
        self.assertEqual(Path(output["authors"][0]["manifest"]).parent.name,
                         quote(biz, safe=""))

    def test_recent_export_overrides_old_terminal_claim_but_preserves_old_evidence(self):
        self.subscribe("xiaohongshu", "author-a")
        old = self.workflow.run_all({"xiaohongshu": SyntheticAdapter()})
        with self.workflow.connect() as db:
            before = tuple(db.execute("SELECT coverage,terminal_evidence FROM runs WHERE id=?",
                                      (old["runs"][0]["id"],)).fetchone())
        output = self.workflow.export_all(batch_id=old["batch_id"],
                                         recent_window_authors={("xiaohongshu", "author-a")})
        author = output["authors"][0]
        manifest = json.loads(Path(author["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["coverage"], "recent_window_only")
        self.assertIsNone(manifest["terminal_evidence"])
        self.assertEqual(manifest["window_size"], 30)
        html = Path(author["index"]).read_text(encoding="utf-8")
        self.assertIn("两次刷新间新增超过窗口可能遗漏", html)
        self.assertNotIn("已观察到当前可获取范围的明确末页", html)
        with self.workflow.connect() as db:
            self.assertEqual(tuple(db.execute("SELECT coverage,terminal_evidence FROM runs WHERE id=?",
                                             (old["runs"][0]["id"],)).fetchone()), before)

    def test_failed_author_and_missing_platform_do_not_stop_other_authors(self):
        for platform, author_id in (("wechat", "blocked"), ("wechat", "healthy"),
                                    ("xiaohongshu", "other")):
            self.subscribe(platform, author_id)
        adapter = FailOneAuthor()
        first = self.workflow.run_all({"wechat": adapter})
        by_author = {run["author_id"]: run for run in first["runs"]}
        self.assertEqual((by_author["blocked"]["state"], by_author["blocked"]["pages"],
                          by_author["blocked"]["cursor"]), ("partial", 2, "2"))
        self.assertEqual(by_author["healthy"]["state"], "succeeded")
        self.assertEqual((by_author["other"]["state"], by_author["other"]["reason"]),
                         ("partial", "adapter_unavailable"))
        second = ArchiveWorkflow(self.root).run_all(
            {"wechat": SyntheticAdapter(), "xiaohongshu": SyntheticAdapter()},
            batch_id=first["batch_id"])
        self.assertEqual({r["author_id"]: r["state"] for r in second["runs"]},
                         {"blocked": "succeeded", "healthy": "succeeded", "other": "succeeded"})
        self.assertEqual({r["author_id"]: r["item_count"] for r in second["runs"]},
                         {"blocked": 60, "healthy": 60, "other": 60})

    def test_missing_terminal_proof_cannot_mark_author_complete(self):
        self.subscribe("wechat", "author-a")
        result = self.workflow.run_all({"wechat": MissingTerminalAdapter()})
        run = result["runs"][0]
        self.assertEqual((run["state"], run["coverage"], run["reason"], run["pages"],
                          run["item_count"]),
                         ("partial", "partial", "missing_terminal_evidence", 0, 0))

    def test_offline_export_references_local_asset_and_preserves_user_edit(self):
        self.subscribe("xiaohongshu", "author-a")
        self.workflow.run_all({"xiaohongshu": SyntheticAdapter()})
        item_id = "author-a-000"
        self.workflow.save_detail("xiaohongshu", item_id, "author-a", "正文段落",
                                  f"https://www.xiaohongshu.com/explore/{item_id}?xsec_token=private-sample")
        image = self.root / "sample.png"
        image.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/"
            "l1sAAAAASUVORK5CYII="))
        asset = self.workflow.attach_media("xiaohongshu", item_id, "image-1", image,
                                           position=0, kind="image", mime="image/png")
        self.assertTrue(asset.is_file())
        self.workflow.attach_media("xiaohongshu", item_id, "image-2", image,
                                   position=1, kind="image", mime="image/png")
        output = self.workflow.export_all()
        author = output["authors"][0]
        manifest = json.loads(Path(author["manifest"]).read_text(encoding="utf-8"))
        item = next(row for row in manifest["items"] if row["item_id"] == item_id)
        self.assertEqual(item["detail_state"], "complete")
        self.assertNotIn("private-sample", Path(author["corpus"]).read_text(encoding="utf-8"))
        self.assertEqual(len(item["assets"]), 2)
        media_ref = "assets/image-1.png"
        note = self.root / "archive" / "xiaohongshu" / "author-a" / item_id / "article.md"
        html = note.with_name("index.html")
        self.assertIn(media_ref, note.read_text(encoding="utf-8"))
        self.assertIn(media_ref, html.read_text(encoding="utf-8"))
        self.assertTrue((note.parent / media_ref).is_file())
        moved = self.root / "moved-offline" / "author-a"
        shutil.copytree(Path(author["index"]).parent, moved)
        moved_html = (moved / item_id / "index.html").read_text(encoding="utf-8")
        self.assertLess(moved_html.index("assets/image-1.png"), moved_html.index("assets/image-2.png"))
        self.assertIn("class='body'>正文段落", moved_html)
        for ref in ("image-1.png", "image-2.png"):
            self.assertTrue((moved / item_id / "assets" / ref).is_file())
        note.write_text("用户手工修改，不得丢失", encoding="utf-8")
        self.workflow.export_all()
        self.assertEqual(note.read_text(encoding="utf-8"), "用户手工修改，不得丢失")
        self.assertEqual(len(list(note.parent.glob("article.*.md"))), 1)

    def test_missing_local_asset_is_not_silently_reported_as_available(self):
        self.subscribe("xiaohongshu", "author-a")
        self.workflow.run_all({"xiaohongshu": SyntheticAdapter()})
        item_id = "author-a-000"
        image = self.root / "sample.png"
        image.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/"
            "l1sAAAAASUVORK5CYII="))
        asset = self.workflow.attach_media("xiaohongshu", item_id, "image-1", image,
                                           position=0, kind="image", mime="image/png")
        asset.unlink()  # Simulate a removed file between download and export.
        try:
            output = self.workflow.export_all()
        except (ValueError, FileNotFoundError):
            return  # A clear export failure also avoids a false success report.
        manifest = json.loads(Path(output["authors"][0]["manifest"]).read_text(encoding="utf-8"))
        item = next(row for row in manifest["items"] if row["item_id"] == item_id)
        self.assertEqual(output["authors"][0]["missing_registered_assets"], 1)
        self.assertEqual(manifest["missing_registered_assets"], 1)
        failures = json.loads(Path(output["authors"][0]["failures"]).read_text(encoding="utf-8"))
        self.assertTrue(any(entry["reason"] == "registered_asset_missing_or_corrupt"
                            for entry in failures["entries"]))
        for row in item["assets"]:
            path = self.root / "archive" / row["relative_path"]
            self.assertEqual(row["state"], "missing")
            self.assertFalse(path.is_file())


if __name__ == "__main__":
    unittest.main()
