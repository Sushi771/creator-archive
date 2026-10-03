"""Experimental checkpoint consumer for tool-observed XHS browser responses.

The browser driver is external. This does not restart a browser, sign requests,
or establish that an unattended production transport exists.
"""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4

from creator_archive.adapters.xhs import XhsPageAdapter
from creator_archive.validation import AdapterFailure
from creator_archive.workflow import ArchiveWorkflow
from scripts.verify_xhs_har import atomic_json


FORMAT = "xhs-observed-browser-v1"


def validate_record(record, author, cursor, ticket=None):
    """Reject mismatches before the workflow transaction, including stale input."""
    if not isinstance(record, dict):
        raise ValueError("invalid_response")
    if record.get("authorId") != author or record.get("requestCursor") != cursor:
        raise ValueError("identity_mismatch")
    if type(record.get("httpStatus")) is int and record["httpStatus"] in (401, 429):
        raise AdapterFailure("needs_login" if record["httpStatus"] == 401 else "rate_limited", retry_after=60)
    if (type(record.get("httpStatus")) is not int or record["httpStatus"] != 200
            or record.get("success") is not True or type(record.get("code")) is not int
            or record["code"] != 0):
        raise AdapterFailure("unavailable")
    if ticket:
        if record.get("ticket") != ticket["ticket"]:
            raise ValueError("stale_checkpoint")
        try:
            captured = datetime.fromisoformat(record["capturedAt"].replace("Z", "+00:00"))
            requested = datetime.fromisoformat(ticket["requestedAt"])
            if captured.tzinfo is None or captured < requested:
                raise ValueError("stale_checkpoint")
        except (KeyError, TypeError, ValueError):
            raise ValueError("stale_checkpoint") from None
    data = record.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("notes"), list):
        raise ValueError("invalid_response")
    if data.get("has_more") and not re.fullmatch(r"[0-9a-f]{24}", str(data.get("cursor", ""))):
        raise ValueError("missing_cursor")
    for note in data["notes"]:
        if (not isinstance(note, dict) or not re.fullmatch(r"[0-9a-f]{24}", str(note.get("note_id", "")))
                or not isinstance(note.get("user"), dict) or note["user"].get("user_id") != author):
            raise ValueError("identity_mismatch")
    # SSR is an explicitly labelled first-page seed, never API terminal evidence.
    if record.get("source") == "server_rendered_document" and (cursor or data.get("has_more") is not True):
        raise ValueError("invalid_response")
    return {"success": True, "code": 0, "data": {
        "cursor": data.get("cursor"), "has_more": data.get("has_more"),
        "notes": [{"note_id": n["note_id"], "user": {"user_id": author}} for n in data["notes"]]}}


class BrowserInboxAdapter(XhsPageAdapter):
    version = FORMAT

    def __init__(self, root, seed, timeout):
        self.root, self.timeout = Path(root), timeout
        self.seed = {}
        for record in seed:
            key = (record["authorId"], record["requestCursor"])
            validate_record(record, *key)
            if key in self.seed and self.seed[key] != record:
                raise ValueError("conflicting_seed")
            self.seed[key] = record
        super().__init__(self.fetch)

    def fetch(self, author, cursor):
        from creator_archive import network_safety
        network_safety.require_browser_disabled()
        if (author, cursor) in self.seed:
            return validate_record(self.seed[(author, cursor)], author, cursor)
        ticket = {"ticket": uuid4().hex, "authorId": author, "requestCursor": cursor,
                  "requestedAt": datetime.now(timezone.utc).isoformat(), "pid": os.getpid()}
        atomic_json(self.root / "pending.json", ticket)
        print(json.dumps({"event": "awaiting_browser_response", "pid": os.getpid(),
                          "cursor_sha256": sha256(cursor.encode()).hexdigest()}), flush=True)
        response_path = self.root / (ticket["ticket"] + ".json")
        deadline = time.monotonic() + self.timeout
        while not response_path.exists():
            if time.monotonic() >= deadline:
                raise AdapterFailure("timeout")
            time.sleep(0.2)
        if response_path.stat().st_size > 1024 * 1024:
            raise ValueError("invalid_response")
        record = json.loads(response_path.read_text(encoding="utf-8"))
        return validate_record(record, author, cursor, ticket)


