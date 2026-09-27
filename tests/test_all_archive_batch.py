"""Fixed all-subscription archive batches using synthetic pages and temporary files."""
from pathlib import Path
from html.parser import HTMLParser
import json
import re
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urljoin, urlsplit, unquote

from fastapi.testclient import TestClient

from creator_archive.app import create_app
from creator_archive.service import WorkspaceService
from creator_archive.validation import AdapterFailure, Item, Page
from tests.test_demo_pipeline import DemoSource


def ids(start, count):
    return [f"{number:024x}" for number in range(start, start + count)]


class LocalLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag in {"a", "img", "video"}:
            attributes = dict(attrs)
            link = attributes.get("href") or attributes.get("src")
            if link:
                self.links.append(link)


class AllArchiveBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = DemoSource()
        self.service = WorkspaceService(self.root, adapter_factory=lambda _: self.source)
        self.addCleanup(self.service.close)
        self.first, self.second, self.pending, self.wechat = (letter * 24 for letter in "abcd")
        for author in (self.first, self.second, self.pending):
            self.service.workflow.subscribe("xiaohongshu", author, author, verified=True,
                                            evidence="synthetic fixture" if author != self.pending else "observed_browser_exact_note")
        self.service.workflow.subscribe("wechat", self.wechat, "Wechat fixture", verified=True,
                                        evidence="synthetic fixture")
        self.service.workflow.set_enabled("xiaohongshu", self.second, False)
        self.service.workflow.set_enabled("xiaohongshu", self.pending, False)
        self.first_ids, self.second_ids = ids(1, 55), ids(101, 3)
        self.source.author_pages = {
            self.first: [self.first_ids[:25], self.first_ids[25:50], self.first_ids[50:]],
            self.second: [self.second_ids],
        }

    def batch(self, service=None):
        return (service or self.service).workspace()["archive_batches"][0]

    def test_export_file_failure_isolates_author_and_resumes_without_redownloading(self):
        for failed_name in ("article.md", "manifest.json"):
            with self.subTest(failed_name=failed_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                first, second = "a" * 24, "b" * 24
                source = DemoSource()
                source.author_pages = {first: [ids(1, 2)], second: [ids(101, 1)]}
                service = WorkspaceService(root, adapter_factory=lambda _: source)
                for author in (first, second):
                    service.workflow.subscribe("xiaohongshu", author, author, verified=True, evidence="synthetic fixture")
                service._spawn = lambda _: None
                batch_id = service.start("all_archive")["batch_id"]
                members = {m["author_id"]: m for m in service.workspace()["archive_batches"][0]["members"]}
                from creator_archive import workflow
                original_write = workflow._managed_write

                def fail_first(path, content):
                    if first in str(path) and path.name == failed_name:
                        raise OSError("synthetic export write failure")
                    return original_write(path, content)

                with patch.object(workflow, "_managed_write", side_effect=fail_first):
                    service._execute(members[first]["job_id"])
                    service._execute(members[second]["job_id"])
                batch = service.workspace()["archive_batches"][0]
                current = {m["author_id"]: m for m in batch["members"]}
                jobs = {j["id"]: j for j in service.workspace()["runs"]}
                self.assertEqual((batch["state"], batch["complete"], batch["unfinished"]), ("partial", 1, 1))
                self.assertEqual((jobs[members[first]["job_id"]]["stage"], current[first]["reason"]), ("archive", "export_failed"))
                self.assertEqual((current[first]["pages"], current[first]["target_count"], current[first]["item_count"]), (1, 2, 2))
                self.assertTrue(current[first]["list_finished"])
                self.assertEqual(current[second]["state"], "succeeded")
                self.assertTrue(jobs[members[second]["job_id"]]["export"]["authors"][0]["manifest"])
                self.assertIn("导出", current[first]["message"])
                self.assertIn("恢复", current[first]["next_step"])
                with service.workflow.connect() as db:
                    fixed = [tuple(r) for r in db.execute("""SELECT p.page_number,p.child_job_id,j.item_id FROM pipeline_pages p
                        JOIN job_items j ON j.job_id=p.child_job_id WHERE p.parent_job_id=? ORDER BY p.page_number,j.item_id""", (members[first]["job_id"],))]
                    run_id = db.execute("SELECT run_id FROM jobs WHERE id=?", (members[first]["job_id"],)).fetchone()[0]
                    checkpoint = tuple(db.execute("SELECT pages,terminal_evidence FROM runs WHERE id=?", (run_id,)).fetchone())
                manual = root / "archive" / "xiaohongshu" / first / "manual-note.md"
                manual.write_text("human note", encoding="utf-8")
                saved = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (root / "archive").rglob("*") if p.is_file()}
                service.close()
                fresh = DemoSource()
                fresh.author_pages = source.author_pages
                restarted = WorkspaceService(root, adapter_factory=lambda _: fresh)
                self.assertEqual(restarted.start("all_archive")["batch_id"], batch_id)
                self.assertEqual(restarted.resume_archive_batch(batch_id)["resumed_job_ids"], [members[first]["job_id"]])
                restarted.wait()
                final = {m["author_id"]: m for m in restarted.workspace()["archive_batches"][0]["members"]}
                self.assertEqual((final[first]["state"], final[second]["state"]), ("succeeded", "succeeded"))
                self.assertEqual([value for event, value in fresh.events if event == "detail"], [])
                with restarted.workflow.connect() as db:
                    self.assertEqual(tuple(db.execute("SELECT pages,terminal_evidence FROM runs WHERE id=?", (run_id,)).fetchone()), checkpoint)
                    self.assertEqual([tuple(r) for r in db.execute("""SELECT p.page_number,p.child_job_id,j.item_id FROM pipeline_pages p
                        JOIN job_items j ON j.job_id=p.child_job_id WHERE p.parent_job_id=? ORDER BY p.page_number,j.item_id""", (members[first]["job_id"],))], fixed)
                self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in saved}, saved)
                restarted.close()

    def test_fixed_scope_failure_isolation_restart_and_resource_preservation(self):
        self.source.failures[self.second_ids[0]] = "item_unavailable"
        response = self.service.start("all_archive")
        batch_id = response["batch_id"]
        self.assertFalse(response["reused"])
        self.assertEqual(self.service.start("all_archive")["batch_id"], batch_id)
        self.service.wait()
        batch = self.batch()
        self.assertEqual((batch["total"], batch["complete"], batch["unfinished"], batch["blocked"]), (3, 1, 1, 1))
        self.assertEqual({m["author_id"] for m in batch["members"]}, {self.first, self.second, self.wechat})
        members = {m["author_id"]: m for m in batch["members"]}
        self.assertEqual((members[self.first]["pages"], members[self.first]["item_count"], members[self.first]["target_count"]), (3, 55, 55))
        self.assertTrue(members[self.first]["list_finished"])
        self.assertEqual((members[self.second]["state"], members[self.second]["item_count"], members[self.second]["target_count"]), ("partial", 2, 3))
        self.assertEqual(members[self.wechat]["reason"], "wechat_blocked")
        self.assertEqual(self.service.start("all_archive")["batch_id"], batch_id)
        with self.service.workflow.connect() as db:
            job_count = db.execute("SELECT count(*) FROM jobs WHERE mode='author_archive'").fetchone()[0]
            fixed = [tuple(row) for row in db.execute("""SELECT m.platform,m.author_id,m.job_id FROM archive_batch_members m
                WHERE m.batch_id=? ORDER BY m.platform,m.author_id""", (batch_id,))]
            targets = [tuple(row) for row in db.execute("""SELECT p.parent_job_id,j.item_id FROM pipeline_pages p
                JOIN job_items j ON j.job_id=p.child_job_id WHERE p.parent_job_id IN (?,?) ORDER BY p.parent_job_id,j.item_id""",
                (members[self.first]["job_id"], members[self.second]["job_id"]))]
            self.assertEqual(job_count, 2)
        archive = self.root / "archive" / "xiaohongshu" / self.first
        manual = archive / "manual-note.md"
        manual.write_text("human note", encoding="utf-8")
        saved = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in archive.rglob("*") if p.is_file()}

        self.service.close()
        fresh = DemoSource()
        fresh.author_pages = self.source.author_pages
        restarted = WorkspaceService(self.root, adapter_factory=lambda _: fresh)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.start("all_archive")["batch_id"], batch_id)
        self.assertEqual(restarted.resume_archive_batch(batch_id)["resumed_job_ids"], [members[self.second]["job_id"]])
        restarted.wait()
        final = self.batch(restarted)
        self.assertEqual((final["complete"], final["unfinished"], final["blocked"]), (2, 0, 1))
        self.assertEqual([item for event, item in fresh.events if event == "detail"], [self.second_ids[0]])
        self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in saved}, saved)
        with restarted.workflow.connect() as db:
            self.assertEqual([tuple(row) for row in db.execute("SELECT platform,author_id,job_id FROM archive_batch_members WHERE batch_id=? ORDER BY platform,author_id", (batch_id,))], fixed)
            self.assertEqual([tuple(row) for row in db.execute("""SELECT p.parent_job_id,j.item_id FROM pipeline_pages p
                JOIN job_items j ON j.job_id=p.child_job_id WHERE p.parent_job_id IN (?,?) ORDER BY p.parent_job_id,j.item_id""",
                (members[self.first]["job_id"], members[self.second]["job_id"]))], targets)

    def test_list_failures_isolate_authors_and_resume_exact_checkpoints(self):
        cases = (("needs_login_home", "needs_login", None),
                 ("needs_login_deep", "needs_login", "page-2"),
                 ("empty_nonterminal_page", "empty_nonterminal_page", "page-2"),
                 ("repeated_cursor", "repeated_cursor", "page-2"),
                 ("rate_limited", "rate_limited", "page-2"))
        for case, category, fault_cursor in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                first, second = "a" * 24, "b" * 24
                first_ids, second_ids = ids(1, 2), ids(101, 1)

                class FailingPageSource(DemoSource):
                    def page(self, author, cursor):
                        if author == first and cursor == fault_cursor:
                            if category in {"needs_login", "rate_limited"}:
                                raise AdapterFailure(category, 60 if category == "rate_limited" else 0)
                            if category == "empty_nonterminal_page":
                                return Page((), "page-3", True, None)
                            return Page((Item(first_ids[1], author, ""),), "page-2", True, None)
                        return super().page(author, cursor)

                failing = FailingPageSource()
                failing.author_pages = {first: [[first_ids[0]], [first_ids[1]]], second: [second_ids]}
                service = WorkspaceService(root, adapter_factory=lambda _: failing)
                for author in (first, second):
                    service.workflow.subscribe("xiaohongshu", author, author, verified=True, evidence="synthetic fixture")
                service._spawn = lambda _: None
                batch_id = service.start("all_archive")["batch_id"]
                members = {member["author_id"]: member for member in service.workspace()["archive_batches"][0]["members"]}
                service._execute(members[first]["job_id"])
                service._execute(members[second]["job_id"])
                batch = service.workspace()["archive_batches"][0]
                members = {member["author_id"]: member for member in batch["members"]}
                saved_pages = 0 if fault_cursor is None else 1
                self.assertEqual((members[first]["pages"], members[first]["item_count"], members[first]["list_finished"]),
                                 (saved_pages, saved_pages, False))
                self.assertEqual(members[first]["reason"], category)
                self.assertIn("恢复", members[first]["next_step"])
                self.assertEqual(members[second]["state"], "rate_limited" if category == "rate_limited" else "succeeded")
                self.assertEqual(batch["state"], "partial")
                with service.workflow.connect() as db:
                    parent = members[first]["job_id"]
                    run_id = db.execute("SELECT run_id FROM jobs WHERE id=?", (parent,)).fetchone()[0]
                    self.assertEqual(tuple(db.execute("SELECT pages,cursor,terminal_evidence FROM runs WHERE id=?", (run_id,)).fetchone()),
                                     (saved_pages, fault_cursor, None))
                    original_scope = [tuple(row) for row in db.execute("""SELECT p.page_number,j.item_id FROM pipeline_pages p
                        JOIN job_items j ON j.job_id=p.child_job_id WHERE p.parent_job_id=?""", (parent,))]
                    asset_paths = [root / "archive" / row[0] for row in db.execute("SELECT relative_path FROM assets WHERE item_id=?", (first_ids[0],))]
                    if category == "rate_limited":
                        db.execute("UPDATE platform_cooldowns SET retry_at=0")
                archive = root / "archive" / "xiaohongshu" / first
                archive.mkdir(parents=True, exist_ok=True)
                manual = archive / "manual-note.md"
                manual.write_text("human note", encoding="utf-8")
                preserved = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in [*asset_paths, manual]}
                self.assertEqual(len(asset_paths), saved_pages)
                service.close()

                fresh = DemoSource()
                fresh.author_pages = failing.author_pages
                restarted = WorkspaceService(root, adapter_factory=lambda _: fresh)
                self.assertEqual(restarted.start("all_archive")["batch_id"], batch_id)
                resumed = restarted.resume_archive_batch(batch_id)
                self.assertEqual(set(resumed["resumed_job_ids"]), {member["job_id"] for member in members.values() if member["state"] != "succeeded"})
                restarted.wait()
                final = restarted.workspace()["archive_batches"][0]
                self.assertEqual(final["complete"], 2)
                self.assertEqual(final["unfinished"], 0)
                self.assertEqual([value for event, value in fresh.events if event == "detail"],
                                 first_ids[saved_pages:] + (second_ids if category == "rate_limited" else []))
                with restarted.workflow.connect() as db:
                    self.assertEqual([tuple(row) for row in db.execute("""SELECT p.page_number,j.item_id FROM pipeline_pages p
                        JOIN job_items j ON j.job_id=p.child_job_id WHERE p.parent_job_id=? ORDER BY p.page_number""", (parent,))][:len(original_scope)], original_scope)
                self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in preserved}, preserved)
                restarted.close()

    def test_content_failures_isolate_authors_and_resume_fixed_items(self):
        cases = ("item_unavailable", "media_failed", "needs_login", "rate_limited")
        for category in cases:
            with self.subTest(category=category), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                first, second = "a" * 24, "b" * 24
                first_ids, second_ids = ids(1, 2), ids(101, 1)

                class FailingContentSource(DemoSource):
                    def detail(self, author, item, source_url=""):
                        if author == first and item == first_ids[0] and category in {"needs_login", "rate_limited"}:
                            self.events.append(("detail", item))
                            raise AdapterFailure(category, 60 if category == "rate_limited" else 0)
                        return super().detail(author, item, source_url=source_url)

                    def download_media(self, candidate, target):
                        if category == "media_failed" and target.name == first_ids[0]:
                            raise AdapterFailure("media_failed")
                        return super().download_media(candidate, target)

                failing = FailingContentSource()
                failing.author_pages = {first: [first_ids], second: [second_ids]}
                if category == "item_unavailable":
                    failing.failures[first_ids[0]] = category
                service = WorkspaceService(root, adapter_factory=lambda _: failing)
                for author in (first, second):
                    service.workflow.subscribe("xiaohongshu", author, author, verified=True, evidence="synthetic fixture")
                service._spawn = lambda _: None
                batch_id = service.start("all_archive")["batch_id"]
                initial = {m["author_id"]: m for m in service.workspace()["archive_batches"][0]["members"]}
                service._execute(initial[first]["job_id"])
                service._execute(initial[second]["job_id"])
                batch = service.workspace()["archive_batches"][0]
                members = {m["author_id"]: m for m in batch["members"]}
                first_member = members[first]
                self.assertEqual((first_member["pages"], first_member["target_count"], first_member["item_count"]),
                                 (1, 2, 1 if category in {"item_unavailable", "media_failed"} else 0))
                self.assertEqual(first_member["failed_count"], 1)
                self.assertEqual(first_member["list_finished"], True)
                self.assertEqual(first_member["reason"], category if category in {"needs_login", "rate_limited"} else "author_archive_partial")
                self.assertEqual(members[second]["state"], "rate_limited" if category == "rate_limited" else "succeeded")
                failure = service.job_failures(first_member["job_id"])["items"][0]
                self.assertEqual((failure["item_id"], failure["reason"]), (first_ids[0], category))
                self.assertTrue(failure["next_step"])
                with service.workflow.connect() as db:
                    fixed = [tuple(row) for row in db.execute("""SELECT p.page_number,p.child_job_id,j.item_id FROM pipeline_pages p
                        JOIN job_items j ON j.job_id=p.child_job_id WHERE p.parent_job_id=? ORDER BY p.page_number,j.item_id""",
                        (first_member["job_id"],))]
                    run_id = db.execute("SELECT run_id FROM jobs WHERE id=?", (first_member["job_id"],)).fetchone()[0]
                    checkpoint = tuple(db.execute("SELECT pages,terminal_evidence FROM runs WHERE id=?", (run_id,)).fetchone())
                    if category == "rate_limited":
                        db.execute("UPDATE platform_cooldowns SET retry_at=0")
                archive = root / "archive" / "xiaohongshu" / first
                archive.mkdir(parents=True, exist_ok=True)
                manual = archive / "manual-note.md"
                manual.write_text("human note", encoding="utf-8")
                saved = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in archive.rglob("*") if p.is_file()}
                service.close()

                fresh = DemoSource()
                fresh.author_pages = failing.author_pages
                restarted = WorkspaceService(root, adapter_factory=lambda _: fresh)
                self.assertEqual(restarted.start("all_archive")["batch_id"], batch_id)
                restarted.resume_archive_batch(batch_id)
                restarted.wait()
                final = {m["author_id"]: m for m in restarted.workspace()["archive_batches"][0]["members"]}
                self.assertEqual((final[first]["state"], final[second]["state"]), ("succeeded", "succeeded"))
                expected = first_ids if category in {"needs_login", "rate_limited"} else first_ids[:1]
                if category == "rate_limited":
                    expected += second_ids
                self.assertEqual([value for event, value in fresh.events if event == "detail"], expected)
                with restarted.workflow.connect() as db:
                    self.assertEqual([tuple(row) for row in db.execute("""SELECT p.page_number,p.child_job_id,j.item_id FROM pipeline_pages p
                        JOIN job_items j ON j.job_id=p.child_job_id WHERE p.parent_job_id=? ORDER BY p.page_number,j.item_id""",
                        (first_member["job_id"],))], fixed)
                    self.assertEqual(tuple(db.execute("SELECT pages,terminal_evidence FROM runs WHERE id=?", (run_id,)).fetchone()), checkpoint)
                self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in saved}, saved)
                restarted.close()

    def test_reopen_interrupted_batch_keeps_original_author_set(self):
        with self.service.workflow.connect() as db:
            old_batch = db.execute("INSERT INTO batches(mode,created_at) VALUES('full',1)").lastrowid
            db.execute("INSERT INTO runs(batch_id,platform,author_id,mode,adapter_version,updated_at) VALUES(?,? ,?,'full','synthetic',1)",
                       (old_batch,"xiaohongshu",self.first))
        self.service._spawn = lambda _: None
        first = self.service.start("all_archive")
        queued = next(m for m in self.batch()["members"] if m["author_id"] == self.first)
        pending_scan = self.service.job_scan(queued["job_id"])
        self.assertEqual(pending_scan["status"], "scan_not_started")
        self.assertEqual(pending_scan["counts"]["observed_unique"], 0)
        self.assertEqual(self.service.workflow.status()["batch_id"], old_batch,
                         "A member-only orchestration batch must not hide the latest workflow run")
        third = "e" * 24
        self.service.workflow.subscribe("xiaohongshu", third, "Added later", verified=True, evidence="synthetic fixture")
        self.service.close()
        restarted = WorkspaceService(self.root, adapter_factory=lambda _: self.source)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.start("all_archive")["batch_id"], first["batch_id"])
        self.assertEqual({m["author_id"] for m in self.batch(restarted)["members"]}, {self.first, self.second, self.wechat})
        self.assertTrue(all(m["state"] == "interrupted" for m in self.batch(restarted)["members"] if m["job_id"]))

    def test_run_scan_manifest_separates_observed_failures_and_library_only_after_restart(self):
        old_id = ids(900, 1)[0]
        with self.service.workflow.connect() as db:
            db.execute("INSERT INTO items(platform,item_id,author_id,published_at) VALUES(?,?,?,'')",
                       ("xiaohongshu", old_id, self.first))
        observed = self.first_ids[:3]
        self.source.author_pages[self.first] = [observed[:2], [observed[0], observed[2]]]
        self.source.failures[observed[2]] = "item_unavailable"
        parent = self.service.start("author_archive", "xiaohongshu", self.first)["job_id"]
        self.service.wait()
        job = next(row for row in self.service.workspace()["runs"] if row["id"] == parent)
        exported = job["export"]["authors"][0]
        scan_path = Path(exported["scan_manifest"])
        scan = json.loads(scan_path.read_text(encoding="utf-8"))
        self.assertEqual(scan["counts"], {"observed_unique": 3, "targeted_unique": 3,
                                          "complete": 2, "partial": 1, "pending": 0,
                                          "not_targeted": 0, "library_only": 1})
        self.assertEqual(scan["library_only_item_ids"], [old_id])
        self.assertEqual([(item["item_id"], item["download_state"]) for item in scan["observed_items"]],
                         [(observed[0], "complete"), (observed[1], "complete"), (observed[2], "partial")])
        self.assertEqual(scan["observed_items"][0]["pages"], [1, 2])
        self.assertEqual(scan["observed_items"][2]["reason"], "item_unavailable")
        self.assertEqual(scan["unseen_items"], "unknown_not_enumerable")
        current = self.service.job_scan(parent)
        self.assertEqual(current["counts"], scan["counts"])
        self.assertEqual(len(json.loads(Path(exported["manifest"]).read_text(encoding="utf-8"))["items"]), 4)
        self.assertIn("本轮扫描清单", Path(exported["index"]).read_text(encoding="utf-8"))
        saved = (scan_path.read_bytes(), scan_path.stat().st_mtime_ns)
        manual = scan_path.parent / "manual-note.md"
        manual.write_text("kept", encoding="utf-8")

        self.service.close()
        fresh = DemoSource()
        fresh.author_pages = self.source.author_pages
        restarted = WorkspaceService(self.root, adapter_factory=lambda _: fresh)
        self.addCleanup(restarted.close)
        restarted.resume(parent)
        restarted.wait()
        result = next(row for row in restarted.workspace()["runs"] if row["id"] == parent)
        new_scan = json.loads(Path(result["export"]["authors"][0]["scan_manifest"]).read_text(encoding="utf-8"))
        self.assertEqual((new_scan["run_id"], new_scan["parent_job_id"]), (scan["run_id"], parent))
        self.assertEqual(new_scan["counts"]["complete"], 3)
        self.assertEqual(new_scan["counts"]["library_only"], 1)
        self.assertEqual([value for event, value in fresh.events if event == "detail"], [observed[2]])
        self.assertEqual((scan_path.read_bytes(), scan_path.stat().st_mtime_ns), saved)
        self.assertEqual(manual.read_text(encoding="utf-8"), "kept")

    def test_upgrade_creates_verified_backup_before_new_table(self):
        self.service.close()
        with self.service.workflow.connect() as db:
            db.execute("DROP TABLE archive_batch_members")
        restarted = WorkspaceService(self.root, adapter_factory=lambda _: self.source)
        self.addCleanup(restarted.close)
        self.assertIsNotNone(restarted.batch_migration_backup)
        self.assertTrue(Path(restarted.batch_migration_backup).is_file())
        with restarted.workflow.connect() as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_local_api_exposes_fixed_batch_and_resume(self):
        root = self.root / "api"
        app = create_app(root)
        app.state.service.adapter_factory = lambda _: DemoSource(pages=[ids(301, 2)])
        app.state.service.workflow.subscribe("xiaohongshu", self.first, "API fixture", verified=True, evidence="synthetic fixture")
        headers = {"x-creator-archive": "local-validation"}
        with TestClient(app) as client:
            started = client.post("/api/jobs", json={"mode": "all_archive"}, headers=headers)
            self.assertEqual(started.status_code, 200)
            batch_id = started.json()["batch_id"]
            app.state.service.wait()
            batch = client.get("/api/workspace").json()["archive_batches"][0]
            self.assertEqual((batch["id"], batch["complete"], batch["total"]), (batch_id, 1, 1))
            member = batch["members"][0]
            scan = client.get(member["scan_url"])
            self.assertEqual(scan.status_code, 200)
            self.assertEqual(scan.json()["counts"]["observed_unique"], 2)
            exported_scan = client.get(member["scan_manifest_url"])
            self.assertEqual(exported_scan.status_code, 200)
            self.assertEqual(exported_scan.json()["run_id"], scan.json()["run_id"])
            resumed = client.post(f"/api/archive-batches/{batch_id}/resume", json={}, headers=headers)
            self.assertEqual(resumed.status_code, 200)
            self.assertEqual(resumed.json()["resumed_job_ids"], [])
            self.assertEqual(client.post("/api/jobs", json={"mode": "all_archive", "author_id": self.first}, headers=headers).status_code, 422)

    def test_partial_batch_exports_read_back_over_http_and_offline_after_resume(self):
        root = self.root / "readback"
        first, second = "a" * 24, "b" * 24
        first_ids, second_ids = ids(1, 2), ids(101, 1)
        source = DemoSource()
        source.author_pages = {first: [first_ids], second: [second_ids]}
        source.failures[first_ids[0]] = "item_unavailable"
        app = create_app(root)
        app.state.service.adapter_factory = lambda _: source
        for author in (first, second):
            app.state.service.workflow.subscribe("xiaohongshu", author, author,
                                                 verified=True, evidence="synthetic fixture")
        headers = {"x-creator-archive": "local-validation"}

        def read_exports(client, expected_complete):
            workspace = client.get("/api/workspace").json()
            batch = workspace["archive_batches"][0]
            members = {member["author_id"]: member for member in batch["members"]}
            jobs = {job["id"]: job for job in workspace["runs"]}
            self.assertEqual((batch["complete"], batch["unfinished"]),
                             (expected_complete, 2 - expected_complete))
            for author, observed_ids in ((first, first_ids), (second, second_ids)):
                member = members[author]
                job = jobs[member["job_id"]]
                exported = job["export"]["authors"][0]
                scan = client.get(member["scan_url"]).json()
                saved_scan = client.get(member["scan_manifest_url"]).json()
                self.assertEqual(scan, saved_scan)
                self.assertEqual(scan["counts"]["observed_unique"], len(observed_ids))
                self.assertEqual(scan["counts"]["complete"],
                                 len(observed_ids) - (author == first and expected_complete == 1))
                manifest = client.get(exported["manifest_url"]).json()
                corpus_response = client.get(exported["corpus_url"])
                self.assertEqual(corpus_response.status_code, 200)
                corpus = [json.loads(line) for line in corpus_response.text.splitlines()]
                self.assertEqual({row["item_id"] for row in manifest["items"]}, set(observed_ids))
                complete = {row["item_id"] for row in manifest["items"] if row["detail_state"] == "complete"}
                self.assertEqual({row["item_id"] for row in corpus}, complete)
                self.assertEqual(scan["counts"]["complete"], len(complete))
                self.assertTrue(all("\\" not in ref for row in corpus for ref in row["media_refs"]))
                for row in manifest["items"]:
                    self.assertEqual(bool(row["files"]), row["item_id"] in complete)
                    self.assertTrue(all("\\" not in filename for filename in row["files"].values()))
                    for asset in row["assets"]:
                        self.assertEqual(asset["state"], "complete")
                        self.assertNotIn("\\", asset["relative_path"])
                # Follow every local index/article link through the actual HTTP route,
                # then compare the response with the file a disconnected reader opens.
                pending = [exported["index_url"]]
                pending.extend(urljoin(exported["index_url"], Path(filename).as_posix())
                               for row in manifest["items"] for filename in row["files"].values())
                visited = set()
                while pending:
                    url = pending.pop()
                    if url in visited:
                        continue
                    visited.add(url)
                    response = client.get(url)
                    self.assertEqual(response.status_code, 200, url)
                    local = root / "archive" / unquote(urlsplit(url).path.removeprefix("/archive/"))
                    self.assertEqual(response.content, local.read_bytes(), url)
                    if local.suffix == ".html":
                        links = LocalLinks()
                        links.feed(response.text)
                        pending.extend(urljoin(url, link) for link in links.links)
                    elif local.suffix == ".md":
                        pending.extend(urljoin(url, link) for link in re.findall(r"!?(?:\[[^]]*\])\(([^)]+)\)", response.text)
                                       if not link.startswith("https://"))
                for key in ("manifest_url", "corpus_url", "scan_manifest_url"):
                    url = exported[key]
                    self.assertIn(url, visited)
                for row in manifest["items"]:
                    for filename in row["files"].values():
                        self.assertIn(urljoin(exported["index_url"], Path(filename).as_posix()), visited)
                    for asset in row["assets"]:
                        self.assertIn("/archive/" + asset["relative_path"], visited)
            return batch, members, jobs

        with TestClient(app) as client:
            batch_id = client.post("/api/jobs", json={"mode": "all_archive"}, headers=headers).json()["batch_id"]
            app.state.service.wait()
            batch, members, jobs = read_exports(client, 1)
            self.assertEqual((members[first]["state"], members[second]["state"]), ("partial", "succeeded"))
            self.assertEqual(members[first]["reason"], "author_archive_partial")
            second_export = jobs[members[second]["job_id"]]["export"]["authors"][0]
            preserved = {Path(second_export[key]): (Path(second_export[key]).read_bytes(),
                                                    Path(second_export[key]).stat().st_mtime_ns)
                         for key in ("index", "manifest", "corpus", "scan_manifest")}
            manual = root / "archive" / "xiaohongshu" / first / "manual-note.md"
            manual.write_text("human note", encoding="utf-8")
        fresh = DemoSource()
        fresh.author_pages = source.author_pages
        restarted = create_app(root)
        restarted.state.service.adapter_factory = lambda _: fresh
        with TestClient(restarted) as client:
            response = client.post(f"/api/archive-batches/{batch_id}/resume", json={}, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["resumed_job_ids"], [members[first]["job_id"]])
            restarted.state.service.wait()
            batch, members, _ = read_exports(client, 2)
            self.assertEqual([value for event, value in fresh.events if event == "detail"], [first_ids[0]])
            self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns)
                              for path in preserved}, preserved)
            self.assertEqual(manual.read_text(encoding="utf-8"), "human note")

    def test_missing_and_corrupt_assets_have_no_live_export_links_then_resume(self):
        root = self.root / "damaged-assets"
        first, second = "a" * 24, "b" * 24
        first_ids, second_ids = ids(1, 2), ids(101, 1)

        class FailingMediaSource(DemoSource):
            def download_media(self, candidate, target):
                if target.name in first_ids:
                    raise AdapterFailure("media_failed")
                return super().download_media(candidate, target)

        original = DemoSource()
        original.author_pages = {first: [first_ids], second: [second_ids]}
        app = create_app(root)
        app.state.service.adapter_factory = lambda _: original
        for author in (first, second):
            app.state.service.workflow.subscribe("xiaohongshu", author, author,
                                                 verified=True, evidence="synthetic fixture")
        headers = {"x-creator-archive": "local-validation"}
        with TestClient(app) as client:
            first_batch = client.post("/api/jobs", json={"mode": "all_archive"}, headers=headers).json()["batch_id"]
            app.state.service.wait()
            self.assertEqual(client.get("/api/workspace").json()["archive_batches"][0]["state"], "succeeded")
            with app.state.service.workflow.connect() as db:
                paths = {row["item_id"]: root / "archive" / row["relative_path"] for row in
                         db.execute("SELECT item_id,relative_path FROM assets")}
            paths[first_ids[0]].unlink()
            paths[first_ids[1]].write_bytes(b"corrupt image")
            preserved = {paths[second_ids[0]]: (paths[second_ids[0]].read_bytes(),
                                               paths[second_ids[0]].stat().st_mtime_ns)}
            manual = root / "archive" / "xiaohongshu" / first / "manual-note.md"
            manual.write_text("human note", encoding="utf-8")
            preserved[manual] = (manual.read_bytes(), manual.stat().st_mtime_ns)
            failing = FailingMediaSource()
            failing.author_pages = original.author_pages
            app.state.service.adapter_factory = lambda _: failing
            app.state.service._transport = failing
            batch_id = client.post("/api/jobs", json={"mode": "all_archive"}, headers=headers).json()["batch_id"]
            self.assertNotEqual(batch_id, first_batch)
            app.state.service.wait()
            workspace = client.get("/api/workspace").json()
            batch = next(row for row in workspace["archive_batches"] if row["id"] == batch_id)
            members = {row["author_id"]: row for row in batch["members"]}
            jobs = {row["id"]: row for row in workspace["runs"]}
            self.assertEqual((batch["state"], batch["complete"], batch["unfinished"]), ("partial", 1, 1))
            self.assertEqual((members[first]["state"], members[second]["state"]), ("partial", "succeeded"))
            exported = jobs[members[first]["job_id"]]["export"]["authors"][0]
            manifest = client.get(exported["manifest_url"]).json()
            self.assertEqual(manifest["missing_registered_assets"], 2)
            for row in manifest["items"]:
                self.assertEqual(row["assets"][0]["state"], "missing")
                self.assertEqual(row["media_state"], "partial")
                self.assertEqual(row["detail_state"], "complete")
                self.assertEqual(json.loads(client.get(exported["corpus_url"]).text.splitlines()[
                    first_ids.index(row["item_id"])])["media_refs"], [])
                for filename in row["files"].values():
                    url = urljoin(exported["index_url"], filename)
                    response = client.get(url)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.content, (root / "archive" / url.removeprefix("/archive/")).read_bytes())
                    self.assertNotIn(b"assets/", response.content)
            index = client.get(exported["index_url"])
            self.assertEqual(index.status_code, 200)
            self.assertNotIn(b"assets/", index.content)
            self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in preserved}, preserved)

        repaired = DemoSource()
        repaired.author_pages = original.author_pages
        restarted = create_app(root)
        restarted.state.service.adapter_factory = lambda _: repaired
        with TestClient(restarted) as client:
            self.assertEqual(client.post(f"/api/archive-batches/{batch_id}/resume", json={}, headers=headers).json()[
                "resumed_job_ids"], [members[first]["job_id"]])
            restarted.state.service.wait()
            workspace = client.get("/api/workspace").json()
            batch = next(row for row in workspace["archive_batches"] if row["id"] == batch_id)
            self.assertEqual((batch["state"], batch["complete"], batch["unfinished"]), ("succeeded", 2, 0))
            self.assertEqual([value for event, value in repaired.events if event == "detail"], first_ids)
            job = next(row for row in workspace["runs"] if row["id"] == members[first]["job_id"])
            exported = job["export"]["authors"][0]
            manifest = client.get(exported["manifest_url"]).json()
            self.assertEqual(manifest["missing_registered_assets"], 0)
            corpus = {row["item_id"]: row for row in
                      (json.loads(line) for line in client.get(exported["corpus_url"]).text.splitlines())}
            index = client.get(exported["index_url"])
            self.assertEqual(index.status_code, 200)
            self.assertIn(b"assets/", index.content)
            for row in manifest["items"]:
                asset = row["assets"][0]
                self.assertEqual(asset["state"], "complete")
                self.assertEqual(corpus[row["item_id"]]["media_refs"], [asset["relative_path"]])
                url = "/archive/" + asset["relative_path"]
                response = client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, (root / "archive" / asset["relative_path"]).read_bytes())
                for filename in row["files"].values():
                    self.assertIn(b"assets/", client.get(urljoin(exported["index_url"], filename)).content)
            self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in preserved}, preserved)


if __name__ == "__main__":
    unittest.main()
