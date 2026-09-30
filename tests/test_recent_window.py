"""Recent-window subscriptions with fake transport and temporary data only."""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
import sys
import sqlite3
import tempfile
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from creator_archive.service import WorkspaceService, PlatformAccessPaused
from creator_archive.validation import AdapterFailure, Item, Page
from test_xhs_integration import AUTHOR, PROFILE, IDS, FakeXhsHttpTransport


class RecentWindowTests(unittest.TestCase):
    def setUp(self):
        FakeXhsHttpTransport.reset()
        self.module = ModuleType("creator_archive.adapters.xhs_http")
        self.module.XhsHttpTransport = FakeXhsHttpTransport
        self.transport_patch = patch.dict(sys.modules, {self.module.__name__: self.module})
        self.transport_patch.start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "sources.json").write_text(json.dumps({
            "xiaohongshu/*": {"kind": "xhs_http", "api_base": "http://127.0.0.1:1"}
        }), encoding="utf-8")
        self.service = WorkspaceService(self.root)
        self.service.subscribe(PROFILE)

    def tearDown(self):
        self.service.wait(30)
        self.service.close()
        self.temp.cleanup()
        self.transport_patch.stop()

    def job(self, job_id):
        return next(row for row in self.service.workspace()["runs"] if row["id"] == job_id)

    def start(self, mode="source_refresh"):
        result = self.service.start(mode, "xiaohongshu", AUTHOR)
        self.service.wait(30)
        return self.job(result["job_id"])

    def test_thirty_items_one_page_body_images_video_and_local_export(self):
        item_ids = [f"{number:024x}" for number in range(1, 31)]
        FakeXhsHttpTransport.pages[0] = Page(tuple(Item(item_id, AUTHOR, "2026-09-01") for item_id in item_ids), "page-2", True)
        job = self.start("full")
        self.assertEqual((job["mode"], job["state"], job["listed_count"]), ("recent_window", "succeeded", 30), job)
        self.assertEqual(FakeXhsHttpTransport.page_requests, [None])
        self.assertEqual(set(FakeXhsHttpTransport.detail_requests), set(item_ids))
        self.assertEqual(job["registered_media"], {"image": 30, "video": 1})
        self.assertEqual((job["coverage"], job["list_finished"], job["pages"]), ("recent_window_only", False, 1))
        self.assertIn("可能遗漏", job["window_warning"])
        with self.service.workflow.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM items WHERE detail_state='complete'").fetchone()[0], 30)
            run = db.execute("SELECT cursor,terminal_evidence FROM runs WHERE id=?", (job["run_id"],)).fetchone()
            self.assertEqual(tuple(run), (None, None))
            for asset in db.execute("SELECT * FROM assets"):
                self.assertTrue((self.service.workflow.archive_root / asset["relative_path"]).is_file())
        manifest = json.loads(Path(job["export"]["authors"][0]["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual({item["item_id"] for item in manifest["items"]}, set(item_ids))
        self.assertEqual(self.service.job_scan(job["id"])["kind"], "recent_window_scan")

    def test_refresh_skips_all_existing_ids_and_archives_only_new(self):
        first = self.start("author_archive")
        self.assertEqual(first["state"], "succeeded")
        before_details = list(FakeXhsHttpTransport.detail_requests)
        before_downloads = list(FakeXhsHttpTransport.downloads)
        unchanged = self.start("latest")
        self.assertEqual(unchanged["reason"], "recent_window_unchanged")
        self.assertEqual(unchanged["skipped_existing_count"], 2)
        self.assertEqual(FakeXhsHttpTransport.detail_requests, before_details)
        self.assertEqual(FakeXhsHttpTransport.downloads, before_downloads)
        new_id = IDS[2]
        FakeXhsHttpTransport.pages[0] = Page((Item(new_id, AUTHOR, "2026-09-30"), Item(IDS[0], AUTHOR, "2026-09-01")), "ignored", True)
        refreshed = self.start()
        self.assertEqual((refreshed["state"], refreshed["target_count"], refreshed["skipped_existing_count"]), ("succeeded", 1, 1))
        self.assertEqual(FakeXhsHttpTransport.detail_requests, before_details + [new_id])
        self.assertEqual(FakeXhsHttpTransport.page_requests, [None, None, None])

    def test_existing_partial_body_and_assets_are_preserved_and_difference_recorded(self):
        FakeXhsHttpTransport.fail_media_once = True
        first = self.start()
        self.assertEqual(first["state"], "partial")
        with self.service.workflow.connect() as db:
            previous = dict(db.execute("SELECT * FROM items WHERE item_id=?", (IDS[0],)).fetchone())
            assets = [dict(row) for row in db.execute("SELECT * FROM assets")]
        FakeXhsHttpTransport.edited_text[IDS[0]] = "变更正文不能可靠识别，保持本地正文"
        FakeXhsHttpTransport.pages[0] = Page((SimpleNamespace(item_id=IDS[0], author_id=AUTHOR, published_at="2026-09-30", title="变更列表标题"),), None, False)
        before = len(FakeXhsHttpTransport.detail_requests)
        refreshed = self.start()
        self.assertEqual((refreshed["reason"], refreshed["listed_difference_count"]), ("recent_window_unchanged", 1))
        self.assertEqual(len(FakeXhsHttpTransport.detail_requests), before)
        with self.service.workflow.connect() as db:
            self.assertEqual(dict(db.execute("SELECT * FROM items WHERE item_id=?", (IDS[0],)).fetchone()), previous)
            self.assertEqual([dict(row) for row in db.execute("SELECT * FROM assets")], assets)
            difference = json.loads(db.execute("SELECT difference_json FROM recent_window_observations WHERE job_id=?", (refreshed["id"],)).fetchone()[0])
            self.assertEqual(difference["title"]["listed"], "变更列表标题")

    def test_partial_retry_after_restart_reuses_snapshot_and_only_missing_media(self):
        FakeXhsHttpTransport.fail_media_once = True
        first = self.start("full")
        self.assertEqual(first["state"], "partial")
        self.service.close()
        self.service = WorkspaceService(self.root)
        self.service.resume(first["id"])
        self.service.wait(30)
        finished = self.job(first["id"])
        self.assertEqual(finished["state"], "succeeded", finished)
        self.assertEqual(FakeXhsHttpTransport.page_requests, [None])
        self.assertEqual(FakeXhsHttpTransport.downloads.count(f"memory://{IDS[0]}/image"), 1)
        self.assertEqual(len(FakeXhsHttpTransport.downloads), 4)

    def test_missing_saved_references_never_rereads_old_first_page(self):
        FakeXhsHttpTransport.fail_media_once = True
        first = self.start()
        self.service.close()
        for path in (self.root / "private" / "xhs-page-references").rglob("*.json"):
            path.unlink()
        self.service = WorkspaceService(self.root)
        self.service.resume(first["id"])
        self.service.wait(30)
        self.assertEqual(FakeXhsHttpTransport.page_requests, [None])
        self.assertEqual(self.job(first["id"])["state"], "partial")

    def test_current_window_does_not_require_cursor_or_history_terminal(self):
        FakeXhsHttpTransport.pages[0] = Page(tuple(Item(item_id, AUTHOR, "") for item_id in IDS[:2]), None, True)
        job = self.start()
        self.assertEqual(job["state"], "succeeded", job)
        self.assertFalse(job["list_finished"])
        self.assertEqual(FakeXhsHttpTransport.page_requests, [None])

    def test_batch_routes_never_reuse_old_deep_history(self):
        now = time.time()
        with self.service.workflow.connect() as db:
            batch = db.execute("INSERT INTO batches(mode,created_at) VALUES('full',?)", (now,)).lastrowid
            run = db.execute("INSERT INTO runs(batch_id,platform,author_id,mode,adapter_version,pages,cursor,state,updated_at) VALUES(?,'xiaohongshu',?,'full',?,7,'page-8','partial',?)", (batch, AUTHOR, FakeXhsHttpTransport.version, now)).lastrowid
            old = db.execute("INSERT INTO jobs(platform,author_id,mode,state,run_id,created_at,updated_at) VALUES('xiaohongshu',?,'full','partial',?,?,?)", (AUTHOR, run, now, now)).lastrowid
        with self.assertRaisesRegex(ValueError, "旧深分页任务已停用"):
            self.service.resume(old)
        self.assertFalse(self.job(old)["can_resume"])
        batch = self.service.start("all_archive")
        self.service.wait(30)
        self.assertNotIn(old, batch["reused_job_ids"])
        self.assertEqual(self.job(batch["job_ids"][0])["mode"], "recent_window")
        self.assertEqual(FakeXhsHttpTransport.page_requests, [None])
        with self.service.workflow.connect() as db:
            self.assertEqual(tuple(db.execute("SELECT pages,cursor,state FROM runs WHERE id=?", (run,)).fetchone()), (7, "page-8", "partial"))

    def test_selected_and_unscoped_refresh_routes_use_recent_window(self):
        batch = self.service.start("selected_archive", selected_authors=[{"platform": "xiaohongshu", "author_id": AUTHOR}])
        self.service.wait(30)
        self.assertEqual(self.job(batch["job_ids"][0])["mode"], "recent_window")
        result = self.service.start("source_refresh")
        self.service.wait(30)
        self.assertEqual(self.job(result["job_id"])["reason"], "recent_window_unchanged")

    def test_invalid_cross_author_page_does_not_commit_items_or_download(self):
        FakeXhsHttpTransport.pages[0] = Page((Item(IDS[0], "b" * 24, ""),), None, False)
        job = self.start()
        self.assertEqual(job["reason"], "identity_mismatch")
        self.assertEqual(FakeXhsHttpTransport.detail_requests, [])
        self.assertEqual(FakeXhsHttpTransport.downloads, [])
        self.assertEqual(self.service.workspace()["stats"]["items"], 0)

    def test_business_failure_stops_before_next_work_and_retains_window(self):
        FakeXhsHttpTransport.unavailable_items = {IDS[0]}
        job = self.start()
        self.assertEqual(job["reason"], "unavailable")
        self.assertEqual(FakeXhsHttpTransport.detail_requests, [IDS[0]])
        self.assertEqual(FakeXhsHttpTransport.page_requests, [None])
        self.assertEqual(job["listed_count"], 2)

    def test_no_artificial_pause_is_recreated_and_schedule_stays_off(self):
        self.assertFalse((self.root / "xhs-network-paused.json").exists())
        self.assertIsNone(self.service._platform_access_reason("xiaohongshu"))
        self.assertFalse(self.service.refresh_schedule()["enabled"])
        self.assertIsNone(self.service._scheduler)

    def test_every_legacy_network_entry_converts_new_work_and_retires_old_jobs(self):
        for mode in ("full", "author_archive", "page_archive", "demo_archive", "latest", "source_refresh"):
            with self.subTest(mode=mode):
                job = self.start(mode)
                self.assertEqual(job["mode"], "recent_window")
                with self.service.workflow.connect() as db:
                    old = db.execute("INSERT INTO jobs(platform,author_id,mode,state,created_at,updated_at) VALUES('xiaohongshu',?,?,'partial',?,?)", (AUTHOR, mode, time.time(), time.time())).lastrowid
                    db.execute("INSERT INTO job_items(job_id,platform,item_id) VALUES(?,'xiaohongshu',?)", (old, IDS[0]))
                before = list(FakeXhsHttpTransport.detail_requests)
                with self.assertRaisesRegex(ValueError, "旧深分页任务已停用"):
                    self.service.resume(old)
                self.assertTrue(self.job(old)["retired"])
                self.assertEqual(FakeXhsHttpTransport.detail_requests, before)
        self.assertEqual(FakeXhsHttpTransport.page_requests, [None] * 6)

    def test_old_batch_members_cannot_be_reused_by_new_recent_batch(self):
        now = time.time()
        with self.service.workflow.connect() as db:
            batch_id = db.execute("INSERT INTO batches(mode,created_at) VALUES('all_archive',?)", (now,)).lastrowid
            old = db.execute("INSERT INTO jobs(platform,author_id,mode,state,created_at,updated_at) VALUES('xiaohongshu',?,'full','partial',?,?)", (AUTHOR, now, now)).lastrowid
            db.execute("INSERT INTO archive_batch_members VALUES(?,'xiaohongshu',?,?,NULL)", (batch_id, AUTHOR, old))
        new_batch = self.service.start("all_archive")
        self.service.wait(30)
        self.assertNotEqual(new_batch["batch_id"], batch_id)
        self.assertFalse(new_batch["reused"])
        self.assertEqual(self.job(new_batch["job_ids"][0])["mode"], "recent_window")

    def test_identity_failure_in_detail_latches_stop_for_other_authors(self):
        def incorrect_identity(transport, author_id, item_id, source_url=""):
            FakeXhsHttpTransport.detail_requests.append(item_id)
            raise AdapterFailure("identity_mismatch")
        with patch.object(FakeXhsHttpTransport, "detail", incorrect_identity):
            job = self.start()
        self.assertEqual(job["reason"], "identity_mismatch")
        self.assertEqual(FakeXhsHttpTransport.detail_requests, [IDS[0]])
        with self.assertRaises(PlatformAccessPaused):
            self.service.start("source_refresh", "xiaohongshu", AUTHOR)

    def test_single_local_export_requires_current_confirmed_subscription(self):
        self.start()
        self.service.cancel_subscription("xiaohongshu", AUTHOR)
        with self.assertRaisesRegex(ValueError, "仍订阅"):
            self.service.start("archive", "xiaohongshu", AUTHOR)
        self.service.resubscribe("xiaohongshu", AUTHOR)
        with self.service.workflow.connect() as db:
            db.execute("UPDATE subscriptions SET identity_evidence='observed_browser_exact_note',enabled=0 WHERE platform='xiaohongshu'")
            db.execute("DELETE FROM subscription_confirmations")
        with self.assertRaisesRegex(ValueError, "已确认"):
            self.service.start("archive", "xiaohongshu", AUTHOR)

    def test_populated_legacy_database_backup_verified_before_observation_table(self):
        self.start()
        self.service.close()
        with closing(sqlite3.connect(self.root / "archive.sqlite3")) as db:
            # Drop only the new table in this synthetic fixture to model an old DB.
            db.execute("DROP TABLE recent_window_observations")
            db.commit()
            expected = db.execute("SELECT item_id,detail_text FROM items ORDER BY item_id").fetchall()
        self.service = WorkspaceService(self.root)
        backup = self.service.recent_window_migration_backup
        self.assertTrue(Path(backup).is_file())
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT item_id,detail_text FROM items ORDER BY item_id").fetchall(), expected)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='recent_window_observations'").fetchone())


if __name__ == "__main__":
    unittest.main()
