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


    def test_socket_guard_blocks_platform_without_any_real_request(self):
        with self.assertRaises(OfflineEgressBlocked):
            socket.getaddrinfo("www.xiaohongshu.com", 443)
        self.assertGreater(len(self.guard.denied), 0)

    def test_browser_authorization_is_disabled_without_creating_cookie(self):
        target = self.root / "browser.cookie"
        with self.assertRaises(AdapterFailure) as failure:
            authorize_session(target)
        self.assertEqual(failure.exception.category, "browser_automation_disabled")
        self.assertFalse(target.exists())

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
