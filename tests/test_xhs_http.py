"""Offline contract tests for the optional XHS HTTP transport."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from creator_archive.adapters.xhs_http import (
    XhsHttpTransport, _http_get, _json_state, _read_cookies, _replace_javascript_literals,
    authorize_session,
)
from creator_archive.validation import AdapterFailure
from creator_archive.adapters.xhs import MediaCandidate
from creator_archive.adapters.xhs_media import MediaFailure


AUTHOR = "a" * 24
FOREIGN = "b" * 24
NOTE_ONE = "1" * 24
NOTE_TWO = "2" * 24


def _listing(ids, *, more=False, cursor="", author=AUTHOR, with_token=True):
    return {"notes": [{"note_id": item_id, "user": {"user_id": author},
                       **({"xsec_token": "synthetic-token", "xsec_source": "pc_user"} if with_token else {})}
                      for item_id in ids], "has_more": more, "cursor": cursor}


def _html(item_id, *, author=AUTHOR, video=False, live_photo=False):
    note = {"noteId": item_id, "user": {"userId": author}, "title": "A title",
            "desc": "A complete body", "type": "video" if video else "normal",
            "time": 1700000000000, "lastUpdateTime": 1700000100000,
            "imageList": [{"urlDefault": "https://ci.xhscdn.com/one.jpg", "width": 800, "height": 600,
                           "livePhoto": live_photo,
                           "stream": {"h264": [{"masterUrl": "https://sns-video.xhscdn.com/live.mp4"}]} if live_photo else {}}],
            "video": {"media": {"video": {"md5": "a" * 32}, "stream": {"h264": [{"masterUrl": "https://sns-video.xhscdn.com/one.mp4",
                                                       "width": 1280, "size": 100, "qualityType": "HD"}]}}}}
    state = {"note": {"noteDetailMap": {item_id: {"note": note}}}}
    return ("<html><script>window.__INITIAL_STATE__=" + json.dumps(state) + ";</script></html>").encode()


class XhsHttpTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cookie_file = Path(self.temporary.name) / "cookie.txt"
        self.cookie_file.write_text("a1=synthetic-a1; web_session=synthetic-session\n", encoding="utf-8")
        self.transport = XhsHttpTransport({"kind": "xhs_http", "cookie_file": str(self.cookie_file)},
                                          "xiaohongshu", AUTHOR)
        self.addCleanup(self.transport.close)

    def test_private_cookie_required_and_repository_file_refused(self):
        self.assertEqual(_read_cookies(self.cookie_file)[1]["a1"], "synthetic-a1")
        self.cookie_file.write_text("a1=only-one\n", encoding="utf-8")
        with self.assertRaises(AdapterFailure) as caught:
            _read_cookies(self.cookie_file)
        self.assertEqual(caught.exception.category, "needs_login")
        self.assertNotIn(str(self.cookie_file), str(caught.exception))
        with self.assertRaises(ValueError):
            XhsHttpTransport({"kind": "xhs_http", "cookie_file": "relative.txt"}, "xiaohongshu", AUTHOR)
        with self.assertRaises(ValueError):
            XhsHttpTransport({"kind": "xhs_http", "cookie_file": str(Path(__file__).resolve())},
                             "xiaohongshu", AUTHOR)
        with self.assertRaises(ValueError):
            XhsHttpTransport({"kind": "xhs_http", "cookie_file": str(self.cookie_file),
                              "min_interval_seconds": "nan"}, "xiaohongshu", AUTHOR)

    def test_page_uses_actual_author_and_explicit_terminal(self):
        self.transport._get_api = lambda cursor: _listing([NOTE_ONE], more=not bool(cursor), cursor=NOTE_ONE)
        first = self.transport.page(AUTHOR, None)
        self.assertEqual([item.item_id for item in first.items], [NOTE_ONE])
        self.assertTrue(first.has_more)
        self.assertEqual(first.next_cursor, NOTE_ONE)
        last = self.transport.page(AUTHOR, first.next_cursor)
        self.assertFalse(last.has_more)
        self.assertIn("has_more=false", last.terminal_evidence)
        self.assertEqual(self.transport.prepare_page_details(AUTHOR, [NOTE_ONE]), ())

    def test_identity_and_abnormal_page_never_advance(self):
        cases = [(_listing([NOTE_ONE], author=FOREIGN), "identity_mismatch"),
                 (_listing([], more=False), "invalid_page"),
                 (_listing([], more=True, cursor=NOTE_ONE), "missing_cursor"),
                 (_listing([NOTE_ONE], more=True, cursor=""), "missing_cursor"),
                 ({"notes": [_listing([NOTE_ONE])["notes"][0]]}, "invalid_response")]
        for response, category in cases:
            with self.subTest(category=category):
                self.transport._page_cache = None
                self.transport._get_api = lambda cursor, response=response: response
                with self.assertRaises(AdapterFailure) as caught:
                    self.transport.page(AUTHOR, None)
                self.assertEqual(caught.exception.category, category)
        self.transport._get_api = lambda cursor: _listing([], more=False)
        with self.assertRaises(AdapterFailure) as caught:
            self.transport.page(AUTHOR, "prior-page-cursor")
        self.assertEqual(caught.exception.category, "invalid_page")

    def test_detail_matches_exact_note_and_author_with_ordered_media(self):
        self.transport._get_api = lambda cursor: _listing([NOTE_ONE], more=False)
        self.transport.page(AUTHOR, None)
        calls = []

        def get(host, path, **kwargs):
            calls.append((host, path))
            return _html(NOTE_ONE, video=True)

        self.transport._get = get
        result = self.transport.detail(AUTHOR, NOTE_ONE)
        self.assertEqual(result["item_id"], NOTE_ONE)
        self.assertEqual(result["author_id"], AUTHOR)
        self.assertEqual(result["text"], "A complete body")
        self.assertEqual([candidate.kind for candidate in result["media"]], ["image", "video"])
        self.assertEqual(result["source"], "xhs_http_ssr")
        self.assertTrue(result["updated_at"].startswith("2023-11-14"))
        self.assertEqual(result["video_md5_observed"], "a" * 32)
        self.assertEqual(len(calls), 1)
        self.transport.detail(AUTHOR, NOTE_ONE)
        self.assertEqual(len(calls), 1)
        self.transport.detail(AUTHOR, NOTE_ONE, f"https://www.xiaohongshu.com/explore/{NOTE_ONE}")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("synthetic-token", result["source_url"])

    def test_detail_rejects_wrong_author_and_unobserved_reference(self):
        self.transport._get_api = lambda cursor: _listing([NOTE_ONE], more=False)
        self.transport.page(AUTHOR, None)
        self.transport._get = lambda *args, **kwargs: _html(NOTE_ONE, author=FOREIGN)
        with self.assertRaises(AdapterFailure) as caught:
            self.transport.detail(AUTHOR, NOTE_ONE)
        self.assertEqual(caught.exception.category, "identity_mismatch")
        with self.assertRaises(AdapterFailure) as caught:
            self.transport.detail(AUTHOR, NOTE_TWO)
        self.assertEqual(caught.exception.category, "reference_missing")
        foreign_url = f"https://www.xiaohongshu.com/user/profile/{FOREIGN}/{NOTE_ONE}?xsec_token=synthetic"
        with self.assertRaises(AdapterFailure) as caught:
            self.transport.detail(AUTHOR, NOTE_ONE, foreign_url)
        self.assertEqual(caught.exception.category, "reference_missing")

    def test_live_photo_video_uses_observed_h264_stream(self):
        self.transport._get_api = lambda cursor: _listing([NOTE_ONE], more=False)
        self.transport.page(AUTHOR, None)
        self.transport._get = lambda *args, **kwargs: _html(NOTE_ONE, live_photo=True)
        result = self.transport.detail(AUTHOR, NOTE_ONE)
        self.assertEqual([candidate.kind for candidate in result["media"]], ["image", "video"])
        self.assertNotIn("live_photo_0_video_unresolved", result["missing"])

    def test_poll_latest_is_one_page_and_reuses_detail_for_content_phase(self):
        self.transport._get_api = lambda cursor: _listing([NOTE_ONE, NOTE_TWO], more=True, cursor=NOTE_TWO)
        calls = []
        self.transport._get = lambda host, path, **kwargs: (calls.append(path), _html(NOTE_ONE if NOTE_ONE in path else NOTE_TWO))[1]
        # The test responder keys on the path's note ID, not a local record.
        entries = self.transport.poll_latest()
        self.assertEqual([entry["item_id"] for entry in entries], [NOTE_ONE, NOTE_TWO])
        self.assertEqual(len(calls), 2)
        self.transport.detail(AUTHOR, NOTE_ONE)
        self.assertEqual(len(calls), 2)

    def test_saved_cursor_can_refetch_only_its_page_after_process_restart(self):
        saved_cursor = "prior-page-cursor"
        expected = {NOTE_TWO}
        requested = []
        self.transport._get_api = lambda cursor: (requested.append(cursor), _listing([NOTE_TWO], more=False))[1]
        recovered_page = self.transport.page(AUTHOR, saved_cursor)
        self.assertEqual(requested, [saved_cursor])
        self.assertEqual({item.item_id for item in recovered_page.items}, expected)
        self.assertEqual(self.transport.prepare_page_details(AUTHOR, expected), ())
        self.transport._get = lambda host, path, **kwargs: _html(NOTE_TWO)
        detail = self.transport.detail(AUTHOR, NOTE_TWO,
                                       f"https://www.xiaohongshu.com/explore/{NOTE_TWO}")
        self.assertEqual(detail["item_id"], NOTE_TWO)
        self.assertEqual(detail["author_id"], AUTHOR)

    def test_reference_snapshot_restores_without_listing_and_validates_before_update(self):
        self.transport._get_api = lambda cursor: _listing([NOTE_ONE])
        self.transport.page(AUTHOR, None)
        references = self.transport.export_page_references(AUTHOR, [NOTE_ONE, NOTE_TWO])
        self.transport._refs.clear()
        self.transport._page_cache = None
        self.transport._get_api = lambda cursor: self.fail("reference restore must not fetch a listing")
        self.transport.restore_page_references(AUTHOR, references)
        self.assertEqual(self.transport.prepare_page_details(AUTHOR, [NOTE_ONE]), ())
        self.transport._get = lambda *args, **kwargs: _html(NOTE_ONE)
        self.assertEqual(self.transport.detail(AUTHOR, NOTE_ONE)["text"], "A complete body")
        self.transport._refs.clear()
        for invalid in ("https://example.org/invalid", 42):
            with self.assertRaises(AdapterFailure) as caught:
                self.transport.restore_page_references(AUTHOR, {**references, NOTE_TWO: invalid})
            self.assertEqual(caught.exception.category, "reference_missing")
            self.assertEqual(self.transport._refs, {})

    def test_recent_window_needs_no_deep_cursor_and_each_refresh_is_fresh(self):
        calls = []
        response = _listing([NOTE_ONE], more=True, cursor="")
        response["notes"][0].update(display_title="Window title", time=1700000000000)
        def listing(cursor):
            calls.append(cursor)
            return response
        self.transport._get_api = listing
        with patch.object(self.transport, "detail", side_effect=AssertionError("list only")):
            for _ in range(2):
                page = self.transport.recent_page(AUTHOR)
                self.assertEqual((page.next_cursor, page.terminal_evidence), (None, None))
                self.assertEqual(page.items[0].title, "Window title")
                self.assertTrue(page.items[0].published_at)
        self.assertEqual(calls, ["", ""])

    def test_media_access_denial_is_source_stop_not_recoverable_asset_gap(self):
        candidate = MediaCandidate("image", 0, "https://ci.xhscdn.com/synthetic.jpg")
        for status, category in [(401, "needs_login"), (403, "verification_required"),
                                 (406, "verification_required"), (461, "verification_required"),
                                 (471, "verification_required"), (429, "rate_limited")]:
            reason = "media_rate_limited" if status == 429 else "media_url_expired_or_unavailable"
            with self.subTest(status=status), patch.object(self.transport, "_gate"), patch(
                    "creator_archive.adapters.xhs_http.download_media",
                    side_effect=MediaFailure(reason, http_status=status)):
                with self.assertRaises(AdapterFailure) as caught:
                    self.transport.download_media(candidate, Path(self.temporary.name) / "media")
                self.assertEqual(caught.exception.category, category)

    def test_recent_window_accepts_thirty_and_rejects_abnormal_scope(self):
        ids = [f"{index:024x}" for index in range(30)]
        self.transport._get_api = lambda cursor: _listing(ids, more=True, cursor="ignored")
        self.assertEqual(len(self.transport.recent_page(AUTHOR).items), 30)
        for response, category in [(_listing(ids + ["f" * 24]), "invalid_response"),
                                   (_listing([NOTE_ONE, NOTE_ONE]), "invalid_response"),
                                   (_listing([], more=False), "invalid_page"),
                                   (_listing([NOTE_ONE], author=FOREIGN), "identity_mismatch")]:
            with self.subTest(category=category):
                self.transport._get_api = lambda cursor, response=response: response
                with self.assertRaises(AdapterFailure) as caught:
                    self.transport.recent_page(AUTHOR)
                self.assertEqual(caught.exception.category, category)

    def test_embedded_state_is_data_and_undefined_inside_text_is_preserved(self):
        sample = b'<script>window.__INITIAL_STATE__={"text":"undefined in a string","missing":undefined};</script>'
        self.assertEqual(_json_state(sample), {"text": "undefined in a string", "missing": None})
        self.assertEqual(_replace_javascript_literals('{"x":new Map([])}'), '{"x":null}')
        with self.assertRaises(AdapterFailure):
            _json_state(b"<script>alert('no state')</script>")

    def test_http_status_and_signer_fail_closed(self):
        with patch("creator_archive.adapters.xhs_http._signed_headers", side_effect=AdapterFailure("unavailable")):
            with self.assertRaises(AdapterFailure) as caught:
                self.transport._get("edith.xiaohongshu.com", "/api/sns/web/v1/user_posted", api_params={})
        self.assertEqual(caught.exception.category, "unavailable")
        self.transport.close()
        with self.assertRaises(AdapterFailure):
            self.transport._gate()

    def test_detail_http_status_is_item_gap_without_weakening_access_stops(self):
        cases = [
            ("www.xiaohongshu.com", 404, None, "item_unavailable"),
            ("www.xiaohongshu.com", 410, None, "item_unavailable"),
            ("edith.xiaohongshu.com", 404, None, "unavailable"),
            ("www.xiaohongshu.com", 401, None, "needs_login"),
            ("www.xiaohongshu.com", 302, "/login", "needs_login"),
            ("www.xiaohongshu.com", 302, "/website-login/captcha?token=private-test-token", "verification_required"),
            ("www.xiaohongshu.com", 302, "/404?return=/login&token=private-test-token", "unavailable"),
            ("www.xiaohongshu.com", 302, "/explore/" + NOTE_ONE + "?token=private-test-token", "unavailable"),
            ("www.xiaohongshu.com", 302, "https://private-test-token.example/login", "unavailable"),
            ("www.xiaohongshu.com", 302, None, "unavailable"),
            ("www.xiaohongshu.com", 403, None, "verification_required"),
            ("www.xiaohongshu.com", 406, None, "verification_required"),
            ("www.xiaohongshu.com", 429, None, "rate_limited"),
            ("www.xiaohongshu.com", 503, None, "unavailable"),
        ]
        for host, status, location, category in cases:
            with self.subTest(host=host, status=status):
                response = SimpleNamespace(
                    status=status,
                    getheader=lambda name, default=None: location if name == "Location" else default,
                )

                class Connection:
                    sock = None
                    closed = False
                    request_count = 0

                    def request(self, method, path, headers):
                        self.request_count += 1
                        self.requested = (method, path, headers)

                    def getresponse(self):
                        return response

                    def close(self):
                        self.closed = True

                connection = Connection()
                context = SimpleNamespace(wrap_socket=lambda sock, server_hostname: object())
                with patch("creator_archive.adapters.xhs_http._public_addresses", return_value=["203.0.113.1"]), \
                     patch("creator_archive.adapters.xhs_http.http.client.HTTPSConnection", return_value=connection), \
                     patch("creator_archive.adapters.xhs_http.socket.create_connection", return_value=object()), \
                     patch("creator_archive.adapters.xhs_http.ssl.create_default_context", return_value=context):
                    with self.assertRaises(AdapterFailure) as caught:
                        _http_get(host, "/explore/" + NOTE_ONE, {"Cookie": "private-test-cookie"}, max_bytes=1024)
                self.assertEqual(caught.exception.category, category)
                self.assertTrue(connection.closed)
                self.assertEqual(connection.request_count, 1)
                self.assertNotIn("private-test-cookie", str(caught.exception))
                self.assertNotIn("private-test-token", json.dumps(caught.exception.diagnostics))
                if status == 302:
                    diagnostic = caught.exception.diagnostics
                    self.assertEqual(diagnostic["redirect_location_present"], bool(location))
                    self.assertEqual(bool(diagnostic["redirect_location_sha256"]), bool(location))
                    if location and location.startswith("/404"):
                        self.assertEqual(diagnostic["redirect_path_type"], "error")

    def test_detail_preserves_observed_query_and_native_session_headers(self):
        token, source = "synthetic+/token==&suffix", "pc_user"
        response = _listing([NOTE_ONE])
        response["notes"][0].update(xsec_token=token, xsec_source=source)
        self.transport._get_api = lambda cursor: response
        self.transport.recent_page(AUTHOR)
        references = self.transport.export_page_references(AUTHOR, [NOTE_ONE])
        self.transport.restore_page_references(AUTHOR, references)
        sent = []

        def receive(host, path, headers, **kwargs):
            sent.append((host, path, headers))
            return _html(NOTE_ONE)

        with patch("creator_archive.adapters.xhs_http._http_get", side_effect=receive):
            detail = self.transport.detail(AUTHOR, NOTE_ONE)
        self.assertTrue(detail["text"])
        self.assertEqual(len(sent), 1)
        host, path, headers = sent[0]
        self.assertEqual(host, "www.xiaohongshu.com")
        self.assertEqual(path, f"/explore/{NOTE_ONE}?" + urlencode({"xsec_token": token, "xsec_source": source}))
        self.assertEqual(parse_qs(urlsplit(path).query), {"xsec_token": [token], "xsec_source": [source]})
        self.assertEqual(headers["Cookie"], self.cookie_file.read_text().strip())
        self.assertEqual(headers["Referer"], "https://www.xiaohongshu.com/")
        from creator_archive.xhs_account import _UA as account_user_agent
        self.assertEqual(headers["User-Agent"], account_user_agent)
        self.assertEqual(headers["Accept-Encoding"], "identity")

    def test_signed_api_request_uses_fixed_platform_host_and_maps_refusal(self):
        sent = []
        def fake_get(host, path, headers, *, max_bytes):
            sent.append((host, path, headers.copy()))
            return json.dumps({"success": True, "code": 0, "data": _listing([NOTE_ONE], more=False)}).encode()
        with patch("creator_archive.adapters.xhs_http._signed_headers", return_value={"x-s": "synthetic-sign", "x-t": "1"}), \
             patch("creator_archive.adapters.xhs_http._http_get", side_effect=fake_get):
            page = self.transport.page(AUTHOR, None)
        self.assertFalse(page.has_more)
        self.assertEqual(sent[0][0], "edith.xiaohongshu.com")
        self.assertIn("user_id=" + AUTHOR, sent[0][1])
        self.assertEqual(sent[0][2]["x-s"], "synthetic-sign")
        self.transport._page_cache = None
        self.transport._get_api = lambda cursor: (_ for _ in ()).throw(AdapterFailure("verification_required"))
        with self.assertRaises(AdapterFailure) as caught:
            self.transport.page(AUTHOR, "next")
        self.assertEqual(caught.exception.category, "verification_required")

    def test_old_browser_login_helper_is_disabled_without_creating_session(self):
        target = Path(self.temporary.name) / "authorized.txt"
        with patch("creator_archive.network_safety.require_browser_disabled",
                   side_effect=AdapterFailure("browser_automation_disabled")), patch("subprocess.Popen") as launch:
            with self.assertRaises(AdapterFailure):
                authorize_session(target)
            launch.assert_not_called()
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
