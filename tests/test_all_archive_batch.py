"""Fixed all-subscription archive batches using synthetic pages and temporary files."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from creator_archive.app import create_app
from creator_archive.service import WorkspaceService
from tests.test_demo_pipeline import DemoSource


def ids(start, count):
    return [f"{number:024x}" for number in range(start, start + count)]


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

    def test_reopen_interrupted_batch_keeps_original_author_set(self):
        with self.service.workflow.connect() as db:
            old_batch = db.execute("INSERT INTO batches(mode,created_at) VALUES('full',1)").lastrowid
            db.execute("INSERT INTO runs(batch_id,platform,author_id,mode,adapter_version,updated_at) VALUES(?,? ,?,'full','synthetic',1)",
                       (old_batch,"xiaohongshu",self.first))
        self.service._spawn = lambda _: None
        first = self.service.start("all_archive")
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
            resumed = client.post(f"/api/archive-batches/{batch_id}/resume", json={}, headers=headers)
            self.assertEqual(resumed.status_code, 200)
            self.assertEqual(resumed.json()["resumed_job_ids"], [])
            self.assertEqual(client.post("/api/jobs", json={"mode": "all_archive", "author_id": self.first}, headers=headers).status_code, 422)


if __name__ == "__main__":
    unittest.main()
