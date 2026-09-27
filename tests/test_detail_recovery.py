"""Synthetic batch/restart/renewal regressions; not live login evidence."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock

from creator_archive.service import WorkspaceService
from creator_archive.validation import AdapterFailure
from creator_archive.adapters.xhs_transport import XhsBrowserTransport
from creator_archive.adapters.xhs import XhsPageAdapter

AUTHOR = "a" * 24
IDS = [f"{n:024x}" for n in range(1, 27)]


class Source:
    def __init__(self):
        self.calls = []
        self.preparations = []
        self.failures = {}

    def prepare_details(self, author, ids):
        self.preparations.append(list(ids))

    def detail(self, author, item, source_url=""):
        self.calls.append((item, source_url))
        if item in self.failures:
            raise AdapterFailure(self.failures[item], 60)
        return dict(item_id=item, author_id=author, source="synthetic", observed_at=time.time(), metrics={})


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = Source()
        self.service = WorkspaceService(self.root, adapter_factory=lambda _: self.source)
        self.addCleanup(self.service.close)
        self.service.workflow.subscribe("xiaohongshu", AUTHOR, "Synthetic", verified=True, evidence="synthetic")
        with self.service.workflow.connect() as db:
            db.executemany("INSERT INTO items(platform,author_id,item_id,published_at) VALUES('xiaohongshu',?,?,'')", [(AUTHOR, i) for i in IDS[:3]])

    def run_job(self):
        job = self.service.start("metrics")["job_id"]
        self.service.wait()
        return job

    def test_item_failure_isolated_and_resume_renews_only_failed_scope(self):
        self.source.failures[IDS[1]] = "item_unavailable"
        job = self.run_job()
        self.assertEqual([i for i, _ in self.source.calls], IDS[:3])
        self.assertEqual(self.service.workspace()["runs"][0]["state"], "partial")
        self.source.failures.clear()
        restarted = WorkspaceService(self.root, adapter_factory=lambda _: self.source)
        self.addCleanup(restarted.close)
        restarted.resume(job)
        restarted.wait()
        self.assertEqual(self.source.preparations[-1], [IDS[1]])
        self.assertEqual([i for i, _ in self.source.calls], [*IDS[:3], IDS[1]])
        self.assertEqual(restarted.workspace()["runs"][0]["state"], "succeeded")

    def test_login_pauses_remaining_and_renewal_preserves_success(self):
        self.source.failures[IDS[1]] = "needs_login"
        job = self.run_job()
        run = self.service.workspace()["runs"][0]
        self.assertEqual((run["state"],run["item_count"],run["failed_count"],run["pending_count"]), ("needs_login",1,1,1))
        self.source.failures.clear()
        self.service.resume(job)
        self.service.wait()
        self.assertEqual([i for i, _ in self.source.calls], [IDS[0],IDS[1],IDS[1],IDS[2]])
        self.assertEqual(self.source.preparations[-1], IDS[1:3])

    def test_batch_links_atomic_scope_validation_and_no_persistence(self):
        self.source.failures = dict.fromkeys(IDS[:3], "reference_missing")
        job = self.run_job()
        link = lambda item: f"https://www.xiaohongshu.com/explore/{item}?xsec_token=SECRET_TEST_MARKER"
        with self.assertRaises(ValueError):
            self.service.resume(job, source_urls=[link(IDS[0]),link(IDS[4])])
        self.assertEqual(self.service._job_sources,{})
        self.assertEqual(self.service.workspace()["runs"][0]["state"], "partial")
        self.source.failures.clear()
        self.service.resume(job, source_urls=[link(i) for i in IDS[:3]])
        self.service.wait()
        self.assertTrue(all("SECRET_TEST_MARKER" in url for _, url in self.source.calls[-3:]))
        self.assertEqual(self.source.preparations[-1], [])
        self.assertEqual(self.service._job_sources,{})
        self.assertNotIn(b"SECRET_TEST_MARKER", self.service.workflow.db_path.read_bytes())

    def test_preparation_failures_keep_typed_reason_and_successful_checkpoint(self):
        self.source.failures[IDS[1]] = "needs_login"
        job = self.run_job()
        self.source.failures.clear()
        for category in ("unavailable", "timeout", "invalid_cursor", "verification_required", "needs_login"):
            with self.subTest(category=category):
                self.source.prepare_details = MagicMock(side_effect=AdapterFailure(category))
                self.service.resume(job)
                self.service.wait()
                run = self.service.workspace()["runs"][0]
                self.assertEqual(run["reason"], category)
                self.assertEqual(run["state"], "needs_login" if category == "needs_login" else "blocked")
                self.assertEqual((run["item_count"],run["failed_count"],run["pending_count"]),(1,1,1))
                self.assertNotIn("尚未配置",run["message"])
                self.assertTrue(run["next_step"])
                self.assertEqual([i for i,_ in self.source.calls],IDS[:2])
        self.source.prepare_details = MagicMock()
        self.service.resume(job)
        self.service.wait()
        self.assertEqual([i for i,_ in self.source.calls],[*IDS[:2],*IDS[1:3]])
        self.assertEqual(self.service.workspace()["runs"][0]["state"],"succeeded")

    def test_all_failure_pages_remain_accessible_beyond_twenty(self):
        with self.service.workflow.connect() as db:
            db.executemany("INSERT INTO items(platform,author_id,item_id,published_at) VALUES('xiaohongshu',?,?,'')", [(AUTHOR,i) for i in IDS[3:]])
        self.source.failures = dict.fromkeys(IDS,"reference_missing")
        job = self.run_job()
        a,b = self.service.job_failures(job,0,20),self.service.job_failures(job,20,20)
        self.assertEqual(a["total"],26)
        self.assertEqual([i["item_id"] for i in a["items"]+b["items"]],IDS)
        self.assertTrue(all(i["next_step"] for i in b["items"]))

    def test_selected_scope_dedup_rejects_foreign_and_api_pages(self):
        from fastapi.testclient import TestClient
        from creator_archive.app import create_app
        app=create_app(self.root)
        app.state.service.adapter_factory=lambda _:self.source
        with TestClient(app) as client:
            headers={"x-creator-archive":"local-validation"}
            body=dict(mode="metrics",platform="xiaohongshu",author_id=AUTHOR,item_ids=[IDS[0],IDS[0],IDS[1]])
            bad=client.post('/api/jobs',headers=headers,json={**body,"item_ids":[IDS[5]]})
            self.assertEqual(bad.status_code,422)
            self.source.failures[IDS[1]]="reference_missing"
            response=client.post('/api/jobs',headers=headers,json=body)
            self.assertEqual(response.status_code,200)
            app.state.service.wait()
            job=response.json()['job_id']
            self.assertEqual(app.state.service.workspace()['runs'][0]['target_count'],2)
            result=client.get(f'/api/jobs/{job}/failures?limit=1').json()
            self.assertEqual(result['items'][0]['reason'],'reference_missing')
            self.assertEqual(client.get(f'/api/jobs/{job}/failures?offset=-1').status_code,422)

    def test_real_media_failure_category_does_not_stop_later_items(self):
        from types import SimpleNamespace
        method=self.source.detail
        def detail(*args,**kwargs):
            return {**method(*args,**kwargs),"text":"Synthetic body","source_url":f'https://www.xiaohongshu.com/explore/{args[1]}',"media":[SimpleNamespace(asset_id='image-000',position=0,kind='image')],"missing":[]}
        def download(*_):
            raise AdapterFailure('media_failed')
        self.source.detail=detail
        self.source.download_media=download
        self.service.start('content');self.service.wait()
        self.assertEqual([i for i,_ in self.source.calls],IDS[:3])
        self.assertEqual(self.service.workspace()['runs'][0]['state'],'partial')

    def test_open_login_rejects_active_jobs_then_clears_ephemeral_links(self):
        self.service._spawn=lambda _:None
        job=self.service.start('metrics')['job_id']
        self.source.open_login=lambda:{'state':'needs_login'}
        with self.assertRaises(ValueError):
            self.service.open_login()
        self.service._finish(job,'needs_login','needs_login')
        self.service._job_sources[job]={IDS[0]:'ephemeral'}
        self.service.open_login()
        self.assertEqual(self.service._job_sources,{})

    def test_actual_subprocess_exit_resumes_without_reprocessing_success(self):
        script = '''
import sys,time
from pathlib import Path
from creator_archive.service import WorkspaceService
root=Path(sys.argv[1])
class Source:
 def detail(self,author,item,source_url=''):
  if item.endswith('2'):
   (root/'waiting').write_text('ready')
   time.sleep(60)
  return dict(item_id=item,author_id=author,source='synthetic',observed_at=time.time(),metrics={})
service=WorkspaceService(root,adapter_factory=lambda _:Source())
service.start('metrics');service.wait(90)
'''
        child = subprocess.Popen([sys.executable,"-c",script,str(self.root)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            deadline=time.monotonic()+15
            while not (self.root/'waiting').exists() and time.monotonic()<deadline:
                time.sleep(.05)
            self.assertTrue((self.root/'waiting').exists())
        finally:
            child.terminate();child.wait(timeout=10)
        restarted=WorkspaceService(self.root,adapter_factory=lambda _:self.source)
        self.addCleanup(restarted.close)
        run=restarted.workspace()["runs"][0]
        self.assertEqual((run["state"],run["item_count"]),("interrupted",1))
        restarted.resume(run["id"]);restarted.wait()
        self.assertEqual([i for i,_ in self.source.calls],IDS[1:3])


class ReferenceTests(unittest.TestCase):
    def test_reference_refresh_is_bounded_and_missing_does_not_navigate(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport=XhsBrowserTransport(Path(tmp)/"profile")
            try:
                transport._ensure=lambda:None
                transport._page=MagicMock()
                transport._page.locator.return_value.evaluate_all.return_value=[]
                calls=[]
                def fetch(author,cursor):
                    calls.append(cursor)
                    return {"success":True,"data":{"notes":[{"note_id":IDS[len(calls)],"user":{"user_id":AUTHOR}}],"cursor":IDS[len(calls)],"has_more":True}}
                transport._fetch=fetch
                transport.prepare_details(AUTHOR,[IDS[-1]])
                self.assertEqual(len(calls),3)
                with self.assertRaises(AdapterFailure) as error:
                    transport.detail(AUTHOR,IDS[-1])
                self.assertEqual(error.exception.category,"reference_missing")
                transport._page.goto.assert_not_called()
            finally:
                transport.close()

    def test_renewal_discards_stale_author_links_and_login_failure_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport=XhsBrowserTransport(Path(tmp)/"profile")
            try:
                transport._ensure=lambda:None
                transport._detail_links[(AUTHOR,IDS[0])]="https://www.xiaohongshu.com/explore/old?xsec_token=STALE"
                def fetch(*_):
                    raise AdapterFailure("needs_login")
                transport._fetch=fetch
                with self.assertRaises(AdapterFailure):
                    transport.prepare_details(AUTHOR,IDS[:3])
                self.assertEqual(transport._detail_links,{})
            finally:
                transport.close()
