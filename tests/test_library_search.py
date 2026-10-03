"""Local library search regression against a synthetic, previously saved workspace."""
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from creator_archive.app import create_app
from creator_archive.service import WorkspaceService


class LibrarySearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "workspace"
        self.service = WorkspaceService(self.root)
        self.archive = self.service.workflow.archive_root
        self.archive.mkdir(parents=True, exist_ok=True)
        self.note = self.archive / "manual-notes.md"
        self.note.write_text("用户手写笔记", encoding="utf-8")
        self.service.workflow.subscribe("xiaohongshu", "author-a", "北京妈妈", verified=True,
                                        evidence="synthetic old library")
        self.service.workflow.subscribe("wechat", "author-w", "公众号妈妈", verified=True,
                                        evidence="synthetic old library")
        self.seed("xiaohongshu", "complete", "author-a", "北京食谱", "中文正文 搜索目标",
                  "complete", "complete_for_observed_detail")
        self.seed("xiaohongshu", "partial", "author-a", "照片", "部分正文",
                  "complete", "complete_for_observed_detail", asset=b"saved-image", missing=True)
        self.seed("xiaohongshu", "legacy", "author-a", "旧标题", "旧正文",
                  "complete", "unknown")
        self.seed("wechat", "missing", "author-w", "公众号记录", None, "missing", "unknown")
        for index in range(25):
            self.seed("xiaohongshu", f"page-{index:02}", "author-a", f"跨页标题 {index:02}",
                      "跨页中文", "complete", "complete_for_observed_detail")
        with self.service.workflow.connect() as db:
            db.execute("INSERT INTO batches(id,mode,created_at) VALUES(901,'full',0)")
            db.execute("INSERT INTO batches(id,mode,created_at) VALUES(902,'full',0)")
            db.execute("""INSERT INTO runs(batch_id,platform,author_id,mode,adapter_version,
                       state,coverage,updated_at) VALUES(901,'xiaohongshu','author-a','full',
                       'synthetic','partial','partial',0)""")
            db.execute("""INSERT INTO runs(batch_id,platform,author_id,mode,adapter_version,
                       state,coverage,updated_at) VALUES(902,'wechat','author-w','full',
                       'synthetic','succeeded','complete_for_accessible_scope',0)""")

    def seed(self, platform, item_id, author_id, title, body, detail_state, media_state,
             *, asset=None, missing=False):
        with self.service.workflow.connect() as db:
            db.execute("""INSERT INTO items(platform,item_id,author_id,published_at,detail_text,
                       detail_state,title,content_type,media_state) VALUES(?,?,?,?,?,?,?,?,?)""",
                       (platform,item_id,author_id,"2026-01-01",body,detail_state,title,"image",media_state))
            if asset is not None:
                relative = f"xiaohongshu/{author_id}/{item_id}.png"
                target = self.archive / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(asset)
                db.execute("""INSERT INTO assets(platform,item_id,asset_id,position,kind,relative_path,
                           bytes,sha256,mime) VALUES(?,?,?,?,?,?,?,?,?)""",
                           (platform,item_id,"asset",0,"image",relative,len(asset),sha256(asset).hexdigest(),"image/png"))
                if missing:
                    target.unlink()

    def test_full_library_text_status_and_cross_page(self):
        self.assertEqual([r["item_id"] for r in self.service.items(text="中文正文")["items"]], ["complete"])
        self.assertEqual(self.service.items(text="北京食谱")["items"][0]["item_id"], "complete")
        self.assertEqual(self.service.items(text="北京妈妈")["total"], 28)
        self.assertEqual(self.service.items(text="author-a")["total"], 28)
        self.assertEqual(self.service.items(text="跨页中文", offset=20, limit=10)["total"], 25)
        self.assertEqual(len(self.service.items(text="跨页中文", offset=20, limit=10)["items"]), 5)
        self.assertEqual(self.service.items(text="跨页中文", archive_status="complete", offset=20,
                                            limit=10)["total"], 25)
        for status, item_id in (("partial","partial"),("missing","missing"),("unknown","legacy")):
            result = self.service.items(archive_status=status)
            self.assertEqual([row["item_id"] for row in result["items"]], [item_id])
        self.assertEqual(self.service.item("xiaohongshu", "complete")["archive_status"], "complete")
        self.assertEqual(self.service.item("wechat", "missing")["archive_status"], "missing")
        self.assertEqual(self.service.items(platform="wechat",text="公众号")["total"], 1)
        self.assertEqual(self.service.items(text="%", archive_status="complete")["total"], 0)
        self.assertEqual(self.service.items(text="partial")["items"][0]["item_id"], "partial")
        with self.assertRaisesRegex(ValueError, "归档状态"):
            self.service.items(archive_status="invalid")

    def test_restart_http_detail_and_saved_files_unchanged(self):
        original = self.note.read_bytes()
        before = self.service.items(text="北京妈妈", archive_status="partial")
        app = create_app(self.root)
        with TestClient(app) as client:
            response = client.get("/api/items", params={"text":"跨页中文", "offset":20,
                                                        "limit":10,"archive_status":"complete"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["total"], 25)
            self.assertEqual(len(response.json()["items"]), 5)
            detail = client.get("/api/items/xiaohongshu/partial").json()
            self.assertEqual(detail["archive_status"], "partial")
            self.assertEqual(detail["assets"][0]["state"], "missing")
            self.assertIsNone(detail["assets"][0]["url"])
            self.assertEqual(client.get("/api/items",params={"archive_status":"bad"}).status_code, 422)
        restarted = WorkspaceService(self.root)
        self.assertEqual(restarted.items(text="北京妈妈",archive_status="partial"), before)
        self.assertEqual(self.note.read_bytes(), original)
        with restarted.workflow.connect() as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT count(*) FROM items").fetchone()[0], 29)


if __name__ == "__main__":
    unittest.main()
