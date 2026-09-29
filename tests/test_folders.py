"""Folder changes against disposable old workspaces and real files."""
from hashlib import sha256
from pathlib import Path
import base64
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from creator_archive.app import create_app
from creator_archive.folders import configure_folders, load_folders
from creator_archive.validation import SyntheticAdapter
from creator_archive.workflow import ArchiveWorkflow


class FolderSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Windows runners may return an 8.3 temp alias while the workflow
        # resolves it to the long path. Use one canonical spelling for both.
        self.base = Path(self.temp.name).resolve()
        self.data = (self.base / "old-workspace").resolve()
        self.workflow = ArchiveWorkflow(self.data)
        self.workflow.subscribe("xiaohongshu", "author-a", "作者 A", verified=True,
                                evidence="disposable synthetic identity")
        self.workflow.run_all({"xiaohongshu": SyntheticAdapter()}, mode="archive")
        with self.workflow.connect() as db:
            item_id = db.execute("SELECT item_id FROM items LIMIT 1").fetchone()[0]
        self.workflow.save_detail("xiaohongshu", item_id, "author-a", "可离线读取的合成正文",
                                  f"https://www.xiaohongshu.com/explore/{item_id}")
        image = self.base / "source.png"
        image.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9WlNX0sAAAAASUVORK5CYII="))
        saved_image = self.workflow.attach_media("xiaohongshu", item_id, "image-a", image,
                                                 position=0, kind="image", mime="image/png")
        self.image_relative = saved_image.relative_to(self.data / "archive")
        self.image_bytes = image.read_bytes()
        self.workflow.export_all()
        self.old_archive = self.data / "archive"
        self.note = self.old_archive / "xiaohongshu" / "author-a" / "my-note.md"
        self.note.write_text("手工笔记：请勿覆盖", encoding="utf-8")
        self.old_files = {p.relative_to(self.old_archive): sha256(p.read_bytes()).hexdigest()
                          for p in self.old_archive.rglob("*") if p.is_file()}
        self.db_hash = sha256((self.data / "archive.sqlite3").read_bytes()).hexdigest()

    def test_copy_first_custom_root_restart_http_and_obsidian_note_protection(self):
        self.assertEqual(self.workflow.archive_root, self.data / "archive")
        custom = self.base / "archive-custom"
        vault = self.base / "vault" / "Creator Archive"
        result = configure_folders(self.data, str(custom), str(vault))
        self.assertEqual(result["copied_files"], len(self.old_files))
        self.assertEqual(load_folders(self.data), (custom, vault))
        self.assertEqual(sha256((self.data / "archive.sqlite3").read_bytes()).hexdigest(), self.db_hash)
        for relative, digest in self.old_files.items():
            self.assertEqual(sha256((self.old_archive / relative).read_bytes()).hexdigest(), digest)
            self.assertEqual(sha256((custom / relative).read_bytes()).hexdigest(), digest)
        self.assertEqual((custom / self.note.relative_to(self.old_archive)).read_text(encoding="utf-8"),
                         "手工笔记：请勿覆盖")
        self.assertTrue(ArchiveWorkflow(self.data).asset_valid("xiaohongshu", self.image_relative.parts[2], "image-a"))

        restarted = ArchiveWorkflow(self.data)
        self.assertEqual(restarted.archive_root, custom)
        exported = restarted.export_all()
        self.assertEqual(exported["archive_root"], str(custom))
        # Synthetic IDs depend on the adapter; select one real generated Markdown file.
        article = next((custom / "xiaohongshu" / "author-a").rglob("article.md"))
        relative = article.relative_to(custom)
        obsidian_article = vault / "小红书" / relative.relative_to("xiaohongshu")
        self.assertEqual(obsidian_article.read_bytes(), article.read_bytes())
        self.assertEqual((vault / "小红书" / self.image_relative.relative_to("xiaohongshu")).read_bytes(), self.image_bytes)
        obsidian_article.write_text("手工编辑的 Obsidian 文章", encoding="utf-8")
        restarted.export_all()
        self.assertEqual(obsidian_article.read_text(encoding="utf-8"), "手工编辑的 Obsidian 文章")
        self.assertTrue((obsidian_article.parent / f"article.{sha256(article.read_bytes()).hexdigest()[:12]}.md").is_file())

        with TestClient(create_app(self.data)) as client:
            workspace = client.get("/api/workspace").json()
            self.assertEqual(workspace["archive_dir"], str(custom))
            self.assertEqual(workspace["obsidian_dir"], str(vault))
            self.assertEqual(client.get("/archive/" + relative.as_posix()).content, article.read_bytes())
            self.assertEqual(client.get("/archive/" + self.image_relative.as_posix()).content, self.image_bytes)
        # Closing/uninstalling the executable has no cleanup path into any data root.
        self.assertTrue(self.note.is_file())
        self.assertTrue((custom / relative).is_file())
        self.assertTrue(obsidian_article.is_file())

    def test_destination_conflict_rolls_back_configuration_and_old_files(self):
        custom = self.base / "archive-custom"
        relative = next(iter(self.old_files))
        conflict = custom / relative
        conflict.parent.mkdir(parents=True)
        conflict.write_bytes(b"user owned different content")
        with self.assertRaisesRegex(ValueError, "目标已有不同内容"):
            configure_folders(self.data, str(custom))
        self.assertEqual(load_folders(self.data), (self.old_archive, None))
        self.assertFalse((self.data / "folders.json").exists())
        self.assertEqual(conflict.read_bytes(), b"user owned different content")
        self.assertEqual(sha256((self.data / "archive.sqlite3").read_bytes()).hexdigest(), self.db_hash)
        self.assertEqual({p.relative_to(self.old_archive): sha256(p.read_bytes()).hexdigest()
                          for p in self.old_archive.rglob("*") if p.is_file()}, self.old_files)

    def test_existing_wechat_vault_stays_in_place(self):
        vault = self.base / "existing-vault"
        vault.mkdir()
        old_wechat_file = vault / "earlier-wechat-article.md"
        old_wechat_file.write_text("现有公众号笔记", encoding="utf-8")
        configure_folders(self.data, str(self.old_archive), str(vault))
        restarted = ArchiveWorkflow(self.data)
        restarted.subscribe("wechat", "author-w", "公众号作者", verified=True,
                            evidence="disposable synthetic identity")
        restarted.export_all()
        self.assertEqual(old_wechat_file.read_text(encoding="utf-8"), "现有公众号笔记")
        self.assertTrue((vault / "author-w" / "manifest.json").is_file())
        self.assertFalse((vault / "wechat").exists())

    def test_copy_io_failure_keeps_old_configuration_and_files(self):
        custom = self.base / "fault-target"
        with patch("creator_archive.folders.os.link", side_effect=OSError("synthetic disk failure")):
            with self.assertRaisesRegex(OSError, "synthetic disk failure"):
                configure_folders(self.data, str(custom))
        self.assertEqual(load_folders(self.data), (self.old_archive, None))
        self.assertEqual(sha256((self.data / "archive.sqlite3").read_bytes()).hexdigest(), self.db_hash)
        self.assertEqual({p.relative_to(self.old_archive): sha256(p.read_bytes()).hexdigest()
                          for p in self.old_archive.rglob("*") if p.is_file()}, self.old_files)

    def test_missing_configured_archive_stops_before_database_open(self):
        custom = self.base / "custom"
        hidden = self.base / "temporarily-unavailable"
        configure_folders(self.data, str(custom))
        before = sha256((self.data / "archive.sqlite3").read_bytes()).hexdigest()
        custom.rename(hidden)
        with self.assertRaisesRegex(ValueError, "已配置归档目录不可访问"):
            ArchiveWorkflow(self.data)
        self.assertEqual(sha256((self.data / "archive.sqlite3").read_bytes()).hexdigest(), before)
        hidden.rename(custom)
        self.assertEqual(ArchiveWorkflow(self.data).archive_root, custom)

    def test_reconfigure_keeps_backup_and_rejects_nested_or_relative_paths(self):
        first = self.base / "first"
        second = self.base / "second"
        configure_folders(self.data, str(first))
        configure_folders(self.data, str(second))
        self.assertEqual(load_folders(self.data), (second, None))
        backups = list((self.data / "backups").glob("folders-before-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8"))["archive_dir"], str(first))
        with self.assertRaisesRegex(ValueError, "绝对路径"):
            configure_folders(self.data, "relative")
        with self.assertRaisesRegex(ValueError, "不能相互包含"):
            configure_folders(self.data, str(second / "inside"))
        self.assertEqual(load_folders(self.data), (second, None))

    def test_windows_configuration_entry_from_stopped_workspace(self):
        script = Path(__file__).resolve().parent.parent / "configure-folders.ps1"
        archive = self.base / "entry-archive"
        vault = self.base / "entry-vault"
        runtime = self.base / "entry-runtime"
        completed = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                    "-File", str(script), "-DataDir", str(self.data),
                                    "-ArchiveDir", str(archive), "-ObsidianDir", str(vault),
                                    "-RuntimeDir", str(runtime)], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace")
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertEqual(load_folders(self.data), (archive, vault))
        self.assertEqual((archive / self.image_relative).read_bytes(), self.image_bytes)


if __name__ == "__main__":
    unittest.main()
