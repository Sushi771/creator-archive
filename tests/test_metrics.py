"""Offline metrics, fixed item jobs, upgrade and full-library query regressions."""
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import json
import shutil
import sqlite3
import tempfile
import time
import unittest

from fastapi.testclient import TestClient
from creator_archive.app import create_app
from creator_archive.service import WorkspaceService, PlatformCooldown
from creator_archive.workflow import ArchiveWorkflow
from creator_archive.validation import AdapterFailure


class DetailSource:
    def __init__(self):
        self.calls = []
        self.downloads = []
        self.fail_item = None
        self.fail_media = False
        self.rate_limited = False

    def detail(self, author_id, item_id, source_url=""):
        self.calls.append(item_id)
        if self.rate_limited:
            raise AdapterFailure("rate_limited", 60)
        if self.fail_item == item_id:
            raise AdapterFailure("timeout")
        return dict(item_id=item_id,author_id=author_id,title="Synthetic title",text="Synthetic body",
                    content_type="image",published_at="2026-09-27T10:00:00+08:00",
                    source_url=f"https://www.xiaohongshu.com/explore/{item_id}",source="synthetic_detail",
                    observed_at=time.time(),metrics={"likes":dict(value=len(self.calls),quality="exact",raw=str(len(self.calls)))},
                    media=[SimpleNamespace(asset_id=f"image-{n:03}",position=n,kind="image") for n in range(2)],missing=[])

    def download_media(self, candidate, target_dir):
        self.downloads.append(candidate.asset_id)
        if self.fail_media and candidate.position == 1:
            raise AdapterFailure("timeout")
        target_dir.mkdir(parents=True,exist_ok=True)
        path = target_dir / (candidate.asset_id+".jpg")
        path.write_bytes(b"\xff\xd8\xffsynthetic-image")
        return dict(path=str(path),mime="image/jpeg")


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = DetailSource()
        self.service = WorkspaceService(self.root,adapter_factory=lambda _: self.source)
        self.addCleanup(self.service.wait)
        self.service.workflow.subscribe("xiaohongshu","author-a","A",verified=True,evidence="synthetic")

    def seed(self, count=1, author="author-a"):
        with self.service.workflow.connect() as db:
            db.executemany("INSERT INTO items(platform,item_id,author_id,published_at) VALUES('xiaohongshu',?,?,?)",
                           [(f"{author}-{n:03}",author,"2026-09-27") for n in range(count)])

    def save(self, item="author-a-000", value=9, timestamp=100, key="one", quality="exact", **kwargs):
        self.service.workflow.save_metrics("xiaohongshu",item,{"likes":dict(value=value,quality=quality,raw=str(value))},
            source="synthetic_detail",collected_at=timestamp,observation_key=key,**kwargs)

    def test_zero_unknown_approximate_lower_bound_decrease_stale_failure_and_dedup(self):
        self.seed()
        self.save(value=0)
        self.save(value=10000,timestamp=200,key="two",quality="approximate")
        self.save(value=9999,timestamp=300,key="three",quality="lower_bound")
        self.save(value=50,timestamp=400,key="three") # identical observation id is idempotent
        self.save(value=None,timestamp=500,key="missing")
        self.save(value=80000,timestamp=600,key="failed",status="failed",reason="timeout")
        self.save(value=80000,timestamp=50,key="late-old")
        detail = self.service.item("xiaohongshu","author-a-000")
        self.assertEqual(detail["metrics"]["likes"]["value"],9999)
        self.assertEqual(detail["metrics"]["likes"]["collected_at"],300)
        self.assertEqual(detail["metrics"]["likes"]["quality"],"lower_bound")
        self.assertEqual(detail["metrics"]["likes"]["last_attempt_status"],"failed")
        self.assertIsNone(detail["metrics"]["comments"]["value"])
        self.assertEqual(detail["snapshot_total"],6)
        self.assertEqual(detail["metric_snapshots"][-2]["metrics"]["likes"]["value"],0)

    def test_query_covers_entire_library_with_unknown_last_combined_filters(self):
        self.seed(130)
        self.seed(2,"author-b")
        for n in range(129):
            self.save(item=f"author-a-{n:03}",value=n,timestamp=100+n,key=str(n))
        with self.service.workflow.connect() as db:
            db.execute("UPDATE items SET content_type='video' WHERE item_id='author-a-128'")
        top = self.service.items(limit=1,sort="likes")
        self.assertEqual(top["items"][0]["item_id"],"author-a-128")
        self.assertEqual(top["total"],132)
        second = self.service.items(limit=1,sort="likes",offset=1)
        self.assertEqual(second["items"][0]["metrics"]["likes"]["value"],127)
        last = self.service.items(limit=5,sort="likes",order="asc",offset=129)
        self.assertTrue(all(i["metrics"]["likes"]["value"] is None for i in last["items"]))
        result = self.service.items(author_id="author-a",date_from="2026-09-27",date_to="2026-09-27",content_type="video",min_likes=100)
        self.assertEqual(result["total"],1)
        self.assertEqual(self.service.items(min_likes=0)["total"],129)
        self.assertEqual(self.service.items(missing_metric="likes")["total"],3)

    def test_metrics_only_never_downloads_and_content_retry_reuses_successes(self):
        self.seed(2)
        self.source.fail_media = True
        job = self.service.start("content","xiaohongshu","author-a","author-a-000")["job_id"]
        self.service.wait()
        self.assertEqual(self.service.workspace()["runs"][0]["state"],"partial")
        self.source.fail_media = False
        self.service.resume(job)
        self.service.wait()
        self.assertEqual(self.source.downloads,["image-000","image-001","image-001"])
        self.assertEqual(self.service.item("xiaohongshu","author-a-000")["snapshot_total"],2)
        self.service.start("metrics","xiaohongshu","author-a","author-a-000")
        self.service.wait()
        self.assertEqual(len(self.source.downloads),3)
        self.assertEqual(self.service.item("xiaohongshu","author-a-000")["snapshot_total"],3)
        self.assertEqual(self.source.calls,["author-a-000"]*3)
        self.assertEqual(self.service.item("xiaohongshu","author-a-001")["detail_state"],"missing")

    def test_legacy_media_reuse_requires_item_kind_position_and_hash(self):
        self.seed()
        original = self.root / "legacy.jpg"
        original.write_bytes(b"\xff\xd8\xfflegacy-image")
        saved = self.service.workflow.attach_media("xiaohongshu","author-a-000","old-image-name",original,position=0,kind="image",mime="image/jpeg")
        self.service.start("content","xiaohongshu","author-a","author-a-000")
        self.service.wait()
        self.assertEqual(self.source.downloads,["image-001"])
        self.assertEqual(len(self.service.item("xiaohongshu","author-a-000")["assets"]),2)
        video = SimpleNamespace(asset_id="video-000",position=0,kind="video")
        self.assertFalse(self.service.workflow.candidate_saved("xiaohongshu","author-a-000",video))
        saved.write_bytes(b"corrupted")
        self.source.downloads.clear()
        self.service.start("content","xiaohongshu","author-a","author-a-000")
        self.service.wait()
        self.assertEqual(self.source.downloads,["image-000"])
        self.assertEqual(original.read_bytes(),b"\xff\xd8\xfflegacy-image")

    def test_interrupted_fixed_scope_resumes_failed_items_and_preserves_metric_time(self):
        self.seed(2)
        self.save(item="author-a-001",timestamp=100)
        self.source.fail_item = "author-a-001"
        job = self.service.start("metrics")["job_id"]
        self.service.wait()
        before = self.service.item("xiaohongshu","author-a-001")["metrics"]["likes"]
        self.assertEqual((before["value"],before["collected_at"]),(9,100))
        with self.service.workflow.connect() as db:
            db.execute("UPDATE jobs SET state='running' WHERE id=?",(job,))
            db.execute("INSERT INTO items(platform,item_id,author_id,published_at) VALUES('xiaohongshu','new-later','author-a','')")
        self.source.fail_item = None
        restarted = WorkspaceService(self.root,adapter_factory=lambda _: self.source)
        restarted.resume(job)
        restarted.wait()
        self.assertEqual(self.source.calls,["author-a-000","author-a-001","author-a-001"])
        self.assertEqual(restarted.workspace()["runs"][0]["target_count"],2)
        self.assertEqual(restarted.workspace()["runs"][0]["state"],"succeeded")

    def test_detail_rate_limit_persists_and_blocks_new_jobs(self):
        self.seed()
        self.source.rate_limited = True
        self.service.start("metrics")
        self.service.wait()
        restarted = WorkspaceService(self.root,adapter_factory=lambda _: self.source)
        self.assertEqual(restarted.workspace()["runs"][0]["state"],"rate_limited")
        with self.assertRaises(PlatformCooldown):
            restarted.start("content")

    def test_unavailable_and_stop_do_not_continue_full_library(self):
        self.seed(3)
        def blocked(*args, **kwargs):
            self.source.calls.append(args[1])
            raise AdapterFailure("unavailable")
        self.source.detail = blocked
        job = self.service.start("metrics")["job_id"]
        self.service.wait()
        self.assertEqual(len(self.source.calls),1)
        self.assertEqual(self.service.workspace()["runs"][0]["state"],"blocked")
        self.service.close()
        self.service.resume(job)
        self.service.wait()
        self.assertEqual(len(self.source.calls),1)
        self.assertEqual(self.service.workspace()["runs"][0]["state"],"interrupted")

    def test_supplemental_link_transient_validated_and_not_persisted(self):
        self.seed()
        captured = []
        detail_method = self.source.detail
        def detail(author,item,source_url=""):
            captured.append(source_url)
            return detail_method(author,item,source_url)
        self.source.detail = detail
        link = "https://www.xiaohongshu.com/explore/author-a-000?xsec_token=SECRET_SENTINEL"
        self.service.start("metrics","xiaohongshu","author-a","author-a-000",link)
        self.service.wait()
        self.assertEqual(captured,[link])
        self.assertEqual(self.service._job_sources,{})
        self.assertNotIn(b"SECRET_SENTINEL",self.service.workflow.db_path.read_bytes())
        with self.assertRaisesRegex(ValueError,"作品ID"):
            self.service.start("metrics","xiaohongshu","author-a","author-a-000",link.replace("author-a-000","other"))
        with self.assertRaises(ValueError):
            self.service.start("metrics","xiaohongshu","author-a",source_url=link)

    def test_export_includes_metrics_for_missing_body_and_preserves_manual_edit(self):
        self.seed(2)
        self.save()
        self.service.workflow.save_detail("xiaohongshu","author-a-000","author-a","body","https://www.xiaohongshu.com/explore/author-a-000")
        first = self.service.workflow.export_all()["authors"][0]
        manifest = json.loads(Path(first["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"],2)
        self.assertIn("metrics",manifest["items"][1])
        self.assertEqual(manifest["items"][1]["files"],{})
        corpus = json.loads(Path(first["corpus"]).read_text(encoding="utf-8"))
        self.assertEqual(corpus["metrics"]["likes"]["value"],9)
        article = Path(first["manifest"]).parent / manifest["items"][0]["files"]["article.md"]
        article.write_text("MANUAL NOTE",encoding="utf-8")
        self.save(value=10,timestamp=200,key="new")
        self.service.workflow.export_all()
        self.assertEqual(article.read_text(encoding="utf-8"),"MANUAL NOTE")

    def test_legacy_backup_is_restorable_upgrade_idempotent_notes_preserved(self):
        legacy = self.root / "legacy"
        legacy.mkdir()
        original = legacy / "archive.sqlite3"
        with closing(sqlite3.connect(original)) as db:
            db.executescript("CREATE TABLE items(platform TEXT,item_id TEXT,author_id TEXT,published_at TEXT,detail_text TEXT,source_url TEXT,detail_state TEXT,PRIMARY KEY(platform,item_id)); INSERT INTO items VALUES('xiaohongshu','old','author-a','2020-01-01','old body',NULL,'complete'); CREATE TABLE manual_notes(id TEXT,note TEXT); INSERT INTO manual_notes VALUES('old','keep me');")
        workflow = ArchiveWorkflow(legacy)
        backup = Path(workflow.migration_backup)
        with closing(sqlite3.connect(backup)) as db:
            self.assertNotIn("content_type",[r[1] for r in db.execute("PRAGMA table_info(items)")])
            self.assertEqual(db.execute("SELECT note FROM manual_notes").fetchone()[0],"keep me")
        with workflow.connect() as db:
            self.assertEqual(db.execute("SELECT detail_text,content_type FROM items").fetchone()[:],("old body","unknown"))
            self.assertEqual(db.execute("SELECT note FROM manual_notes").fetchone()[0],"keep me")
        self.assertIsNone(ArchiveWorkflow(legacy).migration_backup)
        self.assertTrue(Path(workflow.cancellation_migration_backup).is_file())
        self.assertTrue(Path(workflow.tags_migration_backup).is_file())
        self.assertEqual(len(list((legacy/"backups").iterdir())),3)
        restored = self.root / "restored"
        restored.mkdir()
        shutil.copyfile(backup,restored/"archive.sqlite3")
        with ArchiveWorkflow(restored).connect() as db:
            self.assertEqual(db.execute("SELECT detail_text FROM items").fetchone()[0],"old body")

    def test_api_filters_validated_and_optional_single_item_job(self):
        app = create_app(self.root)
        app.state.service.adapter_factory = lambda _: self.source
        self.seed()
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/items?sort=likes&min_likes=0").json()["total"],0)
            self.assertEqual(client.get("/api/items?min_likes=-1").status_code,422)
            self.assertEqual(client.get("/api/items?sort=bad").status_code,422)
            result = client.post("/api/jobs",headers={"x-creator-archive":"local-validation"},json={"mode":"metrics","platform":"xiaohongshu","author_id":"author-a","item_id":"author-a-000"})
            self.assertEqual(result.status_code,200)
            app.state.service.wait()
            detail = client.get("/api/items/xiaohongshu/author-a-000").json()
            self.assertEqual(detail["snapshot_total"],1)
