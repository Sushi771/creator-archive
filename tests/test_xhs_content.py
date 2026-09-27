"""Synthetic detail/download tests; not evidence of live platform access."""
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from creator_archive.adapters.xhs import MediaCandidate
from creator_archive.adapters.xhs_content import detail_url, parse_count, project_detail
from creator_archive.adapters.xhs_media import MediaFailure, download_media, safe_media_url
from creator_archive.adapters.xhs_transport import XhsBrowserTransport, TransportFailure
from creator_archive.validation import AdapterFailure


AUTHOR, ITEM = "a" * 24, "b" * 24
URL = "https://sns-webpic-qc.xhscdn.com/synthetic.webp?token=not-real"
WEBP = b"RIFF" + b"\x20\x00\x00\x00" + b"WEBP" + b"synthetic-content"


def detail():
    return {"note": {"noteId": ITEM, "user": {"userId": AUTHOR}, "type": "normal",
                     "title": "Title", "desc": "Body\nSecond line", "time": 1720000000000,
                     "interactInfo": {"likedCount": "0", "collectedCount": "1.2万", "commentCount": None},
                     "imageList": [{"urlDefault": URL}]}}


class ContentTests(unittest.TestCase):
    def test_unknown_zero_abbreviation_lower_bound_and_invalid_counts(self):
        expected = [(None, None, "unknown"), (True, None, "unknown"), (-1, None, "unknown"),
                    (0, 0, "exact"), ("0", 0, "exact"), ("1,234", 1234, "exact"),
                    ("1.2万", 12000, "approximate"), ("999+", 999, "lower_bound"),
                    ("1.2w+", 12000, "lower_bound"), ("赞", None, "unknown"),
                    ("1.2", None, "unknown"), ("-1", None, "unknown")]
        for raw, value, quality in expected:
            with self.subTest(raw=raw):
                result = parse_count(raw)
                self.assertEqual((result["value"], result["quality"]), (value, quality))

    def test_projection_preserves_identity_source_and_unknowns_without_tokens(self):
        data = detail()
        data["note"]["xsecToken"] = "secret"
        result = project_detail(data, author_id=AUTHOR, item_id=ITEM, observed_at=123)
        self.assertEqual(result["observed_at"], 123)
        self.assertEqual(result["content_type"], "image")
        self.assertEqual(result["metrics"]["likes"]["value"], 0)
        self.assertIsNone(result["metrics"]["comments"]["value"])
        self.assertEqual(result["source_url"], f"https://www.xiaohongshu.com/explore/{ITEM}")
        self.assertEqual(result["media"][0].asset_id, "image-000")
        self.assertNotIn("secret", str(result))
        with self.assertRaises(AdapterFailure):
            project_detail(data, author_id="c" * 24, item_id=ITEM)

    def test_missing_body_and_video_stay_explicit(self):
        data = detail()
        del data["note"]["desc"]
        data["note"]["type"] = "video"
        result = project_detail(data, author_id=AUTHOR, item_id=ITEM)
        self.assertIn("body_missing", result["missing"])
        self.assertIn("video_stream_missing", result["missing"])

    def test_video_selects_observed_avc_source_and_upgrades_only_known_cdn_http(self):
        data = detail()
        data["note"].update(type="video", video={"media": {"stream": {
            "h264": [{"masterUrl": URL.replace("https:", "http:"), "width": 720, "qualityType": "h264:HD"}],
            "h265": [{"masterUrl": URL, "width": 2160, "qualityType": "h265:4K"}]}}})
        result = project_detail(data, author_id=AUTHOR, item_id=ITEM)
        video = result["media"][-1]
        self.assertEqual((video.kind, video.width), ("video", 720))
        self.assertTrue(video.url.startswith("https:"))

    def test_detail_navigation_rejects_wrong_identity_and_foreign_host(self):
        good = f"https://www.xiaohongshu.com/explore/{ITEM}?xsec_token=synthetic"
        self.assertEqual(detail_url(ITEM, good), good)
        for url in [good.replace("www.xiaohongshu.com", "evil.example"), good.replace(ITEM, AUTHOR),
                    good.replace("https://", "https://user@"), good.replace("https:", "http:")]:
            with self.assertRaises(ValueError):
                detail_url(ITEM, url)


class FakeResponse(io.BytesIO):
    status = 200

    def __init__(self, body=WEBP, length=None):
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body) if length is None else length)}

    def geturl(self):
        return URL


