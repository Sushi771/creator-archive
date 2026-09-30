"""Synthetic batch/restart/renewal regressions; not live login evidence."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

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

    def test_resolve_registers_exact_item_without_tokens_or_overwriting_old_data(self):
        foreign = "c" * 24
        item = IDS[5]
        link = f"https://www.xiaohongshu.com/discovery/item/{item}?xsec_token=REGISTRATION_SECRET"
        self.source.resolve_item = MagicMock(return_value=dict(author_id=foreign,item_id=item,title="Observed",content_type="image"))
        result = self.service.resolve_item("Share " + link)
        self.assertEqual(result["author_id"],foreign)
        with self.service.workflow.connect() as db:
            self.assertEqual(db.execute("SELECT enabled FROM subscriptions WHERE author_id=?",(foreign,)).fetchone()[0],0)
            db.execute("UPDATE subscriptions SET display_name='My note' WHERE author_id=?",(foreign,))
            db.execute("UPDATE items SET title='My title' WHERE item_id=?",(item,))
        self.service.resolve_item(f"https://www.xiaohongshu.com/user/profile/{foreign}/{item}?xsec_token=REGISTRATION_SECRET")
        with self.service.workflow.connect() as db:
            self.assertEqual(db.execute("SELECT display_name FROM subscriptions WHERE author_id=?",(foreign,)).fetchone()[0],"My note")
            self.assertEqual(db.execute("SELECT title FROM items WHERE item_id=?",(item,)).fetchone()[0],"My title")
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM pages").fetchone()[0],0)
        self.assertNotIn(b"REGISTRATION_SECRET",self.service.workflow.db_path.read_bytes())
        self.source.resolve_item.return_value["author_id"] = AUTHOR
        with self.assertRaises(ValueError):
            self.service.resolve_item(link)
        self.source.resolve_item.return_value["item_id"] = IDS[6]
        with self.assertRaises(ValueError):
            self.service.resolve_item(link)
        self.source.resolve_item.side_effect = AdapterFailure("needs_login")
        with self.assertRaises(AdapterFailure):
            self.service.resolve_item(link)

    def test_other_item_supplement_cannot_expand_original_job(self):
        self.source.failures[IDS[1]] = "reference_missing"
        job = self.run_job()
        before = self.service.workspace()["runs"][0]
        with self.assertRaises(ValueError):
            self.service.resume(job,source_urls=[f"https://www.xiaohongshu.com/explore/{IDS[5]}?xsec_token=OTHER"])
        self.assertEqual(self.service.workspace()["runs"][0],before)
        self.assertEqual(self.service._job_sources,{})

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

    def test_local_directory_conflict_resumes_same_batch_and_reuses_successful_media(self):
        """Real filesystem conflict/repair; detail and CDN response are synthetic."""
        from creator_archive.adapters.xhs import MediaCandidate
        from creator_archive.adapters.xhs_media import download_media
        url = 'https://sns-webpic-qc.xhscdn.com/synthetic.webp'
        payload = b'RIFF' + b'\x20\x00\x00\x00' + b'WEBPsynthetic-content'
        media = [MediaCandidate('image', position, url) for position in range(2)]
        original_detail = self.source.detail
        self.source.detail = lambda *args, **kwargs: {
            **original_detail(*args, **kwargs), 'text': 'Synthetic body',
            'source_url': f'https://www.xiaohongshu.com/explore/{args[1]}',
            'media': media, 'missing': []}
        downloads = []
        def download(candidate, target):
            downloads.append((target.name, candidate.asset_id))
            return download_media(candidate, target)
        self.source.download_media = download
        saved = self.root / 'already-saved.webp'
        saved.write_bytes(payload)
        archive = self.service.workflow.attach_media('xiaohongshu', IDS[0], media[0].asset_id,
            saved, position=0, kind='image', mime='image/webp')
        baseline = (archive.read_bytes(), archive.stat().st_mtime_ns)
        conflict = self.root / 'downloads' / 'xiaohongshu' / IDS[0]
        conflict.parent.mkdir(parents=True)
        conflict.write_text('Controlled local filesystem conflict', encoding='utf-8')

        def open_response(*args, **kwargs):
            response = MagicMock()
            response.__enter__.return_value = response
            response.status = 200
            response.headers = {'Content-Length': str(len(payload))}
            response.geturl.return_value = url
            response.read.side_effect = io.BytesIO(payload).read
            return response
        with patch('creator_archive.adapters.xhs_media.safe_media_url', side_effect=lambda value, **_: value), \
             patch('creator_archive.adapters.xhs_media.build_opener') as opener:
            opener.return_value.open.side_effect = open_response
            job = self.service.start('content', 'xiaohongshu', AUTHOR, item_ids=IDS[:2])['job_id']
            self.service.wait()
            run = self.service.workspace()['runs'][0]
            self.assertEqual((run['state'], run['item_count'], run['failed_count']), ('partial', 1, 1))
            failure = self.service.job_failures(job)['items'][0]
            self.assertEqual(failure['reason'], 'media_write_failed')
            self.assertIn('downloads', failure['next_step'])
            self.assertIn('权限', failure['next_step'])
            self.assertNotIn('浏览器', failure['next_step'])
            self.assertEqual(downloads, [(IDS[0], 'image-001'), (IDS[1], 'image-000'), (IDS[1], 'image-001')])
            conflict.unlink()
            restarted = WorkspaceService(self.root, adapter_factory=lambda _: self.source)
            self.addCleanup(restarted.close)
            restarted.resume(job)
            restarted.wait()
            run = restarted.workspace()['runs'][0]
            self.assertEqual((run['id'], run['target_count'], run['item_count'], run['state']), (job, 2, 2, 'succeeded'))
        self.assertEqual([item for item, _ in self.source.calls], [IDS[0], IDS[1], IDS[0]])
        self.assertEqual(downloads[-1], (IDS[0], 'image-001'))
        self.assertEqual(len(downloads), 4)
        self.assertEqual((archive.read_bytes(), archive.stat().st_mtime_ns), baseline)

    def test_open_login_rejects_active_jobs_then_clears_ephemeral_links(self):
        self.service._spawn=lambda _:None
        job=self.service.start('metrics')['job_id']
        self.source.open_login=lambda:{'state':'needs_login'}
        with self.assertRaises(ValueError):
            self.service.open_login()
        self.service._finish(job,'needs_login','needs_login')
        self.service._job_sources[job]={IDS[0]:'ephemeral'}
        with patch.object(self.service.account, 'start', return_value={'state':'waiting_scan'}):
            self.service.open_login()
        self.assertEqual(self.service._job_sources,{job:{IDS[0]:'ephemeral'}})

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
    def test_verified_deep_link_survives_batch_prepare_and_revalidates_live_detail(self):
        from tests.test_xhs_content import detail, AUTHOR as author, ITEM
        with tempfile.TemporaryDirectory() as tmp:
            transport = XhsBrowserTransport(Path(tmp)/"profile")
            try:
                transport._ensure = lambda: None
                transport._page = MagicMock()
                transport._detail_page = MagicMock()
                transport._detail_page.goto.return_value.status = 200
                transport._detail_page.evaluate.return_value = detail()
                url = f"https://www.xiaohongshu.com/explore/{ITEM}?xsec_token=VERIFIED_ONLY_IN_MEMORY"
                transport.resolve_item(url)
                transport._fetch = MagicMock(side_effect=AssertionError("unnecessary author scan"))
                transport.prepare_details(author, [ITEM])
                transport._detail_page.evaluate.return_value["note"]["desc"] = "Fresh body"
                result = transport.detail(author, ITEM)
                self.assertEqual(result["text"], "Fresh body")
                self.assertEqual(transport._detail_page.goto.call_count, 2)
                transport._fetch.assert_not_called()
                self.assertNotIn("VERIFIED_ONLY_IN_MEMORY", result["source_url"])
            finally:
                transport.close()
            self.assertEqual(transport._verified_detail_links, {})

    def test_mixed_batch_keeps_verified_link_but_discards_unverified_stale_link(self):
        from tests.test_xhs_content import detail, AUTHOR as author, ITEM
        with tempfile.TemporaryDirectory() as tmp:
            transport = XhsBrowserTransport(Path(tmp)/"profile")
            try:
                transport._ensure = lambda: None
                transport._page = MagicMock()
                transport._detail_page = MagicMock()
                transport._detail_page.goto.return_value.status = 200
                transport._detail_page.evaluate.return_value = detail()
                url = f"https://www.xiaohongshu.com/explore/{ITEM}?xsec_token=VERIFIED"
                transport.resolve_item(url)
                transport._detail_links[(author, IDS[-1])] = "unverified-stale"
                transport._page.locator.return_value.evaluate_all.return_value = []
                calls = []
                def fetch(a, cursor):
                    calls.append(cursor)
                    return {"success": True, "data": {"notes": [{"note_id": IDS[len(calls)], "user": {"user_id": a}}], "cursor": IDS[len(calls)], "has_more": True}}
                transport._fetch = fetch
                transport.prepare_details(author, [ITEM, IDS[-1]])
                self.assertEqual(len(calls), 3)
                self.assertEqual(transport._detail_links[(author, ITEM)], url)
                self.assertNotIn((author, IDS[-1]), transport._detail_links)
            finally:
                transport.close()

    def test_verified_links_clear_on_login_wall_and_unavailable_item(self):
        from tests.test_xhs_content import detail, AUTHOR as author, ITEM
        for category in ("needs_login", "verification_required", "item_unavailable", "resolve_unavailable"):
            with self.subTest(category=category), tempfile.TemporaryDirectory() as tmp:
                transport = XhsBrowserTransport(Path(tmp)/"profile")
                try:
                    transport._ensure = lambda: None
                    transport._page = MagicMock()
                    transport._detail_page = MagicMock()
                    transport._detail_page.goto.return_value.status = 200
                    transport._detail_page.evaluate.return_value = detail()
                    transport.resolve_item(f"https://www.xiaohongshu.com/explore/{ITEM}?xsec_token=VERIFIED")
                    if category in {"item_unavailable", "resolve_unavailable"}:
                        transport._detail_page.evaluate.return_value = None
                        transport._detail_page.locator.return_value.inner_text.return_value = "当前笔记暂时无法浏览"
                        action = (lambda: transport.detail(author, ITEM)) if category == "item_unavailable" else (lambda: transport.resolve_item(f"https://www.xiaohongshu.com/explore/{ITEM}?xsec_token=VERIFIED"))
                    else:
                        def action():
                            def fail():
                                raise AdapterFailure(category)
                            transport._call(fail)
                    with self.assertRaises(AdapterFailure) as error:
                        action()
                    self.assertEqual(error.exception.category, "item_unavailable" if category == "resolve_unavailable" else category)
                    self.assertEqual(transport._verified_detail_links, {})
                    self.assertEqual(transport._detail_links, {})
                finally:
                    transport.close()

    def test_resolve_uses_exact_note_author_and_rejects_mismatch_without_scanning(self):
        from tests.test_xhs_content import detail, AUTHOR as observed_author, ITEM
        with tempfile.TemporaryDirectory() as tmp:
            transport = XhsBrowserTransport(Path(tmp)/"profile")
            try:
                transport._ensure = lambda: None
                transport._page = MagicMock()
                transport._detail_page = MagicMock()
                transport._detail_page.goto.return_value.status = 200
                transport._detail_page.evaluate.return_value = detail()
                url = f"https://www.xiaohongshu.com/discovery/item/{ITEM}?xsec_token=RESOLVE_SECRET"
                result = transport.resolve_item(url)
                self.assertEqual(result["author_id"], observed_author)
                self.assertNotIn("RESOLVE_SECRET",result["source_url"])
                transport._page.mouse.wheel.assert_not_called()
                self.assertEqual(transport._detail_page.goto.call_count,1)
                from creator_archive.links import classify
                profile_url = f"https://www.xiaohongshu.com/user/profile/{observed_author}/{ITEM}?xsec_token=RESOLVE_SECRET"
                classified = classify(profile_url)
                self.assertEqual(classified["kind"], "item")
                self.assertIsNone(classified["candidate_author_id"])
                self.assertEqual(transport.resolve_item(profile_url)["author_id"], observed_author)
                with self.assertRaises(AdapterFailure):
                    transport.resolve_item(profile_url.replace(observed_author, "c" * 24))
                transport._detail_page.evaluate.return_value["note"]["noteId"] = IDS[6]
                with self.assertRaises(AdapterFailure):
                    transport.resolve_item(url)
                with self.assertRaises(ValueError):
                    transport.resolve_item(f"https://www.xiaohongshu.com/explore/{ITEM}")
            finally:
                transport.close()

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
