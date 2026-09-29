"""Isolated native-source service contract; no Xiaohongshu account is contacted."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
from types import ModuleType
import unittest
from unittest.mock import patch

from creator_archive.adapters.xhs import MediaCandidate
from creator_archive.service import WorkspaceService
from creator_archive.validation import AdapterFailure, Item, Page


AUTHOR = "a" * 24
PROFILE = f"https://www.xiaohongshu.com/user/profile/{AUTHOR}"
IDS = [f"{number:024x}" for number in range(1, 7)]
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic image"
MP4 = b"\x00\x00\x00\x18ftypisom" + b"synthetic video"


class FakeXhsHttpTransport:
    version = "xhs-http-test-v1"
    pages = []
    page_requests = []
    detail_requests = []
    downloads = []
    fail_media_once = False
    edited_text = {}

    @classmethod
    def reset(cls):
        cls.pages = [
            Page(tuple(Item(item_id, AUTHOR, "2026-09-01") for item_id in IDS[:2]), "page-2", True),
            Page(tuple(Item(item_id, AUTHOR, "2026-08-01") for item_id in IDS[2:4]), "page-3", True),
            Page(tuple(Item(item_id, AUTHOR, "2026-07-01") for item_id in IDS[4:]), None, False,
                 "source user_posted has_more=false"),
        ]
        cls.page_requests = []
        cls.detail_requests = []
        cls.downloads = []
        cls.fail_media_once = False
        cls.edited_text = {}

    def __init__(self, config, platform, author_id):
        assert config["kind"] == "xhs_http"
        assert (platform, author_id) == ("xiaohongshu", AUTHOR)
        self._refs = set()

    def verify_author(self, author_id):
        assert author_id == AUTHOR
        return {"author_id": AUTHOR, "display_name": "模拟博主"}

    def page(self, author_id, cursor):
        assert author_id == AUTHOR
        self.page_requests.append(cursor)
        page = self.pages[{None: 0, "page-2": 1, "page-3": 2}[cursor]]
        self._refs.update(item.item_id for item in page.items)
        return page

    def prepare_page_details(self, author_id, item_ids):
        assert author_id == AUTHOR
        return tuple(item_id for item_id in item_ids if item_id not in self._refs)

    def poll_latest(self):
        self._refs.update(IDS[:2])
        return [{"author_id": AUTHOR, "item_id": item_id,
                 "title": f"标题 {item_id[-1]}",
                 "source_url": f"https://www.xiaohongshu.com/explore/{item_id}"}
                for item_id in IDS[:2]]

    def detail(self, author_id, item_id, source_url=""):
        assert author_id == AUTHOR
        if item_id not in self._refs:
            raise AdapterFailure("reference_missing")
        self.detail_requests.append(item_id)
        media = [MediaCandidate("image", 0, f"memory://{item_id}/image")]
        if item_id == IDS[0]:
            media.append(MediaCandidate("video", 1, f"memory://{item_id}/video"))
        return {"author_id": AUTHOR, "item_id": item_id,
                "title": f"标题 {item_id[-1]}", "text": self.edited_text.get(item_id, f"正文 {item_id}"),
                "source_url": f"https://www.xiaohongshu.com/explore/{item_id}",
                "content_type": "video" if item_id == IDS[0] else "image",
                "published_at": "2026-09-01", "media": media, "missing": [],
                "metrics": {}, "source": "mock_xhs_http", "observed_at": time.time()}

    def download_media(self, candidate, target_dir):
        self.downloads.append(candidate.url)
        if type(self).fail_media_once and candidate.kind == "video":
            type(self).fail_media_once = False
            raise AdapterFailure("media_failed")
        target_dir = Path(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".mp4" if candidate.kind == "video" else ".png"
        target = target_dir / f"{candidate.asset_id}{suffix}"
        target.write_bytes(MP4 if candidate.kind == "video" else PNG)
        return {"path": str(target), "mime": "video/mp4" if candidate.kind == "video" else "image/png"}

    def close(self):
        pass


class TwoAuthorAccessTransport:
    version = "xhs-http-access-test-v1"
    requests = []
    authorized = False
    failure_reason = "needs_login"

    def __init__(self, config, platform, author_id):
        self.author_id = author_id

    def verify_author(self, author_id):
        if not type(self).authorized:
            raise AdapterFailure(type(self).failure_reason, retry_after=1)
        return {"author_id": author_id, "display_name": author_id}

    def page(self, author_id, cursor):
        type(self).requests.append(author_id)
        if author_id == AUTHOR and not type(self).authorized:
            raise AdapterFailure(type(self).failure_reason, retry_after=1)
        return Page((), None, False, "explicit test terminal")

    def prepare_page_details(self, author_id, item_ids):
        return ()

    def close(self):
        pass


class XhsIntegrationTests(unittest.TestCase):
    def setUp(self):
        FakeXhsHttpTransport.reset()
        module = ModuleType("creator_archive.adapters.xhs_http")
        module.XhsHttpTransport = FakeXhsHttpTransport
        self.patch = patch.dict(sys.modules, {module.__name__: module})
        self.patch.start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "sources.json").write_text(json.dumps({
            "xiaohongshu/*": {"kind": "xhs_http", "api_base": "http://127.0.0.1:1"}
        }), encoding="utf-8")
        self.service = WorkspaceService(self.root)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()
        self.patch.stop()

    def _author_job(self, job_id):
        return next(j for j in self.service.workspace()["runs"] if j["id"] == job_id)

    def test_profile_link_to_full_history_content_media_export_and_refresh(self):
        subscribed = self.service.subscribe(PROFILE)
        self.assertTrue(subscribed["identity_verified"])
        self.assertEqual(subscribed["source_kind"], "xhs_http")
        started = self.service.start("all_archive")
        self.service.wait(30)
        job = self._author_job(started["job_ids"][0])
        self.assertEqual((job["mode"], job["state"]), ("full", "succeeded"), job)
        self.assertEqual(FakeXhsHttpTransport.page_requests, [None, "page-2", "page-3"])
        self.assertEqual(len(FakeXhsHttpTransport.detail_requests), 6)
        with self.service.workflow.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM items WHERE detail_state='complete'").fetchone()[0], 6)
            self.assertEqual(db.execute("SELECT count(*) FROM assets WHERE kind='image'").fetchone()[0], 6)
            self.assertEqual(db.execute("SELECT count(*) FROM assets WHERE kind='video'").fetchone()[0], 1)
            run = db.execute("SELECT pages,coverage,terminal_evidence FROM runs WHERE id=?", (job["run_id"],)).fetchone()
            self.assertEqual((run["pages"], run["coverage"]), (3, "complete_for_accessible_scope"))
            self.assertTrue(run["terminal_evidence"])
        manifest = json.loads(Path(job["export"]["authors"][0]["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual({item["item_id"] for item in manifest["items"]}, set(IDS))
        downloads_before = len(FakeXhsHttpTransport.downloads)
        refreshed = self.service.start("source_refresh", "xiaohongshu", AUTHOR)
        self.service.wait(30)
        self.assertEqual(self._author_job(refreshed["job_id"])["state"], "succeeded")
        self.assertEqual(len(FakeXhsHttpTransport.downloads), downloads_before)
        FakeXhsHttpTransport.edited_text[IDS[0]] = "修改后的正文"
        changed = self.service.start("source_refresh", "xiaohongshu", AUTHOR)
        self.service.wait(30)
        self.assertEqual(self._author_job(changed["job_id"])["state"], "succeeded")
        self.assertEqual(len(FakeXhsHttpTransport.downloads), downloads_before + 2)
        with self.service.workflow.connect() as db:
            self.assertEqual(db.execute("SELECT detail_text FROM items WHERE platform='xiaohongshu' AND item_id=?", (IDS[0],)).fetchone()[0], "修改后的正文")

    def test_media_failure_preserves_cursor_and_resume_downloads_missing_only(self):
        self.service.subscribe(PROFILE)
        FakeXhsHttpTransport.fail_media_once = True
        started = self.service.start("author_archive", "xiaohongshu", AUTHOR)
        self.service.wait(30)
        job = self._author_job(started["job_id"])
        self.assertEqual((job["mode"], job["state"]), ("full", "partial"), job)
        self.assertEqual(FakeXhsHttpTransport.page_requests, [None])
        with self.service.workflow.connect() as db:
            run = db.execute("SELECT pages,cursor FROM runs WHERE id=?", (job["run_id"],)).fetchone()
            self.assertEqual((run["pages"], run["cursor"]), (1, "page-2"))
            self.assertEqual(db.execute("SELECT count(*) FROM assets").fetchone()[0], 2)
        self.service.resume(started["job_id"])
        self.service.wait(30)
        done = self._author_job(started["job_id"])
        self.assertEqual(done["state"], "succeeded", done)
        self.assertEqual(FakeXhsHttpTransport.page_requests, [None, "page-2", "page-3"])
        with self.service.workflow.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM assets").fetchone()[0], 7)

    def test_process_restart_refetches_only_unfinished_saved_page(self):
        self.service.subscribe(PROFILE)
        FakeXhsHttpTransport.fail_media_once = True
        started = self.service.start("author_archive", "xiaohongshu", AUTHOR)
        self.service.wait(30)
        first = self._author_job(started["job_id"])
        self.assertEqual(first["state"], "partial")
        self.service.close()
        self.service = WorkspaceService(self.root)
        self.service.resume(started["job_id"])
        self.service.wait(30)
        done = self._author_job(started["job_id"])
        self.assertEqual(done["state"], "succeeded", done)
        self.assertEqual(FakeXhsHttpTransport.page_requests, [None, None, "page-2", "page-3"])
        first_image = f"memory://{IDS[0]}/image"
        self.assertEqual(FakeXhsHttpTransport.downloads.count(first_image), 1)

    def test_changed_saved_page_stops_without_moving_cursor(self):
        self.service.subscribe(PROFILE)
        FakeXhsHttpTransport.fail_media_once = True
        started = self.service.start("author_archive", "xiaohongshu", AUTHOR)
        self.service.wait(30)
        self.service.close()
        FakeXhsHttpTransport.pages[0] = Page((Item("f" * 24, AUTHOR, "2026-09-02"),), "page-2", True)
        self.service = WorkspaceService(self.root)
        self.service.resume(started["job_id"])
        self.service.wait(30)
        job = self._author_job(started["job_id"])
        self.assertEqual((job["state"], job["reason"]), ("partial", "stale_checkpoint"), job)
        with self.service.workflow.connect() as db:
            run = db.execute("SELECT pages,cursor FROM runs WHERE id=?", (job["run_id"],)).fetchone()
            self.assertEqual((run["pages"], run["cursor"]), (1, "page-2"))

    def test_refresh_schedule_defaults_off_and_only_starts_due_native_authors(self):
        self.assertFalse(self.service.refresh_schedule()["enabled"])
        self.service.subscribe(PROFILE)
        self.assertEqual(self.service._run_due_refresh(time.time()), 0)
        with self.service.workflow.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        self.service.set_refresh_schedule(True, 15)
        path = self.root / "refresh-schedule.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["next_at"] = time.time() - 1
        self.service._save_refresh_schedule(config)
        self.assertEqual(self.service._run_due_refresh(time.time()), 1)
        self.service.wait(30)
        with self.service.workflow.connect() as db:
            self.assertEqual(tuple(db.execute("SELECT mode,state FROM jobs").fetchone()), ("source_refresh", "succeeded"))
        self.assertFalse(self.service.set_refresh_schedule(False, 15)["enabled"])

    def test_login_failure_pauses_all_queued_authors_until_explicit_reverification(self):
        second_author = "b" * 24
        TwoAuthorAccessTransport.requests = []
        TwoAuthorAccessTransport.authorized = False
        TwoAuthorAccessTransport.failure_reason = "needs_login"
        sys.modules["creator_archive.adapters.xhs_http"].XhsHttpTransport = TwoAuthorAccessTransport
        for author_id in (AUTHOR, second_author):
            self.service.workflow.subscribe("xiaohongshu", author_id, author_id, verified=True,
                                            evidence="synthetic fixture")
        batch = self.service.start("all_archive")
        self.service.wait(30)
        self.assertEqual(TwoAuthorAccessTransport.requests, [AUTHOR])
        jobs = {job["author_id"]: job for job in self.service.workspace()["runs"]}
        self.assertEqual((jobs[AUTHOR]["state"], jobs[second_author]["reason"]), ("needs_login", "needs_login"))
        self.service.close()
        self.service = WorkspaceService(self.root)
        with self.assertRaisesRegex(ValueError, "访问已暂停"):
            self.service.start("source_refresh", "xiaohongshu", second_author)
        TwoAuthorAccessTransport.authorized = True
        self.assertTrue(self.service.verify("xiaohongshu", AUTHOR)["identity_verified"])
        self.service.resume(jobs[second_author]["id"])
        self.service.wait(30)
        self.assertEqual(TwoAuthorAccessTransport.requests, [AUTHOR, second_author])
        self.assertEqual(self._author_job(jobs[second_author]["id"])["state"], "succeeded")

    def test_rate_limit_stays_paused_after_cooldown_until_explicit_verification(self):
        second_author = "b" * 24
        TwoAuthorAccessTransport.requests = []
        TwoAuthorAccessTransport.authorized = False
        TwoAuthorAccessTransport.failure_reason = "rate_limited"
        sys.modules["creator_archive.adapters.xhs_http"].XhsHttpTransport = TwoAuthorAccessTransport
        for author_id in (AUTHOR, second_author):
            self.service.workflow.subscribe("xiaohongshu", author_id, author_id, verified=True,
                                            evidence="synthetic fixture")
        batch = self.service.start("all_archive")
        self.service.wait(30)
        self.assertEqual(TwoAuthorAccessTransport.requests, [AUTHOR])
        jobs = {job["author_id"]: job for job in self.service.workspace()["runs"]}
        self.assertEqual(jobs[AUTHOR]["reason"], "rate_limited")
        with self.service.workflow.connect() as db:
            db.execute("UPDATE platform_cooldowns SET retry_at=? WHERE platform='xiaohongshu'", (time.time() - 1,))
            db.execute("UPDATE runs SET retry_at=? WHERE platform='xiaohongshu'", (time.time() - 1,))
        with self.assertRaisesRegex(ValueError, "访问已暂停"):
            self.service.start("source_refresh", "xiaohongshu", second_author)
        self.assertEqual(TwoAuthorAccessTransport.requests, [AUTHOR])
        TwoAuthorAccessTransport.authorized = True
        self.assertTrue(self.service.verify("xiaohongshu", AUTHOR)["identity_verified"])
        self.service.resume(jobs[second_author]["id"])
        self.service.wait(30)
        self.assertEqual(TwoAuthorAccessTransport.requests, [AUTHOR, second_author])


if __name__ == "__main__":
    unittest.main()
