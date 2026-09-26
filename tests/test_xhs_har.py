"""Synthetic HAR and local process tests; never evidence of a platform fetch."""

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from creator_archive.adapters.xhs_har import read_har
from scripts.verify_xhs_har import verify


AUTHORS = {"XHS-A": {"author_id": "a" * 24}, "XHS-B": {"author_id": "b" * 24}}
SECRET = "must-not-retain-secret"


def entry(author, page=0, more=True, status=200):
    offset = 0 if author == "a" * 24 else 1000
    notes = [{"note_id": f"{offset + page * 15 + n:024x}", "user": {"user_id": author},
              "display_title": SECRET, "xsec_token": SECRET} for n in range(15)]
    if page:
        notes.append({"note_id": f"{offset:024x}"})
    cursor = f"{page:024x}" if page else ""
    payload = {"code": 0, "success": True, "data": {
        "notes": notes, "has_more": more, "cursor": f"{page + 1:024x}"}}
    return {"startedDateTime": f"2026-09-26T09:00:{page:02d}Z",
            "request": {"method": "GET", "url": "https://edith.xiaohongshu.com/api/sns/web/v1/user_posted"
                        f"?user_id={author}&cursor={cursor}&xsec_token={SECRET}",
                        "headers": [{"name": "Cookie", "value": SECRET}]},
            "response": {"status": status, "headers": [{"name": "Set-Cookie", "value": SECRET}],
                         "content": {"text": json.dumps(payload)}}}


class HarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "private-results"

    def har(self, entries, name="input.har"):
        path = self.root / name
        path.write_text(json.dumps({"log": {"entries": entries}}), encoding="utf-8")
        return path

    def test_four_pages_two_processes_deduplicate_and_strip_secrets(self):
        path = self.har([entry(a["author_id"], p, p < 3) for a in AUTHORS.values() for p in range(4)])
        samples = self.root / "samples.json"
        samples.write_text(json.dumps(AUTHORS), encoding="utf-8")
        env = dict(os.environ, LOCALAPPDATA=str(self.root), PYTHONIOENCODING="utf-8")
        command = [sys.executable, "-m", "scripts.verify_xhs_har", "--har", str(path),
                   "--samples", str(samples), "--root", str(self.output)]
        for budget, expected_pages in ((2, 2), (100, 4), (100, 4)):
            result = subprocess.run(command + ["--max-pages", str(budget)], env=env,
                                    capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn(SECRET, result.stdout + result.stderr)
            report = json.loads((self.output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual([r["pages"] for r in report["runs"]], [expected_pages] * 2)
        self.assertEqual([r["unique_ids"] for r in report["runs"]], [60, 60])
        self.assertEqual({r["state"] for r in report["runs"]}, {"succeeded"})
        self.assertFalse(report["g1_passed"])
        self.assertFalse(report["live_transport_resume_verified"])
        saved = (self.output / "capture-index.json").read_text(encoding="utf-8")
        for value in (SECRET, "headers", "https://", "xsec_token", "display_title"):
            self.assertNotIn(value, saved)

    def test_missing_page_then_later_capture_continues_checkpoint(self):
        author = AUTHORS["XHS-A"]["author_id"]
        first = self.har([entry(author)])
        report = verify([first], AUTHORS, self.output)
        self.assertEqual(report["runs"][0]["pages"], 1)
        self.assertEqual(report["runs"][0]["reason"], "missing_captured_page")
        later = self.har([entry(author, 1, False)], "later.har")
        report = verify([later], AUTHORS, self.output)
        self.assertEqual((report["runs"][0]["pages"], report["runs"][0]["unique_ids"]), (2, 30))
        self.assertEqual(report["runs"][0]["state"], "succeeded")
        self.assertEqual(report["runs"][1]["state"], "partial")

    def test_http_error_or_missing_body_cannot_be_terminal(self):
        author = AUTHORS["XHS-A"]["author_id"]
        for status in (401, 429, 500, 200, 200.0, "200", True, None):
            failed = entry(author, 1, False, status)
            if type(status) is int and status == 200:
                failed["response"]["content"] = {}
            report = verify([self.har([entry(author), failed])], AUTHORS, self.root / str(status))
            run = report["runs"][0]
            self.assertEqual((run["pages"], run["state"], run["terminal_evidence"]), (1, "partial", None))
            self.assertTrue(run["reason"].startswith("captured_"))
            self.assertTrue(run["next_step"])

    def test_conflict_does_not_overwrite_previously_imported_data(self):
        author = AUTHORS["XHS-A"]["author_id"]
        original = entry(author, 0, False)
        verify([self.har([original])], AUTHORS, self.output)
        before = {p.name: p.read_bytes() for p in self.output.iterdir()}
        payload = json.loads(original["response"]["content"]["text"])
        payload["data"]["notes"][0]["note_id"] = "f" * 24
        original["response"]["content"]["text"] = json.dumps(payload)
        with self.assertRaisesRegex(ValueError, "conflicting_captured_pages"):
            verify([self.har([original], "changed.har")], AUTHORS, self.output)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.output.iterdir()})

    def test_base64_and_ignored_other_origins_and_authors(self):
        author = AUTHORS["XHS-A"]["author_id"]
        good = entry(author, 0, False)
        body = good["response"]["content"]["text"]
        good["response"]["content"] = {"encoding": "base64", "text": base64.b64encode(body.encode()).decode()}
        bad = entry(author)
        bad["request"]["url"] = bad["request"]["url"].replace("edith.xiaohongshu.com", "edith.xiaohongshu.com.evil.test")
        records, _ = read_har(self.har([good, bad, entry("c" * 24)]), {author})
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["response"]["data"]["has_more"])

    def test_invalid_api_envelope_or_identity_remains_failed(self):
        author = AUTHORS["XHS-A"]["author_id"]
        for change in ("success", "code", "identity", "has_more"):
            raw = entry(author, 0, False)
            payload = json.loads(raw["response"]["content"]["text"])
            if change == "success":
                payload["success"] = 1
            elif change == "code":
                payload["code"] = 100
            elif change == "identity":
                payload["data"]["notes"][0]["user"]["user_id"] = "b" * 24
            else:
                payload["data"]["has_more"] = "false"
            raw["response"]["content"]["text"] = json.dumps(payload)
            records, _ = read_har(self.har([raw]), {author})
            self.assertEqual(records[0]["error"], "unavailable")
            self.assertNotIn("response", records[0])

    def test_unrelated_old_database_and_manual_notes_unchanged(self):
        self.output.mkdir()
        old = self.output / "archive.sqlite3"
        old.write_bytes(b"not this validator database")
        with self.assertRaisesRegex(ValueError, "unrecognized_existing_root"):
            verify([self.har([entry("a" * 24)])], AUTHORS, self.output)
        self.assertEqual(old.read_bytes(), b"not this validator database")

    def test_empty_initial_response_and_cycle_do_not_finish(self):
        author = AUTHORS["XHS-A"]["author_id"]
        raw = entry(author, 0, False)
        payload = json.loads(raw["response"]["content"]["text"])
        payload["data"]["notes"] = []
        raw["response"]["content"]["text"] = json.dumps(payload)
        report = verify([self.har([raw])], AUTHORS, self.output)
        self.assertEqual(report["runs"][0]["pages"], 0)
        cyclic = entry(author, 1)
        payload = json.loads(cyclic["response"]["content"]["text"])
        payload["data"]["cursor"] = f"{1:024x}"
        cyclic["response"]["content"]["text"] = json.dumps(payload)
        report = verify([self.har([entry(author), cyclic], "cycle.har")], AUTHORS, self.output)
        self.assertEqual((report["runs"][0]["pages"], report["runs"][0]["state"]), (1, "partial"))


if __name__ == "__main__":
    unittest.main()
