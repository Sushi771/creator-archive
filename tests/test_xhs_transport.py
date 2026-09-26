"""Synthetic transport regressions. These are not live platform evidence."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from creator_archive.adapters.xhs_transport import (
    XhsBrowserTransport, TransportFailure, normalized_response, response_key,
)

AUTHOR = "a" * 24
CURSOR = "b" * 24
NOTE = "c" * 24


def payload(more=False, cursor=""):
    return {"success": True, "code": 0, "data": {
        "notes": [{"note_id": NOTE, "user": {"user_id": AUTHOR}}],
        "has_more": more, "cursor": cursor}}


class Response:
    def __init__(self, cursor, data=None, status=200, author=AUTHOR):
        self.url = f"https://edith.xiaohongshu.com/api/sns/web/v1/user_posted?user_id={author}&cursor={cursor}"
        self.status, self.data = status, data or payload()

    def json(self):
        return self.data


class FakePage:
    def __init__(self, transport, initial=None, responses=(), text=""):
        self.transport, self.initial, self.responses = transport, initial or {}, list(responses)
        self.text, self.scrolls = text, 0
        self.mouse = self

    def goto(self, *args, **kwargs):
        pass

    def locator(self, selector):
        return self

    def inner_text(self, **kwargs):
        return self.text

    def evaluate(self, code):
        return self.initial

    def wheel(self, *args):
        self.scrolls += 1
        if self.responses:
            self.transport._observe(self.responses.pop(0))

    def wait_for_timeout(self, timeout):
        pass


class TransportTests(unittest.TestCase):
    def make(self, **kwargs):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        transport = XhsBrowserTransport(Path(temp.name) / "profile", timeout=.03)
        transport._ensure = lambda: None
        transport._page = FakePage(transport, **kwargs)
        self.addCleanup(transport.close)
        return transport

    def test_request_matching_does_not_accept_other_hosts_or_duplicate_identity(self):
        good = Response(CURSOR).url
        self.assertEqual(response_key(good), (AUTHOR, CURSOR))
        self.assertIsNone(response_key(good.replace("edith.xiaohongshu.com", "evil.example")))
        self.assertIsNone(response_key(good + "&user_id=" + AUTHOR))

    def test_normalization_drops_tokens_and_rejects_missing_author(self):
        data = payload()
        data["token"] = "synthetic-sensitive-marker"
        data["data"]["notes"][0]["image_url"] = "https://example.invalid/private"
        clean = normalized_response(200, data)
        self.assertNotIn("token", str(clean))
        self.assertNotIn("image_url", str(clean))
        data["data"]["notes"][0]["user"] = {}
        with self.assertRaises(TransportFailure):
            normalized_response(200, data)

    def test_restart_replays_until_exact_checkpoint_without_delivering_prior_page(self):
        transport = self.make(responses=[Response("", payload(True, CURSOR)), Response(CURSOR)])
        result = transport.page(AUTHOR, CURSOR)
        self.assertFalse(result.has_more)
        self.assertEqual(result.items[0].item_id, NOTE)
        self.assertEqual(transport._page.scrolls, 2)

    def test_foreign_author_response_ignored(self):
        transport = self.make(responses=[Response(CURSOR, author="d" * 24), Response(CURSOR)])
        self.assertFalse(transport.page(AUTHOR, CURSOR).has_more)
        self.assertEqual(transport._page.scrolls, 2)

    def test_new_initial_scan_does_not_reuse_cached_prior_scan(self):
        transport = self.make(text="扫码登录")
        transport._author = AUTHOR
        transport._responses[(AUTHOR, "")] = normalized_response(200, payload())
        with self.assertRaises(TransportFailure) as error:
            transport.page(AUTHOR, None)
        self.assertEqual(error.exception.category, "needs_login")
        self.assertEqual(transport._responses, {})

    def test_http_failure_and_boolean_code_are_not_success(self):
        for status, body in [(403, payload()), (500, payload()), (200, dict(payload(), code=False))]:
            with self.subTest(status=status, code=body["code"]):
                with self.assertRaises(TransportFailure):
                    normalized_response(status, body)

    def test_ssr_false_is_not_terminal_api_evidence(self):
        transport = self.make(initial={"author": AUTHOR, "has_more": False})
        with self.assertRaises(TransportFailure) as error:
            transport.page(AUTHOR, None)
        self.assertEqual(error.exception.category, "invalid_cursor")

    def test_ssr_true_seeds_initial_page(self):
        state = dict(payload(True, CURSOR)["data"], author=AUTHOR)
        transport = self.make(initial=state)
        result = transport.page(AUTHOR, None)
        self.assertTrue(result.has_more)
        self.assertEqual(result.next_cursor, CURSOR)

    def test_login_wall_stops_before_scrolling(self):
        transport = self.make(text="请扫码登录")
        with self.assertRaises(TransportFailure) as error:
            transport.page(AUTHOR, None)
        self.assertEqual(error.exception.category, "needs_login")
        self.assertEqual(transport._page.scrolls, 0)

    def test_logged_out_ssr_with_redacted_ids_reports_login_before_malformed_data(self):
        # Reproduced with the live dedicated empty profile: loggedIn=false,
        # 32 visible preview cards, but every note ID is redacted to "".
        transport = self.make(initial={"logged_in": False, "author": AUTHOR,
                                      "has_more": True, "notes": [{"note_id": ""}]})
        with self.assertRaises(TransportFailure) as error:
            transport.page(AUTHOR, None)
        self.assertEqual(error.exception.category, "needs_login")
        self.assertEqual(transport._page.scrolls, 0)

    def test_rate_limit_stops_and_does_not_accept_later_success(self):
        transport = self.make(responses=[Response(CURSOR, status=429), Response(CURSOR)])
        with self.assertRaises(TransportFailure) as error:
            transport.page(AUTHOR, CURSOR)
        self.assertEqual(error.exception.category, "rate_limited")
        self.assertEqual(transport._page.scrolls, 1)
        self.assertGreater(error.exception.retry_after, 0)

    def test_missing_response_is_timeout_not_completion(self):
        transport = self.make()
        with self.assertRaises(TransportFailure) as error:
            transport.page(AUTHOR, CURSOR)
        self.assertEqual(error.exception.category, "timeout")

    def test_browser_profile_cannot_be_written_in_repository(self):
        with self.assertRaises(ValueError):
            XhsBrowserTransport(Path(__file__).resolve().parents[1] / "profile")


if __name__ == "__main__":
    unittest.main()
