"""Offline-only account safety and synthetic failure/checkpoint regression.

No result here is a platform reproduction or proof of platform recovery.
Every test installs a socket-level guard before constructing application code.
"""
from __future__ import annotations

import _socket
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from creator_archive import network_safety
from creator_archive.adapters.feed_http import FeedHttpTransport
from creator_archive.adapters.xhs import MediaCandidate
from creator_archive.adapters.xhs_http import XhsHttpTransport, _http_get, authorize_session
from creator_archive.adapters.xhs_media import download_media, safe_media_url
from creator_archive.adapters.xhs_share import expand_share_link
from creator_archive.adapters.xhs_transport import XhsBrowserTransport
from creator_archive.service import PlatformAccessPaused, WorkspaceService
from creator_archive.validation import AdapterFailure, Item, Page
from creator_archive.xhs_source_setup import configure_native_source
from scripts.run_offline_tests import EgressGuard, OfflineEgressBlocked


AUTHOR = "a" * 24
OTHER = "b" * 24
IDS = [f"{value:024x}" for value in range(1, 8)]
CDN = "https://ci.xhscdn.com/synthetic.png"
PNG = b"\x89PNG\r\n\x1a\n" + b"offline synthetic pixels"


class NetworkSafetyTests(unittest.TestCase):
    def setUp(self):
        self.guard = self.enterContext(EgressGuard())
        self.temp = tempfile.TemporaryDirectory(prefix="creator-safety-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.transport = XhsHttpTransport({"kind": "xhs_http", "cookie_file": str(self.root / "not-read-cookie")},
                                          "xiaohongshu", AUTHOR)
        self.addCleanup(self.transport.close)

    def assertPaused(self, call):
        with self.assertRaises(AdapterFailure) as caught:
            call()
        self.assertEqual(caught.exception.category, "network_paused")

    def test_default_policy_cannot_be_released_by_environment(self):
        with patch.dict("os.environ", {"XHS_NETWORK_PAUSED": "0", "CREATOR_ARCHIVE_ALLOW_XHS": "1"}):
            self.assertTrue(network_safety.xhs_network_paused())
            self.assertPaused(network_safety.require_xhs_network)
            self.assertPaused(network_safety.require_browser_disabled)

    def test_local_runtime_start_requires_both_network_and_browser_locks(self):
        network_safety.require_paused_local_runtime()
        with patch.object(network_safety, "xhs_network_paused", return_value=False):
            with self.assertRaises(RuntimeError):
                network_safety.require_paused_local_runtime()
        for name in ("require_xhs_network", "require_browser_disabled"):
            with patch.object(network_safety, name, return_value=None):
                with self.assertRaises(RuntimeError):
                    network_safety.require_paused_local_runtime()

    def test_http_all_entry_points_block_before_cookies_signing_or_dns(self):
        candidate = MediaCandidate("image", 0, CDN)
        calls = (
            lambda: self.transport.page(AUTHOR, "checkpoint"),
            lambda: self.transport.detail(AUTHOR, IDS[0]),
            lambda: self.transport.prepare_page_details(AUTHOR, IDS),
            lambda: self.transport.prepare_details(AUTHOR, IDS),
            self.transport.poll_latest,
            lambda: self.transport.verify_author(AUTHOR),
            lambda: self.transport.download_media(candidate, self.root / "media"),
            lambda: self.transport._get("edith.xiaohongshu.com", "/api/sns/web/v1/user_posted"),
            lambda: _http_get("edith.xiaohongshu.com", "/", {}, max_bytes=1),
        )
        with (patch("creator_archive.adapters.xhs_http._read_cookies") as cookie,
             patch("creator_archive.adapters.xhs_http._signed_headers") as signing,
             patch("creator_archive.adapters.xhs_http.socket.getaddrinfo") as dns):
            for number, call in enumerate(calls):
                with self.subTest(entry=number):
                    self.assertPaused(call)
            cookie.assert_not_called()
            signing.assert_not_called()
            dns.assert_not_called()
        self.assertFalse((self.root / "not-read-cookie").exists())

    def test_cached_http_detail_does_not_bypass_pause(self):
        self.transport._detail_cache[IDS[0]] = (time.monotonic(), {"text": "already cached"})
        self.assertPaused(lambda: self.transport.detail(AUTHOR, IDS[0]))
        self.assertPaused(lambda: self.transport.prepare_details(AUTHOR, ()))

    def test_feed_source_and_existing_feed_cache_are_paused(self):
        config = {"url": "http://127.0.0.1:1/feed", "format": "json"}
        self.assertPaused(lambda: FeedHttpTransport(config, "xiaohongshu", AUTHOR))
        with patch.object(network_safety, "xhs_network_paused", return_value=False):
            feed = FeedHttpTransport(config, "xiaohongshu", AUTHOR)
        feed._cache[IDS[0]] = {"item_id": IDS[0], "text": "saved synthetic cache"}
        for call in (lambda: feed.page(AUTHOR, None), feed.poll_latest,
                     lambda: feed.detail(AUTHOR, IDS[0]),
                     lambda: feed.prepare_page_details(AUTHOR, IDS[:1]),
                     lambda: feed.prepare_details(AUTHOR, IDS[:1]),
                     lambda: feed.verify_author(AUTHOR),
                     lambda: feed.download_media(MediaCandidate("image", 0, CDN), self.root)):
            with self.subTest(entry=call):
                self.assertPaused(call)

    def test_media_dns_download_share_and_source_authorization_block(self):
        with (patch("creator_archive.adapters.xhs_media.socket.getaddrinfo") as dns,
             patch("creator_archive.adapters.xhs_media.build_opener") as opener,
             patch("creator_archive.adapters.xhs_share.httpx.Client") as client):
            for call in (lambda: safe_media_url(CDN),
                         lambda: download_media(MediaCandidate("image", 0, CDN), self.root / "media"),
                         lambda: expand_share_link("https://xhslink.com/a/synthetic"),
                         lambda: configure_native_source(self.root, self.root / "not-read-cookie")):
                self.assertPaused(call)
            dns.assert_not_called()
            opener.assert_not_called()
            client.assert_not_called()

    def test_browser_and_login_block_before_process_or_profile_creation(self):
        browser = XhsBrowserTransport(self.root / "profile")
        self.addCleanup(browser.close)
        with patch("subprocess.Popen") as child:
            for call in (browser._ensure, browser.open_login,
                         lambda: browser.page(AUTHOR, None),
                         lambda: browser.detail(AUTHOR, IDS[0]),
                         lambda: browser.verify_author(AUTHOR),
                         lambda: browser.prepare_page_details(AUTHOR, IDS[:1]),
                         lambda: browser.prepare_details(AUTHOR, IDS[:1]),
                         lambda: authorize_session(self.root / "private")):
                self.assertPaused(call)
            child.assert_not_called()
            browser.close()
        self.assertFalse((self.root / "profile").exists())
        self.assertFalse((self.root / "private").exists())

    def test_socket_guard_denies_tcp_udp_and_public_dns_without_request(self):
        with socket.socket() as tcp, socket.socket(type=socket.SOCK_DGRAM) as udp:
            for call in (lambda: tcp.connect(("203.0.113.1", 443)),
                         lambda: tcp.connect_ex(("203.0.113.1", 443)),
                         lambda: udp.sendto(b"synthetic", ("203.0.113.1", 53)),
                         lambda: socket.getaddrinfo("www.xiaohongshu.com", 443),
                         lambda: socket.gethostbyname("www.xiaohongshu.com"),
                         lambda: socket.gethostbyaddr("203.0.113.1")):
                with self.assertRaises(OfflineEgressBlocked):
                    call()
        self.assertGreaterEqual(len(self.guard.denied), 6)
        self.assertEqual(socket.getaddrinfo("localhost", 80, type=socket.SOCK_STREAM)[0][4][0], "127.0.0.1")

    def test_socket_audit_guard_also_catches_unpatched_original_method(self):
        # A captured native descriptor bypasses mock patching but not auditing.
        with socket.socket() as sock, self.assertRaises(OfflineEgressBlocked):
            _socket.socket.connect(sock, ("203.0.113.1", 443))

    def test_runner_refuses_real_browser_subprocess(self):
        with self.assertRaises(OfflineEgressBlocked):
            subprocess.Popen(["msedge.exe", "--headless", "https://example.invalid"])

    def test_runner_refuses_site_bypass_options_and_external_git_helpers(self):
        for arguments in (("-S",), ("-I",), ("-E",), ("-IS",), ("-sS",), ("-X", "utf8", "-IE")):
            with self.subTest(options=arguments), self.assertRaises(OfflineEgressBlocked):
                subprocess.Popen([sys.executable, *arguments, "-c", "raise AssertionError('must not run')"])
        for arguments in (("diff", "--ext-diff"), ("show", "--textconv"), ("log",), ("status",)):
            with self.subTest(git=arguments), self.assertRaises(OfflineEgressBlocked):
                subprocess.Popen(["git", *arguments])

    def test_runner_checks_options_without_scanning_synthetic_code(self):
        from scripts.run_offline_tests import _ignores_site
        self.assertFalse(_ignores_site(["-X", "utf8", "-c", "-IS"]))
        self.assertFalse(_ignores_site(["-W", "ignore", "-m", "synthetic", "-S"]))
        self.assertTrue(_ignores_site(["-X", "utf8", "-sS", "-c", "pass"]))

    @unittest.skipUnless(os.environ.get("CREATOR_ARCHIVE_OFFLINE_CHILD_SITE"), "child guard verified with scripts/run_offline_tests.py")
    def test_python_child_inherits_socket_guard_and_default_policy(self):
        program = """import socket
from creator_archive import network_safety
assert network_safety.xhs_network_paused()
for operation in (lambda: socket.getaddrinfo('example.invalid', 443), lambda: socket.socket().connect(('203.0.113.1',443))):
    try:
        operation()
    except OSError as error:
        assert 'offline test egress denied' in str(error)
    else:
        raise AssertionError('child egress unexpectedly allowed')
print('child guard passed')
"""
        result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("child guard passed", result.stdout)

    def _seed_service(self):
        service = WorkspaceService(self.root / "workspace", validation_root=self.root / "validation")
        self.addCleanup(service.close)
        service.workflow.subscribe("xiaohongshu", AUTHOR, "Synthetic author", verified=True, evidence="synthetic")
        batch_id, runs = service.workflow._batch("full", None, True)
        run_id = runs[0]["id"]
        service.workflow._commit_page(run_id, None, Page((Item(IDS[0], AUTHOR, ""),), "saved-cursor", True))
        with service.workflow.connect() as db:
            job_id = db.execute("INSERT INTO jobs(platform,author_id,mode,state,run_id,reason,created_at,updated_at) VALUES(?,?,'full','partial',?,'unknown_business_error',?,?)",
                                ("xiaohongshu", AUTHOR, run_id, time.time(), time.time())).lastrowid
            db.execute("INSERT INTO job_items(job_id,platform,item_id,state) VALUES(?,'xiaohongshu',?,'pending')", (job_id, IDS[0]))
        return service, job_id, run_id, batch_id

    def test_pause_restart_resume_and_schedule_preserve_checkpoint_and_raw_reason(self):
        service, job_id, run_id, _ = self._seed_service()
        service.close()
        service = WorkspaceService(service.root, validation_root=self.root / "validation")
        self.addCleanup(service.close)
        with patch.object(service, "transport_for") as transport, patch.object(service, "_spawn") as spawn:
            for call in (lambda: service.resume(job_id), lambda: service.verify("xiaohongshu", AUTHOR),
                         lambda: service.set_refresh_schedule(True), service.open_login,
                         service.transport,
                         lambda: service._clear_platform_access_pause("xiaohongshu")):
                with self.assertRaises(PlatformAccessPaused):
                    call()
            self.assertEqual(service._run_due_refresh(), 0)
            transport.assert_not_called()
            spawn.assert_not_called()
        self.assertIsNone(service._scheduler)
        marker = json.loads((service.root / "xhs-network-paused.json").read_text())
        self.assertTrue(marker["paused"])
        self.assertFalse(marker["automatic_release"])
        with service.workflow.connect() as db:
            job = dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            run = dict(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            self.assertEqual((job["state"], job["reason"]), ("partial", "unknown_business_error"))
            self.assertEqual((run["pages"], run["cursor"]), (1, "saved-cursor"))
            self.assertEqual(db.execute("SELECT state FROM job_items WHERE job_id=?", (job_id,)).fetchone()[0], "pending")

    def test_preexisting_due_schedule_is_not_started_after_restart(self):
        root = self.root / "scheduled"
        root.mkdir()
        (root / "refresh-schedule.json").write_text(json.dumps({"enabled": True, "interval_minutes": 15, "next_at": 1}))
        service = WorkspaceService(root, validation_root=self.root / "validation")
        self.addCleanup(service.close)
        self.assertIsNone(service._scheduler)
        with patch.object(service, "start") as start:
            self.assertEqual(service._run_due_refresh(time.time()), 0)
            start.assert_not_called()

    def test_existing_media_hash_reuse_is_local_and_no_download_occurs(self):
        candidate = MediaCandidate("image", 0, CDN)
        key = sha256(f"{candidate.asset_id}|ci.xhscdn.com/synthetic.png".encode()).hexdigest()
        (self.root / "saved.png").write_bytes(PNG)
        (self.root / f"{key}.json").write_text(json.dumps({"filename": "saved.png", "size": len(PNG), "sha256": sha256(PNG).hexdigest()}))
        with patch("creator_archive.adapters.xhs_media.build_opener") as opener:
            saved = download_media(candidate, self.root)
            self.assertTrue(saved["reused"])
            opener.assert_not_called()
        self.assertEqual((self.root / "saved.png").read_bytes(), PNG)

    def test_local_read_export_and_csp_never_fill_remote_media(self):
        from fastapi.testclient import TestClient
        from creator_archive.app import create_app
        service, _, _, batch_id = self._seed_service()
        service.workflow.save_detail("xiaohongshu", IDS[0], AUTHOR, "Saved body", f"https://www.xiaohongshu.com/explore/{IDS[0]}")
        with service.workflow.connect() as db:
            db.execute("UPDATE items SET title='Saved title',media_state='partial' WHERE item_id=?", (IDS[0],))
        with patch.object(service, "transport_for") as transport:
            self.assertEqual(service.item("xiaohongshu", IDS[0])["detail_text"], "Saved body")
            service.workflow.export_all(batch_id=batch_id)
            transport.assert_not_called()
        with TestClient(create_app(service.root)) as client:
            response = client.get("/")
            self.assertEqual(response.status_code, 200)
            policy = response.headers["content-security-policy"]
            self.assertIn("img-src 'self'", policy)
            self.assertIn("media-src 'self'", policy)
            self.assertNotIn("img-src 'self' https:", policy)
            self.assertEqual(client.get(f"/api/items/xiaohongshu/{IDS[0]}").status_code, 200)
        for output in service.workflow.archive_root.rglob("*.html"):
            rendered = output.read_text(encoding="utf-8")
            self.assertNotIn('src="https://', rendered)
            self.assertNotIn("src='https://", rendered)


class ManualValidationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(EgressGuard())
        self.temp = tempfile.TemporaryDirectory(prefix="creator-manual-validation-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = WorkspaceService(self.root)
        self.addCleanup(self.service.close)
        self.plan = self.root / "xhs-manual-validation.json"
        self.plan.write_text(json.dumps({"schema": 1, "author_id": AUTHOR,
                                        "owner_authorized": True, "account_normal_confirmed": True,
                                        "actions": {}}))
        (self.root / "sources.json").write_text(json.dumps({"xiaohongshu/*": {
            "kind": "xhs_http", "cookie_file": str(self.root / "not-read")}}))
        self.calls = []
        outer = self

        class LocalTransport:
            version = "xhs-author-http-v1"
            fail = None
            notes = IDS[:5]
            media_per_note = 1
            requested_ids = []

            def recent_page(self, author_id):
                network_safety.require_xhs_network()
                network_safety.require_manual_author(author_id)
                network_safety.consume_manual_request("edith.xiaohongshu.com",
                    "/api/sns/web/v1/user_posted?user_id=" + author_id + "&cursor=&num=30")
                outer.calls.append("list")
                if self.fail == "list":
                    raise AdapterFailure("unknown_business_error")
                return Page(tuple(Item(i, AUTHOR, "", "Synthetic") for i in self.notes), None, False)

            def verify_author(self, author_id):
                self.recent_page(author_id)
                return {"author_id": author_id, "display_name": "Synthetic"}

            def detail(self, author_id, item_id, source_url=""):
                network_safety.require_xhs_network()
                network_safety.consume_manual_request("www.xiaohongshu.com", "/explore/" + item_id)
                outer.calls.append("detail")
                self.requested_ids.append(item_id)
                if self.fail == "detail":
                    raise AdapterFailure("reference_missing")
                return {"author_id": author_id, "item_id": item_id, "title": "Synthetic", "text": "Saved synthetic body",
                        "source_url": "https://www.xiaohongshu.com/explore/" + item_id, "content_type": "image",
                        "published_at": "", "source": "synthetic", "observed_at": time.time(), "metrics": {},
                        "missing": [], "media": [MediaCandidate("image", position, CDN + "?item=" + item_id + "&position=" + str(position)) for position in range(self.media_per_note)]}

            def download_media(self, candidate, target_dir):
                network_safety.require_xhs_network()
                network_safety.consume_manual_request("ci.xhscdn.com", "/synthetic.png?" + str(len(outer.calls)))
                outer.calls.append("media")
                if self.fail == "media":
                    raise AdapterFailure("media_failed")
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / "saved.png"
                target.write_bytes(PNG)
                return {"path": str(target), "mime": "image/png"}

            def export_page_references(self, author_id, ids):
                return {i: "https://www.xiaohongshu.com/explore/" + i + "?xsec_token=synthetic" for i in ids}

        self.transport = LocalTransport()
        self.transport.requested_ids = []
        self.service._source_transports[("xiaohongshu", AUTHOR)] = self.transport

    def act(self, name):
        return self.service.manual_validation.perform(name, AUTHOR, True)

    def test_only_explicit_confirmed_action_can_request_and_cannot_repeat(self):
        with self.assertRaises(ValueError):
            self.service.manual_validation.perform("validate", AUTHOR, False)
        with self.assertRaises(ValueError):
            self.service.manual_validation.perform("validate", OTHER, True)
        with self.assertRaises(PlatformAccessPaused):
            self.service.verify("xiaohongshu", AUTHOR)
        self.assertEqual(self.calls, [])
        self.act("validate")
        self.service.wait(10)
        previous = list(self.calls)
        self.assertEqual(self.calls.count("list"), 1)
        with self.assertRaises(ValueError):
            self.act("validate")
        self.assertEqual(self.calls, previous)
        self.assertTrue(network_safety.xhs_network_paused())
        self.assertFalse((self.root / "not-read").exists())
        with self.service.workflow.connect() as db:
            self.assertEqual(db.execute("SELECT reason FROM platform_access_pauses").fetchone()[0], network_safety.SAFETY_REASON)

    def test_one_list_and_original_first_three_only_never_fourth_or_refresh(self):
        self.transport.notes = [IDS[4], IDS[0], IDS[2], IDS[1], IDS[3]]
        job = self.act("validate")
        self.service.wait(10)
        with self.service.workflow.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM jobs WHERE id=?", (job["job_id"],)).fetchone()[0], "succeeded")
            self.assertEqual(db.execute("SELECT count(*) FROM items").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT count(*) FROM assets").fetchone()[0], 3)
        self.assertEqual(self.transport.requested_ids, self.transport.notes[:3])
        self.assertFalse((self.root / "private" / "xhs-page-references").exists())
        self.assertEqual(self.calls.count("list"), 1)
        self.assertEqual(self.calls.count("detail"), 3)
        self.assertEqual(self.calls.count("media"), 3)
        previous = list(self.calls)
        with self.assertRaises(ValueError):
            self.act("refresh")
        self.assertEqual(self.calls, previous)
        self.assertTrue(network_safety.xhs_network_paused())
        self.assertIsNone(self.service._scheduler)
        self.assertEqual(self.service.manual_validation.status()["actions"]["validate"]["state"], "succeeded")

    def _check_failure(self, stage):
        self.transport.fail = stage
        before = len(self.calls)
        if stage == "list":
            with self.assertRaises(AdapterFailure):
                self.act("validate")
        else:
            self.act("validate")
        self.service.wait(10)
        self.assertTrue(self.service.manual_validation.status()["blocked_reason"])
        after = len(self.calls)
        with self.assertRaises(ValueError):
            self.act("validate")
        self.assertEqual(len(self.calls), after)
        self.assertLessEqual(after - before, {"list": 1, "detail": 2, "media": 5}[stage])

    def test_list_failure_stops_without_retry(self):
        self._check_failure("list")

    def test_detail_failure_stops_without_retry(self):
        self._check_failure("detail")

    def test_media_failure_stops_without_retry(self):
        self._check_failure("media")

    def test_scope_denies_deep_pages_other_authors_unknown_notes_repeat_and_budget(self):
        from creator_archive.manual_validation import ManualScope
        cases = (("edith.xiaohongshu.com", "/api/sns/web/v1/user_posted?user_id=" + OTHER + "&cursor=&num=30"),
                 ("edith.xiaohongshu.com", "/api/sns/web/v1/user_posted?user_id=" + AUTHOR + "&cursor=deep&num=30"),
                 ("www.xiaohongshu.com", "/explore/" + IDS[0]), ("evil.invalid", "/media"))
        for host,path in cases:
            scope = ManualScope(AUTHOR, "validate", lambda x: None)
            with self.assertRaises(AdapterFailure):
                scope.consume(host, path)
            self.assertTrue(scope.stopped)
        scope = ManualScope(AUTHOR, "validate", lambda x: None)
        path = "/api/sns/web/v1/user_posted?user_id=" + AUTHOR + "&cursor=&num=30"
        scope.consume("edith.xiaohongshu.com", path)
        with self.assertRaises(AdapterFailure):
            scope.consume("edith.xiaohongshu.com", path)
        scope = ManualScope(AUTHOR, "validate", lambda x: None)
        scope.observe(IDS[:3])
        scope.budget = 1
        scope.consume("www.xiaohongshu.com", "/explore/" + IDS[0])
        with self.assertRaises(AdapterFailure):
            scope.consume("www.xiaohongshu.com", "/explore/" + IDS[1])

    def test_restart_never_resumes_running_scope_and_browser_remains_disabled(self):
        scope = self.service.manual_validation.begin("validate", AUTHOR, True)
        with network_safety.manual_action(scope):
            with self.assertRaises(AdapterFailure):
                network_safety.require_browser_disabled()
            with self.assertRaises(AdapterFailure):
                network_safety.require_manual_author(OTHER)
        self.plan.write_text(json.dumps({"schema": 1, "author_id": AUTHOR,
            "owner_authorized": True, "account_normal_confirmed": True, "actions": {"validate": {"state": "running"}}}))
        self.service.manual_validation.interrupt_pending()
        self.assertEqual(self.service.manual_validation.status()["blocked_reason"], "process_interrupted")
        self.assertTrue(network_safety.xhs_network_paused())

    def test_api_read_is_offline_and_mutation_needs_header_and_confirmation(self):
        from fastapi.testclient import TestClient
        from creator_archive.app import create_app
        with TestClient(create_app(self.root)) as client, patch("creator_archive.adapters.xhs_http._read_cookies") as cookies:
            self.assertEqual(client.get("/api/xhs/manual-validation").json()["author_id"], AUTHOR)
            payload = {"action": "validate", "author_id": AUTHOR, "confirmed": True}
            self.assertEqual(client.post("/api/xhs/manual-validation", json=payload).status_code, 403)
            payload["confirmed"] = False
            self.assertEqual(client.post("/api/xhs/manual-validation", json=payload,
                headers={"X-Creator-Archive": "local-validation"}).status_code, 422)
            self.assertEqual(client.post("/api/xhs/manual-validation", json=payload,
                headers={"X-Creator-Archive": "local-validation", "Origin": "https://example.invalid"}).status_code, 403)
            cookies.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_cdn_redirect_is_rejected_before_dns_or_second_http_request(self):
        from creator_archive.adapters.xhs_media import _SafeRedirect, MediaFailure
        from creator_archive.manual_validation import ManualScope
        scope = ManualScope(AUTHOR, "validate", lambda x: None)
        with network_safety.manual_action(scope), patch("creator_archive.adapters.xhs_media.socket.getaddrinfo") as dns:
            with self.assertRaises(MediaFailure):
                _SafeRedirect().redirect_request(None, None, 302, "redirect", {}, CDN)
            dns.assert_not_called()
        self.assertTrue(scope.stopped)

    def test_excess_media_reports_actual_need_without_downloading_or_extra_budget(self):
        self.transport.media_per_note = 5
        self.act("validate")
        self.service.wait(10)
        state = self.service.manual_validation.status()
        self.assertEqual(state["blocked_reason"], "manual_media_budget_exceeded")
        self.assertEqual(state["media_required"], 15)
        self.assertEqual(state["media_remaining"], 15)
        self.assertEqual(self.calls, ["list", "detail", "detail", "detail"])
        with self.assertRaises(ValueError):
            self.act("validate")

    def test_exact_sixteen_requests_is_the_hard_ceiling(self):
        self.transport.media_per_note = 4
        self.act("validate")
        self.service.wait(10)
        state = self.service.manual_validation.status()
        self.assertEqual(state["actions"]["validate"]["state"], "succeeded")
        self.assertEqual(state["actions"]["validate"]["counts"], {"list": 1, "detail": 3, "media": 12})
        self.assertEqual(len(self.calls), 16)

    def test_fourth_note_is_denied_even_if_present_in_first_page(self):
        from creator_archive.manual_validation import ManualScope
        scope = ManualScope(AUTHOR, "validate", lambda x: None)
        scope.observe(IDS[:5])
        self.assertNotIn(IDS[3], scope.ids)
        with self.assertRaises(AdapterFailure):
            scope.consume("www.xiaohongshu.com", "/explore/" + IDS[3])
        self.assertEqual(scope.counts["detail"], 0)


class SyntheticUnknownFailureTests(unittest.TestCase):
    """Synthetic HTTP 200/-100 only; never a root-cause platform claim."""

    def setUp(self):
        self.guard = self.enterContext(EgressGuard())
        self.policy = self.enterContext(patch.object(network_safety, "xhs_network_paused", return_value=False))
        self.temp = tempfile.TemporaryDirectory(prefix="creator-synthetic-minus100-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        response_body = json.dumps({"success": False, "code": -100, "msg": "Cookie=synthetic-secret; x-s=synthetic-signature"}).encode()

        class LocalHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), LocalHandler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.transport = XhsHttpTransport({"kind": "xhs_http", "cookie_file": str(self.root / "never-read")}, "xiaohongshu", AUTHOR)
        self.addCleanup(self.transport.close)
        self.requests = []

        def local_get(host, path, **kwargs):
            self.requests.append(kwargs["api_params"]["cursor"])
            with urlopen(f"http://127.0.0.1:{self.server.server_port}/synthetic", timeout=2) as response:
                self.assertEqual(response.status, 200)
                return response.read()

        self.transport._get = local_get

    def test_http_200_unknown_code_keeps_structured_redacted_diagnostics(self):
        with self.assertRaises(AdapterFailure) as caught:
            self.transport.page(AUTHOR, "page-six-request")
        error = caught.exception
        self.assertEqual(error.category, "unknown_business_error")
        self.assertEqual(error.retry_after, 0)
        self.assertEqual(error.diagnostics["http_status"], 200)
        self.assertIs(error.diagnostics["success"], False)
        self.assertEqual(error.diagnostics["business_code"], -100)
        self.assertEqual(error.diagnostics["stage"], "list")
        self.assertEqual(error.diagnostics["request_cursor_sha256"], sha256(b"page-six-request").hexdigest())
        self.assertEqual(len(error.diagnostics["message_sha256"]), 64)
        self.assertNotIn("synthetic-secret", json.dumps(error.diagnostics))
        self.assertNotIn("synthetic-signature", json.dumps(error.diagnostics))
        self.assertEqual(self.requests, ["page-six-request"])
        self.assertIsNone(self.transport._page_cache)

    def _seed_checkpoint(self, pending):
        service = WorkspaceService(self.root / "workspace", validation_root=self.root / "validation")
        self.addCleanup(service.close)
        (service.root / "sources.json").write_text(json.dumps({"xiaohongshu/*": {"kind": "xhs_http", "cookie_file": str(self.root / "never-read")}}))
        service.workflow.subscribe("xiaohongshu", AUTHOR, "Synthetic author", verified=True, evidence="synthetic")
        batch_id, runs = service.workflow._batch("full", None, True)
        run_id = runs[0]["id"]
        cursor = None
        for number, item_id in enumerate(IDS, 1):
            service.workflow._commit_page(run_id, cursor, Page((Item(item_id, AUTHOR, ""),), f"next-{number}", True))
            cursor = f"next-{number}"
        with service.workflow.connect() as db:
            db.execute("UPDATE runs SET adapter_version=? WHERE id=?", (self.transport.version, run_id))
            job_id = db.execute("INSERT INTO jobs(platform,author_id,mode,state,run_id,created_at,updated_at) VALUES('xiaohongshu',?,'full','running',?,?,?)",
                                (AUTHOR, run_id, time.time(), time.time())).lastrowid
            for item_id in IDS:
                state = "pending" if pending and item_id == IDS[-1] else "succeeded"
                db.execute("INSERT INTO job_items(job_id,platform,item_id,state) VALUES(?,'xiaohongshu',?,?)", (job_id, item_id, state))
                if state == "succeeded":
                    db.execute("UPDATE items SET detail_text='Preserved body',detail_state='complete',media_state='complete_for_observed_detail' WHERE item_id=?", (item_id,))
            job = dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        return service, job, run_id, batch_id

    def _check_retired_checkpoint(self, pending):
        service, job, run_id, _ = self._seed_checkpoint(pending)
        with service.workflow.connect() as db:
            before_run = tuple(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            before_items = [tuple(row) for row in db.execute("SELECT * FROM job_items WHERE job_id=? ORDER BY item_id", (job["id"],))]
        with self.assertRaisesRegex(ValueError, "旧深分页任务已停用"):
            service.resume(job["id"])
        self.assertEqual(self.requests, [])
        with service.workflow.connect() as db:
            self.assertEqual(tuple(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()), before_run)
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM job_items WHERE job_id=? ORDER BY item_id", (job["id"],))], before_items)
        observed = next(row for row in service.workspace()["runs"] if row["id"] == job["id"])
        self.assertFalse(observed["can_resume"])
        self.assertTrue(observed["retired"])

    def test_old_seventh_page_pending_checkpoint_is_readonly_and_not_resumed(self):
        self._check_retired_checkpoint(pending=True)

    def test_old_deep_history_checkpoint_is_readonly_and_not_resumed(self):
        self._check_retired_checkpoint(pending=False)

    def test_recent_first_page_unknown_failure_stops_all_authors_without_retry(self):
        service = WorkspaceService(self.root / "recent", validation_root=self.root / "validation")
        self.addCleanup(service.close)
        (service.root / "sources.json").write_text(json.dumps({"xiaohongshu/*": {"kind": "xhs_http", "cookie_file": str(self.root / "never-read")}}))
        service.workflow.subscribe("xiaohongshu", AUTHOR, "Synthetic author", verified=True, evidence="synthetic")
        with patch.object(service, "transport_for", return_value=self.transport):
            started = service.start("source_refresh", "xiaohongshu", AUTHOR)
            service.wait(10)
        job = next(row for row in service.workspace()["runs"] if row["id"] == started["job_id"])
        self.assertEqual((job["mode"], job["reason"]), ("recent_window", "unknown_business_error"))
        self.assertEqual(self.requests, [""])
        with service.workflow.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM items").fetchone()[0], 0)
            evidence = dict(db.execute("SELECT * FROM request_failures WHERE job_id=?", (job["id"],)).fetchone())
            self.assertEqual(json.loads(evidence["diagnostic_json"])["business_code"], -100)
        with self.assertRaises(PlatformAccessPaused):
            service.resume(job["id"])
        second = {"platform": "xiaohongshu", "author_id": OTHER}
        with patch.object(self.transport, "recent_page") as page:
            with self.assertRaises(PlatformAccessPaused):
                service._source_call(second, self.transport.recent_page, OTHER)
            page.assert_not_called()
        self.assertEqual(self.requests, [""])

    def test_full_retry_keeps_nonempty_body_and_verified_asset_without_redownload(self):
        service, job, _, _ = self._seed_checkpoint(pending=False)
        image = MediaCandidate("image", 0, CDN)
        video = MediaCandidate("video", 1, "https://sns-video.xhscdn.com/synthetic.mp4")
        saved_input = self.root / "image.png"
        saved_input.write_bytes(PNG)
        saved = service.workflow.attach_media("xiaohongshu", IDS[0], image.asset_id, saved_input,
                                              position=image.position, kind=image.kind, mime="image/png")
        before = (saved.stat().st_mtime_ns, saved.read_bytes())
        with service.workflow.connect() as db:
            db.execute("UPDATE items SET media_state='partial' WHERE item_id=?", (IDS[0],))
            db.execute("UPDATE job_items SET state='partial' WHERE job_id=? AND item_id=?", (job["id"], IDS[0]))

        class LocalContent:
            downloads = []

            def detail(self, author_id, item_id, source_url=""):
                return {"author_id": author_id, "item_id": item_id, "text": "Must not replace preserved body",
                        "title": "Synthetic title", "content_type": "video", "published_at": "",
                        "source_url": f"https://www.xiaohongshu.com/explore/{item_id}",
                        "metrics": {}, "source": "synthetic_local_content", "observed_at": time.time(),
                        "media": [image, video], "missing": []}

            def download_media(self, candidate, target_dir):
                self.downloads.append(candidate.kind)
                target = Path(target_dir) / "local.mp4"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"synthetic video")
                return {"path": str(target), "mime": "video/mp4"}

        content = LocalContent()
        with patch.object(service, "transport_for", return_value=content), patch.object(service.workflow, "save_detail") as save_body:
            pending = service._execute_content(job, finalize=False, only_item_ids=[IDS[0]])
            save_body.assert_not_called()
        self.assertEqual(pending, 0)
        self.assertEqual(content.downloads, ["video"])
        self.assertEqual((saved.stat().st_mtime_ns, saved.read_bytes()), before)
        with service.workflow.connect() as db:
            self.assertEqual(db.execute("SELECT detail_text FROM items WHERE item_id=?", (IDS[0],)).fetchone()[0], "Preserved body")
            self.assertEqual(db.execute("SELECT count(*) FROM assets WHERE item_id=?", (IDS[0],)).fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT state FROM job_items WHERE job_id=? AND item_id=?", (job["id"], IDS[0])).fetchone()[0], "succeeded")


if __name__ == "__main__":
    unittest.main()
