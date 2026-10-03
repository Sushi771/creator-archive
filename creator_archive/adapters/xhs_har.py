"""Read user-supplied XHS HAR captures, without replaying HTTP or credentials.

This verifies a captured pagination chain, not an authenticated live transport.
Only allowlisted IDs, cursors and response status survive normalization.
"""

import base64
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from .xhs import XhsPageAdapter
from ..validation import AdapterFailure


ENDPOINT = "/api/sns/web/v1/user_posted"
MAX_HAR_BYTES = 128 * 1024 * 1024


def _identifier(value, *, empty=False):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{24}", value):
        if empty and value == "":
            return value
        raise ValueError("invalid_capture_id_or_cursor")
    return value


def read_har(path: Path, authors: set[str]) -> tuple[list[dict], str]:
    """Project known-author list responses out of a HAR; ignore unrelated traffic.

    Failed/missing responses remain explicit records so they cannot look like
    an empty terminal page. No response body, header, full URL or token is saved.
    """
    with path.open("rb") as stream:
        raw = stream.read(MAX_HAR_BYTES + 1)
    if len(raw) > MAX_HAR_BYTES:
        raise ValueError("har_too_large")
    try:
        entries = json.loads(raw.decode("utf-8-sig"))["log"]["entries"]
        if not isinstance(entries, list):
            raise ValueError
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError("invalid_har") from error
    records = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        request = entry.get("request", {})
        if not isinstance(request, dict) or not isinstance(request.get("url"), str):
            continue
        try:
            url = urlsplit(request["url"])
            if (url.scheme != "https" or url.hostname not in {"edith.xiaohongshu.com", "www.xiaohongshu.com"}
                    or url.port not in {None, 443} or url.username or url.password or url.path != ENDPOINT):
                continue
            query = parse_qs(url.query, keep_blank_values=True)
        except ValueError:
            continue
        author = query.get("user_id", [None])[0]
        if author not in authors:
            continue
        if (request.get("method") != "GET" or len(query.get("user_id", [])) != 1
                or len(query.get("cursor", [])) != 1):
            raise ValueError("ambiguous_capture_request")
        _identifier(author)
        cursor = _identifier(query["cursor"][0], empty=True)
        try:
            observed = datetime.fromisoformat(entry["startedDateTime"].replace("Z", "+00:00"))
            if observed.tzinfo is None:
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise ValueError("capture_time_missing") from error
        response = entry.get("response", {})
        if not isinstance(response, dict):
            response = {}
        status = response.get("status")
        record = {"author_id": author, "request_cursor": cursor,
                  "observed_at": observed.isoformat(), "http_status": status if type(status) is int else 0}
        if type(status) is not int or status != 200:
            record["error"] = {401: "needs_login", 429: "rate_limited"}.get(record["http_status"], "unavailable")
        else:
            try:
                content = response["content"]
                body = content["text"]
                if content.get("encoding") == "base64":
                    body = base64.b64decode(body, validate=True).decode("utf-8")
                elif content.get("encoding") not in {None, ""}:
                    raise ValueError
                payload = json.loads(body)
                if payload.get("success") is not True or type(payload.get("code")) is not int or payload["code"] != 0:
                    raise ValueError
                page = XhsPageAdapter(lambda _a, _c: payload).page(author, cursor or None)
                # Preserve an API's leftover terminal cursor as evidence, not a next request.
                api_cursor = payload["data"].get("cursor")
                if api_cursor is not None:
                    _identifier(api_cursor, empty=True)
                for item in page.items:
                    _identifier(item.item_id)
                record["response"] = {"success": True, "code": 0, "data": {
                    "notes": [{"note_id": item.item_id, "user": {"user_id": author}} for item in page.items],
                    "has_more": page.has_more, "cursor": api_cursor}}
            except (ValueError, KeyError, TypeError, AttributeError, AdapterFailure):
                record["error"] = "unavailable"
        records.append(record)
    if not records:
        raise ValueError("no_matching_user_posted_responses")
    return records, sha256(raw).hexdigest()


class CapturedXhsAdapter:
    """Choose only an exact captured request cursor. Missing pages stop the run."""

    version = "xhs-har-captured-chain-v1"

    def __init__(self, records: list[dict]):
        self.records = {}
        for record in records:
            key = (record["author_id"], record["request_cursor"])
            self.records.setdefault(key, []).append(record)
        self.stop_reasons = {}
        self.adapter = XhsPageAdapter(self.fetch)

    def fetch(self, author: str, cursor: str):
        options = self.records.get((author, cursor), [])
        successful = [r for r in options if "response" in r]
        variants = {json.dumps(r["response"], sort_keys=True) for r in successful}
        if len(variants) > 1:
            self.stop_reasons[author] = "conflicting_captured_pages"
            raise AdapterFailure("unavailable")
        if successful:
            return successful[0]["response"]
        if options:
            latest = max(options, key=lambda r: datetime.fromisoformat(r["observed_at"]))
            self.stop_reasons[author] = "captured_" + latest["error"]
            # A HAR is historical: do not simulate a current HTTP cooldown or login.
        else:
            self.stop_reasons[author] = "missing_captured_page"
        raise AdapterFailure("unavailable")

    def page(self, author_id, cursor):
        return self.adapter.page(author_id, cursor)
