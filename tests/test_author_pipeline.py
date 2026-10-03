"""Synthetic sources, real SQLite/files/processes; these are not platform evidence."""
import base64
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from creator_archive.service import WorkspaceService
from creator_archive.validation import AdapterFailure, Item, Page


AUTHOR = "b" * 24
IDS = [f"{n:024x}" for n in range(1, 7)]
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII=")


class AuthorSource:
    version = "synthetic-author-pipeline-v1"

    def __init__(self):
        self.events = []
        self.failures = {}

    def page(self, author, cursor):
        self.events.append(("page", cursor))
        number = 1 if cursor is None else int(cursor.split("-")[1])
        pages = [IDS[:2], [IDS[0], IDS[2]], [IDS[3]], [IDS[1], *IDS[4:]], []]
        terminal = number == len(pages)
        return Page(tuple(Item(i, author, "") for i in pages[number - 1]),
                    None if terminal else f"page-{number + 1}", not terminal,
                    "synthetic explicit empty terminal" if terminal else None)

    def prepare_page_details(self, author, ids):
        self.events.append(("prepare", tuple(ids)))

    def prepare_details(self, *args):
        raise AssertionError("Fresh page details must use the page-scoped references")

    def detail(self, author, item, source_url=""):
        self.events.append(("detail", item))
        if item in self.failures:
            raise AdapterFailure(self.failures[item])
        return dict(item_id=item, author_id=author, title="Synthetic author item",
                    text="Author body " + item, content_type="image", published_at="",
                    source_url=f"https://www.xiaohongshu.com/explore/{item}",
                    source="synthetic_author_detail", observed_at=time.time(), metrics={},
                    media=[SimpleNamespace(asset_id="image-000", position=0, kind="image")],
                    missing=[])

    def download_media(self, candidate, target):
        self.events.append(("download", target.name))
        target.mkdir(parents=True, exist_ok=True)
        image = target / "sample.png"
        image.write_bytes(PNG)
        return dict(path=str(image), mime="image/png")


class AuthorPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = AuthorSource()
        self.service = WorkspaceService(self.root, adapter_factory=lambda _: self.source)
        self.addCleanup(self.service.close)
        self.service.workflow.subscribe("xiaohongshu", AUTHOR, "Synthetic author",
                                        verified=True, evidence="synthetic fixture")

    def start(self, mode="author_archive"):
        job = self.service.start(mode, "xiaohongshu", AUTHOR)["job_id"]
        self.service.wait()
        return job

    def job(self, job_id, service=None):
        return next(row for row in (service or self.service).workspace()["runs"] if row["id"] == job_id)

    def children(self, job_id, service=None):
        with (service or self.service).workflow.connect() as db:
            return [tuple(row) for row in db.execute(
                "SELECT page_number,child_job_id FROM pipeline_pages WHERE parent_job_id=? ORDER BY page_number", (job_id,))]

    def resources(self, service=None):
        with (service or self.service).workflow.connect() as db:
            paths = [self.root / "archive" / row[0] for row in db.execute("SELECT relative_path FROM assets")]
        return {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}

    def test_user_pause_keeps_page_checkpoint_and_resume_processes_remaining_items(self):
        entered, release = Event(), Event()
        original_page = self.source.page

        def held_page(author, cursor):
            if cursor is None and not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(5))
            return original_page(author, cursor)

        self.source.page = held_page
        job_id = self.service.start("author_archive", "xiaohongshu", AUTHOR)["job_id"]
        self.assertTrue(entered.wait(5))
        self.assertEqual(self.service.pause(job_id)["state"], "pausing")
        release.set()
        self.service.wait(30)
        paused = self.job(job_id)
        self.assertEqual((paused["state"], paused["reason"], paused["pages"]),
                         ("interrupted", "user_paused", 1))
        with self.service.workflow.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM items WHERE detail_state='complete'").fetchone()[0], 0)
        self.service.resume(job_id)
        self.service.wait(30)
        completed = self.job(job_id)
        self.assertEqual((completed["state"], completed["listed_count"], completed["item_count"]),
                         ("succeeded", 6, 6))

    def test_author_reaches_empty_terminal_and_reuses_fixed_two_page_success_without_changing_old_scope(self):
        old = self.start("page_archive")
        self.assertEqual([v for event, v in self.source.events if event == "page"], [None, "page-2"])
        old_children = self.children(old)
        old_ids = [old, *[child for _, child in old_children]]
        with self.service.workflow.connect() as db:
            schema = db.execute("SELECT sql FROM sqlite_master WHERE name='page_pipelines'").fetchone()[0]
            queries = {
                "jobs": "SELECT * FROM jobs WHERE id IN (" + ",".join(map(str, old_ids)) + ") ORDER BY id",
                "scope": "SELECT * FROM job_items ORDER BY job_id,item_id",
                "pages": "SELECT * FROM pages ORDER BY run_id,page_number",
                "pipeline": "SELECT * FROM page_pipelines ORDER BY parent_job_id",
                "links": "SELECT * FROM pipeline_pages ORDER BY parent_job_id,page_number",
            }
            before = {name: [tuple(row) for row in db.execute(sql)] for name, sql in queries.items()}
        assets = self.resources()
        manual = self.root / "archive" / "xiaohongshu" / AUTHOR / IDS[0] / "article.md"
        manual.write_text("My own notes", encoding="utf-8")
        note = (manual.read_bytes(), manual.stat().st_mtime_ns)
        self.service.close()
        restarted = WorkspaceService(self.root, adapter_factory=lambda _: self.source)
        self.addCleanup(restarted.close)
        self.assertIsNone(restarted.pipeline_migration_backup, "Existing page-pipeline schema needs no rewrite")
        self.source.events.clear()
        new = restarted.start("author_archive", "xiaohongshu", AUTHOR)["job_id"]
        restarted.wait()
        result = self.job(new, restarted)
        self.assertEqual((result["state"], result["coverage"], result["target_count"], result["item_count"]),
                         ("succeeded", "complete_for_accessible_scope", 6, 6))
        self.assertIsNone(result["page_limit"])
        self.assertTrue(result["until_terminal"])
        self.assertEqual([v for event, v in self.source.events if event == "detail"], IDS[3:])
        self.assertEqual(len(self.children(new, restarted)), 5)
        self.assertEqual(self.children(old, restarted), old_children)
        with restarted.workflow.connect() as db:
            self.assertEqual(db.execute("SELECT sql FROM sqlite_master WHERE name='page_pipelines'").fetchone()[0], schema)
            for name, sql in queries.items():
                after = [tuple(row) for row in db.execute(sql)]
                self.assertEqual(after[:len(before[name])], before[name], name)
            self.assertEqual(db.execute("SELECT count(*) FROM metric_snapshots").fetchone()[0], 6)
        self.assertTrue(all(self.resources(restarted)[key] == value for key, value in assets.items()))
        self.assertEqual((manual.read_bytes(), manual.stat().st_mtime_ns), note)
        manifest = json.loads(Path(result["export"]["authors"][0]["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["coverage"], "complete_for_accessible_scope")
        self.assertEqual(len(manifest["items"]), 6)

    def test_per_execution_budget_remains_partial_then_resumes_same_run_until_evidenced_end(self):
        with patch("creator_archive.page_pipeline.AUTHOR_PAGE_BUDGET", 2):
            job = self.start()
            first = self.job(job)
            self.assertEqual((first["state"], first["reason"], first["coverage"]),
                             ("partial", "page_budget_reached", "partial"))
            children = self.children(job)
            self.service.resume(job)
            self.service.wait()
            second = self.job(job)
            self.assertEqual((second["state"], second["reason"]), ("partial", "page_budget_reached"))
            self.assertEqual(second["run_id"], first["run_id"])
            self.assertEqual(len(self.children(job)), 4)
            self.assertEqual(self.children(job)[:2], children)
            self.service.resume(job)
            self.service.wait()
        final = self.job(job)
        self.assertEqual((final["state"], final["coverage"]), ("succeeded", "complete_for_accessible_scope"))
        self.assertEqual(final["run_id"], first["run_id"])
        self.assertEqual([v for event, v in self.source.events if event == "page"],
                         [None, "page-2", "page-3", "page-4", "page-5"])
        self.assertEqual([v for event, v in self.source.events if event == "detail"], IDS)

    def test_invalid_deep_page_or_login_failure_never_becomes_terminal_and_keeps_committed_prefix(self):
        original = self.source.page
        cases = [Page((), None, False), Page((), "page-4", True),
                 Page((Item(IDS[3], AUTHOR, ""),), "page-2", True),
                 Page((Item(IDS[3], AUTHOR, ""),), "page-4", True, "contradictory end"),
                 AdapterFailure("needs_login")]
        for bad in cases:
            with self.subTest(response=repr(bad)):
                def invalid(author, cursor):
                    if cursor == "page-3":
                        if isinstance(bad, Exception):
                            raise bad
                        return bad
                    return original(author, cursor)
                with patch.object(self.source, "page", side_effect=invalid):
                    job = self.start()
                result = self.job(job)
                self.assertNotEqual(result["state"], "succeeded")
                self.assertEqual(result["coverage"], "partial")
                with self.service.workflow.connect() as db:
                    run = db.execute("SELECT pages,cursor,terminal_evidence FROM runs WHERE id=?", (result["run_id"],)).fetchone()
                    self.assertEqual(tuple(run), (2, "page-3", None))
                    self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE state IN ('running','queued')").fetchone()[0], 0)
                prefix = self.children(job)
                self.service.resume(job)
                self.service.wait()
                self.assertEqual(self.children(job)[:2], prefix)
                self.assertEqual(self.job(job)["state"], "succeeded")

    def test_terminal_list_and_partial_content_are_separate_and_retry_reuses_success(self):
        self.source.failures[IDS[3]] = "item_unavailable"
        job = self.start()
        result = self.job(job)
        self.assertEqual((result["state"], result["coverage"], result["failed_count"]),
                         ("partial", "complete_for_accessible_scope", 1))
        children = self.children(job)
        assets = self.resources()
        self.source.events.clear()
        self.source.failures.clear()
        self.service.resume(job)
        self.service.wait()
        self.assertEqual([v for event, v in self.source.events if event == "page"], ["page-3"])
        self.assertEqual([v for event, v in self.source.events if event == "detail"], [IDS[3]])
        self.assertEqual(self.children(job), children)
        self.assertEqual(self.job(job)["state"], "succeeded")
        self.assertTrue(all(self.resources()[key] == value for key, value in assets.items()))

    def test_hard_process_exit_after_third_page_resumes_deeper_checkpoint_and_success_resources(self):
        self.service.close()
        script = """
import os, runpy, sys
from pathlib import Path
ns = runpy.run_path(sys.argv[1])
source = ns['AuthorSource']()
service = ns['WorkspaceService'](Path(sys.argv[2]), adapter_factory=lambda _: source)
execute = service._execute_content
def stop_after_third(*args, **kwargs):
    execute(*args, **kwargs)
    if sum(event == 'page' for event, value in source.events) == 3:
        os._exit(73)
service._execute_content = stop_after_third
service.start('author_archive', 'xiaohongshu', ns['AUTHOR'])
service.wait()
raise RuntimeError('Crash boundary was not reached')
"""
        process = subprocess.run([sys.executable, "-c", script, str(Path(__file__).resolve()), str(self.root)],
                                 cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 73, process.stderr)
        with closing(sqlite3.connect(self.service.workflow.db_path)) as db:
            job, run = db.execute("SELECT id,run_id FROM jobs WHERE mode='author_archive'").fetchone()
            self.assertEqual(db.execute("SELECT pages,cursor FROM runs WHERE id=?", (run,)).fetchone(), (3, "page-4"))
        assets = self.resources()
        children = self.children(job)
        restarted = WorkspaceService(self.root, adapter_factory=lambda _: self.source)
        self.addCleanup(restarted.close)
        restarted.resume(job)
        restarted.wait()
        self.assertEqual([v for event, v in self.source.events if event == "page"], ["page-4", "page-5"])
        self.assertEqual([v for event, v in self.source.events if event == "detail"], IDS[4:])
        self.assertEqual(self.children(job, restarted)[:3], children)
        self.assertEqual(self.job(job, restarted)["state"], "succeeded")
        self.assertTrue(all(self.resources(restarted)[key] == value for key, value in assets.items()))
        with restarted.workflow.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM metric_snapshots").fetchone()[0], 6)


if __name__ == "__main__":
    unittest.main()
