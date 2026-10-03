"""Synthetic localhost HTTP feed tests. No platform account or public request."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest

from creator_archive.adapters.feed_http import FeedHttpTransport, validate_source_url
from creator_archive.validation import AdapterFailure


AUTHOR = "a" * 24
FIRST = "b" * 24
SECOND = "c" * 24
PROFILE = f"https://www.xiaohongshu.com/user/profile/{AUTHOR}"


class _Handler(BaseHTTPRequestHandler):
    routes = {}
    counts = {}

    def do_GET(self):
        self.counts[self.path] = self.counts.get(self.path, 0) + 1
        status, mime, body = self.routes.get(self.path, (404, "text/plain", b"missing"))
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FeedHttpTests(unittest.TestCase):
    def setUp(self):
        _Handler.routes = {}
        _Handler.counts = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def route_json(self, path, payload):
        _Handler.routes[path] = (200, "application/feed+json", json.dumps(payload).encode())

    def feed(self, **extra):
        return {"version": "https://jsonfeed.org/version/1.1", "home_page_url": PROFILE,
                "items": [{"url": f"https://www.xiaohongshu.com/explore/{FIRST}",
                           "author": {"url": PROFILE},
                           "title": "First", "content_html": "<p>first body</p><img src='" + self.base + "/image.png'><img src='" + self.base + "/image.png'><video src='" + self.base + "/movie.mp4'></video>"}], **extra}

    def test_json_history_page_detail_cache_and_media(self):
        self.route_json("/feed", self.feed(next_url="/feed?page=2"))
        self.route_json("/feed?page=2", {"home_page_url": PROFILE, "items": [{"url": f"https://www.xiaohongshu.com/user/profile/{AUTHOR}/{SECOND}", "content_text": "second body"}], "history_complete": True})
        _Handler.routes["/image.png"] = (200, "image/png", b"\x89PNG\r\n\x1a\nimage")
        _Handler.routes["/movie.mp4"] = (200, "video/mp4", b"\x00\x00\x00\x10ftypisomvideo")
        adapter = FeedHttpTransport({"url": self.base + "/feed"}, "xiaohongshu", AUTHOR)
        self.assertEqual(adapter.verify_author(AUTHOR)["author_id"], AUTHOR)
        self.assertEqual(len(adapter.poll_latest()), 1)
        self.assertEqual(_Handler.counts["/feed"], 1)  # verify and poll share one bounded document.
        first = adapter.page(AUTHOR, None)
        self.assertTrue(first.has_more)
        self.assertEqual(first.items[0].item_id, FIRST)
        detail = adapter.detail(AUTHOR, FIRST)
        self.assertEqual(detail["text"], "first body")
        self.assertEqual([(m.kind, m.position) for m in detail["media"]], [("image", 0), ("video", 0)])
        with tempfile.TemporaryDirectory() as temporary:
            stored = adapter.download_media(detail["media"][0], Path(temporary))
            self.assertEqual(stored["mime"], "image/png")
            self.assertEqual(Path(stored["path"]).read_bytes(), b"\x89PNG\r\n\x1a\nimage")
            self.assertTrue(adapter.download_media(detail["media"][0], Path(temporary))["reused"])
        second = adapter.page(AUTHOR, first.next_cursor)
        self.assertFalse(second.has_more)
        self.assertIn("history_complete", second.terminal_evidence)
        self.assertEqual(adapter.detail(AUTHOR, SECOND)["text"], "second body")
        self.assertEqual(_Handler.counts["/feed"], 1)

    def test_rss_and_atom_only_poll_latest(self):
        _Handler.routes["/rss"] = (200, "application/rss+xml", f"<rss><channel><title>x</title><link>{PROFILE}</link><item><title>One</title><link>https://www.xiaohongshu.com/user/profile/{AUTHOR}/{FIRST}</link><pubDate>Tue, 29 Sep 2026 12:00:00 +0800</pubDate><description>&lt;p&gt;RSS body&lt;/p&gt;</description></item></channel></rss>".encode())
        _Handler.routes["/atom"] = (200, "application/atom+xml", f"<feed xmlns='http://www.w3.org/2005/Atom'><link rel='alternate' href='{PROFILE}'/><entry><title>One</title><link href='https://www.xiaohongshu.com/user/profile/{AUTHOR}/{FIRST}'/><content>Atom body</content></entry></feed>".encode())
        for fmt in ("rss", "atom"):
            adapter = FeedHttpTransport({"url": self.base + "/" + fmt}, "xiaohongshu", AUTHOR)
            self.assertEqual(adapter.poll_latest()[0]["item_id"], FIRST)
            if fmt == "rss":
                self.assertEqual(adapter.poll_latest()[0]["published_at"], "2026-09-29T04:00:00+00:00")
            with self.assertRaises(AdapterFailure) as error:
                adapter.page(AUTHOR, None)
            self.assertEqual(error.exception.category, "missing_terminal_evidence")

    def test_missing_history_marker_and_identity_mismatch_are_not_terminal(self):
        self.route_json("/feed", self.feed())
        adapter = FeedHttpTransport({"url": self.base + "/feed"}, "xiaohongshu", AUTHOR)
        with self.assertRaises(AdapterFailure) as error:
            adapter.page(AUTHOR, None)
        self.assertEqual(error.exception.category, "missing_terminal_evidence")
        self.route_json("/wrong", {**self.feed(history_complete=True), "home_page_url": f"https://www.xiaohongshu.com/user/profile/{SECOND}"})
        with self.assertRaises(AdapterFailure) as error:
            FeedHttpTransport({"url": self.base + "/wrong"}, "xiaohongshu", AUTHOR).poll_latest()
        self.assertEqual(error.exception.category, "identity_mismatch")
        self.route_json("/unattributed", {"home_page_url": PROFILE, "items": [{
            "url": f"https://www.xiaohongshu.com/explore/{FIRST}", "content_text": "unknown owner"}]})
        with self.assertRaises(AdapterFailure) as error:
            FeedHttpTransport({"url": self.base + "/unattributed"}, "xiaohongshu", AUTHOR).poll_latest()
        self.assertEqual(error.exception.category, "identity_mismatch")
        self.route_json("/wrong-item", {"home_page_url": PROFILE, "items": [{"url": f"https://www.xiaohongshu.com/user/profile/{SECOND}/{FIRST}", "content_text": "wrong"}]})
        with self.assertRaises(AdapterFailure) as error:
            FeedHttpTransport({"url": self.base + "/wrong-item"}, "xiaohongshu", AUTHOR).poll_latest()
        self.assertEqual(error.exception.category, "identity_mismatch")

    def test_next_cursor_scope_redirect_and_private_address(self):
        self.route_json("/feed", self.feed(next_url="/other?page=2"))
        with self.assertRaises(AdapterFailure) as error:
            FeedHttpTransport({"url": self.base + "/feed"}, "xiaohongshu", AUTHOR).page(AUTHOR, None)
        self.assertEqual(error.exception.category, "invalid_cursor")
        _Handler.routes["/redirect"] = (302, "text/plain", b"")
        with self.assertRaises(AdapterFailure) as error:
            FeedHttpTransport({"url": self.base + "/redirect"}, "xiaohongshu", AUTHOR).poll_latest()
        self.assertEqual(error.exception.category, "unavailable")
        for unsafe in ("http://192.168.1.1/feed", "https://10.0.0.1/feed", "https://169.254.1.2/feed", "http://example.com/feed", "http://user:pass@127.0.0.1/feed"):
            with self.assertRaises(ValueError):
                validate_source_url(unsafe)

    def test_wechat_article_uses_stable_identity_and_strips_tracking(self):
        biz = "MzA1NjQ5MDAxMg=="
        self.route_json("/wx", {"home_page_url": f"https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz={biz}",
                                "items": [{"url": f"https://mp.weixin.qq.com/s?__biz={biz}&mid=123&idx=1&sn={'a' * 32}&chksm=private",
                                           "content_text": "公众号正文"}], "history_complete": True})
        adapter = FeedHttpTransport({"url": self.base + "/wx"}, "wechat", biz)
        page = adapter.page(biz, None)
        self.assertFalse(page.has_more)
        self.assertTrue(page.items[0].item_id.startswith("wx-"))
        detail = adapter.detail(biz, page.items[0].item_id)
        self.assertEqual(detail["text"], "公众号正文")
        self.assertNotIn("chksm", detail["source_url"])

    def test_config_secret_is_not_persisted_in_cursor(self):
        self.route_json("/feed?api_key=private", self.feed(next_url="/feed?page=2&api_key=private"))
        self.route_json("/feed?page=2&api_key=private", {"home_page_url": PROFILE, "items": [], "history_complete": True})
        adapter = FeedHttpTransport({"url": self.base + "/feed?api_key=private"}, "xiaohongshu", AUTHOR)
        first = adapter.page(AUTHOR, None)
        self.assertNotIn("private", first.next_cursor)
        restarted = FeedHttpTransport({"url": self.base + "/feed?api_key=private"}, "xiaohongshu", AUTHOR)
        terminal = restarted.page(AUTHOR, first.next_cursor)
        self.assertFalse(terminal.has_more)
        self.assertEqual(_Handler.counts["/feed?page=2&api_key=private"], 1)

    def test_media_cannot_probe_other_local_origin(self):
        feed = self.feed(history_complete=True)
        feed["items"][0]["content_html"] = "<p>body</p><img src='http://127.0.0.1:9/private'>"
        self.route_json("/feed", feed)
        adapter = FeedHttpTransport({"url": self.base + "/feed"}, "xiaohongshu", AUTHOR)
        detail = adapter.poll_latest()[0]
        self.assertEqual(detail["media"], [])
        self.assertEqual(detail["missing"], ["image_0_unsafe_source"])


if __name__ == "__main__":
    unittest.main()
