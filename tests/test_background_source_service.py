"""End-to-end local HTTP source contract; no platform account is contacted."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tests.test_feed_http import _Handler, FeedHttpTests, AUTHOR, PROFILE
from creator_archive.service import WorkspaceService


class BackgroundSourceServiceTests(FeedHttpTests):
    def test_source_added_after_blocked_batch_creates_new_scope_without_rewriting_old_batch(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = WorkspaceService(Path(temporary))
            service.workflow.subscribe("xiaohongshu", AUTHOR, "Synthetic", verified=True,
                                       evidence="synthetic fixture")
            old = service.start("all_archive")
            self.assertEqual(old["job_ids"], [])
            service.set_source("xiaohongshu", AUTHOR, self.base + "/feed")
            service._spawn = lambda _: None
            new = service.start("all_archive")
            self.assertNotEqual(old["batch_id"], new["batch_id"])
            self.assertEqual(len(new["job_ids"]), 1)
            with service.workflow.connect() as db:
                self.assertEqual(db.execute("SELECT job_id FROM archive_batch_members WHERE batch_id=?",
                                            (old["batch_id"],)).fetchone()[0], None)
            service.close()

    def test_six_items_history_content_media_export_and_incremental_reuse(self):
        ids = [f"{i + 1:024x}" for i in range(6)]
        image = b"\x89PNG\r\n\x1a\n" + b"saved-image"
        _Handler.routes["/image.png"] = (200, "image/png", image)
        def entries(numbers):
            return [{"url": f"https://www.xiaohongshu.com/explore/{ids[i]}",
                     "author": {"url": PROFILE},
                     "title": f"Note {i}", "content_html": f"<p>Body {i}</p><img src='{self.base}/image.png'>"}
                    for i in numbers]
        self.route_json("/feed", {"version": "https://jsonfeed.org/version/1.1",
                                  "home_page_url": PROFILE, "items": entries(range(4)),
                                  "next_url": "/feed?page=2"})
        self.route_json("/feed?page=2", {"version": "https://jsonfeed.org/version/1.1",
                                         "home_page_url": PROFILE, "items": entries(range(4, 6)),
                                         "history_complete": True})
        with tempfile.TemporaryDirectory() as temporary:
            service = WorkspaceService(Path(temporary))
            service.subscribe(PROFILE, "Synthetic")
            service.set_source("xiaohongshu", AUTHOR, self.base + "/feed")
            self.assertTrue(service.verify("xiaohongshu", AUTHOR)["identity_verified"])
            started = service.start("author_archive", "xiaohongshu", AUTHOR)
            service.wait(30)
            snapshot = service.workspace()
            job = next(j for j in snapshot["runs"] if j["id"] == started["job_id"])
            self.assertEqual(job["state"], "succeeded", job)
            self.assertEqual((job["listed_count"], job["item_count"]), (6, 6))
            self.assertTrue(job["list_finished"])
            author = next(s for s in snapshot["subscriptions"] if s["author_id"] == AUTHOR)
            self.assertEqual((author["detail_count"], author["registered_media"].get("image"),
                              author["registered_media"].get("video"), author["media_partial_item_count"]),
                             (6, 6, None, 0))
            self.assertTrue(author["source_terminal_observed"])
            content_jobs = [j for j in snapshot["runs"] if j.get("parent_job_id") == started["job_id"]]
            self.assertEqual(sum(j["body_saved_count"] for j in content_jobs), 6)
            self.assertEqual(sum(j["registered_media"].get("image", 0) for j in content_jobs), 6)
            with service.workflow.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM items WHERE platform='xiaohongshu'").fetchone()[0], 6)
                self.assertEqual(db.execute("SELECT count(*) FROM items WHERE detail_state='complete'").fetchone()[0], 6)
                self.assertEqual(db.execute("SELECT count(*) FROM assets").fetchone()[0], 6)
                before = {row["item_id"]: (row["relative_path"], row["sha256"]) for row in db.execute("SELECT * FROM assets")}
            exported = job["export"]["authors"][0]
            manifest = json.loads(Path(exported["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["items"]), 6)
            self.assertEqual([item["item_id"] for item in manifest["items"]], ids)
            self.assertIsNone(service._transport)  # The legacy browser was never created.

            refresh = service.start("source_refresh", "xiaohongshu", AUTHOR)
            service.wait(30)
            updated = next(j for j in service.workspace()["runs"] if j["id"] == refresh["job_id"])
            self.assertEqual((updated["state"], updated["reason"]), ("succeeded", "source_refresh_unchanged"))
            with service.workflow.connect() as db:
                after = {row["item_id"]: (row["relative_path"], row["sha256"]) for row in db.execute("SELECT * FROM assets")}
            self.assertEqual(before, after)
            service.close()


if __name__ == "__main__":
    unittest.main()
