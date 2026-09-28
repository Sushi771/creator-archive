"""Synthetic transport regressions. These are not live platform evidence."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch
from creator_archive.validation import AdapterFailure

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
        self.navigations = []
        self.links = []
        self.mouse = self

    def goto(self, *args, **kwargs):
        self.navigations.append(args[0])

    def locator(self, selector):
        return self

    def inner_text(self, **kwargs):
        return self.text

    def evaluate_all(self, code):
        return self.links

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

    def test_valid_observed_navigation_reference_only_in_memory_not_page_evidence(self):
        data = payload()
        data["data"]["notes"][0].update(xsec_token="synthetic-navigation-reference", xsec_source="pc_user")
        transport = self.make(responses=[Response(CURSOR, data)])
        result = transport.page(AUTHOR, CURSOR)
        self.assertEqual(result.items[0].item_id, NOTE)
        self.assertIn("synthetic-navigation-reference", transport._detail_links[(AUTHOR, NOTE)])
        self.assertNotIn("synthetic-navigation-reference", str(transport._responses))
        self.assertNotIn("synthetic-navigation-reference", str(result))
        transport.close()
        self.assertEqual(transport._detail_links, {})

    def test_unvalidated_foreign_page_does_not_supply_detail_reference(self):
        data = payload()
        data["data"]["notes"][0].update(xsec_token="synthetic-navigation-reference", user={"user_id": "d" * 24})
        transport = self.make(responses=[Response(CURSOR, data)])
        with self.assertRaises(AdapterFailure):
            transport.page(AUTHOR, CURSOR)
        self.assertEqual(transport._detail_links, {})

    def test_replay_deadline_tracks_validated_page_progress(self):
        # A deep checkpoint can take longer than one idle timeout to rejoin.
        # Each distinct validated response is progress, not terminal evidence.
        first, second = "1" * 24, "2" * 24
        responses = [
            Response("", payload(True, first)),
            Response(first, payload(True, second)),
            Response(second, payload(True, CURSOR)),
            Response(CURSOR, payload(False, "")),
        ]
        transport = self.make(responses=responses)
        transport.timeout = 10
        clock = [0.0]
        transport._page.wait_for_timeout = lambda _ms: clock.__setitem__(0, clock[0] + 4.0)
        with patch("creator_archive.adapters.xhs_transport.time.monotonic", side_effect=lambda: clock[0]):
            result = transport._fetch(AUTHOR, CURSOR)
        self.assertEqual(transport._page.scrolls, 4)
        self.assertEqual(result["data"]["notes"][0]["note_id"], NOTE)
        self.assertGreater(clock[0], transport.timeout)

    def test_replay_duplicate_response_does_not_extend_idle_timeout(self):
        previous = Response("", payload(True, CURSOR))
        transport = self.make(responses=[previous, previous, previous, previous])
        transport.timeout = 10
        clock = [0.0]
        transport._page.wait_for_timeout = lambda _ms: clock.__setitem__(0, clock[0] + 4.0)
        with patch("creator_archive.adapters.xhs_transport.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(TransportFailure) as error:
                transport._fetch(AUTHOR, CURSOR)
        self.assertEqual(error.exception.category, "timeout")
        self.assertEqual(transport._page.scrolls, 4)
        self.assertEqual(set(transport._responses), {(AUTHOR, "")})

    def test_restart_replays_until_exact_checkpoint_without_delivering_prior_page(self):
        transport = self.make(responses=[Response("", payload(True, CURSOR)), Response(CURSOR)])
        result = transport.page(AUTHOR, CURSOR)
        self.assertFalse(result.has_more)
        self.assertEqual(result.items[0].item_id, NOTE)
        self.assertEqual(transport._page.scrolls, 2)

    def test_restart_caches_ssr_reference_without_replacing_checkpoint_page(self):
        from tests.test_xhs_content import detail
        state = dict(payload(True, CURSOR)["data"], author=AUTHOR)
        marker = "synthetic-replay-ssr-reference"
        state["notes"][0]["xsec_token"] = marker
        later = payload()
        later["data"]["notes"][0]["note_id"] = "d" * 24
        transport = self.make(initial=state, responses=[Response(CURSOR, later)])
        transport._detail_page = MagicMock()
        transport._detail_page.goto.return_value.status = 200
        snapshot = detail()
        snapshot["note"]["noteId"] = NOTE
        transport._detail_page.evaluate.return_value = snapshot

        page = transport.page(AUTHOR, CURSOR)
        self.assertEqual([item.item_id for item in page.items], ["d" * 24])
        self.assertFalse(page.has_more)
        self.assertEqual(set(transport._responses), {(AUTHOR, CURSOR)})
        self.assertEqual(transport._page.links, [])
        # The old fixed batch may contain an item now visible only in SSR.
        self.assertEqual(transport.prepare_page_details(AUTHOR, [NOTE]), ())
        result = transport.detail(AUTHOR, NOTE)
        self.assertIn(marker, transport._detail_page.goto.call_args.args[0])
        self.assertNotIn(marker, str(page))
        self.assertNotIn(marker, str(transport._responses))
        self.assertNotIn(marker, result["source_url"])
        self.assertEqual(len(transport._page.navigations), 1)
        self.assertEqual(transport._page.scrolls, 1)
        transport.close()
        self.assertEqual(transport._detail_links, {})
        self.assertEqual(transport._verified_detail_links, {})

    def test_foreign_author_response_ignored(self):
        transport = self.make(responses=[Response(CURSOR, author="d" * 24), Response(CURSOR)])
        self.assertFalse(transport.page(AUTHOR, CURSOR).has_more)
        self.assertEqual(transport._page.scrolls, 2)

    def test_replay_seed_cannot_supply_terminal_or_missing_checkpoint_evidence(self):
        for has_more, expected in ((True, "timeout"), (False, "requested_response_missing")):
            with self.subTest(has_more=has_more):
                state = dict(payload(has_more, CURSOR)["data"], author=AUTHOR)
                state["notes"][0]["xsec_token"] = "synthetic-seed-only"
                transport = self.make(initial=state)
                with self.assertRaises(AdapterFailure) as error:
                    transport.page(AUTHOR, CURSOR)
                self.assertEqual(error.exception.category, expected)
                self.assertEqual(transport._responses, {})
                if not has_more:
                    self.assertEqual(transport._detail_links, {})

    def test_replay_validates_whole_seed_before_retaining_references(self):
        for invalid in ("foreign_item", "invalid_id", "missing_cursor", "logged_out"):
            with self.subTest(invalid=invalid):
                state = dict(payload(True, CURSOR)["data"], author=AUTHOR)
                state["notes"][0]["xsec_token"] = "synthetic-valid-first"
                second = {"note_id": "d" * 24, "user": {"user_id": AUTHOR},
                          "xsec_token": "synthetic-invalid-second"}
                state["notes"].append(second)
                if invalid == "foreign_item":
                    second["user"]["user_id"] = "e" * 24
                elif invalid == "invalid_id":
                    second["note_id"] = "invalid"
                elif invalid == "missing_cursor":
                    state["cursor"] = ""
                else:
                    state["logged_in"] = False
                transport = self.make(initial=state, responses=[Response(CURSOR)])
                with self.assertRaises(AdapterFailure):
                    transport.page(AUTHOR, CURSOR)
                self.assertEqual(transport._detail_links, {})
                self.assertEqual(transport._responses, {})
                self.assertEqual(transport._page.scrolls, 0)

    def test_replay_ignores_foreign_seed_and_invalid_reference_values(self):
        for invalid in ("foreign_seed", None, "", False, "x" * 2049, "ref\ncontrol"):
            with self.subTest(invalid_type=type(invalid).__name__):
                state = dict(payload(True, CURSOR)["data"], author=AUTHOR)
                state["notes"][0]["xsec_token"] = invalid
                if invalid == "foreign_seed":
                    state["author"] = "e" * 24
                transport = self.make(initial=state, responses=[Response(CURSOR)])
                self.assertFalse(transport.page(AUTHOR, CURSOR).has_more)
                self.assertEqual(transport.prepare_page_details(AUTHOR, [NOTE]), (NOTE,))
                self.assertEqual(transport._detail_links, {})

    def test_replay_reads_seed_when_navigation_delivers_api_and_keeps_newer_reference(self):
        state = dict(payload(True, CURSOR)["data"], author=AUTHOR)
        state["notes"][0]["xsec_token"] = "synthetic-older-seed"
        extra = "d" * 24
        state["notes"].append({"note_id": extra, "user": {"user_id": AUTHOR},
                               "xsec_token": "synthetic-seed-extra"})
        current = payload()
        current["data"]["notes"][0]["xsec_token"] = "synthetic-newer-api"
        transport = self.make(initial=state)
        goto = transport._page.goto
        def navigate(*args, **kwargs):
            goto(*args, **kwargs)
            transport._observe(Response(CURSOR, current))
        transport._page.goto = navigate
        page = transport.page(AUTHOR, CURSOR)
        self.assertEqual([item.item_id for item in page.items], [NOTE])
        self.assertIn("synthetic-newer-api", transport._detail_links[(AUTHOR, NOTE)])
        self.assertIn("synthetic-seed-extra", transport._detail_links[(AUTHOR, extra)])
        self.assertEqual(transport._page.scrolls, 0)
        self.assertEqual(set(transport._responses), {(AUTHOR, CURSOR)})
        self.assertNotIn("synthetic-", str(transport._responses))

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
        self.assertEqual(error.exception.category, "requested_response_missing")
        self.assertEqual(transport._page.scrolls, 1)

    def test_ssr_false_before_first_scroll_can_still_observe_requested_response(self):
        state = {"author": AUTHOR, "has_more": False}
        transport = self.make(initial=state, responses=[Response(CURSOR, payload())])
        page = transport.page(AUTHOR, CURSOR)
        self.assertEqual([item.item_id for item in page.items], [NOTE])
        self.assertFalse(page.has_more)
        self.assertEqual(transport._page.scrolls, 1)

    def test_same_request_cursor_with_different_observed_pages_is_distinct_failure(self):
        transport = self.make()
        first = payload(True, CURSOR)
        second = payload(True, CURSOR)
        second["data"]["notes"][0]["note_id"] = "d" * 24
        def two_responses(*_args):
            transport._page.scrolls += 1
            transport._observe(Response("", first))
            transport._observe(Response("", second))
        transport._page.wheel = two_responses
        with self.assertRaises(TransportFailure) as error:
            transport.page(AUTHOR, CURSOR)
        self.assertEqual(error.exception.category, "cursor_response_conflict")
        self.assertEqual(transport._responses[(AUTHOR, "")], normalized_response(200, first))
        self.assertEqual(transport._page.scrolls, 1)

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

    def test_two_pages_with_immediate_details_keep_listing_position_and_observations(self):
        from tests.test_xhs_content import detail
        first = payload(True, CURSOR)
        first["data"]["notes"][0]["xsec_token"] = "first-page-reference"
        second = payload()
        second["data"]["notes"][0].update(note_id="d" * 24, xsec_token="second-page-reference")
        transport = self.make(responses=[Response("", first), Response(CURSOR, second)])
        transport._detail_page = MagicMock()
        transport._detail_page.goto.return_value.status = 200
        snapshot = detail()
        snapshot["note"]["noteId"] = NOTE
        transport._detail_page.evaluate.return_value = snapshot
        first_page = transport.page(AUTHOR, None)
        self.assertEqual(transport.prepare_page_details(AUTHOR, [NOTE]), ())
        listing = transport._page
        observations = dict(transport._responses)
        transport.detail(AUTHOR, NOTE)
        self.assertEqual(transport._responses, observations)
        self.assertEqual(transport._author, AUTHOR)
        second_page = transport.page(AUTHOR, first_page.next_cursor)
        self.assertEqual(transport.prepare_page_details(AUTHOR, ["d" * 24]), ())
        snapshot["note"]["noteId"] = "d" * 24
        transport.detail(AUTHOR, "d" * 24)
        self.assertFalse(second_page.has_more)
        self.assertEqual(listing.navigations, [f"https://www.xiaohongshu.com/user/profile/{AUTHOR}"])
        self.assertEqual(listing.scrolls, 2)
        self.assertEqual(transport._detail_page.goto.call_count, 2)
        self.assertIn("first-page-reference", transport._detail_page.goto.call_args_list[0].args[0])
        self.assertIn("second-page-reference", transport._detail_page.goto.call_args_list[1].args[0])

    def test_page_prepare_reports_missing_individually_without_reset_or_fallback(self):
        from tests.test_xhs_content import detail
        data = payload(True, CURSOR)
        data["data"]["notes"][0]["xsec_token"] = "observed-reference"
        transport = self.make(responses=[Response("", data)])
        transport._detail_page = MagicMock()
        transport._detail_page.goto.return_value.status = 200
        snapshot = detail()
        snapshot["note"]["noteId"] = NOTE
        transport._detail_page.evaluate.return_value = snapshot
        transport.page(AUTHOR, None)
        missing = "d" * 24
        self.assertEqual(transport.prepare_page_details(AUTHOR, [NOTE, missing]), (missing,))
        with self.assertRaises(TransportFailure) as error:
            transport.detail(AUTHOR, missing)
        self.assertEqual(error.exception.category, "reference_missing")
        transport._detail_page.goto.assert_not_called()
        self.assertEqual(transport.detail(AUTHOR, NOTE)["item_id"], NOTE)
        self.assertEqual(len(transport._page.navigations), 1)
        self.assertEqual(transport._page.scrolls, 1)

    def test_page_prepare_collects_ssr_anchor_only_for_requested_items(self):
        transport = self.make(initial=dict(payload(True, CURSOR)["data"], author=AUTHOR))
        transport._page.links = [
            f"https://www.xiaohongshu.com/user/profile/{AUTHOR}/{NOTE}?xsec_token=observed-anchor",
            f"https://www.xiaohongshu.com/explore/{'d' * 24}?xsec_token=other-anchor",
        ]
        transport.page(AUTHOR, None)
        self.assertEqual(transport.prepare_page_details(AUTHOR, [NOTE]), ())
        self.assertEqual(set(transport._detail_links), {(AUTHOR, NOTE)})
        self.assertEqual(len(transport._page.navigations), 1)
        self.assertEqual(transport._page.scrolls, 0)

    def test_ssr_reference_without_dom_anchor_is_consumed_and_never_in_page_evidence(self):
        from tests.test_xhs_content import detail
        state = dict(payload(True, CURSOR)["data"], author=AUTHOR)
        marker = "synthetic-ssr-reference"
        state["notes"][0]["xsec_token"] = marker
        transport = self.make(initial=state)
        transport._detail_page = MagicMock()
        transport._detail_page.goto.return_value.status = 200
        snapshot = detail()
        snapshot["note"]["noteId"] = NOTE
        transport._detail_page.evaluate.return_value = snapshot
        # Capture the actual normalized transport return, not just Page's ID list.
        normalized = transport._call(transport._fetch, AUTHOR, "")
        self.assertNotIn(marker, str(normalized))
        self.assertNotIn(marker, str(transport._responses))
        self.assertEqual(transport._page.links, [])
        self.assertEqual(transport.prepare_page_details(AUTHOR, [NOTE]), ())
        result = transport.detail(AUTHOR, NOTE)
        self.assertIn(marker, transport._detail_page.goto.call_args.args[0])
        self.assertNotIn(marker, result["source_url"])
        self.assertEqual(len(transport._page.navigations), 1)
        self.assertEqual(transport._page.scrolls, 0)
        transport.close()
        self.assertEqual(transport._detail_links, {})
        self.assertEqual(transport._verified_detail_links, {})

    def test_ssr_validates_entire_page_before_retaining_any_reference(self):
        for invalid in ("foreign_author", "invalid_id", "missing_cursor", "logged_out"):
            with self.subTest(invalid=invalid):
                state = dict(payload(True, CURSOR)["data"], author=AUTHOR)
                state["notes"][0]["xsec_token"] = "first-valid-reference"
                second = {"note_id": "d" * 24, "user": {"user_id": AUTHOR}, "xsec_token": "second-reference"}
                state["notes"].append(second)
                if invalid == "foreign_author":
                    second["user"]["user_id"] = "e" * 24
                elif invalid == "invalid_id":
                    second["note_id"] = "invalid"
                elif invalid == "missing_cursor":
                    state["cursor"] = ""
                else:
                    state["logged_in"] = False
                transport = self.make(initial=state)
                with self.assertRaises(AdapterFailure):
                    transport.page(AUTHOR, None)
                self.assertEqual(transport._detail_links, {})
                self.assertEqual(transport._responses, {})

    def test_missing_or_invalid_ssr_token_stays_an_individual_reference_gap(self):
        for token in (None, "", False, 42, "x" * 2049, "reference\ncontrol"):
            with self.subTest(token_type=type(token).__name__, length=len(token) if isinstance(token,str) else 0):
                state = dict(payload(True, CURSOR)["data"], author=AUTHOR)
                state["notes"][0]["xsec_token"] = token
                transport = self.make(initial=state)
                page = transport.page(AUTHOR, None)
                self.assertEqual(page.items[0].item_id, NOTE)
                self.assertEqual(transport.prepare_page_details(AUTHOR, [NOTE]), (NOTE,))
                with self.assertRaises(TransportFailure) as error:
                    transport.detail(AUTHOR, NOTE)
                self.assertEqual(error.exception.category, "reference_missing")
                self.assertEqual(len(transport._page.navigations), 1)
                self.assertEqual(transport._page.scrolls, 0)

    def test_context_creates_distinct_tabs_and_reopens_detail_without_losing_listing(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        transport = XhsBrowserTransport(Path(temp.name) / "profile")
        self.addCleanup(transport.close)
        listing, detail_page, replacement = MagicMock(), MagicMock(), MagicMock()
        for page in (listing, detail_page, replacement):
            page.is_closed.return_value = False
        context = MagicMock()
        context.pages = [listing]
        context.new_page.side_effect = [detail_page, replacement]
        with patch('playwright.sync_api.sync_playwright') as playwright:
            runtime = playwright.return_value.start.return_value
            runtime.chromium.launch_persistent_context.return_value = context
            transport._call(transport._ensure)
        self.assertIs(transport._page, listing)
        self.assertIs(transport._detail_page, detail_page)
        listing.on.assert_called_once_with("response", transport._observe)
        detail_page.on.assert_not_called()
        transport._author = AUTHOR
        transport._responses[(AUTHOR, CURSOR)] = normalized_response(200, payload())
        detail_page.is_closed.return_value = True
        transport._call(transport._ensure)
        self.assertIs(transport._detail_page, replacement)
        self.assertIs(transport._page, listing)
        self.assertEqual(transport._author, AUTHOR)
        self.assertIn((AUTHOR, CURSOR), transport._responses)
        transport.close()
        context.close.assert_called_once()
        self.assertIsNone(transport._detail_page)


if __name__ == "__main__":
    unittest.main()
