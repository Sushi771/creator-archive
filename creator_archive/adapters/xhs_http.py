"""Small, serial XHS HTTP source using a locally held, authorised web session.

The adapter reads the platform's user_posted JSON pages and note HTML SSR.
It never opens a browser, copies a browser profile, follows redirects, creates
an account, or treats a missing/blocked page as the end of an author's history.
The optional xhshow signer is a separate MIT-licensed dependency; none of its
code or MediaCrawler's non-commercial code is copied here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
from importlib.metadata import PackageNotFoundError, version as package_version
import os
import ipaddress
import json
import math
from pathlib import Path
import re
import shutil
import socket
import ssl
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import http.client

from creator_archive.validation import AdapterFailure, Item, Page
from creator_archive import network_safety
from hashlib import sha256
from .xhs import MediaCandidate
from .xhs_content import detail_url, project_detail
from .xhs_media import MediaFailure, download_media, safe_media_url


_ID = re.compile(r"[0-9a-f]{24}\Z")
_COOKIE_NAME = re.compile(r"[A-Za-z0-9_\-]+\Z")
_API_HOST = "edith.xiaohongshu.com"
_WEB_HOST = "www.xiaohongshu.com"
_POSTED = "/api/sns/web/v1/user_posted"
_MAX_HTML = 8 * 1024 * 1024
_MAX_JSON = 4 * 1024 * 1024
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36"


def _id(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise AdapterFailure("invalid_stable_id")
    return value


def _cookie_file(path: object) -> Path:
    if not isinstance(path, str) or not path or not Path(path).is_absolute():
        raise ValueError("absolute_private_cookie_file_required")
    result = Path(path).resolve()
    if result.is_relative_to(Path(__file__).resolve().parents[2]):
        raise ValueError("cookie_file_must_be_outside_repository")
    return result


def _read_cookies(path: Path) -> tuple[str, dict[str, str]]:
    try:
        if not path.is_file() or path.stat().st_size > 32768:
            raise OSError()
        raw = path.read_text(encoding="utf-8").strip()
        if raw.startswith("Cookie:"):
            raw = raw[7:].strip()
        if not raw or any(c in raw for c in "\r\n\x00"):
            raise ValueError()
        values: dict[str, str] = {}
        for part in raw.split(";"):
            name, separator, value = part.strip().partition("=")
            if not separator or not _COOKIE_NAME.fullmatch(name) or not value or any(ord(c) < 33 or ord(c) > 126 for c in value):
                raise ValueError()
            values[name] = value
        if not values.get("a1") or not values.get("web_session"):
            raise ValueError()
        return raw, values
    except (OSError, UnicodeError, ValueError):
        # Never expose the path or the cookie material through job errors.
        raise AdapterFailure("needs_login") from None


def _public_addresses(host: str) -> list[str]:
    network_safety.require_xhs_network()
    try:
        addresses = list(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError()
        return addresses
    except (OSError, ValueError):
        raise AdapterFailure("unavailable") from None


def _http_get(host: str, path: str, headers: dict[str, str], *, max_bytes: int) -> bytes:
    """One pinned HTTPS request to an exact platform host; no redirects."""
    network_safety.require_xhs_network()
    if host not in {_API_HOST, _WEB_HOST} or not path.startswith("/") or path.startswith("//"):
        raise AdapterFailure("unavailable")
    address = _public_addresses(host)[0]
    connection = http.client.HTTPSConnection(host, 443, timeout=20, context=ssl.create_default_context())
    status = None
    try:
        # Keep TLS name validation for the requested host after DNS pinning.
        connection.sock = ssl.create_default_context().wrap_socket(
            socket.create_connection((address, 443), timeout=20), server_hostname=host)
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        status = response.status
        if response.status == 401 or response.status in {302, 303} and "/login" in response.getheader("Location", ""):
            raise AdapterFailure("needs_login")
        if response.status == 429:
            raise AdapterFailure("rate_limited", retry_after=60)
        if response.status in {403, 406, 461, 471}:
            raise AdapterFailure("verification_required")
        if host == _WEB_HOST and response.status in {404, 410}:
            # A missing note is an item-level gap. Keep the API's own 404/410
            # as a source failure so a broken author listing cannot pass.
            raise AdapterFailure("item_unavailable")
        if response.status != 200:
            raise AdapterFailure("unavailable")
        length = response.getheader("Content-Length")
        if length and length.isdigit() and int(length) > max_bytes:
            raise AdapterFailure("invalid_response")
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise AdapterFailure("invalid_response")
        return data
    except AdapterFailure as error:
        error.diagnostics = {"http_status": status, "success": None, "business_code": None,
                             "message": "[HTTP failure; no business response retained]",
                             "stage": "list" if host == _API_HOST else "detail"}
        raise
    except (TimeoutError, socket.timeout):
        raise AdapterFailure("timeout") from None
    except (OSError, ssl.SSLError, http.client.HTTPException):
        raise AdapterFailure("unavailable") from None
    finally:
        connection.close()


def _signed_headers(cookies: dict[str, str], params: dict[str, str]) -> dict[str, str]:
    try:
        installed = package_version("xhshow")
        numbers = tuple(int(part) for part in installed.split(".")[:3])
        if numbers < (0, 2, 0):
            raise ValueError()
        from xhshow import Xhshow
        signed = Xhshow().sign_headers_get(uri=_POSTED, cookies=cookies, params=params)
        allowed = {key.lower(): value for key, value in signed.items()
                   if key.lower() in {"x-s", "x-t", "x-s-common", "x-b3-traceid", "x-xray-traceid", "x-mns", "xy-direction"}
                   and isinstance(value, str) and value and "\n" not in value and "\r" not in value}
        if not allowed.get("x-s") or not allowed.get("x-t"):
            raise ValueError()
        return allowed
    except (PackageNotFoundError, ImportError, ValueError, TypeError, KeyError):
        raise AdapterFailure("unavailable") from None
    except Exception:
        # Signer failures are not platform login failures; never log material.
        raise AdapterFailure("unavailable") from None


class _InitialStateScripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.in_script = False
        self.scripts: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.in_script = tag == "script"

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if self.in_script and "__INITIAL_STATE__" in data:
            self.scripts.append(data)


def _json_state(html: bytes) -> dict:
    """Parse the embedded JSON object as data, never execute page JavaScript."""
    try:
        parser = _InitialStateScripts()
        parser.feed(html.decode("utf-8-sig"))
        for script in parser.scripts:
            match = re.search(r"window\.__INITIAL_STATE__\s*=\s*", script)
            if not match:
                continue
            start = script.find("{", match.end())
            if start < 0:
                continue
            depth, quoted, escaped = 0, False, False
            end = None
            for pos in range(start, len(script)):
                char = script[pos]
                if quoted:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        quoted = False
                elif char == '"':
                    quoted = True
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        end = pos + 1
                        break
            if end is None:
                continue
            candidate = script[start:end]
            # XHS's initial state sometimes contains JavaScript undefined.
            candidate = _replace_javascript_literals(candidate)
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
    except (UnicodeError, ValueError, TypeError):
        pass
    raise AdapterFailure("invalid_response") from None


def _replace_javascript_literals(source: str) -> str:
    output: list[str] = []
    quoted, escaped = False, False
    index = 0
    while index < len(source):
        char = source[index]
        if quoted:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            index += 1
            continue
        if char == '"':
            quoted = True
        for literal in ("undefined", "new Map([])"):
            end = index + len(literal)
            if source.startswith(literal, index) and (index == 0 or not (source[index - 1].isalnum() or source[index - 1] == "_")) and (end == len(source) or not (source[end].isalnum() or source[end] == "_")):
                output.append("null")
                index = end
                break
        else:
            output.append(char)
            index += 1
            continue
        continue
    return "".join(output)


def _unwrap(value: object) -> object:
    for _ in range(3):
        if isinstance(value, dict) and "_rawValue" in value:
            value = value["_rawValue"]
        elif isinstance(value, dict) and "value" in value and len(value) == 1:
            value = value["value"]
        else:
            break
    return value


class XhsHttpTransport:
    version = "xhs-author-http-v1"
    latest_coverage = "first_user_posted_page_only; original_platform_history_unknown"

    def __init__(self, config: dict, platform: str, author_id: str):
        if platform != "xiaohongshu" or not isinstance(config, dict):
            raise ValueError("invalid_xhs_http_scope")
        _id(author_id)
        if config.get("kind", "xhs_http") != "xhs_http":
            raise ValueError("invalid_xhs_http_kind")
        self.cookie_file = _cookie_file(config.get("cookie_file"))
        self.platform, self.author_id = platform, author_id
        self._lock = threading.Lock()
        self._last_request = 0.0
        try:
            interval = float(config.get("min_interval_seconds", 1.5))
        except (TypeError, ValueError):
            raise ValueError("invalid_xhs_http_interval") from None
        if not math.isfinite(interval):
            raise ValueError("invalid_xhs_http_interval")
        self._interval = max(1.5, min(interval, 30.0))
        self._refs: dict[str, str] = {}
        self._detail_cache: dict[str, tuple[float, dict]] = {}
        self._page_cache: tuple[str, float, Page] | None = None
        self._closed = False

    def _gate(self):
        network_safety.require_xhs_network()
        with self._lock:
            if self._closed:
                raise AdapterFailure("unavailable")
            wait = self._interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def _get(self, host: str, path: str, *, api_params: dict[str, str] | None = None) -> bytes:
        network_safety.require_xhs_network()
        raw_cookie, cookies = _read_cookies(self.cookie_file)
        headers = {"Accept": "application/json,text/html;q=0.9,*/*;q=0.8", "Accept-Encoding": "identity",
                   "User-Agent": _UA, "Cookie": raw_cookie, "Referer": "https://www.xiaohongshu.com/"}
        if api_params is not None:
            headers.update(_signed_headers(cookies, api_params))
            headers["Origin"] = "https://www.xiaohongshu.com"
        self._gate()
        return _http_get(host, path, headers, max_bytes=_MAX_JSON if api_params is not None else _MAX_HTML)

    def _get_api(self, cursor: str) -> dict:
        params = {"num": "30", "cursor": cursor, "user_id": self.author_id,
                  "image_formats": "jpg,webp,avif", "xsec_token": "", "xsec_source": "pc_feed"}
        # The order and comma encoding match the signed browser-style query.
        path = _POSTED + "?" + urlencode(params, safe=",")
        try:
            result = json.loads(self._get(_API_HOST, path, api_params=params))
            if not isinstance(result, dict):
                raise ValueError()
        except (UnicodeError, ValueError):
            raise AdapterFailure("invalid_response") from None
        code = result.get("code")
        diagnostic = {"http_status": 200, "success": result.get("success") if type(result.get("success")) is bool else None,
                      "business_code": code if type(code) is int or code is None else "[noninteger code redacted]",
                      "stage": "list", "request_cursor_sha256": sha256(cursor.encode()).hexdigest(),
                      "message": "[platform message redacted]",
                      "message_sha256": sha256(str(result.get("msg", result.get("message", ""))).encode()).hexdigest()}
        if code == 300011:
            raise AdapterFailure("verification_required", diagnostics=diagnostic)
        if code == 300012:
            raise AdapterFailure("rate_limited", retry_after=60, diagnostics=diagnostic)
        if result.get("success") is not True or code not in {0, None}:
            raise AdapterFailure("unknown_business_error", diagnostics=diagnostic)
        data = result.get("data")
        if not isinstance(data, dict):
            raise AdapterFailure("invalid_response")
        return data

    def page(self, author_id: str, cursor: str | None) -> Page:
        network_safety.require_xhs_network()
        if author_id != self.author_id:
            raise AdapterFailure("identity_mismatch")
        if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 1024 or any(not 33 <= ord(c) <= 126 for c in cursor)):
            raise AdapterFailure("invalid_cursor")
        key = cursor or ""
        if self._page_cache and self._page_cache[0] == key and time.monotonic() - self._page_cache[1] < 10:
            return self._page_cache[2]
        data = self._get_api(key)
        notes, more, next_cursor = data.get("notes"), data.get("has_more"), data.get("cursor")
        if not isinstance(notes, list) or type(more) is not bool:
            raise AdapterFailure("invalid_response")
        if more and (not notes or not isinstance(next_cursor, str) or not next_cursor or next_cursor == key
                     or len(next_cursor) > 1024 or any(not 33 <= ord(c) <= 126 for c in next_cursor)):
            raise AdapterFailure("missing_cursor")
        # An empty terminal page is indistinguishable from a login/abnormal
        # response in this source. Require a non-empty observed page.
        if not more and not notes:
            raise AdapterFailure("invalid_page")
        items, refs = [], {}
        for raw in notes:
            if not isinstance(raw, dict):
                raise AdapterFailure("invalid_response")
            item_id = _id(raw.get("note_id"))
            user = raw.get("user")
            observed = user.get("user_id", user.get("userId")) if isinstance(user, dict) else None
            if observed != self.author_id:
                raise AdapterFailure("identity_mismatch")
            items.append(Item(item_id, self.author_id, ""))
            token = raw.get("xsec_token", raw.get("xsecToken"))
            if isinstance(token, str) and 0 < len(token) <= 2048 and all(33 <= ord(c) <= 126 for c in token):
                query = {"xsec_token": token}
                source = raw.get("xsec_source", raw.get("xsecSource"))
                if isinstance(source, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", source):
                    query["xsec_source"] = source
                refs[item_id] = detail_url(item_id) + "?" + urlencode(query)
        if more:
            page = Page(tuple(items), next_cursor, True)
        else:
            page = Page(tuple(items), None, False, "xhs user_posted: observed explicit has_more=false")
        self._refs.update(refs)
        self._page_cache = (key, time.monotonic(), page)
        return page

    def prepare_page_details(self, author_id: str, item_ids) -> tuple[str, ...]:
        network_safety.require_xhs_network()
        if author_id != self.author_id:
            raise AdapterFailure("identity_mismatch")
        return tuple(item_id for item_id in item_ids if item_id not in self._refs)

    def prepare_details(self, author_id: str, item_ids) -> tuple[str, ...]:
        network_safety.require_xhs_network()
        if author_id != self.author_id:
            raise AdapterFailure("identity_mismatch")
        if any(item_id not in self._refs for item_id in item_ids):
            self.page(author_id, None)
        return self.prepare_page_details(author_id, item_ids)

    def detail(self, author_id: str, item_id: str, source_url: str = "") -> dict:
        network_safety.require_xhs_network()
        if author_id != self.author_id:
            raise AdapterFailure("identity_mismatch")
        _id(item_id)
        url = self._refs.get(item_id, "")
        if source_url:
            try:
                canonical = detail_url(item_id, source_url)
                parsed = urlsplit(canonical)
                if parsed.path.startswith("/user/profile/") and parsed.path != f"/user/profile/{author_id}/{item_id}":
                    raise ValueError()
                token = parse_qs(parsed.query).get("xsec_token", [])
                if token:
                    if len(token) != 1 or not token[0]:
                        raise ValueError()
                    url = canonical
            except ValueError:
                raise AdapterFailure("reference_missing") from None
        if not url:
            raise AdapterFailure("reference_missing")
        cached = self._detail_cache.get(item_id)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        parsed = urlsplit(url)
        token = parse_qs(parsed.query).get("xsec_token", [])
        if len(token) != 1 or not 0 < len(token[0]) <= 2048 or any(not 33 <= ord(c) <= 126 for c in token[0]):
            raise AdapterFailure("reference_missing")
        query = {"xsec_token": token[0]}
        source = parse_qs(parsed.query).get("xsec_source", [])
        if len(source) == 1 and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", source[0]):
            query["xsec_source"] = source[0]
        html = self._get(_WEB_HOST, parsed.path + "?" + urlencode(query))
        state = _json_state(html)
        note_root = state.get("note")
        detail_map = _unwrap(note_root.get("noteDetailMap")) if isinstance(note_root, dict) else None
        entry = _unwrap(detail_map.get(item_id)) if isinstance(detail_map, dict) else None
        note = _unwrap(entry.get("note")) if isinstance(entry, dict) else None
        if not isinstance(note, dict):
            raise AdapterFailure("item_unavailable")
        user = note.get("user")
        observed = user.get("userId", user.get("user_id")) if isinstance(user, dict) else None
        if note.get("noteId") != item_id or observed != self.author_id:
            raise AdapterFailure("identity_mismatch")
        normalized_note = dict(note)
        normalized_note["user"] = {"userId": observed}
        normalized_note["imageList"] = _unwrap(note.get("imageList"))
        projected = project_detail({"note": normalized_note}, author_id=author_id, item_id=item_id)
        projected["source"] = "xhs_http_ssr"
        images = normalized_note.get("imageList")
        if isinstance(images, list):
            for index, image in enumerate(images):
                if not isinstance(image, dict) or image.get("livePhoto") is not True:
                    continue
                streams = image.get("stream")
                h264 = streams.get("h264") if isinstance(streams, dict) else None
                first = h264[0] if isinstance(h264, list) and h264 and isinstance(h264[0], dict) else None
                video_url = first.get("masterUrl") if first else None
                if not isinstance(video_url, str):
                    continue
                try:
                    playable = safe_media_url(video_url, resolve=False)
                except MediaFailure:
                    continue
                projected["media"].append(MediaCandidate("video", index, playable))
                unresolved = f"live_photo_{index}_video_unresolved"
                projected["missing"] = [value for value in projected["missing"] if value != unresolved]
        updated = note.get("lastUpdateTime")
        if type(updated) is int and 0 < updated < 32503680000000:
            projected["updated_at"] = datetime.fromtimestamp(updated / 1000, timezone.utc).isoformat()
        video = note.get("video")
        media = video.get("media") if isinstance(video, dict) else None
        original = media.get("video") if isinstance(media, dict) else None
        digest = original.get("md5") if isinstance(original, dict) else None
        if isinstance(digest, str) and re.fullmatch(r"[0-9a-fA-F]{32}", digest):
            projected["video_md5_observed"] = digest.lower()
        self._detail_cache[item_id] = (time.monotonic(), projected)
        return projected

    def poll_latest(self) -> list[dict]:
        network_safety.require_xhs_network()
        self._detail_cache.clear()
        page = self.page(self.author_id, None)
        if not page.items:
            raise AdapterFailure("invalid_page")
        result = []
        for item in page.items:
            try:
                result.append(self.detail(self.author_id, item.item_id))
            except AdapterFailure as error:
                if error.category not in {"reference_missing", "item_unavailable"}:
                    raise
                result.append({"item_id": item.item_id, "author_id": self.author_id,
                               "title": "", "text": "", "published_at": "", "content_type": "unknown",
                               "source": "xhs_http_ssr", "observed_at": time.time(),
                               "source_url": detail_url(item.item_id), "media": [],
                               "missing": [error.category], "metrics": {}})
        return result

    def verify_author(self, author_id: str) -> dict:
        network_safety.require_xhs_network()
        page = self.page(author_id, None)
        if not page.items:
            raise AdapterFailure("identity_mismatch")
        return {"author_id": author_id, "display_name": author_id,
                "evidence": "observed_user_posted_items_with_stable_author_id"}

    def download_media(self, candidate: MediaCandidate, target_dir: Path) -> dict:
        network_safety.require_xhs_network()
        self._gate()
        try:
            return download_media(candidate, target_dir)
        except MediaFailure as error:
            if str(error) == "media_rate_limited":
                raise AdapterFailure("rate_limited", retry_after=60, diagnostics=error.diagnostics) from None
            raise AdapterFailure("media_failed", diagnostics=error.diagnostics) from None

    def close(self):
        self._closed = True
        self._refs.clear()
        self._detail_cache.clear()
        self._page_cache = None


def authorize_session(cookie_file: Path, *, channel: str = "msedge") -> None:
    """Open the official site once for the account holder, then save local cookies.

    No scraping or profile scrolling runs in this browser. The window closes
    after the account holder confirms login in the terminal. This is an
    optional explicit action, never called by page(), detail(), or refresh().
    """
    network_safety.require_browser_disabled()
    target = _cookie_file(str(cookie_file))
    if channel not in {"msedge", "chrome"}:
        raise ValueError("unsupported_browser_channel")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise AdapterFailure("unavailable") from None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel, headless=False)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded", timeout=30000)
            input("请在官方窗口由本人完成登录，完成后回到此终端按 Enter；取消可按 Ctrl+C。")
            cookies = context.cookies(["https://www.xiaohongshu.com/"])
            selected = {cookie["name"]: cookie["value"] for cookie in cookies
                        if (cookie.get("domain", "").lstrip(".") == "xiaohongshu.com"
                            or cookie.get("domain", "").lstrip(".").endswith(".xiaohongshu.com"))
                        and _COOKIE_NAME.fullmatch(cookie.get("name", ""))
                        and isinstance(cookie.get("value"), str) and cookie["value"]
                        and all(33 <= ord(c) <= 126 and c != ";" for c in cookie["value"])}
            if not selected.get("a1") or not selected.get("web_session"):
                raise AdapterFailure("needs_login")
            content = "; ".join(f"{name}={value}" for name, value in selected.items()) + "\n"
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{time.time_ns()}.tmp")
            try:
                with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                if target.exists():
                    backup = target.with_name(f"{target.name}.{time.time_ns()}.bak")
                    shutil.copy2(target, backup)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        finally:
            browser.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="本人在官方小红书窗口完成一次登录，保存仅本机使用的会话文件")
    parser.add_argument("--cookie-file", required=True, type=Path, help="仓库外的绝对私有文件路径")
    parser.add_argument("--channel", choices=("msedge", "chrome"), default="msedge")
    args = parser.parse_args()
    try:
        authorize_session(args.cookie_file, channel=args.channel)
    except AdapterFailure as error:
        parser.exit(1, f"授权未完成：{error.category}；未改变现有来源配置。\n")
    print("本人会话已保存到指定的本机私有文件；请在 Creator Archive 私有来源配置中引用该路径。")
