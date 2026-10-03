"""Fixed ten-item Demo regressions with synthetic platform/media responses."""
import json
from pathlib import Path
import tempfile
import unittest

from creator_archive.service import WorkspaceService
from creator_archive.validation import Item, Page
from tests.test_author_pipeline import AUTHOR, AuthorSource


IDS = [f"{number:024x}" for number in range(1, 41)]


class DemoSource(AuthorSource):
    version = "synthetic-demo-pipeline-v1"

    def __init__(self, pages=None):
        super().__init__()
        self.pages = pages or [IDS[:32], IDS[32:]]
        self.author_pages = {}

    def page(self, author, cursor):
        self.events.extend([("author", author), ("page", cursor)])
        pages = self.author_pages.get(author, self.pages)
        number = 0 if cursor is None else int(cursor.split("-")[1]) - 1
        terminal = number == len(pages) - 1
        return Page(tuple(Item(item, author, "") for item in pages[number]),
                    None if terminal else f"page-{number + 2}", not terminal,
                    "synthetic explicit terminal" if terminal else None)


class DemoPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = DemoSource()
        self.service = WorkspaceService(self.root, adapter_factory=lambda _: self.source)
        self.addCleanup(self.service.close)
        self.service.workflow.subscribe("xiaohongshu", AUTHOR, "Synthetic Demo author",
                                        verified=True, evidence="synthetic fixture")

    def start(self, mode="demo_archive"):
        job = self.service.start(mode, "xiaohongshu", AUTHOR)["job_id"]
        self.service.wait()
        return job

    def job(self, job_id, service=None):
        return next(row for row in (service or self.service).workspace()["runs"] if row["id"] == job_id)

    def scope(self, parent, service=None):
        with (service or self.service).workflow.connect() as db:
            return [tuple(row) for row in db.execute("""
                SELECT p.page_number,p.child_job_id,j.item_id FROM pipeline_pages p
                JOIN job_items j ON j.job_id=p.child_job_id WHERE p.parent_job_id=?
                ORDER BY p.page_number,j.item_id""", (parent,))]

    def assets(self, service=None):
        with (service or self.service).workflow.connect() as db:
            paths = [self.root / "archive" / row[0] for row in db.execute("SELECT relative_path FROM assets")]
        return {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}

    def test_32_item_first_page_selects_ten_and_failure_restart_never_substitutes_eleventh(self):
        self.source.failures[IDS[4]] = "item_unavailable"
        parent = self.start()
        first = self.job(parent)
        self.assertEqual((first["state"], first["target_count"], first["item_count"], first["failed_count"]),
                         ("partial", 10, 9, 1))
        self.assertEqual(first["item_limit"], 10)
        self.assertIsNone(first["page_limit"])
        self.assertFalse(first["until_terminal"])
        self.assertEqual([value for event, value in self.source.events if event == "page"], [None])
        self.assertEqual([value for event, value in self.source.events if event == "detail"], IDS[:10])
        self.assertEqual(len([1 for event, value in self.source.events if event == "download"]), 9)
        fixed = self.scope(parent)
        self.assertEqual([row[2] for row in fixed], IDS[:10])
        assets = self.assets()
        with self.service.workflow.connect() as db:
            observed = json.loads(db.execute("SELECT item_ids FROM pages WHERE run_id=?", (first["run_id"],)).fetchone()[0])
            self.assertEqual(observed, IDS[:32], "Persist actual list evidence without truncating it to Demo scope")
        self.service.close()
        fresh = DemoSource()
        restarted = WorkspaceService(self.root, adapter_factory=lambda _: fresh)
        self.addCleanup(restarted.close)
        restarted.resume(parent)
        restarted.wait()
        final = self.job(parent, restarted)
        self.assertEqual((final["state"], final["target_count"], final["item_count"]), ("succeeded", 10, 10))
        self.assertEqual(final["run_id"], first["run_id"])
        self.assertEqual(self.scope(parent, restarted), fixed)
        self.assertNotEqual(final["coverage"], "complete_for_accessible_scope")
        self.assertEqual([value for event, value in fresh.events if event == "detail"], [IDS[4]])
        self.assertEqual([value for event, value in fresh.events if event == "page"], [None])
        self.assertTrue(all(self.assets(restarted)[key] == value for key, value in assets.items()))
        with restarted.workflow.connect() as db:
            counts = dict(db.execute("SELECT item_id,count(*) FROM metric_snapshots GROUP BY item_id"))
            self.assertEqual({item: counts[item] for item in IDS[:10] if item != IDS[4]},
                             {item: 1 for item in IDS[:10] if item != IDS[4]},
                             "The nine successful items must not acquire repeated snapshots")
            self.assertEqual(set(counts), set(IDS[:10]))

    def test_cross_page_duplicates_select_first_ten_unique_in_observed_order_then_stop(self):
        first = [IDS[9], IDS[2], IDS[19], IDS[0], IDS[12], IDS[8]]
        second = [IDS[9], IDS[0], IDS[17], IDS[3], IDS[5], IDS[1], IDS[4], IDS[6]]
        selected = first + [IDS[17], IDS[3], IDS[5], IDS[1]]
        self.source.pages = [first, second, IDS[20:]]
        parent = self.start()
        result = self.job(parent)
        self.assertEqual((result["state"], result["target_count"], result["item_count"]), ("succeeded", 10, 10))
        self.assertEqual([value for event, value in self.source.events if event == "page"], [None, "page-2"])
        self.assertCountEqual([value for event, value in self.source.events if event == "detail"], selected)
        self.assertEqual(len(self.scope(parent)), 10, "Duplicate pinned IDs do not occupy another child target")
        self.assertCountEqual([row[2] for row in self.scope(parent)], selected)

    def test_batch_creates_ten_per_confirmed_author_including_paused_but_excludes_other_scopes(self):
        second, pending, unverified, wechat = (char * 24 for char in "cdef")
        workflow = self.service.workflow
        workflow.subscribe("xiaohongshu", second, "Confirmed paused", verified=True, evidence="synthetic fixture")
        workflow.set_enabled("xiaohongshu", second, False)
        workflow.subscribe("xiaohongshu", pending, "Needs confirmation", verified=True, evidence="observed_browser_exact_note")
        workflow.set_enabled("xiaohongshu", pending, False)
        self.service.subscribe(f"https://www.xiaohongshu.com/user/profile/{unverified}")
        workflow.subscribe("wechat", wechat, "WeChat", verified=True, evidence="synthetic fixture")
        second_ids = [f"{number:024x}" for number in range(101, 133)]
        self.source.author_pages[second] = [second_ids, [f"{200:024x}"]]
        with workflow.connect() as db:
            subscriptions = [tuple(row) for row in db.execute("SELECT * FROM subscriptions ORDER BY platform,author_id")]
        jobs = self.service.start("demo_archive")["job_ids"]
        self.service.wait()
        self.assertEqual(len(jobs), 2)
        results = [self.job(job) for job in jobs]
        self.assertEqual({row["author_id"] for row in results}, {AUTHOR, second})
        self.assertTrue(all((row["state"], row["item_count"], row["target_count"]) == ("succeeded", 10, 10) for row in results))
        self.assertEqual(set(value for event, value in self.source.events if event == "author"), {AUTHOR, second})
        self.assertCountEqual([value for event, value in self.source.events if event == "detail"], IDS[:10] + second_ids[:10])
        with workflow.connect() as db:
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM subscriptions ORDER BY platform,author_id")], subscriptions)

    def test_demo_does_not_resize_completed_legacy_page_or_author_job_scopes(self):
        self.source.pages = [IDS[:2], IDS[2:4], IDS[4:6]]
        old_page = self.start("page_archive")
        old_author = self.start("author_archive")
        with self.service.workflow.connect() as db:
            old_jobs = [row[0] for row in db.execute("SELECT id FROM jobs")]
            old_runs = [row[0] for row in db.execute("SELECT id FROM runs")]
            queries = {
                "jobs": "SELECT * FROM jobs WHERE id IN (" + ",".join(map(str, old_jobs)) + ") ORDER BY id",
                "scope": "SELECT * FROM job_items WHERE job_id IN (" + ",".join(map(str, old_jobs)) + ") ORDER BY job_id,item_id",
                "pages": "SELECT * FROM pages WHERE run_id IN (" + ",".join(map(str, old_runs)) + ") ORDER BY run_id,page_number",
                "links": f"SELECT * FROM pipeline_pages WHERE parent_job_id IN ({old_page},{old_author}) ORDER BY parent_job_id,page_number",
            }
            before = {name: [tuple(row) for row in db.execute(sql)] for name, sql in queries.items()}
        assets = self.assets()
        self.source.pages = [IDS[:32], IDS[32:]]
        self.source.events.clear()
        demo = self.start()
        self.assertEqual(self.job(demo)["item_count"], 10)
        self.assertEqual([value for event, value in self.source.events if event == "detail"], IDS[6:10])
        with self.service.workflow.connect() as db:
            self.assertEqual({name: [tuple(row) for row in db.execute(sql)] for name, sql in queries.items()}, before)
        self.assertTrue(all(self.assets()[key] == value for key, value in assets.items()))


if __name__ == "__main__":
    unittest.main()
