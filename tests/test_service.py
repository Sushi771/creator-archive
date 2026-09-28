"""Product persistence regressions using synthetic sources, never live evidence."""
from contextlib import closing
from hashlib import sha256
from pathlib import Path
import tempfile
import time
import unittest
import sqlite3
from unittest.mock import patch

from fastapi.testclient import TestClient

from creator_archive.app import create_app
from creator_archive.service import PlatformCooldown, WorkspaceService
from creator_archive.validation import AdapterFailure, SyntheticAdapter
from creator_archive.workflow import ArchiveWorkflow


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = WorkspaceService(self.root / "workspace", validation_root=self.root / "sources", adapter_factory=lambda _: SyntheticAdapter())
        self.addCleanup(self.service.wait)

    def seed(self, author="author-a", platform="xiaohongshu"):
        self.service.workflow.subscribe(platform, author, author, verified=True, evidence="synthetic test")

    def source(self, directory, author):
        workflow = ArchiveWorkflow(self.root / "sources" / directory)
        workflow.subscribe("xiaohongshu", author, author, verified=True, evidence="synthetic source")
        workflow.run_all({"xiaohongshu": SyntheticAdapter()})
        return workflow

    def test_unverified_link_never_becomes_verified_or_copies_tokens(self):
        sub = self.service.subscribe("https://www.xiaohongshu.com/user/profile/" + "a" * 24 + "?xsec_token=SECRET_SENTINEL", "候选")
        self.assertFalse(sub["identity_verified"])
        self.assertEqual(self.service.workflow.subscriptions(), [])
        self.assertNotIn(b"SECRET_SENTINEL", self.service.workflow.db_path.read_bytes())
        self.service.start("archive")
        self.service.wait()
        self.assertEqual(self.service.workspace()["runs"][0]["reason"], "identity_unverified")

    def test_full_includes_paused_and_survives_restart_with_pagination(self):
        self.seed()
        self.seed("author-b")
        self.service.toggle("xiaohongshu", "author-b", False)
        self.service.start("full")
        self.service.wait()
        view = self.service.workspace()
        self.assertEqual(len(view["runs"]), 2)
        self.assertTrue(all(j["state"] == "succeeded" and j["pages"] == 4 for j in view["runs"]))
        total = view["stats"]["items"]
        self.assertGreater(total, 50)
        ids = []
        for offset in range(0, total, 17):
            ids.extend(i["item_id"] for i in self.service.items(offset=offset, limit=17)["items"])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), total)
        restarted = WorkspaceService(self.service.root)
        self.assertEqual(restarted.workspace()["stats"]["items"], total)

    def test_cancel_keeps_archive_checkpoint_and_manual_note_then_resubscribes(self):
        author = "a" * 24
        self.seed(author)
        self.service.start("full", "xiaohongshu", author)
        self.service.wait()
        export = self.service.workflow.export_all()["authors"][0]
        manual = Path(export["manifest"]).parent / "my-notes.md"
        manual.write_text("my annotation", encoding="utf-8")
        with self.service.workflow.connect() as db:
            before = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
                      for table in ("subscriptions", "items", "runs", "pages", "jobs", "assets")}
        saved = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                 for path in (Path(export["manifest"]), manual)}

        cancelled = self.service.cancel_subscription("xiaohongshu", author)
        self.assertFalse(cancelled["subscribed"])
        self.assertFalse(next(s for s in self.service.workspace()["subscriptions"] if s["author_id"] == author)["subscribed"])
        with self.assertRaisesRegex(ValueError, "取消订阅"):
            self.service.start("full", "xiaohongshu", author)
        self.service.start("archive", "xiaohongshu", author)
        self.service.wait()
        restarted = WorkspaceService(self.service.root)
        self.assertFalse(next(s for s in restarted.workspace()["subscriptions"] if s["author_id"] == author)["subscribed"])
        with restarted.workflow.connect() as db:
            for table in ("subscriptions", "items", "runs", "pages", "assets"):
                rows = [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
                self.assertEqual(rows[:len(before[table])], before[table])
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in saved}, saved)
        restored = restarted.resubscribe("xiaohongshu", author)
        self.assertTrue(restored["subscribed"])
        self.assertTrue(restored["enabled"])
        self.assertEqual(manual.read_text(encoding="utf-8"), "my annotation")

    def test_cancel_api_intent_and_confirmation_do_not_reactivate_silently(self):
        root = self.root / "cancel-api"
        app = create_app(root)
        headers = {"X-Creator-Archive": "local-validation"}
        author = "b" * 24
        pending = "c" * 24
        with TestClient(app) as client:
            service = app.state.service
            service.workflow.subscribe("xiaohongshu", author, "Saved name", verified=True, evidence="synthetic")
            service.toggle("xiaohongshu", author, False)
            response = client.post("/api/subscriptions/cancel", json={"platform": "xiaohongshu", "author_id": author}, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["subscribed"])
            self.assertFalse(next(s for s in client.get("/api/workspace").json()["subscriptions"] if s["author_id"] == author)["subscribed"])
            self.assertEqual(client.post("/api/subscriptions/toggle", json={"platform": "xiaohongshu", "author_id": author, "enabled": True}, headers=headers).status_code, 422)
            self.assertEqual(client.post("/api/subscriptions/resubscribe", json={"platform": "xiaohongshu", "author_id": author}, headers=headers).status_code, 200)
            self.assertEqual(next(s for s in service.workspace()["subscriptions"] if s["author_id"] == author)["display_name"], "Saved name")
            service.toggle("xiaohongshu", author, False)
            self.assertEqual(client.post("/api/subscriptions/resubscribe", json={"platform": "xiaohongshu", "author_id": author}, headers=headers).status_code, 422)
            self.assertFalse(next(s for s in service.workspace()["subscriptions"] if s["author_id"] == author)["enabled"])
            service.workflow.subscribe("xiaohongshu", pending, "Observed", verified=True, evidence="observed_browser_exact_note")
            service.workflow.set_enabled("xiaohongshu", pending, False)
            service.cancel_subscription("xiaohongshu", pending)
            self.assertFalse(service.resubscribe("xiaohongshu", pending)["enabled"])
            self.assertTrue(next(s for s in service.workspace()["subscriptions"] if s["author_id"] == pending)["subscription_confirmation_required"])
            intent = "d" * 24
            service.subscribe(f"https://www.xiaohongshu.com/user/profile/{intent}", "My intent")
            service.cancel_subscription("xiaohongshu", intent)
            self.assertFalse(next(s for s in service.workspace()["subscriptions"] if s["author_id"] == intent)["subscribed"])
            service.subscribe(f"https://www.xiaohongshu.com/user/profile/{intent}")
            self.assertTrue(next(s for s in service.workspace()["subscriptions"] if s["author_id"] == intent)["subscribed"])

    def test_cancel_migration_backs_up_old_data(self):
        self.seed("legacy-author")
        with self.service.workflow.connect() as db:
            before = [tuple(row) for row in db.execute("SELECT * FROM subscriptions")]
            db.execute("DROP TABLE subscription_cancellations")
        restarted = WorkspaceService(self.service.root)
        self.assertTrue(Path(restarted.workflow.cancellation_migration_backup).is_file())
        with closing(sqlite3.connect(restarted.workflow.cancellation_migration_backup)) as backup:
            self.assertEqual(backup.execute("SELECT * FROM subscriptions").fetchall(), before)
            self.assertIsNone(backup.execute("SELECT 1 FROM sqlite_master WHERE name='subscription_cancellations'").fetchone())
        with restarted.workflow.connect() as db:
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM subscriptions")], before)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_tags_follow_stable_author_id_without_changing_archive_or_lifecycle(self):
        author = "t" * 24
        self.seed(author)
        self.service.start("full", "xiaohongshu", author)
        self.service.wait()
        export = self.service.workflow.export_all()["authors"][0]
        manual = Path(export["manifest"]).parent / "my-notes.md"
        manual.write_text("manual tag note", encoding="utf-8")
        self.service._spawn = lambda job_id: None
        batch = self.service.start("all_archive")
        with self.service.workflow.connect() as db:
            baseline = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
                        for table in ("subscriptions", "items", "runs", "pages", "jobs", "archive_batch_members", "assets")}
        saved = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                 for path in (Path(export["manifest"]), manual)}
        self.assertEqual(self.service.set_subscription_tags("xiaohongshu", author, ["家庭", " 研究 ", "家庭"])["tags"], ["家庭", "研究"])
        self.service.workflow.subscribe("xiaohongshu", author, "Renamed", verified=True, evidence="synthetic test")
        self.service.toggle("xiaohongshu", author, False)
        self.service.cancel_subscription("xiaohongshu", author)
        with self.service.workflow.connect() as db:
            for table in ("items", "runs", "pages", "jobs", "archive_batch_members", "assets"):
                self.assertEqual([tuple(row) for row in db.execute(f"SELECT * FROM {table}")], baseline[table])
        restarted = WorkspaceService(self.service.root)
        sub = next(s for s in restarted.workspace()["subscriptions"] if s["author_id"] == author)
        self.assertEqual((sub["display_name"], sub["tags"], sub["subscribed"]), ("Renamed", ["家庭", "研究"], False))
        self.assertEqual(restarted.resubscribe("xiaohongshu", author)["subscribed"], True)
        self.assertEqual(next(s for s in restarted.workspace()["subscriptions"] if s["author_id"] == author)["tags"], ["家庭", "研究"])
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in saved}, saved)
        with restarted.workflow.connect() as db:
            for table in ("items", "pages", "archive_batch_members", "assets"):
                self.assertEqual([tuple(row) for row in db.execute(f"SELECT * FROM {table}")], baseline[table])
            self.assertEqual(db.execute("SELECT count(*) FROM archive_batch_members WHERE batch_id=?", (batch["batch_id"],)).fetchone()[0], 1)
        self.assertEqual(restarted.set_subscription_tags("xiaohongshu", author, [])["tags"], [])
        self.assertEqual(next(s for s in restarted.workspace()["subscriptions"] if s["author_id"] == author)["tags"], [])

    def test_tags_api_intent_identity_and_invalid_update(self):
        app = create_app(self.root / "tags-api")
        headers = {"X-Creator-Archive": "local-validation"}
        author = "a" * 24
        with TestClient(app) as client:
            service = app.state.service
            service.subscribe(f"https://www.xiaohongshu.com/user/profile/{author}", "Candidate")
            body = {"platform": "xiaohongshu", "author_id": author, "tags": ["读书", "家长"]}
            self.assertEqual(client.post("/api/subscriptions/tags", json=body, headers=headers).status_code, 200)
            service.workflow.subscribe("xiaohongshu", author, "Verified", verified=True, evidence="synthetic")
            service.workflow.subscribe("wechat", author, "Same ID other platform", verified=True, evidence="synthetic")
            sub = next(s for s in client.get("/api/workspace").json()["subscriptions"] if s["platform"] == "xiaohongshu" and s["author_id"] == author)
            self.assertEqual(sub["tags"], ["读书", "家长"])
            self.assertEqual(sub["display_name"], "Verified")
            other = next(s for s in service.workspace()["subscriptions"] if s["platform"] == "wechat")
            self.assertEqual(other["tags"], [])
            for tags in (["ok", ""], ["a" * 33], ["bad,tag"], ["x"] * 11):
                self.assertEqual(client.post("/api/subscriptions/tags", json={**body, "tags": tags}, headers=headers).status_code, 422)
            self.assertEqual(next(s for s in service.workspace()["subscriptions"] if s["platform"] == "xiaohongshu" and s["author_id"] == author)["tags"], ["读书", "家长"])
            self.assertEqual(client.post("/api/subscriptions/tags", json={**body, "author_id": "missing"}, headers=headers).status_code, 404)
            self.assertEqual(client.post("/api/subscriptions/tags", json=body).status_code, 403)

    def test_tags_migration_has_verified_backup_and_rollback_source(self):
        self.seed("legacy-tag-author")
        with self.service.workflow.connect() as db:
            before = [tuple(row) for row in db.execute("SELECT * FROM subscriptions")]
            db.execute("DROP TABLE subscription_tags")
        restarted = WorkspaceService(self.service.root)
        backup_path = Path(restarted.workflow.tags_migration_backup)
        self.assertTrue(backup_path.is_file())
        with closing(sqlite3.connect(backup_path)) as backup:
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(backup.execute("SELECT * FROM subscriptions").fetchall(), before)
            self.assertIsNone(backup.execute("SELECT 1 FROM sqlite_master WHERE name='subscription_tags'").fetchone())
        with restarted.workflow.connect() as db:
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM subscriptions")], before)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_interrupted_job_reuses_exact_run_and_cursor(self):
        self.seed()
        adapter = SyntheticAdapter(2, "timeout")
        self.service.adapter_factory = lambda _: adapter
        job_id = self.service.start("full")["job_id"]
        self.service.wait()
        self.assertEqual(self.service.workspace()["runs"][0]["pages"], 2)
        with self.service.workflow.connect() as db:
            before = dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (job_id,))
        restarted = WorkspaceService(self.service.root, adapter_factory=lambda _: SyntheticAdapter())
        self.addCleanup(restarted.wait)
        self.assertEqual(restarted.workspace()["runs"][0]["state"], "interrupted")
        restarted.resume(job_id)
        restarted.wait()
        self.assertEqual(restarted.workspace()["runs"][0]["pages"], 4)
        self.assertEqual(restarted.workspace()["runs"][0]["state"], "succeeded")
        with restarted.workflow.connect() as db:
            self.assertEqual(db.execute("SELECT run_id FROM jobs WHERE id=?", (job_id,)).fetchone()[0], before["run_id"])

    def test_resume_cannot_overlap_another_job_for_same_author(self):
        self.seed()
        self.service._spawn = lambda job_id: None
        first = self.service.start("full")["job_id"]
        with self.service.workflow.connect() as db:
            db.execute("UPDATE jobs SET state='interrupted' WHERE id=?", (first,))
        self.service.start("archive")
        with self.assertRaisesRegex(ValueError, "其他任务"):
            self.service.resume(first)

    def test_platform_cooldown_survives_restart_and_blocks_other_authors(self):
        class Limited(SyntheticAdapter):
            calls = 0
            def page(self, author, cursor):
                self.calls += 1
                raise AdapterFailure("rate_limited", 60)
        limited = Limited()
        self.service.adapter_factory = lambda _: limited
        self.seed()
        self.seed("author-b")
        self.service.start("full")
        self.service.wait()
        self.assertEqual(limited.calls, 1, "queued second author must stop before network")
        jobs = self.service.workspace()["runs"]
        self.assertTrue(all(j["state"] == "rate_limited" for j in jobs))
        self.assertTrue(all(j["retry_at"] > time.time() + 55 for j in jobs))
        restarted = WorkspaceService(self.service.root, adapter_factory=lambda _: self.fail("cooldown must prevent transport creation"))
        self.addCleanup(restarted.wait)
        for action in (lambda: restarted.start("full"), lambda: restarted.resume(jobs[0]["id"]),
                       lambda: restarted.verify("xiaohongshu", "author-b"), restarted.open_login):
            with self.assertRaises(PlatformCooldown):
                action()
        restarted.start("archive")
        restarted.wait()
        self.assertTrue(all(j["state"] == "succeeded" for j in restarted.workspace()["runs"] if j["mode"] == "archive"))
        with restarted.workflow.connect() as db:
            db.execute("UPDATE runs SET retry_at=0")
            db.execute("UPDATE platform_cooldowns SET retry_at=0")
        restarted.adapter_factory = lambda _: SyntheticAdapter()
        restarted.start("full")
        restarted.wait()
        self.assertTrue(all(j["state"] == "succeeded" for j in restarted.workspace()["runs"][:2]))

    def test_verify_and_login_limit_without_run_is_persistent(self):
        class Limited:
            def verify_author(self, author):
                raise AdapterFailure("rate_limited", 60)
            def open_login(self):
                raise AdapterFailure("rate_limited", 60)
        for entry in ("verify", "login"):
            with self.subTest(entry=entry):
                root = self.root / entry
                service = WorkspaceService(root, adapter_factory=lambda _: Limited())
                author = "a" * 24
                service.subscribe(f"https://www.xiaohongshu.com/user/profile/{author}")
                with self.assertRaises(AdapterFailure):
                    service.verify("xiaohongshu", author) if entry == "verify" else service.open_login()
                self.assertEqual(service.workspace()["runs"], [])
                restarted = WorkspaceService(root, adapter_factory=lambda _: self.fail("no network during persisted cooldown"))
                for action in (restarted.open_login, lambda: restarted.verify("xiaohongshu", author), lambda: restarted.start("full")):
                    with self.assertRaises(PlatformCooldown):
                        action()

    def test_workflow_limit_uses_failure_time_and_keeps_injected_clock(self):
        clock = [100.0]
        class SlowLimited(SyntheticAdapter):
            def page(self, author, cursor):
                clock[0] = 200.0
                raise AdapterFailure("rate_limited", 60)
        for fixed_now, expected in ((None, 260.0), (100.0, 160.0)):
            with self.subTest(fixed_now=fixed_now):
                workflow = ArchiveWorkflow(self.root / str(expected))
                workflow.subscribe("xiaohongshu", "a", "a", verified=True, evidence="synthetic")
                workflow.subscribe("xiaohongshu", "b", "b", verified=True, evidence="synthetic")
                clock[0] = 100.0
                with patch("creator_archive.workflow.time.time", side_effect=lambda: clock[0]):
                    result = workflow.run_all({"xiaohongshu": SlowLimited()}, now=fixed_now)
                self.assertEqual([r["retry_at"] for r in result["runs"]], [expected, expected])

    def test_job_count_is_observed_page_snapshot_not_current_library(self):
        self.seed()
        first = self.service.start("full")["job_id"]
        self.service.wait()
        original = self.service.workspace()["runs"][0]["item_count"]
        self.service.start("archive")
        self.service.wait()
        with self.service.workflow.connect() as db:
            db.execute("INSERT INTO items(platform,item_id,author_id,published_at) VALUES('xiaohongshu','new-after-old-run','author-a','2026-09-27')")
        jobs = self.service.workspace()["runs"]
        self.assertEqual(next(j for j in jobs if j["id"] == first)["item_count"], original)
        self.assertEqual(jobs[0]["item_count"], original, "archive count is its export snapshot")
        self.assertTrue(all(j["library_item_count"] == original + 1 for j in jobs))

    def test_readonly_import_idempotence_and_archive_keeps_user_files(self):
        first = self.source("xhs-live-validation", "author-a")
        second = self.source("xhs-live-validation-b", "author-b")
        hashes = [sha256(w.db_path.read_bytes()).hexdigest() for w in (first, second)]
        self.assertEqual(len(self.service.import_validation()["imported"]), 2)
        total = self.service.workspace()["stats"]["items"]
        self.assertGreater(total, 0)
        self.assertEqual(self.service.import_validation()["imported"], [])
        self.assertEqual(hashes, [sha256(w.db_path.read_bytes()).hexdigest() for w in (first, second)])
        self.assertEqual(self.service.workspace()["stats"]["details"], 0)
        base = self.service.root / "archive/xiaohongshu/author-a"
        base.mkdir(parents=True)
        (base / "manifest.json").write_text("user manual notes", encoding="utf-8")
        self.service.toggle("xiaohongshu", "author-b", False)
        self.service.start("archive")
        self.service.wait()
        exports = [j for j in self.service.workspace()["runs"] if j["mode"] == "archive"]
        self.assertEqual(len(exports), 2)
        self.assertTrue(all(j["state"] == "succeeded" for j in exports))
        self.assertEqual((base / "manifest.json").read_text(encoding="utf-8"), "user manual notes")
        self.assertTrue(all(j["export"]["authors"][0]["details"] == 0 for j in exports))
        self.assertEqual(self.service.workspace()["stats"]["items"], total)

    def test_wechat_failure_isolated_and_no_adapter_request(self):
        self.seed(platform="wechat")
        self.seed("xhs")
        self.service.start("full")
        self.service.wait()
        jobs = {j["platform"]: j for j in self.service.workspace()["runs"]}
        self.assertEqual(jobs["wechat"]["reason"], "wechat_blocked")
        self.assertEqual(jobs["xiaohongshu"]["state"], "succeeded")

    def test_asset_filter_and_local_item_links(self):
        self.seed()
        self.service.start("full")
        self.service.wait()
        item = self.service.items()["items"][0]
        image = self.root / "sample.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
        self.service.workflow.attach_media("xiaohongshu", item["item_id"], "sample", image, position=0, kind="image", mime="image/png")
        selected = self.service.items(has_assets=True)
        self.assertEqual(selected["total"], 1)
        self.assertEqual(selected["items"][0]["asset_count"], 1)
        self.assertTrue(self.service.item("xiaohongshu", item["item_id"])["assets"][0]["url"].startswith("/archive/"))

    def test_item_detail_only_links_verified_registered_assets(self):
        self.seed()
        self.service.start("full")
        self.service.wait()
        item = self.service.items()["items"][0]
        source = self.root / "sample.png"
        original = b"\x89PNG\r\n\x1a\nsynthetic-detail"
        source.write_bytes(original)
        asset_path = self.service.workflow.attach_media("xiaohongshu", item["item_id"], "sample", source,
                                                        position=0, kind="image", mime="image/png")
        kept_path = self.service.workflow.attach_media("xiaohongshu", item["item_id"], "kept", source,
                                                       position=1, kind="image", mime="image/png")
        kept = (kept_path.read_bytes(), kept_path.stat().st_mtime_ns)
        manual = self.service.root / "archive" / "notes.json"
        manual.write_text("manual note", encoding="utf-8")
        preserved = (manual.read_bytes(), manual.stat().st_mtime_ns)
        with TestClient(create_app(self.service.root)) as client:
            url = f"/api/items/xiaohongshu/{item['item_id']}"
            valid = client.get(url).json()["assets"][0]
            old_asset_url = valid["url"]
            self.assertEqual(valid["state"], "complete")
            self.assertEqual(client.get(old_asset_url).content, original)
            asset_path.unlink()
            missing = client.get(url).json()["assets"][0]
            self.assertEqual(missing["state"], "missing")
            self.assertFalse(missing.get("url"))
            self.assertFalse(missing.get("archive_url"))
            self.assertEqual(client.get(old_asset_url).status_code, 404)
            asset_path.write_bytes(original[:-1] + b"X")
            corrupt = client.get(url).json()["assets"][0]
            self.assertEqual(corrupt["state"], "missing")
            self.assertFalse(corrupt.get("url"))
            self.assertEqual(client.get(old_asset_url).status_code, 404)
            self.assertEqual(client.get("/archive/" + kept_path.relative_to(self.service.root / "archive").as_posix()).content, original)
            self.assertEqual(client.get("/archive/notes.json").content, preserved[0])
            asset_path.write_bytes(original)
            recovered = client.get(url).json()["assets"][0]
            self.assertEqual(recovered["state"], "complete")
            self.assertEqual(client.get(recovered["url"]).content, original)
            self.assertEqual(client.get(old_asset_url).content, original)
        self.assertEqual((manual.read_bytes(), manual.stat().st_mtime_ns), preserved)
        self.assertEqual((kept_path.read_bytes(), kept_path.stat().st_mtime_ns), kept)

    def test_author_index_contains_all_items_and_preserves_edited_index(self):
        self.seed()
        with self.service.workflow.connect() as db:
            db.execute("UPDATE subscriptions SET display_name=?", ("<script>bad()</script>",))
        self.service.start("full")
        self.service.wait()
        all_items = self.service.items(limit=200)["items"]
        self.assertGreater(len(all_items), 50)
        first = all_items[0]
        self.service.workflow.save_detail("xiaohongshu", first["item_id"], first["author_id"], "正文", "https://www.xiaohongshu.com/explore/example")
        article_path = self.service.root / "archive/xiaohongshu/author-a" / first["item_id"] / "index.html"
        article_path.parent.mkdir(parents=True)
        article_path.write_text("edited article", encoding="utf-8")
        self.service.start("archive")
        self.service.wait()
        exported = self.service.workspace()["runs"][0]["export"]["authors"][0]
        index = Path(exported["index"])
        html = index.read_text(encoding="utf-8")
        self.assertEqual(html.count("<li>"), len(all_items))
        self.assertTrue(all(item["item_id"] in html for item in all_items))
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertIn("媒体预期总数未知", html)
        self.assertIn("明确末页", html)
        self.assertIn("index.", html)
        self.assertEqual(article_path.read_text(encoding="utf-8"), "edited article")
        self.assertEqual(self.service.workspace()["subscriptions"][0]["archive_url"], exported["index_url"])
        index.write_text("edited author index", encoding="utf-8")
        self.service.start("archive")
        self.service.wait()
        newest = self.service.workspace()["runs"][0]["export"]["authors"][0]
        self.assertNotEqual(newest["index"], str(index))
        self.assertEqual(index.read_text(encoding="utf-8"), "edited author index")
        self.assertEqual(Path(newest["index"]).read_text(encoding="utf-8"), html)

    def test_verify_failure_is_actionable_api_response(self):
        class NeedsLogin:
            def verify_author(self, author):
                raise AdapterFailure("needs_login")
        app = create_app(self.root / "api-verify")
        app.state.service.adapter_factory = lambda _: NeedsLogin()
        headers = {"x-creator-archive": "local-validation"}
        author = "a" * 24
        with TestClient(app) as client:
            client.post("/api/subscriptions", json={"text": f"https://www.xiaohongshu.com/user/profile/{author}"}, headers=headers)
            result = client.post("/api/subscriptions/verify", json={"platform": "xiaohongshu", "author_id": author}, headers=headers)
            self.assertEqual(result.status_code, 409)
            self.assertEqual(result.json()["reason"], "needs_login")
            self.assertFalse(client.get("/api/workspace").json()["subscriptions"][0]["identity_verified"])

    def test_api_writes_protected_and_archive_path_contained(self):
        app = create_app(self.root / "api")
        headers = {"x-creator-archive": "local-validation"}
        with TestClient(app) as client:
            for route in ("/api/subscriptions", "/api/jobs", "/api/import/validation", "/api/platforms/xiaohongshu/login"):
                self.assertEqual(client.post(route, json={}).status_code, 403)
                self.assertEqual(client.post(route, json={}, headers={**headers, "origin": "https://evil.invalid"}).status_code, 403)
            archive = app.state.service.root / "archive"
            archive.mkdir()
            (archive / "safe.json").write_text("{}")
            (archive.parent / "secret.json").write_text("private")
            self.assertEqual(client.get("/archive/safe.json").status_code, 200)
            self.assertEqual(client.get("/archive/..%2Fsecret.json").status_code, 404)
            self.assertEqual(client.get("/api/items?limit=0").status_code, 422)
            self.assertEqual(client.get("/api/items/xiaohongshu/missing").status_code, 404)
            self.assertEqual(client.post("/api/jobs", json={"mode": "bad"}, headers=headers).status_code, 422)
            self.assertEqual(client.get("/api/status").json()["mode"], "local_mvp")
            self.assertEqual(client.post("/api/server/stop", json={}).status_code, 403)
            self.assertEqual(client.post("/api/server/stop", json={}, headers=headers).status_code, 409)
            called = []
            app.state.request_shutdown = lambda: called.append(True)
            self.assertEqual(client.post("/api/server/stop", json={}, headers=headers).json()["state"], "stopping")
            self.assertEqual(called, [True])


if __name__ == "__main__":
    unittest.main()
