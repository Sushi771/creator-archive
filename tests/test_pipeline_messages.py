"""Regression for parent recovery advice; local SQLite only, no platform calls."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from creator_archive.service import WorkspaceService


AUTHOR = "c" * 24


class PipelineMessageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.service = WorkspaceService(Path(temporary.name))
        self.addCleanup(self.service.close)
        self.service.workflow.subscribe("xiaohongshu", AUTHOR, "Synthetic author",
                                        verified=True, evidence="synthetic fixture")

    def create_failed_job(self, mode, reason):
        with patch.object(self.service, "_spawn"):
            job_id = self.service.start(mode, "xiaohongshu", AUTHOR)["job_id"]
        with self.service.workflow.connect() as db:
            db.execute("UPDATE page_pipelines SET stage='content' WHERE parent_job_id=?", (job_id,))
        self.service._finish(job_id, "blocked", reason)
        return next(row for row in self.service.workspace()["runs"] if row["id"] == job_id)

    def test_parent_advice_uses_supported_resume_without_supplemental_links(self):
        for mode in ("page_archive", "author_archive", "demo_archive"):
            for reason in ("unavailable", "timeout", "reference_missing", "item_unavailable"):
                with self.subTest(mode=mode, reason=reason):
                    job = self.create_failed_job(mode, reason)
                    self.assertTrue(job["can_resume"])
                    self.assertIn("恢复本父任务", job["next_step"])
                    self.assertIn("原检查点", job["next_step"])
                    self.assertIn("成功资源会复用", job["next_step"])
                    self.assertNotIn("粘贴", job["next_step"])
                    self.assertNotIn("补充", job["next_step"])
                    if reason in {"unavailable", "timeout"}:
                        self.assertIn("作品详情或媒体", job["message"])
                        self.assertNotIn("作者列表等待超时", job["message"])

    def test_standalone_content_recovery_points_to_background_source(self):
        job = self.create_failed_job("content", "unavailable")
        self.assertIn("后台来源", job["next_step"])
        self.assertNotIn("专用浏览器", job["next_step"])
        self.assertNotIn("父任务", job["next_step"])

    def test_author_card_keeps_partial_parent_when_newer_child_succeeds(self):
        parent = self.create_failed_job("author_archive", "author_archive_partial")
        self.service._finish(parent["id"], "partial", "author_archive_partial")
        with patch.object(self.service, "_spawn"):
            child_id = self.service.start("content", "xiaohongshu", AUTHOR)["job_id"]
        with self.service.workflow.connect() as db:
            db.execute("INSERT INTO pipeline_pages VALUES(?,?,?)", (parent["id"], 1, child_id))
        self.service._finish(child_id, "succeeded", "content_complete")

        workspace = self.service.workspace()
        author = workspace["subscriptions"][0]
        jobs = {row["id"]: row for row in workspace["runs"]}
        self.assertEqual(author["latest_state"], "partial")
        self.assertEqual(author["reason"], "author_archive_partial")
        self.assertEqual(author["message"], jobs[parent["id"]]["message"])
        self.assertEqual(jobs[child_id]["state"], "succeeded")
        self.assertEqual(jobs[child_id]["parent_job_id"], parent["id"])


if __name__ == "__main__":
    unittest.main()
