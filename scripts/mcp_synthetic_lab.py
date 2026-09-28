"""Disposable synthetic archive for manual ChatGPT/MCP connection testing.

Run from the checkout with ``python -m scripts.mcp_synthetic_lab``. No real
workspace, browser profile, platform request, credential or tunnel is opened.
"""

import argparse
import json
from pathlib import Path
import shlex
import socket
import struct
import sys
import tempfile
import threading
import zlib

import uvicorn

from creator_archive.app import create_app
from tests.test_demo_pipeline import DemoSource, IDS


def vision_probe_png() -> bytes:
    """A recognizable image whose layout is absent from MCP metadata and prompts."""
    width, height = 128, 96
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # PNG filter: none
        for x in range(width):
            color = (226, 42, 49) if 48 <= x < 80 and 32 <= y < 64 else (
                (22, 96, 215) if x < 64 else (255, 208, 12))
            rows.extend(color)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


class VisionProbeSource(DemoSource):
    def download_media(self, candidate, target):
        self.events.append(("download", target.name))
        target.mkdir(parents=True, exist_ok=True)
        image = target / "sample.png"
        image.write_bytes(vision_probe_png())
        return {"path": str(image), "mime": "image/png"}


def main():
    parser = argparse.ArgumentParser(description="Disposable synthetic MCP archive; Ctrl+C stops and removes the lab")
    parser.add_argument("--port", type=int, default=0, help="Loopback port; 0 chooses an unused port")
    parser.add_argument("--duration", type=int, default=0, help="Stop after N seconds; 0 waits for Ctrl+C")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535 or args.duration < 0:
        parser.error("port must be 0..65535 and duration must be non-negative")
    with tempfile.TemporaryDirectory(prefix="creator-archive-synthetic-mcp-") as directory:
        app = create_app(Path(directory))
        service = app.state.service
        timer = None
        try:
            source = VisionProbeSource(pages=[IDS[:2], IDS[1:4], IDS[4:5], IDS[4:6], IDS[5:6]])
            service.adapter_factory = lambda _: source
            author = "a" * 24
            service.workflow.subscribe("xiaohongshu", author, "Synthetic MCP author",
                                       verified=True, evidence="synthetic fixture only")
            parent = service.start("author_archive", "xiaohongshu", author)["job_id"]
            service.wait()
            if service.job_scan(parent)["pages_scanned"] != 5 or service.workspace()["stats"]["running"]:
                raise RuntimeError("Synthetic fixture did not finish; no tunnel was started")
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", args.port))
                listener.listen()
                port = listener.getsockname()[1]
                server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                                       access_log=False, log_level="error"))
                if args.duration:
                    timer = threading.Timer(args.duration, lambda: setattr(server, "should_exit", True))
                    timer.daemon = True
                    timer.start()
                print(json.dumps({
                    "evidence_level": "synthetic_only_not_chatgpt_acceptance",
                    "base_url": f"http://127.0.0.1:{port}", "archive_root": str(Path(directory) / "archive"),
                    "platform": "xiaohongshu", "author_id": author, "unique_items": 6, "scan_pages": 5,
                    "mcp_command": shlex.join([Path(sys.executable).as_posix(), "-X", "utf8", "-m",
                                               "creator_archive.mcp_readonly", "--base-url", f"http://127.0.0.1:{port}"]),
                    "command_working_directory": Path(__file__).resolve().parents[1].as_posix(),
                    "stop": "Ctrl+C; temporary synthetic files are removed on normal exit",
                }, ensure_ascii=True), flush=True)
                server.run(sockets=[listener])
        finally:
            if timer:
                timer.cancel()
            service.close()


if __name__ == "__main__":
    main()
