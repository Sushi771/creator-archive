"""Actual stdio MCP client against a synthetic HTTP archive, with no platform I/O."""

import asyncio
import base64
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import struct
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
import zlib

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
        self.exercise_archive()

    @unittest.skipUnless(os.environ.get("CREATOR_ARCHIVE_TUNNEL_CLIENT"), "optional official tunnel-client binary not selected")
    def test_local_tunnel_reads_all_pages_body_and_verified_image_without_writes(self):
        self.exercise_archive(tunnel=True)

    @contextmanager
    def client_target(self, params, tunnel):
        if not tunnel:
            yield params
            return
        # No user profile, credential, hosted tunnel or public listener is used.
        with tempfile.TemporaryDirectory(prefix="creator-archive-mcp-proxy-") as directory:
            output = Path(directory) / "connection.json"
            env = {key: value for key, value in os.environ.items()
                   if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}}
            with (Path(directory) / "proxy.log").open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    [os.environ["CREATOR_ARCHIVE_TUNNEL_CLIENT"], "dev", "proxy", "--backend", "go",
                     "--listen", "127.0.0.1:0", "--duration", "90s", "--readiness-timeout", "15s",
                     "--mcp-command", shlex.join([Path(params.command).as_posix(), *params.args]),
                     "--url-file", str(output)], cwd=params.cwd, env=env, stdout=log, stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                try:
                    deadline = time.monotonic() + 20
                    while not output.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertTrue(output.exists(), (Path(directory) / "proxy.log").read_text(encoding="utf-8")[-3000:])
                    target = json.loads(output.read_text(encoding="utf-8"))["mcp_url"]
                    self.assertEqual(urlsplit(target).hostname, "127.0.0.1")
                    yield target
                finally:
                    if process.poll() is None:
                        if os.name == "nt":
                            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                           capture_output=True, timeout=10, check=False)
                        else:
                            process.terminate()
                    process.wait(timeout=10)

    def test_base_url_rejects_non_loopback_and_ambiguous_targets(self):
        from creator_archive.mcp_readonly import create_server
        for url in ("https://127.0.0.1:8765", "http://localhost:8765", "http://example.invalid:8765",
                    "http://127.0.0.1", "http://user:secret@127.0.0.1:8765", "http://127.0.0.1:8765/api",
                    "http://127.0.0.1:8765/?next=remote", "http://127.0.0.1:8765/#fragment"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                create_server(url)

    def test_synthetic_lab_uses_temporary_root_and_removes_it_on_normal_stop(self):
        process = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-m", "scripts.mcp_synthetic_lab", "--duration", "20"],
            cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8")
        try:
            metadata = json.loads(process.stdout.readline())
            root = Path(metadata["archive_root"]).parent
            self.assertEqual(root.parent.resolve(), Path(tempfile.gettempdir()).resolve())
            self.assertTrue(root.name.startswith("creator-archive-synthetic-mcp-"))
            with httpx.Client(base_url=metadata["base_url"], trust_env=False, timeout=3) as client:
                response = client.get("/api/items", params={"limit": 2, "offset": 4})
                self.assertEqual(response.status_code, 200)
                self.assertEqual((response.json()["total"], len(response.json()["items"])), (6, 2))
                self.assertEqual(client.get("/api/workspace").json()["stats"]["running"], 0)
                detail = client.get("/api/items/xiaohongshu/" + IDS[0]).json()
                image = client.get(detail["assets"][0]["url"]).content
                self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
                self.assertEqual(struct.unpack(">II", image[16:24]), (128, 96))
                idat_start = image.index(b"IDAT") + 4
                compressed_length = struct.unpack(">I", image[idat_start - 8:idat_start - 4])[0]
                pixels = zlib.decompress(image[idat_start:idat_start + compressed_length])
                def pixel(x, y):
                    start = y * (1 + 128 * 3) + 1 + x * 3
                    return tuple(pixels[start:start + 3])
                self.assertEqual(pixel(10, 48), (22, 96, 215))
                self.assertEqual(pixel(118, 48), (255, 208, 12))
                self.assertEqual(pixel(64, 48), (226, 42, 49))
            async def read_image_through_mcp():
                command = shlex.split(metadata["mcp_command"])
                params = StdioServerParameters(command=command[0], args=command[1:],
                                               cwd=metadata["command_working_directory"])
                async with Client(params) as client:
                    result = await client.call_tool("read_image", {"platform": "xiaohongshu", "item_id": IDS[0],
                                                                   "asset_id": detail["assets"][0]["asset_id"]})
                    self.assertFalse(result.is_error)
                    block = next(value for value in result.content if isinstance(value, ImageContent))
                    self.assertEqual(base64.b64decode(block.data), image)
            asyncio.run(read_image_through_mcp())
            _, errors = process.communicate(timeout=25)
            self.assertEqual(process.returncode, 0, errors)
            self.assertFalse(root.exists())
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=10)

    def exercise_archive(self, tunnel=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            author = "a" * 24
            source = DemoSource(pages=[IDS[:2], IDS[1:4], IDS[4:5], IDS[4:6], IDS[5:6]])
            app = create_app(root)
            requests = []

            @app.middleware("http")
            async def record_requests(request, call_next):
                requests.append((request.method, request.url.path))
                return await call_next(request)

            service = app.state.service
            service.adapter_factory = lambda _: source
            service.workflow.subscribe("xiaohongshu", author, "Synthetic MCP author",
                                       verified=True, evidence="synthetic fixture")
            service.set_subscription_tags("xiaohongshu", author, ["合成标签"])
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
                    with self.client_target(params, tunnel) as target:
                        async with Client(target) as client:
                            tools = (await client.list_tools()).tools
                            names = {tool.name for tool in tools}
                            self.assertEqual(names, {"list_creators", "get_creator_coverage", "search_items",
                                                     "get_item", "list_assets", "read_corpus_page", "read_image"})
                            for tool in tools:
                                self.assertTrue(tool.annotations.read_only_hint)
                                self.assertFalse(tool.annotations.destructive_hint)
                                self.assertFalse(tool.annotations.open_world_hint)

                            for name, arguments, message in (
                                ("start_archive", {}, "Unknown tool"),
                                ("search_items", {"cursor": "-1"}, "cursor"),
                                ("search_items", {"author_id": author}, "平台"),
                                ("get_creator_coverage", {"platform": "xiaohongshu", "author_id": "missing"}, "作者"),
                                ("read_corpus_page", {"platform": "xiaohongshu", "author_id": author, "limit": 101}, "limit"),
                                ("get_item", {"platform": "xiaohongshu", "item_id": "../../api/workspace"}, "ID"),
                                ("get_item", {"platform": "invalid", "item_id": IDS[0]}, "平台"),
                            ):
                                rejected = await client.call_tool(name, arguments)
                                self.assertTrue(rejected.is_error, name)
                                self.assertIn(message, " ".join(block.text for block in rejected.content if hasattr(block, "text")))

                            async def call(name, arguments):
                                result = await client.call_tool(name, arguments)
                                self.assertFalse(result.is_error, (name, result))
                                return result.structured_content or json.loads(result.content[0].text)

                            creators = await call("list_creators", {"limit": 1})
                            self.assertEqual((creators["total"], creators["returned"]), (1, 1))
                            self.assertTrue(creators["items"][0]["subscribed"])
                            self.assertEqual(creators["items"][0]["tags"], ["合成标签"])
                            coverage = await call("get_creator_coverage", {"platform": "xiaohongshu", "author_id": author})
                            self.assertTrue(coverage["subscribed"])
                            self.assertEqual(coverage["tags"], ["合成标签"])
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
                self.assertTrue(requests)
                self.assertTrue(all(method == "GET" and path.startswith(("/api/workspace", "/api/items", "/archive/"))
                                    for method, path in requests))
            finally:
                web.should_exit = True
                thread.join(timeout=10)
                service.close()


if __name__ == "__main__":
    unittest.main()
