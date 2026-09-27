"""Actual stdio MCP client against a synthetic HTTP archive, with no platform I/O."""

import asyncio
import base64
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest

import httpx
import uvicorn

try:
    from mcp import Client, StdioServerParameters
    from mcp.types import BlobResourceContents, ImageContent
except ImportError:  # Optional MCP bridge is installed from requirements-mcp.txt.
    Client = None

from creator_archive.app import create_app
from tests.test_demo_pipeline import DemoSource, IDS


def fingerprint(root: Path) -> dict:
    return {str(path.relative_to(root)): (sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file()}


@unittest.skipIf(Client is None, "optional mcp==2.1.1 is not installed")
class McpReadOnlyTests(unittest.TestCase):
    def test_stdio_client_reads_all_pages_body_and_verified_image_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            author = "a" * 24
            source = DemoSource(pages=[IDS[:2], IDS[1:4], IDS[4:5], IDS[4:6], IDS[5:6]])
            app = create_app(root)
            service = app.state.service
            service.adapter_factory = lambda _: source
            service.workflow.subscribe("xiaohongshu", author, "Synthetic MCP author",
                                       verified=True, evidence="synthetic fixture")
            parent = service.start("author_archive", "xiaohongshu", author)["job_id"]
            service.wait()
            self.assertEqual(service.job_scan(parent)["pages_scanned"], 5)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            web = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                                access_log=False, log_level="error"))
            thread = threading.Thread(target=web.run, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not web.started and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(web.started)
                before = fingerprint(root)
                with service.workflow.connect() as db:
                    before_counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                                     for table in ("jobs", "runs", "pages", "items", "assets")}

                async def exercise():
                    params = StdioServerParameters(command=sys.executable,
                                                   args=["-m", "creator_archive.mcp_readonly", "--base-url",
                                                         f"http://127.0.0.1:{port}"],
                                                   cwd=str(Path(__file__).resolve().parents[1]))
                    async with Client(params) as client:
                        names = {tool.name for tool in (await client.list_tools()).tools}
                        self.assertEqual(names, {"list_creators", "get_creator_coverage", "search_items",
                                                 "get_item", "list_assets", "read_corpus_page", "read_image"})

                        async def call(name, arguments):
                            result = await client.call_tool(name, arguments)
                            self.assertFalse(result.is_error, (name, result))
                            return result.structured_content or json.loads(result.content[0].text)

                        creators = await call("list_creators", {"limit": 1})
                        self.assertEqual((creators["total"], creators["returned"]), (1, 1))
                        coverage = await call("get_creator_coverage", {"platform": "xiaohongshu", "author_id": author})
                        self.assertEqual((coverage["item_count"], coverage["detail_count"]), (6, 6))
                        self.assertEqual(coverage["unseen_items"], "unknown_not_enumerable")
                        seen = []
                        cursor = None
                        while True:
                            listed = await call("search_items", {"platform": "xiaohongshu", "author_id": author,
                                                                  "cursor": cursor, "limit": 2})
                            self.assertEqual((listed["total"], listed["returned"]), (6, 2))
                            seen.extend(item["item_id"] for item in listed["items"])
                            cursor = listed["next_cursor"]
                            if cursor is None:
                                break
                        self.assertEqual((len(seen), len(set(seen))), (6, 6))
                        corpus = []
                        cursor = None
                        while True:
                            result = await call("read_corpus_page", {"platform": "xiaohongshu",
                                                                     "author_id": author, "cursor": cursor, "limit": 2})
                            corpus.extend(result["items"])
                            cursor = result["next_cursor"]
                            if cursor is None:
                                break
                        self.assertEqual({row["item_id"] for row in corpus}, set(seen))
                        self.assertTrue(all(row["text"] and row["source_url"] for row in corpus))
                        detail = await call("get_item", {"platform": "xiaohongshu", "item_id": seen[0]})
                        self.assertEqual(detail["detail_text"], next(row["text"] for row in corpus if row["item_id"] == seen[0]))
                        asset = detail["assets"][0]
                        self.assertEqual(asset["state"], "complete")
                        image = await client.call_tool("read_image", {"platform": "xiaohongshu", "item_id": seen[0],
                                                                       "asset_id": asset["asset_id"]})
                        self.assertFalse(image.is_error)
                        block = next(value for value in image.content if isinstance(value, ImageContent))
                        self.assertEqual(base64.b64decode(block.data)[:8], b"\x89PNG\r\n\x1a\n")
                        resource = await client.read_resource(f"archive-image://xiaohongshu/{seen[0]}/{asset['asset_id']}")
                        blob = next(value for value in resource.contents if isinstance(value, BlobResourceContents))
                        self.assertEqual(base64.b64decode(blob.blob), base64.b64decode(block.data))
                        path = root / "archive" / asset["url"].removeprefix("/archive/")
                        original = path.read_bytes()
                        original_stat = path.stat()
                        path.write_bytes(b"x" * len(original))
                        try:
                            rejected = await client.call_tool("read_image", {"platform": "xiaohongshu", "item_id": seen[0],
                                                                              "asset_id": asset["asset_id"]})
                            self.assertTrue(rejected.is_error)
                        finally:
                            path.write_bytes(original)
                            os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
                        return names

                asyncio.run(exercise())
                self.assertEqual(fingerprint(root), before)
                with service.workflow.connect() as db:
                    self.assertEqual({table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                                      for table in before_counts}, before_counts)
                self.assertEqual(service.workspace()["stats"]["running"], 0)
            finally:
                web.should_exit = True
                thread.join(timeout=10)
                service.close()


if __name__ == "__main__":
    unittest.main()
