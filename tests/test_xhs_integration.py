"""Isolated native-source service contract; no Xiaohongshu account is contacted."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
from types import ModuleType
import unittest
from unittest.mock import patch

from creator_archive.adapters.xhs import MediaCandidate
from creator_archive.service import WorkspaceService, PlatformAccessPaused
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
    unavailable_items = set()
    item_unavailable_items = set()
    missing_reference_items = set()
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
        cls.unavailable_items = set()
        cls.item_unavailable_items = set()
        cls.missing_reference_items = set()
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
        self._refs.update(item.item_id for item in page.items
                          if item.item_id not in type(self).missing_reference_items)
        return page

    def prepare_page_details(self, author_id, item_ids):
        assert author_id == AUTHOR
        return tuple(item_id for item_id in item_ids if item_id not in self._refs)

    def export_page_references(self, author_id, item_ids):
        assert author_id == AUTHOR
        return {item_id: f"https://www.xiaohongshu.com/explore/{item_id}?xsec_token=synthetic-token"
                for item_id in item_ids if item_id in self._refs}

    def restore_page_references(self, author_id, references):
        assert author_id == AUTHOR
        self._refs.update(references)

    def poll_latest(self):
        self._refs.update(IDS[:2])
        return [{"author_id": AUTHOR, "item_id": item_id,
                 "title": f"标题 {item_id[-1]}",
                 "source_url": f"https://www.xiaohongshu.com/explore/{item_id}"}
                for item_id in IDS[:2]]

    def detail(self, author_id, item_id, source_url=""):
        assert author_id == AUTHOR
        self.detail_requests.append(item_id)
        if item_id not in self._refs:
            raise AdapterFailure("reference_missing")
        if item_id in type(self).unavailable_items:
            raise AdapterFailure("unavailable")
        if item_id in type(self).item_unavailable_items:
            raise AdapterFailure("item_unavailable")
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

    def test_native_source_does_not_relabel_legacy_list_only_run(self):
        self.service.subscribe(PROFILE)
        now = time.time()
        with self.service.workflow.connect() as db:
            batch = db.execute("INSERT INTO batches(mode,created_at) VALUES('full',?)", (now,)).lastrowid
            run = db.execute("""INSERT INTO runs(batch_id,platform,author_id,mode,adapter_version,state,updated_at)
                VALUES(?,'xiaohongshu',?,'full','xhs-normal-browser-experimental-v1','succeeded',?)""",
                (batch, AUTHOR, now)).lastrowid
            job = db.execute("""INSERT INTO jobs(platform,author_id,mode,state,run_id,created_at,updated_at)
                VALUES('xiaohongshu',?,'full','succeeded',?,?,?)""",
                (AUTHOR, run, now, now)).lastrowid
        status = self._author_job(job)
        self.assertNotIn("target_count", status)
        self.assertEqual(status["mode"], "full")

    def test_profile_link_to_full_history_content_media_export_and_refresh(self):
        subscribed = self.service.subscribe(PROFILE)
        self.assertTrue(subscribed["identity_verified"])
        self.assertEqual(subscribed["source_kind"], "xhs_http")
        started = self.service.start("all_archive")
        self.service.wait(30)
        job = self._author_job(started["job_ids"][0])
        self.assertEqual((job["mode"], job["state"]), ("full", "succeeded"), job)
        self.assertEqual(self.service.workspace()["subscriptions"][0]["source_health"], "last_run_succeeded")
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

    def test_media_failure_keeps_history_coverage_and_resume_downloads_missing_only(self):
        self.service.subscribe(PROFILE)
        FakeXhsHttpTransport.fail_media_once = True
        started = self.service.start("author_archive", "xiaohongshu", AUTHOR)
        self.service.wait(30)
        job = self._author_job(started["job_id"])
        self.assertEqual((job["mode"], job["state"]), ("full", "partial"), job)
        self.assertEqual(FakeXhsHttpTransport.page_requests, [None, "page-2", "page-3"])
        with self.service.workflow.connect() as db:
            run = db.execute("SELECT pages,cursor,coverage FROM runs WHERE id=?", (job["run_id"],)).fetchone()
            self.assertEqual((run["pages"], run["cursor"], run["coverage"]),
                             (3, None, "complete_for_accessible_scope"))
            self.assertEqual(db.execute("SELECT count(*) FROM assets").fetchone()[0], 6)
        self.assertTrue(job["export"]["authors"][0]["manifest"])
        self.service.resume(started["job_id"])
        self.service.wait(30)
        done = self._author_job(started["job_id"])
        self.assertEqual(done["state"], "succeeded", done)
        self.assertEqual(FakeXhsHttpTransport.page_requests, [None, "page-2", "page-3"])
        with self.service.workflow.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM assets").fetchone()[0], 7)

    def test_process_restart_reuses_durable_references_without_refetching_page(self):
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
        self.assertEqual(FakeXhsHttpTransport.page_requests, [None, "page-2", "page-3"])
        first_image = f"memory://{IDS[0]}/image"
        self.assertEqual(FakeXhsHttpTransport.downloads.count(first_image), 1)
        self.assertEqual(len(list((self.root / "private" / "xhs-page-references").rglob("*.json"))), 3)
        manifest = Path(done["export"]["authors"][0]["manifest"]).read_text(encoding="utf-8")
        self.assertNotIn("synthetic-token", manifest)

    def test_reference_scope_mismatch_stops_locally_without_list_or_detail_request(self):
        self.service.subscribe(PROFILE)
        FakeXhsHttpTransport.fail_media_once = True
        started = self.service.start("author_archive", "xiaohongshu", AUTHOR)
        self.service.wait(30)
        self.service.close()
        path = next((self.root / "private" / "xhs-page-references").rglob("*.json"))
        record = json.loads(path.read_text(encoding="utf-8"))
        # Locate the saved page containing the unfinished video.
        for candidate in (self.root / "private" / "xhs-page-references").rglob("*.json"):
            candidate_record = json.loads(candidate.read_text(encoding="utf-8"))
            if IDS[0] in candidate_record["scope"]["item_ids"]:
                path, record = candidate, candidate_record
                break
        record["scope"]["author_id"] = "b" * 24
        path.write_text(json.dumps(record), encoding="utf-8")
        before = (list(FakeXhsHttpTransport.page_requests), list(FakeXhsHttpTransport.detail_requests))
        self.service = WorkspaceService(self.root)
        self.service.resume(started["job_id"])
        self.service.wait(30)
        job = self._author_job(started["job_id"])
        self.assertEqual((job["state"], job["reason"]), ("partial", "reference_missing"))
        self.assertEqual((FakeXhsHttpTransport.page_requests, FakeXhsHttpTransport.detail_requests), before)
        with self.service.workflow.connect() as db:
            run = db.execute("SELECT pages,cursor,coverage FROM runs WHERE id=?", (job["run_id"],)).fetchone()
            self.assertEqual(tuple(run), (3, None, "complete_for_accessible_scope"))

    def test_reference_write_failure_rolls_back_page_checkpoint(self):
        self.service.subscribe(PROFILE)
        with patch.object(self.service, "_save_xhs_page_references", side_effect=OSError("synthetic disk failure")):
            started = self.service.start("author_archive", "xiaohongshu", AUTHOR)
            self.service.wait(30)
        job = self._author_job(started["job_id"])
        self.assertEqual(job["state"], "failed")
        with self.service.workflow.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM pages WHERE run_id=?", (job["run_id"],)).fetchone()[0], 0)
            self.assertEqual(tuple(db.execute("SELECT pages,cursor FROM runs WHERE id=?", (job["run_id"],)).fetchone()), (0, None))
        self.assertEqual(FakeXhsHttpTransport.detail_requests, [])

    def test_changed_saved_page_stops_without_moving_cursor(self):
        self.service.subscribe(PROFILE)
        FakeXhsHttpTransport.fail_media_once = True
        started = self.service.start("author_archive", "xiaohongshu", AUTHOR)
        self.service.wait(30)
        self.service.close()
        # An old checkpoint has no durable references: retain the legacy replay
        # check, including its strict ordered IDs and cursor comparison.
        shutil.rmtree(self.root / "private" / "xhs-page-references")
        FakeXhsHttpTransport.pages[0] = Page((Item("f" * 24, AUTHOR, "2026-09-02"),), "page-2", True)
        self.service = WorkspaceService(self.root)
        self.service.resume(started["job_id"])
        self.service.wait(30)
        job = self._author_job(started["job_id"])
        self.assertEqual((job["state"], job["reason"]), ("partial", "stale_checkpoint"), job)
        with self.service.workflow.connect() as db:
            run = db.execute("SELECT pages,cursor FROM runs WHERE id=?", (job["run_id"],)).fetchone()
            self.assertEqual((run["pages"], run["cursor"]), (3, None))

    def test_missing_note_does_not_stop_history_or_repeat_in_one_run(self):
        self.service.subscribe(PROFILE)
        FakeXhsHttpTransport.item_unavailable_items.add(IDS[0])
        started = self.service.start("author_archive", "xiaohongshu", AUTHOR)
        self.service.wait(30)
        job = self._author_job(started["job_id"])
        self.assertEqual((job["state"], job["reason"]), ("partial", "author_archive_partial"), job)
        self.assertEqual(FakeXhsHttpTransport.page_requests, [None, "page-2", "page-3"])
        self.assertEqual(FakeXhsHttpTransport.detail_requests.count(IDS[0]), 1)
        self.assertEqual(len(FakeXhsHttpTransport.detail_requests), 6)
        with self.service.workflow.connect() as db:
            run = db.execute("SELECT state,pages,coverage,terminal_evidence FROM runs WHERE id=?",
                             (job["run_id"],)).fetchone()
            self.assertEqual((run["state"], run["pages"], run["coverage"]),
                             ("succeeded", 3, "complete_for_accessible_scope"))
            self.assertTrue(run["terminal_evidence"])
            failure = db.execute("SELECT state,reason FROM job_items WHERE job_id=? AND item_id=?",
                                 (started["job_id"], IDS[0])).fetchone()
            self.assertEqual(tuple(failure), ("partial", "item_unavailable"))
            observation = db.execute("SELECT source,status,reason FROM metric_snapshots WHERE platform='xiaohongshu' AND item_id=? ORDER BY id DESC LIMIT 1",
                                     (IDS[0],)).fetchone()
            self.assertEqual(tuple(observation), ("xhs_http_detail_attempt", "failed", "item_unavailable"))
            self.assertEqual(db.execute("SELECT count(*) FROM items WHERE detail_state='complete'").fetchone()[0], 5)
        self.assertTrue(Path(job["export"]["authors"][0]["manifest"]).is_file())
        self.assertTrue(Path(job["export"]["authors"][0]["failures"]).is_file())
        self.service.close()
        self.service = WorkspaceService(self.root)
        self.service.resume(started["job_id"])
        self.service.wait(30)
        resumed = self._author_job(started["job_id"])
        self.assertEqual(resumed["state"], "partial", resumed)
        self.assertEqual(FakeXhsHttpTransport.page_requests, [None, "page-2", "page-3"])
        self.assertEqual(FakeXhsHttpTransport.detail_requests.count(IDS[0]), 2)
        self.assertEqual(len(FakeXhsHttpTransport.detail_requests), 7)

    def test_unknown_source_failure_stops_before_requesting_next_page(self):
        self.service.subscribe(PROFILE)
        FakeXhsHttpTransport.unavailable_items.add(IDS[0])
        started = self.service.start("author_archive", "xiaohongshu", AUTHOR)
        self.service.wait(30)
        job = self._author_job(started["job_id"])
        self.assertEqual((job["state"], job["reason"]), ("blocked", "unavailable"), job)
        self.assertEqual(FakeXhsHttpTransport.page_requests, [None])
        self.assertEqual(FakeXhsHttpTransport.detail_requests.count(IDS[0]), 1)
        with self.service.workflow.connect() as db:
            run = db.execute("SELECT pages,cursor,coverage FROM runs WHERE id=?", (job["run_id"],)).fetchone()
            self.assertEqual(tuple(run), (1, "page-2", "partial"))

    def test_missing_one_detail_reference_keeps_other_pages_and_failure_record(self):
        self.service.subscribe(PROFILE)
        FakeXhsHttpTransport.missing_reference_items.add(IDS[0])
        started = self.service.start("author_archive", "xiaohongshu", AUTHOR)
        self.service.wait(30)
        job = self._author_job(started["job_id"])
        self.assertEqual((job["state"], job["reason"]), ("partial", "author_archive_partial"), job)
        self.assertEqual(FakeXhsHttpTransport.page_requests, [None, None, "page-2", "page-3"])
        self.assertEqual(FakeXhsHttpTransport.detail_requests.count(IDS[0]), 1)
        with self.service.workflow.connect() as db:
            run = db.execute("SELECT pages,coverage FROM runs WHERE id=?", (job["run_id"],)).fetchone()
            self.assertEqual(tuple(run), (3, "complete_for_accessible_scope"))
            failure = db.execute("SELECT state,reason FROM job_items WHERE job_id=? AND item_id=?",
                                 (started["job_id"], IDS[0])).fetchone()
            self.assertEqual(tuple(failure), ("partial", "reference_missing"))
            self.assertEqual(db.execute("SELECT count(*) FROM items WHERE detail_state='complete'").fetchone()[0], 5)

    def test_history_page_budget_preserves_cursor_for_explicit_resume(self):
        self.service.subscribe(PROFILE)
        with patch("creator_archive.service.XHS_HISTORY_PAGE_BUDGET", 2):
            started = self.service.start("author_archive", "xiaohongshu", AUTHOR)
            self.service.wait(30)
        job = self._author_job(started["job_id"])
        self.assertEqual((job["state"], job["reason"]), ("partial", "page_budget_reached"), job)
        self.assertEqual(FakeXhsHttpTransport.page_requests, [None, "page-2"])
        with self.service.workflow.connect() as db:
            run = db.execute("SELECT pages,cursor,coverage FROM runs WHERE id=?", (job["run_id"],)).fetchone()
            self.assertEqual(tuple(run), (2, "page-3", "partial"))
            self.assertEqual(db.execute("SELECT count(*) FROM items WHERE detail_state='complete'").fetchone()[0], 4)
        self.service.close()
        self.service = WorkspaceService(self.root)
        with patch("creator_archive.service.XHS_HISTORY_PAGE_BUDGET", 2):
            self.service.resume(started["job_id"])
            self.service.wait(30)
        done = self._author_job(started["job_id"])
        self.assertEqual(done["state"], "succeeded", done)
        self.assertEqual(FakeXhsHttpTransport.page_requests, [None, "page-2", "page-3"])
        with self.service.workflow.connect() as db:
            run = db.execute("SELECT pages,cursor,coverage FROM runs WHERE id=?", (job["run_id"],)).fetchone()
            self.assertEqual(tuple(run), (3, None, "complete_for_accessible_scope"))

    def test_legacy_browser_success_does_not_prove_new_http_source_health(self):
        self.service.subscribe(PROFILE)
        with self.service.workflow.connect() as db:
            batch_id = db.execute("INSERT INTO batches(mode,created_at) VALUES('full',?)", (time.time(),)).lastrowid
            run_id = db.execute("""INSERT INTO runs(batch_id,platform,author_id,mode,adapter_version,state,coverage,updated_at)
                VALUES(?,'xiaohongshu',?,'full','xhs-browser-legacy','succeeded','unknown',?)""",
                (batch_id, AUTHOR, time.time())).lastrowid
            db.execute("""INSERT INTO jobs(platform,author_id,mode,state,run_id,created_at,updated_at)
                VALUES('xiaohongshu',?,'full','succeeded',?,?,?)""",
                (AUTHOR, run_id, time.time(), time.time()))
            db.execute("""INSERT INTO jobs(platform,author_id,mode,state,created_at,updated_at)
                VALUES('xiaohongshu',?,'content','succeeded',?,?)""",
                (AUTHOR, time.time(), time.time()))
        sub = self.service.workspace()["subscriptions"][0]
        self.assertEqual(sub["source_kind"], "xhs_http")
        self.assertEqual(sub["source_health"], "not_checked")
        self.assertIsNone(sub["source_health_checked_at"])

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

    def test_login_failure_pause_survives_restart_and_verification_cannot_release_it(self):
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
        with self.assertRaises(PlatformAccessPaused):
            self.service.start("source_refresh", "xiaohongshu", second_author)
        TwoAuthorAccessTransport.authorized = True
        with self.assertRaises(PlatformAccessPaused):
            self.service.verify("xiaohongshu", AUTHOR)
        with self.assertRaises(PlatformAccessPaused):
            self.service.resume(jobs[second_author]["id"])
        self.assertEqual(TwoAuthorAccessTransport.requests, [AUTHOR])
        self.assertEqual(self._author_job(jobs[second_author]["id"])["reason"], "needs_login")

    def test_rate_limit_pause_cannot_be_released_by_cooldown_or_verification(self):
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
        with self.assertRaises(PlatformAccessPaused):
            self.service.start("source_refresh", "xiaohongshu", second_author)
        self.assertEqual(TwoAuthorAccessTransport.requests, [AUTHOR])
        TwoAuthorAccessTransport.authorized = True
        with self.assertRaises(PlatformAccessPaused):
            self.service.verify("xiaohongshu", AUTHOR)
        with self.assertRaises(PlatformAccessPaused):
            self.service.resume(jobs[second_author]["id"])
        self.assertEqual(TwoAuthorAccessTransport.requests, [AUTHOR])


if __name__ == "__main__":
    unittest.main()
