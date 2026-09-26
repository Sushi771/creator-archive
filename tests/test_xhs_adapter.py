from pathlib import Path
import tempfile
import unittest

from creator_archive.adapters.xhs import XhsPageAdapter, extract_detail_media
from creator_archive.validation import AdapterFailure, Store, scan


AUTHOR = "a" * 24


class PageAdapterTests(unittest.TestCase):
    def test_four_api_pages_new_store_resume_and_deduplicate(self):
        seen = []

        def fetch(author_id, cursor):
            seen.append((author_id, cursor))
            number = int(cursor) if cursor else 0
            notes = [{"note_id": f"{n:024x}", "user": {"user_id": AUTHOR}}
                     for n in range(number * 15, (number + 1) * 15)]
            if number:
                notes.append({"note_id": "0" * 24})  # Repeated pinned card.
            return {"data": {"notes": notes, "has_more": number < 3,
                             "cursor": str(number + 1)}}

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            adapter = XhsPageAdapter(fetch)
            store = Store(path)
            run = store.create("xiaohongshu", AUTHOR, adapter.version)
            row = scan(store, run, adapter, max_pages=2)
            self.assertEqual((row["pages"], row["cursor"], row["coverage"]),
                             (2, "2", "partial"))
            row = scan(Store(path), run, adapter)
            self.assertEqual((row["pages"], row["item_count"], row["state"]),
                             (4, 60, "succeeded"))
            self.assertEqual(seen, [(AUTHOR, ""), (AUTHOR, "1"), (AUTHOR, "2"), (AUTHOR, "3")])
            with Store(path).connect() as db:
                rows = db.execute("SELECT request_cursor,next_cursor FROM page_evidence ORDER BY page_number").fetchall()
                self.assertEqual([(r[0], r[1]) for r in rows],
                                 [("", "1"), ("1", "2"), ("2", "3"), ("3", None)])

    def test_ambiguous_or_broken_response_does_not_mark_complete(self):
        bad_data = [
            {},
            {"notes": [], "has_more": False, "cursor": ""},
            {"notes": [{"note_id": "n"}], "has_more": True, "cursor": ""},
            {"notes": [{"note_id": "n"}], "has_more": True, "cursor": "1"},
            {"notes": [{"note_id": "n", "user": {"user_id": "other"}}],
             "has_more": False, "cursor": ""},
        ]
        for index, data in enumerate(bad_data):
            with self.subTest(index=index):
                adapter = XhsPageAdapter(lambda _author, _cursor: {"data": data})
                with self.assertRaises(AdapterFailure):
                    adapter.page(AUTHOR, "1" if index == 3 else None)

    def test_explicit_terminal_and_transport_error(self):
        adapter = XhsPageAdapter(lambda _author, _cursor: {
            "data": {"notes": [{"note_id": "n"}], "cursor": "leftover", "has_more": False}})
        terminal = adapter.page(AUTHOR, None)
        self.assertFalse(terminal.has_more)
        self.assertIsNone(terminal.next_cursor)
        self.assertIn("has_more=false", terminal.terminal_evidence)
        blocked = XhsPageAdapter(lambda _author, _cursor: {"success": False, "data": {}})
        with self.assertRaises(AdapterFailure):
            blocked.page(AUTHOR, None)


class DetailMediaTests(unittest.TestCase):
    def test_preserves_image_order_and_video_variants(self):
        detail = {"note": {"noteId": "b" * 24, "user": {"userId": AUTHOR},
                           "desc": "test", "type": "video",
                           "imageList": [
                               {"urlDefault": "https://cdn.example/2.webp"},
                               {"urlDefault": "https://cdn.example/1.webp", "livePhoto": True},
                           ],
                           "video": {"media": {"stream": {"h264": [
                               {"masterUrl": "https://cdn.example/v.mp4", "size": 10,
                                "qualityType": "HD"}]}}}}}
        media = extract_detail_media(detail, author_id=AUTHOR, item_id="b" * 24)
        self.assertEqual([v.position for v in media.images], [0, 1])
        self.assertEqual(len(media.video_streams), 1)
        self.assertEqual(media.video_streams[0].size, 10)
        self.assertIn("live_photo_1_video_unresolved", media.missing)

    def test_missing_sources_and_wrong_identity_are_explicit(self):
        detail = {"note": {"noteId": "n", "user": {"userId": AUTHOR},
                           "type": "video", "imageList": [
                               {"urlDefault": "http://plain.invalid/a"},
                               {"urlDefault": "https://127.0.0.1/private"}],
                           "video": {"media": {"stream": {}}}}}
        media = extract_detail_media(detail, author_id=AUTHOR, item_id="n")
        self.assertEqual(media.images, ())
        self.assertEqual(media.video_streams, ())
        self.assertIn("image_0_source_missing", media.missing)
        self.assertIn("image_1_source_missing", media.missing)
        self.assertIn("video_stream_missing", media.missing)
        with self.assertRaises(AdapterFailure):
            extract_detail_media(detail, author_id="other", item_id="n")


if __name__ == "__main__":
    unittest.main()
