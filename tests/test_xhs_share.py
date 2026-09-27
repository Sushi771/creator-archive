"""Offline redirect boundaries and exact-note registration, not live evidence."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import httpx

from creator_archive.adapters.xhs_share import expand_share_link
from creator_archive.app import create_app
from creator_archive.validation import AdapterFailure
from fastapi.testclient import TestClient


ITEM, AUTHOR = "b" * 24, "a" * 24
SHORT = "https://xhslink.cn/o/synthetic"
TARGET = f"https://www.xiaohongshu.com/discovery/item/{ITEM}?xsec_token=SYNTHETIC_SECRET"


class RedirectTests(unittest.TestCase):
    def expand(self, handler, url=SHORT):
        client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
        try:
            with patch("creator_archive.adapters.xhs_share.httpx.Client", return_value=client):
                return expand_share_link(url)
        finally:
            client.close()

    def test_relative_multi_hop_resolves_without_requesting_detail_or_credentials(self):
        calls = []
        def handler(request):
            calls.append(str(request.url))
            self.assertNotIn("cookie", request.headers)
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(302, headers={"location": "/o/next" if len(calls) == 1 else TARGET})
        self.assertEqual(self.expand(handler), TARGET)
        self.assertEqual(calls, [SHORT, "https://xhslink.cn/o/next"])
        self.assertEqual(self.expand(lambda _: httpx.Response(302, headers={"location": TARGET}), "https://xhslink.com/a/synthetic"), TARGET)

    def test_untrusted_targets_never_requested(self):
        invalid = ["https://evil.example/x", "https://127.0.0.1/x", "http://xhslink.cn/o/x",
                   "https://xhslink.cn.evil.example/x", "https://user@xhslink.cn/o/x",
                   "https://xhslink.cn:8443/o/x", "https://xhslink.cn/\\evil.example/x",
                   f"https://www.xiaohongshu.com/user/profile/{AUTHOR}", TARGET.replace("?", "/?"),
                   TARGET.split("?")[0], TARGET + "&xsec_token=SECOND"]
        for target in invalid:
            with self.subTest(target=target):
                calls = []
                def handler(request):
                    calls.append(str(request.url))
                    return httpx.Response(302, headers={"location": target})
                with self.assertRaises(ValueError):
                    self.expand(handler)
                self.assertEqual(calls, [SHORT])

    def test_invalid_inputs_rejected_before_network(self):
        for url in ("http://xhslink.cn/o/x", "https://evil.example/x", "https://xhslink.cn:bad/x", SHORT + "\n", SHORT + "\\x"):
            handler = MagicMock()
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.expand(handler, url)
            handler.assert_not_called()

    def test_loop_and_hop_budget_are_failures(self):
        calls = []
        def loop(request):
            calls.append(str(request.url))
            return httpx.Response(302, headers={"location": SHORT})
        with self.assertRaisesRegex(ValueError, "循环"):
            self.expand(loop)
        self.assertEqual(len(calls), 1)
        calls.clear()
        def chain(request):
            calls.append(str(request.url))
            return httpx.Response(302, headers={"location": "/o/hop" + str(len(calls))})
        with self.assertRaisesRegex(ValueError, "上限"):
            self.expand(chain)
        self.assertEqual(len(calls), 4)

    def test_statuses_and_network_errors_are_actionable_and_redacted(self):
        for status, category in ((429, "rate_limited"), (401, "needs_login"), (403, "verification_required"), (461, "verification_required")):
            with self.subTest(status=status), self.assertRaises(AdapterFailure) as caught:
                self.expand(lambda _: httpx.Response(status))
            self.assertEqual(caught.exception.category, category)
            self.assertIn("旧资料保留", caught.exception.reason)
        for error, category in ((httpx.ReadTimeout(TARGET), "timeout"), (httpx.ConnectError(TARGET), "unavailable")):
            with self.subTest(category=category), self.assertRaises(AdapterFailure) as caught:
                self.expand(MagicMock(side_effect=error))
            self.assertEqual(caught.exception.category, category)
            self.assertNotIn("SYNTHETIC_SECRET", caught.exception.reason)
        for response in (httpx.Response(200, text="<script>redirect</script>"), httpx.Response(302), httpx.Response(404)):
            with self.assertRaisesRegex(ValueError, "没有返回"):
                self.expand(lambda _: response)


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(Path(self.temp.name))
        self.client = TestClient(self.app)
        self.service = self.app.state.service
        self.source = MagicMock()
        self.source.resolve_item.return_value = dict(author_id=AUTHOR, item_id=ITEM, title="Observed")
        self.service.adapter_factory = lambda _: self.source
        self.addCleanup(self.service.close)
        self.headers = {"X-Creator-Archive": "local-validation"}

    def resolve(self):
        return self.client.post("/api/items/resolve", json={"text": "分享 " + SHORT}, headers=self.headers)

    def test_short_registration_repeats_without_overwrite_or_persisting_parameters(self):
        with patch("creator_archive.service.expand_share_link", return_value=TARGET):
            response = self.resolve()
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["resolved_from_short_link"])
            self.assertNotIn("SYNTHETIC_SECRET", response.text)
            self.source.resolve_item.assert_called_once_with(TARGET)
            with self.service.workflow.connect() as db:
                db.execute("UPDATE subscriptions SET display_name='My note'")
                db.execute("UPDATE items SET title='My title'")
            self.assertEqual(self.resolve().status_code, 200)
        with self.service.workflow.connect() as db:
            self.assertEqual(tuple(db.execute("SELECT display_name,enabled FROM subscriptions").fetchone()), ("My note", 0))
            self.assertEqual(db.execute("SELECT title FROM items").fetchone()[0], "My title")
            for table in ("jobs", "pages"):
                self.assertEqual(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
            dump = "".join(db.iterdump())
            self.assertNotIn("SYNTHETIC_SECRET", dump)
            self.assertNotIn("xhslink", dump)

    def test_failed_expansion_or_wrong_note_or_login_creates_nothing(self):
        with patch("creator_archive.service.expand_share_link", side_effect=ValueError("跳转失败")):
            self.assertEqual(self.resolve().status_code, 422)
        self.source.resolve_item.assert_not_called()
        with patch("creator_archive.service.expand_share_link", return_value=TARGET):
            self.source.resolve_item.return_value["item_id"] = "c" * 24
            self.assertEqual(self.resolve().status_code, 422)
            self.source.resolve_item.side_effect = AdapterFailure("needs_login")
            self.assertEqual(self.resolve().status_code, 409)
        with self.service.workflow.connect() as db:
            for table in ("subscriptions", "items", "jobs", "pages"):
                self.assertEqual(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)

    def test_short_rate_limit_persists_cooldown_before_any_repeat_request(self):
        with patch("creator_archive.service.expand_share_link", side_effect=AdapterFailure("rate_limited", 60)) as expand:
            self.assertEqual(self.resolve().status_code, 409)
            self.assertEqual(self.resolve().status_code, 429)
            expand.assert_called_once()
        self.source.resolve_item.assert_not_called()


if __name__ == "__main__":
    unittest.main()