def main():
    parser = argparse.ArgumentParser(description="实验：保留浏览器会话，由工具输送真实分页响应；不代表独立采集产品。")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--seed", type=Path)
    parser.add_argument("--sample", choices=["XHS-A", "XHS-B"], required=True)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--login-confirmed", action="store_true")
    args = parser.parse_args()
    private = Path(os.environ["LOCALAPPDATA"]) / "CreatorArchive/private-validation/G1-2026-09-26"
    root = args.root.resolve()
    if not root.is_relative_to(private.resolve()) or root == private.resolve():
        parser.error("Use a new subdirectory within the private LOCALAPPDATA evidence root.")
    if args.max_pages < 1 or args.timeout <= 0:
        parser.error("Page budget and timeout must be positive.")
    marker = root / "consumer.json"
    # Seed evidence may be alongside a new DB; never adopt an existing database.
    if (root / "archive.sqlite3").exists() and not marker.exists():
        parser.error("Existing unrecognized database; choose another directory.")
    if root.exists() and not marker.exists():
        allowed = {args.seed.resolve()} if args.seed else set()
        if any(path.resolve() not in allowed for path in root.iterdir()):
            parser.error("Existing unrelated files; choose a new private subdirectory.")
    samples = json.loads((private / "samples.json").read_text(encoding="utf-8"))
    author = samples[args.sample]["author_id"]
    if not re.fullmatch(r"[0-9a-f]{24}", author):
        parser.error("Invalid sample identity.")
    identity = {"format": FORMAT, "authorId": author, "sample": args.sample}
    if marker.exists() and json.loads(marker.read_text(encoding="utf-8")) != identity:
        parser.error("Existing consumer identity differs; original data preserved.")
    seed = json.loads(args.seed.read_text(encoding="utf-8")) if args.seed else []
    if any(r.get("authorId") != author for r in seed):
        parser.error("Seed contains another author.")
    adapter = BrowserInboxAdapter(root, seed, args.timeout)
    root.mkdir(parents=True, exist_ok=True)
    atomic_json(marker, identity)
    workflow = ArchiveWorkflow(root)
    workflow.subscribe("xiaohongshu", author, args.sample, verified=True,
                       evidence="prior_sample_identity_and_observed_browser_response")
    start = datetime.now(timezone.utc).isoformat()
    before = workflow.status()
    result = workflow.run_all({"xiaohongshu": adapter}, batch_id=before["batch_id"], max_pages=args.max_pages,
                              login_confirmed=args.login_confirmed)
    report = {"pid": os.getpid(), "startedAt": start, "finishedAt": datetime.now(timezone.utc).isoformat(),
              "before": before, "after": result, "g1_passed": False,
              "transport": "external_tool_driver_retained_browser", "browser_restart_verified": False}
    atomic_json(root / f"process-{os.getpid()}-{uuid4().hex[:8]}.json", report)
    print(json.dumps({"event": "consumer_stopped", "pid": os.getpid(), "sample": args.sample,
                      "runs": [{"pages": r["pages"], "ids": r["item_count"], "state": r["state"],
                                "reason": r["reason"], "next_step": next_step(r)} for r in result["runs"]]}), flush=True)


def next_step(run):
    if run["state"] == "succeeded":
        return "已到当前响应链末页；正文媒体及浏览器重启恢复须单独验证。"
    prefix = f"作者列表阶段已保留{run['pages']}页；"
    return prefix + {
        "needs_login": "请在验证浏览器自行登录，再以--login-confirmed继续同一目录。",
        "rate_limited": "等待平台冷却后继续同一目录；不要轮换账号或IP。",
        "page_budget_reached": "本次页数预算已用完，可从同一目录继续，尚未到末页。",
        "timeout": "尚未收到匹配的新响应；核对浏览器作者和游标后继续。",
        "identity_mismatch": "拒收作者或游标不匹配的页；检查本次请求，勿取旧回调结果。",
        "stale_checkpoint": "拒收过期票据或响应；按新的pending.json重新观察请求。",
    }.get(run["reason"], "原因尚未确认；核对该游标的HTTP状态及响应字段后继续。")


if __name__ == "__main__":
    main()