class DownloadTests(unittest.TestCase):
    def test_reuses_hash_verified_media_without_network_or_token_metadata(self):
        with TemporaryDirectory() as tmp, patch("creator_archive.adapters.xhs_media.socket.getaddrinfo", return_value=[(0, 0, 0, "", ("1.1.1.1", 443))]), patch("creator_archive.adapters.xhs_media.build_opener") as opener:
            opener.return_value.open.return_value = FakeResponse()
            candidate = MediaCandidate("image", 0, URL)
            result = download_media(candidate, Path(tmp))
            self.assertFalse(result["reused"])
            second = download_media(candidate, Path(tmp))
            self.assertTrue(second["reused"])
            self.assertEqual(opener.return_value.open.call_count, 1)
            self.assertEqual(Path(result["path"]).read_bytes(), WEBP)
            self.assertNotIn("token", next(Path(tmp).glob("*.json")).read_text())
            self.assertEqual(list(Path(tmp).glob("*.part")), [])

    def test_partial_transfer_and_html_fail_without_publishing_media(self):
        for body, length in [(WEBP, len(WEBP) + 1), (b"<html>login</html>", None)]:
            with self.subTest(length=length), TemporaryDirectory() as tmp, patch("creator_archive.adapters.xhs_media.socket.getaddrinfo", return_value=[(0, 0, 0, "", ("1.1.1.1", 443))]), patch("creator_archive.adapters.xhs_media.build_opener") as opener:
                opener.return_value.open.return_value = FakeResponse(body, length)
                with self.assertRaises(MediaFailure):
                    download_media(MediaCandidate("image", 0, URL), Path(tmp))
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_cdn_allowlist_private_dns_and_host_suffix(self):
        for url in ["https://127.0.0.1/a", "https://xhscdn.com.evil.example/a", "https://evil.example/a",
                    "https://user@sns-webpic-qc.xhscdn.com/a", "https://sns-webpic-qc.xhscdn.com:444/a"]:
            with self.assertRaises(MediaFailure):
                safe_media_url(url, resolve=False)
        with patch("creator_archive.adapters.xhs_media.socket.getaddrinfo", return_value=[(0, 0, 0, "", ("127.0.0.1", 443))]):
            with self.assertRaises(MediaFailure):
                safe_media_url(URL)

    def test_changed_existing_media_is_preserved_and_conflict_reported(self):
        with TemporaryDirectory() as tmp, patch("creator_archive.adapters.xhs_media.socket.getaddrinfo", return_value=[(0, 0, 0, "", ("1.1.1.1", 443))]), patch("creator_archive.adapters.xhs_media.build_opener") as opener:
            opener.return_value.open.return_value = FakeResponse()
            candidate = MediaCandidate("image", 0, URL)
            first = download_media(candidate, Path(tmp))
            Path(first["path"]).write_bytes(b"user modified")
            opener.return_value.open.return_value = FakeResponse()
            with self.assertRaisesRegex(MediaFailure, "existing_file_conflict"):
                download_media(candidate, Path(tmp))
            self.assertEqual(Path(first["path"]).read_bytes(), b"user modified")


