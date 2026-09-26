from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from creator_archive.app import create_app


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(Path(self.temp.name))
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.headers = {"x-creator-archive": "local-validation"}

    def test_home_and_static_assets(self):
        for path in ("/", "/static/style.css", "/static/app.js"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertFalse(self.client.get("/api/status").json()["g1_passed"])

    def test_cross_origin_and_untrusted_host_blocked(self):
        self.assertEqual(self.client.post("/api/demo/run", json={}).status_code, 403)
        headers = {**self.headers, "origin": "https://example.invalid"}
        self.assertEqual(self.client.post("/api/demo/run", json={}, headers=headers).status_code, 403)
        self.assertEqual(self.client.get("/api/status", headers={"host": "evil.example"}).status_code, 400)

    def test_batch_failure_keeps_other_authors_and_resume_survives_app_restart(self):
        first = self.client.post("/api/demo/run", json={}, headers=self.headers).json()
        self.assertEqual(len(first["runs"]), 4)
        self.assertEqual([r["pages"] for r in first["runs"]], [4, 2, 4, 2])
        self.assertEqual([r["state"] for r in first["runs"]], ["succeeded", "needs_login", "succeeded", "rate_limited"])
        with TestClient(create_app(Path(self.temp.name))) as restarted:
            resumed = restarted.post("/api/demo/run", json={"inject_failures": False}, headers=self.headers).json()
            self.assertEqual([r["pages"] for r in resumed["runs"]], [4, 4, 4, 2])
            self.assertFalse(resumed["g1_passed"])

    def test_link_input_not_stored(self):
        text = "https://xhslink.com/a/private-example"
        response = self.client.post("/api/links/classify", json={"text": text}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["can_subscribe"])
        self.assertNotIn(b"private-example", (Path(self.temp.name)/"state.sqlite3").read_bytes())


if __name__ == "__main__":
    unittest.main()
