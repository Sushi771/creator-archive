"""Offline page-pipeline risks, real SQLite/files and hard-process recovery.

The source and media are synthetic. These tests are not platform evidence.
"""
import base64
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from creator_archive.app import create_app
from creator_archive.service import WorkspaceService
from creator_archive.validation import AdapterFailure, Item, Page


AUTHOR = "a" * 24
IDS = [f"{n:024x}" for n in range(1, 5)]
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII=")


class PageSource:
    """Two observed pages, including a pinned duplicate; a third page exists."""
    version = "synthetic-page-pipeline-v1"

    def __init__(self, *, terminal=False):
        self.events = []
        self.failures = {}
        self.terminal = terminal

    def page(self, author, cursor):
        self.events.append(("page", cursor))
        if cursor is None:
            return Page(tuple(Item(i, author, "") for i in IDS[:2]), "page-2", True)
        if cursor == "page-2":
            return Page(tuple(Item(i, author, "") for i in (IDS[0], IDS[2])),
                        None if self.terminal else "page-3", not self.terminal,
                        "synthetic explicit terminal" if self.terminal else None)
        raise AssertionError("The fixed two-page experiment must not fetch page 3")

    def prepare_page_details(self, author, ids):
        self.events.append(("prepare", tuple(ids)))

    def prepare_details(self, author, ids):
        raise AssertionError("Fresh page references must not invoke the general prefix refresh")

    def detail(self, author, item, source_url=""):
        self.events.append(("detail", item))
        if item in self.failures:
            raise AdapterFailure(self.failures[item])
        return dict(item_id=item, author_id=author, title="Synthetic page item",
                    text="Offline body " + item, content_type="image", published_at="",
                    source_url=f"https://www.xiaohongshu.com/explore/{item}",
                    source="synthetic_page_detail", observed_at=time.time(), metrics={},
                    media=[SimpleNamespace(asset_id="image-000", position=0, kind="image")],
                    missing=[])

    def download_media(self, candidate, target):
        self.events.append(("download", target.name))
        target.mkdir(parents=True, exist_ok=True)
        image = target / "sample.png"
        image.write_bytes(PNG)
        return dict(path=str(image), mime="image/png")


class PagePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = PageSource()
        self.service = WorkspaceService(self.root, adapter_factory=lambda _: self.source)
        self.addCleanup(self.service.close)
        self.service.workflow.subscribe("xiaohongshu", AUTHOR, "Synthetic author",
                                        verified=True, evidence="synthetic fixture")

    def start(self):
        job = self.service.start("page_archive", "xiaohongshu", AUTHOR)["job_id"]
        self.service.wait()
        return job

    def job(self, job_id, service=None):
        return next(row for row in (service or self.service).workspace()["runs"] if row["id"] == job_id)

    def children(self, job_id, service=None):
        with (service or self.service).workflow.connect() as db:
            return [tuple(row) for row in db.execute(
                "SELECT page_number,child_job_id FROM pipeline_pages WHERE parent_job_id=? ORDER BY page_number", (job_id,))]

    def asset_snapshot(self, service=None):
        workflow = (service or self.service).workflow
        with workflow.connect() as db:
            paths = [self.root / "archive" / row[0] for row in db.execute("SELECT relative_path FROM assets")]
        return {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}

    def test_two_pages_consume_details_before_next_page_deduplicate_and_read_offline(self):
        job = self.start()
        self.assertEqual([value for event, value in self.source.events if event == "page"], [None, "page-2"])
        self.assertEqual([value for event, value in self.source.events if event == "detail"], IDS[:3])
        self.assertLess(self.source.events.index(("download", IDS[1])), self.source.events.index(("page", "page-2")))
        self.assertEqual(len(self.children(job)), 2)
        result = self.job(job)
        self.assertEqual((result["target_count"], result["item_count"]), (3, 3))
        self.assertNotEqual(result["coverage"], "complete_for_accessible_scope")
        with self.service.workflow.connect() as db:
            run = db.execute("SELECT * FROM runs WHERE id=?", (result["run_id"],)).fetchone()
            self.assertEqual((run["pages"], run["cursor"], run["terminal_evidence"]), (2, "page-3", None))
        author = result["export"]["authors"][0]
        self.assertEqual(len(Path(author["corpus"]).read_text(encoding="utf-8").splitlines()), 3)
        manifest = json.loads(Path(author["manifest"]).read_text(encoding="utf-8"))
        self.assertNotEqual(manifest["coverage"], "complete_for_accessible_scope")
        # A local HTTP client actually reads the exported article and attachment.
        with TestClient(create_app(self.root)) as client:
            self.assertEqual(client.get(author["index_url"]).status_code, 200)
            for item in manifest["items"]:
                html = client.get(f"/archive/xiaohongshu/{AUTHOR}/" + item["files"]["index.html"])
                self.assertEqual(html.status_code, 200)
                self.assertIn("Offline body " + item["item_id"], html.text)
                for asset in item["assets"]:
                    response = client.get("/archive/" + asset["relative_path"].replace("\\", "/"))
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.content, PNG)

    def test_terminal_evidence_is_distinct_from_fixed_page_budget(self):
        self.source.terminal = True
        job = self.start()
        result = self.job(job)
        self.assertEqual(result["coverage"], "complete_for_accessible_scope")
        self.assertEqual(result["state"], "succeeded")
        with self.service.workflow.connect() as db:
            self.assertEqual(db.execute("SELECT terminal_evidence FROM runs WHERE id=?", (result["run_id"],)).fetchone()[0],
                             "synthetic explicit terminal")

    def test_between_pages_restart_retains_child_identity_success_and_manual_files(self):
        execute = self.service._execute_content
        def stop_after_content(*args, **kwargs):
            execute(*args, **kwargs)
            self.service._stopping.set()
        with patch.object(self.service, "_execute_content", side_effect=stop_after_content):
            job = self.start()
        self.assertEqual([v for event, v in self.source.events if event == "page"], [None])
        first_child = self.children(job)
        self.assertEqual(len(first_child), 1)
        before = self.asset_snapshot()
        manual = self.root / "archive" / "xiaohongshu" / AUTHOR / IDS[0] / "article.md"
        manual.parent.mkdir(parents=True, exist_ok=True)
        manual.write_text("User-authored notes must survive", encoding="utf-8")
        baseline = (manual.read_bytes(), manual.stat().st_mtime_ns)
        self.service.close()
        source = PageSource()
        restarted = WorkspaceService(self.root, adapter_factory=lambda _: source)
        self.addCleanup(restarted.close)
        restarted.resume(job)
        restarted.wait()
        self.assertEqual(self.children(job, restarted)[:1], first_child)
        self.assertEqual([v for event, v in source.events if event == "detail"], [IDS[2]])
        self.assertEqual([v for event, v in source.events if event == "page"], ["page-2"])
        self.assertEqual((manual.read_bytes(), manual.stat().st_mtime_ns), baseline)
        self.assertTrue(all(self.asset_snapshot(restarted)[key] == value for key, value in before.items()))
        with restarted.workflow.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM metric_snapshots WHERE item_id=?", (IDS[0],)).fetchone()[0], 1)

    def test_partial_item_does_not_block_next_page_and_parent_resume_keeps_fixed_scope(self):
        self.source.failures[IDS[1]] = "item_unavailable"
        job = self.start()
        self.assertEqual([v for event, v in self.source.events if event == "detail"], IDS[:3])
        result = self.job(job)
        self.assertEqual((result["state"], result["failed_count"]), ("partial", 1))
        self.assertIsNotNone(result["export"])
        failures = self.service.job_failures(job)
        self.assertEqual(failures["total"], 1)
        self.assertEqual(failures["items"][0]["item_id"], IDS[1])
        self.assertEqual(failures["items"][0]["reason"], "item_unavailable")
        self.assertTrue(failures["items"][0]["next_step"])
        self.assertNotIn("补充", failures["items"][0]["next_step"],
                         "The parent does not accept supplemental links; advice must use its supported recovery")
        children = self.children(job)
        baseline = self.asset_snapshot()
        with self.service.workflow.connect() as db:
            db.execute("INSERT INTO items(platform,item_id,author_id,published_at) VALUES('xiaohongshu',?,?,'')", (IDS[3], AUTHOR))
        self.service.close()
        source = PageSource()
        restarted = WorkspaceService(self.root, adapter_factory=lambda _: source)
        self.addCleanup(restarted.close)
        restarted.resume(job)
        restarted.wait()
        self.assertEqual(self.children(job, restarted), children)
        self.assertEqual([v for event, v in source.events if event == "detail"], [IDS[1]])
        self.assertEqual(self.job(job, restarted)["target_count"], 3)
        self.assertEqual(self.job(job, restarted)["failed_count"], 0)
        with restarted.workflow.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM job_items WHERE item_id=?", (IDS[3],)).fetchone()[0], 0)
        self.assertTrue(all(self.asset_snapshot(restarted)[key] == value for key, value in baseline.items()))

    def test_sql_abort_during_page_link_rolls_back_items_page_and_content_child(self):
        with self.service.workflow.connect() as db:
            db.execute("CREATE TRIGGER test_atomic_page BEFORE INSERT ON pipeline_pages BEGIN SELECT RAISE(ABORT, 'controlled failure'); END")
        job = self.start()
        with self.service.workflow.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM pages").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM items").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM job_items").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT pages,cursor FROM runs WHERE id=?", (self.job(job)["run_id"],)).fetchone()[:], (0, None))
            db.execute("DROP TRIGGER test_atomic_page")
        self.service.resume(job)
        self.service.wait()
        self.assertEqual(len(self.children(job)), 2)
        self.assertEqual(self.job(job)["item_count"], 3)

    def test_preparation_login_failure_closes_child_and_parent_can_resume_in_same_process(self):
        with patch.object(self.source, "prepare_page_details", side_effect=AdapterFailure("needs_login")):
            job = self.start()
        first_child = self.children(job)
        self.assertEqual(len(first_child), 1)
        self.assertEqual(self.job(job)["state"], "needs_login")
        with self.service.workflow.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0], 0)
        with self.assertRaisesRegex(ValueError, str(job)):
            self.service.resume(first_child[0][1])
        self.service.resume(job)
        self.service.wait()
        self.assertEqual(self.children(job)[:1], first_child)
        self.assertEqual(self.job(job)["item_count"], 3)

    def test_second_page_timeout_reports_partial_list_coverage_and_preserves_cursor(self):
        page = self.source.page
        def fail_second(author, cursor):
            if cursor == "page-2":
                raise AdapterFailure("timeout")
            return page(author, cursor)
        with patch.object(self.source, "page", side_effect=fail_second):
            job = self.start()
        result = self.job(job)
        self.assertEqual(result["reason"], "timeout")
        self.assertEqual(result["coverage"], "partial")
        children = self.children(job)
        baseline = self.asset_snapshot()
        with self.service.workflow.connect() as db:
            run = db.execute("SELECT pages,cursor,reason FROM runs WHERE id=?", (result["run_id"],)).fetchone()
            self.assertEqual(tuple(run), (1, "page-2", "timeout"))
        self.service.resume(job)
        self.service.wait()
        self.assertEqual(self.children(job)[:1], children)
        self.assertEqual(self.job(job)["item_count"], 3)
        self.assertTrue(all(self.asset_snapshot()[key] == value for key, value in baseline.items()))

    def test_distinct_cursor_observation_failures_preserve_one_checkpoint(self):
        original = self.source.page
        failure = ["cursor_response_conflict"]
        def fail_second(author, cursor):
            if cursor == "page-2" and failure[0]:
                raise AdapterFailure(failure[0])
            return original(author, cursor)
        with patch.object(self.source, "page", side_effect=fail_second):
            job = self.start()
            baseline_children = self.children(job)
            baseline_assets = self.asset_snapshot()
            for category in ("cursor_response_conflict", "requested_response_missing"):
                if category != failure[0]:
                    failure[0] = category
                    self.service.resume(job)
                    self.service.wait()
                result = self.job(job)
                self.assertEqual((result["state"], result["reason"], result["coverage"]),
                                 ("blocked", category, "partial"))
                self.assertIn("检查点", result["next_step"])
                self.assertFalse(result["list_finished"])
                self.assertEqual(self.children(job), baseline_children)
                with self.service.workflow.connect() as db:
                    run = db.execute("SELECT pages,cursor,terminal_evidence,reason FROM runs WHERE id=?",
                                     (result["run_id"],)).fetchone()
                    self.assertEqual(tuple(run), (1, "page-2", None, category))
                self.assertEqual(self.asset_snapshot(), baseline_assets)
            failure[0] = None
            self.service.resume(job)
            self.service.wait()
        self.assertEqual(self.children(job)[:1], baseline_children)
        self.assertEqual(self.job(job)["item_count"], 3)
        self.assertTrue(all(self.asset_snapshot()[key] == value for key, value in baseline_assets.items()))

    def test_reuse_file_permission_error_does_not_leave_active_child_or_block_recovery(self):
        workflow = self.service.workflow
        with workflow.connect() as db:
            db.execute("INSERT INTO items(platform,item_id,author_id,published_at) VALUES('xiaohongshu',?,?,'')", (IDS[0], AUTHOR))
        workflow.save_detail("xiaohongshu", IDS[0], AUTHOR, "Existing body",
                             f"https://www.xiaohongshu.com/explore/{IDS[0]}")
        saved = self.root / "previous.png"
        saved.write_bytes(PNG)
        workflow.attach_media("xiaohongshu", IDS[0], "image-000", saved,
                              position=0, kind="image", mime="image/png")
        with workflow.connect() as db:
            db.execute("UPDATE items SET media_state='complete_for_observed_detail' WHERE item_id=?", (IDS[0],))
        baseline = self.asset_snapshot()
        with patch.object(workflow, "asset_valid", side_effect=PermissionError("controlled temporary read denial")):
            job = self.start()
        self.assertEqual(self.job(job)["state"], "failed")
        with workflow.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0], 0)
        children = self.children(job)
        self.assertEqual(len(children), 1)
        self.source.open_login = lambda: {"state": "synthetic_ready"}
        with patch.object(self.service.account, "start", return_value={"state":"synthetic_ready"}):
            self.assertEqual(self.service.open_login(), {"state": "synthetic_ready"})
        self.service.resume(job)
        self.service.wait()
        self.assertEqual(self.children(job)[:1], children)
        self.assertEqual(self.job(job)["item_count"], 3)
        self.assertNotIn(("detail", IDS[0]), self.source.events)
        self.assertEqual(self.service.item("xiaohongshu", IDS[0])["detail_text"], "Existing body")
        self.assertTrue(all(self.asset_snapshot()[key] == value for key, value in baseline.items()))

    def test_hard_process_exit_after_page_commit_recovers_same_child(self):
        self.service.close()
        test_file = str(Path(__file__).resolve())
        script = """
import os, runpy, sys
from pathlib import Path
ns = runpy.run_path(sys.argv[1])
source = ns['PageSource']()
source.prepare_page_details = lambda *args: os._exit(73)
service = ns['WorkspaceService'](Path(sys.argv[2]), adapter_factory=lambda _: source)
service.start('page_archive', 'xiaohongshu', ns['AUTHOR'])
service.wait()
raise RuntimeError('Crash boundary was not reached')
"""
        process = subprocess.run([sys.executable, "-c", script, test_file, str(self.root)],
                                 cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 73, process.stderr)
        with closing(sqlite3.connect(self.service.workflow.db_path)) as db:
            job = db.execute("SELECT id FROM jobs WHERE mode='page_archive'").fetchone()[0]
            child = db.execute("SELECT child_job_id FROM pipeline_pages WHERE parent_job_id=?", (job,)).fetchone()[0]
            self.assertEqual(db.execute("SELECT count(*) FROM pages").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM job_items WHERE job_id=?", (child,)).fetchone()[0], 2)
        source = PageSource()
        restarted = WorkspaceService(self.root, adapter_factory=lambda _: source)
        self.addCleanup(restarted.close)
        restarted.resume(job)
        restarted.wait()
        self.assertEqual(self.children(job, restarted)[0], (1, child))
        self.assertEqual(self.job(job, restarted)["item_count"], 3)
        self.assertEqual([v for event, v in source.events if event == "detail"], IDS[:3])

    def test_archive_failure_restarts_without_any_new_platform_request(self):
        with patch.object(self.service.workflow, "export_all", side_effect=OSError("controlled archive failure")):
            job = self.start()
        self.assertEqual(self.job(job)["state"], "failed")
        self.assertEqual(self.job(job)["stage"], "archive")
        children = self.children(job)
        baseline = self.asset_snapshot()
        self.service.close()
        source = PageSource()
        restarted = WorkspaceService(self.root, adapter_factory=lambda _: source)
        self.addCleanup(restarted.close)
        restarted.resume(job)
        restarted.wait()
        self.assertEqual(source.events, [], "Only export remains; listing/detail/download must not run again")
        self.assertEqual(self.children(job, restarted), children)
        self.assertEqual(self.job(job, restarted)["state"], "succeeded")
        self.assertIsNotNone(self.job(job, restarted)["export"])
        self.assertEqual(self.asset_snapshot(restarted), baseline)

    def test_subscription_only_old_database_is_also_backed_up_before_upgrade(self):
        self.service.close()
        with closing(sqlite3.connect(self.service.workflow.db_path)) as db:
            db.execute("DROP TABLE pipeline_pages")
            db.execute("DROP TABLE page_pipelines")
            db.commit()
            subscriptions = db.execute("SELECT * FROM subscriptions").fetchall()
            self.assertTrue(subscriptions)
            self.assertEqual(db.execute("SELECT count(*) FROM items").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        before = set((self.root / "backups").glob("*.sqlite3"))
        restarted = WorkspaceService(self.root, adapter_factory=lambda _: self.source)
        self.addCleanup(restarted.close)
        added = set((self.root / "backups").glob("*.sqlite3")) - before
        self.assertEqual(len(added), 1)
        with closing(sqlite3.connect(next(iter(added)))) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT * FROM subscriptions").fetchall(), subscriptions)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='page_pipelines'").fetchone())

    def test_additive_upgrade_backs_up_old_schema_and_preserves_existing_rows(self):
        self.service.close()
        db_path = self.service.workflow.db_path
        with closing(sqlite3.connect(db_path)) as db:
            db.execute("DROP TABLE pipeline_pages")
            db.execute("DROP TABLE page_pipelines")
            db.execute("INSERT INTO items(platform,item_id,author_id,published_at) VALUES('xiaohongshu',?,?,'')", (IDS[3], AUTHOR))
            db.commit()
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            before = {name: db.execute('SELECT * FROM "' + name + '"').fetchall() for name in tables}
        backups_before = set((self.root / "backups").glob("*.sqlite3"))
        restarted = WorkspaceService(self.root, adapter_factory=lambda _: self.source)
        self.addCleanup(restarted.close)
        with restarted.workflow.connect() as db:
            self.assertEqual({name: [tuple(row) for row in db.execute('SELECT * FROM "' + name + '"')] for name in tables}, before)
        backups_after = set((self.root / "backups").glob("*.sqlite3"))
        self.assertEqual(len(backups_after - backups_before), 1)
        with closing(sqlite3.connect(next(iter(backups_after - backups_before)))) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual({name: db.execute('SELECT * FROM "' + name + '"').fetchall() for name in tables}, before)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='page_pipelines'").fetchone())
        again = WorkspaceService(self.root, adapter_factory=lambda _: self.source)
        self.addCleanup(again.close)
        self.assertEqual(set((self.root / "backups").glob("*.sqlite3")), backups_after)


if __name__ == "__main__":
    unittest.main()
