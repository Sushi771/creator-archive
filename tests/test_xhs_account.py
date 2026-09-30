"""Normal app routes with temporary data and synthetic HTTP responses only."""
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from creator_archive.app import create_app
from creator_archive.service import WorkspaceService
from creator_archive.validation import AdapterFailure
from creator_archive.xhs_account import XhsAccount
from scripts.run_offline_tests import EgressGuard, OfflineEgressBlocked
from test_xhs_http import _html, _listing
from test_xhs_integration import AUTHOR, PROFILE, IDS, PNG, MP4

LOCAL_HEADERS = {"X-Creator-Archive": "local-validation"}


class XhsAccountTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(EgressGuard())
        self.temporary = tempfile.TemporaryDirectory(prefix="creator-account-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = create_app(self.root)
        self.service = self.app.state.service
        self.addCleanup(self.service.close)
        self.client = self.enterContext(TestClient(self.app))
        self.auth_calls = []
        self.get_calls = []
        self.media_calls = []
        self.guest = False
        self.auth_fail = None
        self.list_fail = False
        self.media_fail = False
        self.window = IDS[:4]
        self.enterContext(patch("creator_archive.xhs_account._https_request", side_effect=self.auth))
        self.enterContext(patch("creator_archive.xhs_account.time.sleep"))
        self.enterContext(patch("creator_archive.adapters.xhs_http.time.sleep"))
        self.enterContext(patch("creator_archive.adapters.xhs_http._http_get", side_effect=self.get))
        self.enterContext(patch("creator_archive.adapters.xhs_http.download_media", side_effect=self.media))

    def post(self, path, **body):
        return self.client.post(path, json=body, headers=LOCAL_HEADERS)

    def auth(self, method, host, path, headers, *, max_bytes, body=None):
        self.auth_calls.append((method, path.split("?")[0]))
        self.assertIn("x-s", headers)
        if self.auth_fail:
            raise AdapterFailure(self.auth_fail)
        received = []
        if path.endswith("/activate"):
            self.assertEqual(body, b"{}")
            received = ["web_session=synthetic-guest; Domain=.xiaohongshu.com; Secure; HttpOnly"]
            data = {}
        elif path.endswith("/create"):
            self.assertEqual(body, b'{"qr_type":1}')
            data = {"qr_id": "synthetic-qr-id", "code": "synthetic-qr-code",
                    "url": "https://www.xiaohongshu.com/login/qr?qr_id=synthetic"}
        elif "/qrcode/status?" in path:
            received = ["web_session=synthetic-user; Domain=.xiaohongshu.com; Secure; HttpOnly"]
            data = {"code_status": 2}
        elif path.endswith("/user/me"):
            data = {"guest": self.guest, "user_id": "b" * 24, "nickname": "模拟账号"}
        else:
            self.fail("unexpected synthetic account endpoint")
        return json.dumps({"success": True, "code": 0, "data": data}).encode(), received

    def get(self, host, path, headers, *, max_bytes):
        self.get_calls.append((host, path.split("?")[0]))
        if "user_posted?" in path:
            if self.list_fail:
                return b'{"success":false,"code":-100,"msg":"synthetic failure"}'
            return json.dumps({"success": True, "code": 0,
                               "data": _listing(self.window, more=True, cursor="never-follow")}).encode()
        item_id = path.split("?")[0].split("/")[-1]
        return _html(item_id, video=item_id == IDS[0])

    def media(self, candidate, target_dir):
        self.media_calls.append(candidate.kind)
        if self.media_fail:
            from creator_archive.adapters.xhs_media import MediaFailure
            raise MediaFailure("synthetic_media_error", http_status=403)
        target_dir.mkdir(parents=True, exist_ok=True)
        file = target_dir / (candidate.asset_id + (".mp4" if candidate.kind == "video" else ".png"))
        file.write_bytes(MP4 if candidate.kind == "video" else PNG)
        return {"path": str(file), "mime": "video/mp4" if candidate.kind == "video" else "image/png"}

    def login(self):
        response = self.post("/api/platforms/xiaohongshu/login")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["qr_active"])
        image = self.client.get("/api/xhs/account/qr-image")
        self.assertEqual(image.status_code, 200)
        self.assertIn("<svg", image.text)
        result = self.post("/api/xhs/account", action="poll")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["state"], "connected")
        self.assertFalse(result.json()["author_list_verified"])
        self.assertEqual(self.get_calls, [])
        self.assertNotIn("synthetic-user", result.text)
        self.assertNotIn("synthetic-qr-code", result.text)

    def test_normal_login_subscribe_first3_read_export_and_refresh_dedup(self):
        self.login()
        result = self.post("/api/subscriptions", text=PROFILE, window_limit=3)
        self.assertEqual(result.status_code, 200, result.text)
        self.service.wait()
        job = next(j for j in self.service.workspace()["runs"] if j["id"] == result.json()["job_id"])
        self.assertEqual((job["state"], job["body_saved_count"], job["registered_media"]),
                         ("succeeded", 3, {"image": 3, "video": 1}), job)
        self.assertEqual(len([path for _, path in self.get_calls if "user_posted" in path]), 1)
        self.assertEqual([path.rsplit("/", 1)[-1] for _, path in self.get_calls if "/explore/" in path], IDS[:3])
        detail = self.client.get(f"/api/items/xiaohongshu/{IDS[0]}").json()
        self.assertEqual(detail["detail_text"], "A complete body")
        with self.service.workflow.connect() as db:
            for path, digest in db.execute("SELECT relative_path,sha256 FROM assets"):
                response = self.client.get("/archive/" + path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(sha256(response.content).hexdigest(), digest)
        calls = (len(self.auth_calls), len(self.get_calls), len(self.media_calls))
        export = self.post("/api/jobs", mode="archive", platform="xiaohongshu", author_id=AUTHOR)
        self.assertEqual(export.status_code, 200)
        self.service.wait()
        self.assertEqual(calls, (len(self.auth_calls), len(self.get_calls), len(self.media_calls)))
        for file in self.service.workflow.archive_root.rglob("*.html"):
            self.assertNotIn('src="https://', file.read_text(encoding="utf-8"))
        refresh = self.post("/api/jobs", mode="source_refresh", platform="xiaohongshu", author_id=AUTHOR)
        self.assertEqual(refresh.status_code, 200, refresh.text)
        self.service.wait()
        job = next(j for j in self.service.workspace()["runs"] if j["id"] == refresh.json()["job_id"])
        self.assertEqual((job["state"], job["target_count"], job["skipped_existing_count"]), ("succeeded", 1, 3))
        self.assertEqual(self.get_calls[-1][1].rsplit("/", 1)[-1], IDS[3])
        self.assertEqual(self.service.refresh_schedule()["enabled"], False)

    def test_guest_or_qr_failure_keeps_old_session_and_stops_polling(self):
        self.login()
        original = self.service.account.cookie_file.read_bytes()
        original_sources = (self.root / "sources.json").read_bytes()
        self.guest = True
        self.post("/api/platforms/xiaohongshu/login")
        failed = self.post("/api/xhs/account", action="poll")
        self.assertEqual(failed.status_code, 409)
        self.assertEqual(failed.json()["reason"], "needs_login")
        self.assertEqual(self.service.account.cookie_file.read_bytes(), original)
        self.assertEqual((self.root / "sources.json").read_bytes(), original_sources)
        count = len(self.auth_calls)
        self.assertEqual(self.post("/api/xhs/account", action="poll").status_code, 422)
        self.assertEqual(len(self.auth_calls), count)
        self.auth_fail = "verification_required"
        self.assertEqual(self.post("/api/platforms/xiaohongshu/login").status_code, 409)
        self.assertEqual(self.service.account.cookie_file.read_bytes(), original)

    def test_list_minus100_is_not_login_success_or_subscription_success(self):
        self.login()
        self.list_fail = True
        response = self.post("/api/subscriptions", text=PROFILE, window_limit=3)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["business_code"], -100)
        self.assertEqual(len(self.get_calls), 1)
        self.assertEqual(self.media_calls, [])
        self.assertFalse(self.service.workspace()["subscriptions"][0]["identity_verified"])
        self.assertEqual(self.service.account.status()["state"], "connected")
        self.assertEqual(self.post("/api/subscriptions", text=PROFILE, window_limit=3).status_code, 422)
        self.assertEqual(len(self.get_calls), 1)

    def test_media_failure_stops_before_second_detail_and_retains_body(self):
        self.login()
        self.media_fail = True
        response = self.post("/api/subscriptions", text=PROFILE, window_limit=3)
        self.service.wait()
        job = next(j for j in self.service.workspace()["runs"] if j["id"] == response.json()["job_id"])
        self.assertEqual(job["reason"], "verification_required")
        self.assertEqual(len(self.get_calls), 2)
        self.assertEqual(len(self.media_calls), 1)
        self.assertEqual(self.service.item("xiaohongshu", IDS[0])["detail_text"], "A complete body")

    def test_local_source_write_failure_rolls_back_session_metadata_and_sources(self):
        self.login()
        files = [self.service.account.cookie_file, self.service.account.metadata, self.root / "sources.json"]
        before = [file.read_bytes() for file in files]
        with patch("creator_archive.xhs_account.configure_native_source", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                self.service.account.verify()
        self.assertEqual(before, [file.read_bytes() for file in files])
        self.assertFalse(self.service.account.verified_in_process)
        self.assertTrue(list((self.root / "backups").glob("session-before-update-*")))

    def test_restart_does_not_request_platform_or_resume_old_work(self):
        self.login()
        count = len(self.auth_calls)
        self.service.close()
        restarted = WorkspaceService(self.root)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.account.status()["state"], "not_checked")
        self.assertEqual(len(self.auth_calls), count)
        self.assertEqual(self.get_calls, [])
        self.assertIsNone(restarted._scheduler)

    def test_page_open_and_cancel_never_request_platform_or_expose_permits(self):
        self.assertEqual(self.client.get("/api/workspace").status_code, 200)
        self.assertEqual(self.auth_calls, [])
        self.assertEqual(self.client.get("/api/xhs/manual-validation").status_code, 404)
        self.assertNotIn('id="manual-validation"', self.client.get("/").text)
        self.assertEqual(self.post("/api/xhs/account", action="cancel").status_code, 200)
        self.assertEqual(self.auth_calls, [])

    def test_migrate_only_artificial_pause_preserves_platform_failure_and_old_jobs(self):
        with self.service.workflow.connect() as db:
            db.execute("INSERT INTO platform_access_pauses VALUES('xiaohongshu',?,1)",
                       ("account_safety_user_instruction_2026_09_30",))
        (self.root / "xhs-network-paused.json").write_text('{"paused":true}')
        (self.root / "xhs-manual-validation.json").write_text('{"unused":true}')
        restarted = WorkspaceService(self.root)
        self.addCleanup(restarted.close)
        self.assertIsNone(restarted._platform_access_reason("xiaohongshu"))
        self.assertFalse((self.root / "xhs-network-paused.json").exists())
        self.assertTrue(list((self.root / "backups").glob("retired-*.json")))
        for file in (self.root / "backups").glob("archive-before-account-flow-*.sqlite3"):
            with closing(sqlite3.connect(file)) as db:
                self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