class DetailTransportTests(unittest.TestCase):
    def test_partial_ssr_counts_rechecked_and_explicit_link_reused_for_refresh(self):
        from unittest.mock import MagicMock
        with TemporaryDirectory() as tmp:
            adapter = XhsBrowserTransport(Path(tmp) / "profile")
            try:
                adapter._ensure = lambda: None
                adapter._page = MagicMock()
                adapter._page.goto.return_value.status = 200
                adapter._page.locator.return_value.inner_text.return_value = ""
                partial = detail()
                partial["note"]["interactInfo"] = {}
                adapter._page.evaluate.side_effect = [partial, detail(), detail()]
                supplied = detail_url(ITEM) + "?xsec_token=synthetic"
                result = adapter.detail(AUTHOR, ITEM, supplied)
                self.assertEqual(result["metrics"]["likes"]["value"], 0)
                self.assertEqual(adapter._page.evaluate.call_count, 2)
                adapter._page.wait_for_timeout.assert_called_once_with(300)
                self.assertEqual(adapter._detail_links[(AUTHOR, ITEM)], supplied)
                adapter.detail(AUTHOR, ITEM, detail_url(ITEM))
                self.assertEqual(adapter._page.goto.call_args.args[0], supplied)
                self.assertEqual(adapter._page.goto.call_count, 2)
                adapter._page.locator.return_value.evaluate_all.assert_not_called()
            finally:
                adapter.close()

    def test_persistently_missing_counts_return_unknown_after_bounded_wait(self):
        from unittest.mock import MagicMock
        with TemporaryDirectory() as tmp:
            adapter = XhsBrowserTransport(Path(tmp) / "profile", timeout=.01)
            try:
                adapter._ensure = lambda: None
                adapter._page = MagicMock()
                adapter._page.goto.return_value.status = 200
                adapter._page.locator.return_value.inner_text.return_value = ""
                partial = detail()
                partial["note"]["interactInfo"] = {}
                adapter._page.evaluate.return_value = partial
                result = adapter.detail(AUTHOR, ITEM, detail_url(ITEM) + "?xsec_token=synthetic")
                self.assertTrue(all(m["value"] is None for m in result["metrics"].values()))
                self.assertEqual(adapter._page.goto.call_count, 1)
            finally:
                adapter.close()

    def test_canonical_refresh_uses_observed_profile_link_not_tokenless_explore(self):
        # Live reproduction: explore anchors lacked tokens; actual navigable
        # cards used /user/profile/{author}/{item}?xsec_token=... .
        from unittest.mock import MagicMock
        with TemporaryDirectory() as tmp:
            adapter = XhsBrowserTransport(Path(tmp) / "profile")
            try:
                adapter._ensure = lambda: None
                adapter._check_wall = lambda: None
                adapter._page = MagicMock()
                adapter._page.goto.return_value.status = 200
                adapter._page.evaluate.return_value = detail()
                profile_url = f"https://www.xiaohongshu.com/user/profile/{AUTHOR}/{ITEM}?xsec_token=synthetic"
                adapter._page.locator.return_value.evaluate_all.return_value = [detail_url(ITEM), profile_url]
                result = adapter.detail(AUTHOR, ITEM, detail_url(ITEM))
                self.assertEqual(adapter._page.goto.call_args.args[0], profile_url)
                self.assertEqual(result["source_url"], detail_url(ITEM))
                self.assertEqual(adapter._page.goto.call_count, 2)
                adapter._page.mouse.wheel.assert_not_called()
            finally:
                adapter.close()

    def test_profile_link_with_wrong_author_rejected_before_browser(self):
        with TemporaryDirectory() as tmp:
            adapter = XhsBrowserTransport(Path(tmp) / "profile")
            try:
                with self.assertRaises(TransportFailure):
                    adapter.detail(AUTHOR, ITEM, f"https://www.xiaohongshu.com/user/profile/{'c' * 24}/{ITEM}?xsec_token=synthetic")
            finally:
                adapter.close()

    def test_supplied_link_fresh_navigation_clears_pagination_location(self):
        from unittest.mock import MagicMock
        with TemporaryDirectory() as tmp:
            adapter = XhsBrowserTransport(Path(tmp) / "profile")
            try:
                adapter._ensure = lambda: None
                adapter._page = MagicMock()
                adapter._page.goto.return_value.status = 200
                adapter._page.evaluate.return_value = detail()
                adapter._author = AUTHOR
                result = adapter.detail(AUTHOR, ITEM, detail_url(ITEM) + "?xsec_token=synthetic")
                self.assertIsNone(adapter._author)
                self.assertEqual(result["text"], "Body\nSecond line")
                adapter._page.mouse.wheel.assert_not_called()
            finally:
                adapter.close()

    def test_detail_rate_limit_is_not_a_success_and_prevents_next_navigation(self):
        from unittest.mock import MagicMock
        with TemporaryDirectory() as tmp:
            adapter = XhsBrowserTransport(Path(tmp) / "profile")
            try:
                adapter._ensure = lambda: None
                adapter._page = MagicMock()
                adapter._page.goto.return_value.status = 429
                for _ in range(2):
                    with self.assertRaises(TransportFailure) as error:
                        adapter.detail(AUTHOR, ITEM, detail_url(ITEM) + "?xsec_token=synthetic")
                    self.assertEqual(error.exception.category, "rate_limited")
                self.assertEqual(adapter._page.goto.call_count, 1)
            finally:
                adapter.close()


if __name__ == "__main__":
    unittest.main()
