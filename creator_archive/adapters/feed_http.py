"""Bounded HTTP feed source. A feed is evidence of its own window, not of platform history.

JSON Feed may supply ``next_url`` and an explicit ``history_complete: true``
extension. RSS and Atom have no trustworthy historical terminal signal here.
No browser, cookie import, redirects, or automatic upstream refresh is used.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import tempfile
import time
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from creator_archive.validation import AdapterFailure, Item, Page
from .xhs import MediaCandidate


MAX_FEED_BYTES = 8 * 1024 * 1024
MAX_MEDIA_BYTES = 1024 * 1024 * 1024
TIMEOUT = 20
_XHS_ID = re.compile(r"[0-9a-f]{24}")
_SENSITIVE_QUERY = re.compile(r"(?:token|key|secret|auth|password|passwd|signature|cookie|credential|session|ticket|code)", re.I)


def validate_source_url(url: str) -> str:
    """Accept explicit local services or public HTTPS; return a fragment-free URL.

    DNS is checked again and the selected address is pinned when connecting.
    Query strings are permitted for feed APIs but never included in errors.
    """
    if not isinstance(url, str) or len(url) > 4096 or any(not 33 <= ord(c) <= 126 for c in url):
        raise ValueError("invalid_source_url")
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        raise ValueError("invalid_source_url") from None
    local = host in {"127.0.0.1", "localhost"}
    if (not host or parsed.username is not None or parsed.password is not None
            or (local and parsed.scheme not in {"http", "https"})
            or (not local and (parsed.scheme != "https" or port not in {None, 443}))
            or parsed.path.startswith("//") or "\\" in parsed.path):
        raise ValueError("invalid_source_url")
    if not local:
        if host.endswith((".localhost", ".local", ".internal")):
            raise ValueError("private_source_address")
        try:
            if not ipaddress.ip_address(host).is_global:
                raise ValueError("private_source_address")
        except ValueError as error:
            if str(error) == "private_source_address":
                raise
    _addresses(host, port or (443 if parsed.scheme == "https" else 80), local)
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


def _addresses(host: str, port: int, local: bool) -> list[str]:
    try:
        found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(row[4][0] for row in found))
        if not addresses:
            raise OSError()
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if local and not ip.is_loopback or not local and not ip.is_global:
                raise ValueError("private_source_address")
        return addresses
    except OSError:
        raise ValueError("source_dns_unavailable") from None


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _request(url: str):
    try:
        validated = validate_source_url(url)
    except ValueError:
        raise AdapterFailure("unavailable") from None
    parsed = urlsplit(validated)
    host = parsed.hostname or ""
    local = host in {"localhost", "127.0.0.1"}
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        address = _addresses(host, port, local)[0]
    except ValueError:
        raise AdapterFailure("unavailable") from None
    connection = (_PinnedHTTPS if parsed.scheme == "https" else _PinnedHTTP)(host, port, address, TIMEOUT)
    try:
        connection.request("GET", urlunsplit(("", "", parsed.path, parsed.query, "")),
                           headers={"Accept": "application/feed+json, application/json, application/atom+xml, application/rss+xml, application/xml, text/xml, */*",
                                    "Accept-Encoding": "identity", "User-Agent": "CreatorArchive/FeedHTTP"})
        response = connection.getresponse()
        if response.status in {401, 403}:
            raise AdapterFailure("needs_login" if response.status == 401 else "verification_required")
        if response.status == 429:
            raise AdapterFailure("rate_limited", retry_after=60)
        if response.status != 200:  # Includes every redirect; never follow one.
            raise AdapterFailure("unavailable")
        return connection, response
    except AdapterFailure:
        connection.close()
        raise
    except (TimeoutError, socket.timeout):
        connection.close()
        raise AdapterFailure("timeout") from None
    except (OSError, http.client.HTTPException, ssl.SSLError):
        connection.close()
        raise AdapterFailure("unavailable") from None
    except Exception:
        connection.close()
        raise


def _get(url: str, max_bytes: int) -> tuple[bytes, str]:
    connection, response = _request(url)
    try:
        length = response.getheader("Content-Length")
        if length and length.isdigit() and int(length) > max_bytes:
            raise AdapterFailure("invalid_response")
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise AdapterFailure("invalid_response")
        return data, response.getheader("Content-Type", "")
    except (OSError, http.client.HTTPException):
        raise AdapterFailure("unavailable") from None
    finally:
        connection.close()


def _official_profile(platform: str, url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443}:
            return ""
    except ValueError:
        return ""
    if platform == "xiaohongshu" and host in {"www.xiaohongshu.com", "xiaohongshu.com"}:
        match = re.fullmatch(r"/user/profile/([0-9a-f]{24})/?", parsed.path)
        return match[1] if match else ""
    if platform == "wechat" and host == "mp.weixin.qq.com" and parsed.path == "/mp/profile_ext":
        biz = parse_qs(parsed.query).get("__biz", [])
        return biz[0] if len(biz) == 1 and re.fullmatch(r"[A-Za-z0-9_+/=-]{1,128}", biz[0]) else ""
    return ""


def _official_item(platform: str, url: str, author_id: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443}:
            raise ValueError()
    except ValueError:
        raise AdapterFailure("invalid_stable_id") from None
    if platform == "xiaohongshu" and host in {"www.xiaohongshu.com", "xiaohongshu.com"}:
        match = re.fullmatch(r"/(?:explore|discovery/item)/([0-9a-f]{24})/?", parsed.path)
        scoped = re.fullmatch(r"/user/profile/([0-9a-f]{24})/([0-9a-f]{24})/?", parsed.path)
        if scoped and scoped[1] != author_id:
            raise AdapterFailure("identity_mismatch")
        item_id = scoped[2] if scoped else match[1] if match else ""
        if item_id:
            return item_id, f"https://www.xiaohongshu.com/explore/{item_id}"
    if platform == "wechat" and host == "mp.weixin.qq.com" and parsed.path == "/s":
        query = parse_qs(parsed.query)
        parts = [query.get(key, []) for key in ("__biz", "mid", "idx", "sn")]
        if all(len(part) == 1 and part[0] for part in parts):
            biz, mid, idx, sn = (part[0] for part in parts)
            if biz != author_id:
                raise AdapterFailure("identity_mismatch")
            if re.fullmatch(r"\d+", mid) and re.fullmatch(r"\d+", idx) and re.fullmatch(r"[0-9a-fA-F]{32}", sn):
                item_id = "wx-" + sha256(f"{biz}\0{mid}\0{idx}".encode()).hexdigest()
                canonical = "https://mp.weixin.qq.com/s?" + urlencode({"__biz": biz, "mid": mid, "idx": idx, "sn": sn})
                return item_id, canonical
    raise AdapterFailure("invalid_stable_id")


class _Body(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.words: list[str] = []
        self.media: list[tuple[str, str]] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "iframe"}:
            self.skip += 1
        if tag == "img":
            self.media.append(("image", urljoin(self.base, attrs.get("src") or attrs.get("data-src") or "")))
        if tag in {"video", "source"}:
            source = attrs.get("src")
            if source:
                self.media.append(("video", urljoin(self.base, source)))
        if tag in {"p", "br", "div", "li", "h1", "h2", "h3"}:
            self.words.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "iframe"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.words.append(data)


def _text(element: ET.Element, local: str) -> str:
    for child in element:
        if child.tag.rsplit("}", 1)[-1] == local and child.text:
            return child.text.strip()
    return ""


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (ValueError, TypeError, IndexError):
            return ""
    if parsed.tzinfo is None:
        return ""
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class _ParsedFeed:
    details: tuple[dict, ...]
    next_url: str | None
    history_complete: bool
    format: str


class FeedHttpTransport:
    version = "http-feed-v1"
    latest_coverage = "feed_window_only; original_platform_history_unknown"

    def __init__(self, config: dict, platform: str, author_id: str):
        if platform not in {"xiaohongshu", "wechat"} or not isinstance(author_id, str):
            raise ValueError("invalid_feed_scope")
        if not isinstance(config, dict) or not isinstance(config.get("url"), str):
            raise ValueError("feed_url_required")
        self.url = validate_source_url(config["url"])
        self.format = config.get("format", "auto")
        if self.format not in {"auto", "json", "rss", "atom"}:
            raise ValueError("invalid_feed_format")
        self.platform, self.author_id = platform, author_id
        self._cache: dict[str, dict] = {}
        self._latest_cache: tuple[float, _ParsedFeed] | None = None

    def _check_cursor_scope(self, url: str) -> str:
        try:
            url = validate_source_url(url)
            base, parsed = urlsplit(self.url), urlsplit(url)
            prefix = base.path.rstrip("/")
            if ((base.scheme, base.hostname, base.port) != (parsed.scheme, parsed.hostname, parsed.port)
                    or not (parsed.path == base.path or parsed.path.startswith(prefix + "/"))):
                raise ValueError()
            return url
        except ValueError:
            raise AdapterFailure("invalid_cursor") from None

    def _cursor_url(self, cursor: str | None) -> str:
        if cursor is None:
            return self.url
        url = self._check_cursor_scope(cursor)
        parsed = urlsplit(url)
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        if any(_SENSITIVE_QUERY.search(key) for key, _ in pairs):
            raise AdapterFailure("invalid_cursor")
        fixed = [(key, value) for key, value in parse_qsl(urlsplit(self.url).query, keep_blank_values=True)
                 if _SENSITIVE_QUERY.search(key)]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(pairs + fixed), ""))

    def _cursor_from_next(self, url: str) -> str:
        scoped = self._check_cursor_scope(url)
        parsed = urlsplit(scoped)
        fixed = [(key, value) for key, value in parse_qsl(urlsplit(self.url).query, keep_blank_values=True)
                 if _SENSITIVE_QUERY.search(key)]
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        supplied = [(key, value) for key, value in pairs if _SENSITIVE_QUERY.search(key)]
        if supplied and supplied != fixed:
            raise AdapterFailure("invalid_cursor")
        public = [(key, value) for key, value in pairs if not _SENSITIVE_QUERY.search(key)]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(public), ""))

    def _load(self, cursor: str | None = None) -> _ParsedFeed:
        if cursor is None and self._latest_cache and time.monotonic() - self._latest_cache[0] < 10:
            return self._latest_cache[1]
        url = self._cursor_url(cursor)
        raw, content_type = _get(url, MAX_FEED_BYTES)
        try:
            fmt = self.format
            if fmt == "auto":
                fmt = "json" if raw.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"{") else "xml"
            if fmt == "json":
                data = json.loads(raw.decode("utf-8-sig"))
                if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                    raise ValueError()
                profile = data.get("home_page_url", "")
                if _official_profile(self.platform, profile) != self.author_id:
                    raise AdapterFailure("identity_mismatch")
                details = tuple(self._project(entry, url) for entry in data["items"])
                next_url = data.get("next_url")
                if next_url is not None and (not isinstance(next_url, str) or not next_url):
                    raise ValueError()
                if next_url is not None:
                    next_url = self._cursor_from_next(urljoin(url, next_url))
                complete = data.get("history_complete") is True
                if complete and next_url:
                    raise ValueError()
                parsed = _ParsedFeed(details, next_url, complete, "json")
                if cursor is None:
                    self._latest_cache = (time.monotonic(), parsed)
                return parsed
            root = ET.fromstring(raw)
            root_name = root.tag.rsplit("}", 1)[-1].lower()
            if fmt == "rss" and root_name != "rss" or fmt == "atom" and root_name != "feed":
                raise ValueError()
            if root_name == "rss":
                channel = root.find("channel")
                if channel is None:
                    raise ValueError()
                profile = _text(channel, "link")
                entries = channel.findall("item")
            elif root_name == "feed":
                links = [e for e in root if e.tag.rsplit("}", 1)[-1] == "link" and e.attrib.get("rel", "alternate") == "alternate"]
                profile = next((e.attrib.get("href", "") for e in links), "")
                entries = [e for e in root if e.tag.rsplit("}", 1)[-1] == "entry"]
            else:
                raise ValueError()
            if _official_profile(self.platform, profile) != self.author_id:
                raise AdapterFailure("identity_mismatch")
            parsed = _ParsedFeed(tuple(self._project_xml(entry, root_name, url) for entry in entries), None, False, root_name)
            if cursor is None:
                self._latest_cache = (time.monotonic(), parsed)
            return parsed
        except AdapterFailure:
            raise
        except (UnicodeError, ValueError, TypeError, ET.ParseError, KeyError):
            raise AdapterFailure("invalid_response") from None

    def _project_xml(self, entry: ET.Element, fmt: str, feed_url: str) -> dict:
        content = None
        if fmt == "rss":
            link = _text(entry, "link") or _text(entry, "guid")
            body = next((e.text or "" for e in entry if e.tag.rsplit("}", 1)[-1] == "encoded"), "") or _text(entry, "description")
            published = _text(entry, "pubDate")
        else:
            links = [e for e in entry if e.tag.rsplit("}", 1)[-1] == "link" and e.attrib.get("rel", "alternate") == "alternate"]
            link = next((e.attrib.get("href", "") for e in links), "")
            content = next((e for e in entry if e.tag.rsplit("}", 1)[-1] == "content"), None)
            if content is None:
                content = next((e for e in entry if e.tag.rsplit("}", 1)[-1] == "summary"), None)
            body = " ".join(content.itertext()) if content is not None and len(content) else (content.text or "" if content is not None else "")
            published = _text(entry, "published") or _text(entry, "updated")
        extra = []
        if fmt == "feed" and content is not None and len(content):
            for child in content.iter():
                tag = child.tag.rsplit("}", 1)[-1]
                if tag in {"img", "video", "source"} and child.attrib.get("src"):
                    extra.append(("image" if tag == "img" else "video", child.attrib["src"]))
        for child in entry:
            if child.tag.rsplit("}", 1)[-1] == "enclosure":
                kind = "video" if child.attrib.get("type", "").startswith("video/") else "image" if child.attrib.get("type", "").startswith("image/") else ""
                if kind:
                    extra.append((kind, child.attrib.get("url", "")))
            if child.tag.rsplit("}", 1)[-1] == "link" and child.attrib.get("rel") == "enclosure":
                kind = "video" if child.attrib.get("type", "").startswith("video/") else "image" if child.attrib.get("type", "").startswith("image/") else ""
                if kind:
                    extra.append((kind, child.attrib.get("href", "")))
        return self._detail_record(link, _text(entry, "title"), body, published, extra, feed_url)

    def _project(self, entry: dict, feed_url: str) -> dict:
        if not isinstance(entry, dict):
            raise AdapterFailure("invalid_response")
        author = entry.get("author")
        authors = entry.get("authors")
        author_links = ([author.get("url", "")] if isinstance(author, dict) else []) + ([a.get("url", "") for a in authors if isinstance(a, dict)] if isinstance(authors, list) else [])
        if any(_official_profile(self.platform, link) != self.author_id for link in author_links if link):
            raise AdapterFailure("identity_mismatch")
        item_author_verified = any(_official_profile(self.platform, link) == self.author_id
                                   for link in author_links if link)
        extra = []
        for attachment in entry.get("attachments", []):
            if not isinstance(attachment, dict):
                continue
            mime = attachment.get("mime_type", "")
            kind = "video" if mime.startswith("video/") else "image" if mime.startswith("image/") else ""
            if kind:
                extra.append((kind, attachment.get("url", "")))
        body = entry.get("content_html") or entry.get("content_text") or entry.get("summary") or ""
        return self._detail_record(entry.get("url", ""), entry.get("title", ""), body,
                                   entry.get("date_published", ""), extra, feed_url,
                                   item_author_verified=item_author_verified)

    def _detail_record(self, link, title, body, published, extra, feed_url, *, item_author_verified=False) -> dict:
        if not isinstance(link, str) or not isinstance(body, str):
            raise AdapterFailure("invalid_response")
        absolute_link = urljoin(feed_url, link)
        item_id, source_url = _official_item(self.platform, absolute_link, self.author_id)
        if self.platform == "xiaohongshu" and not item_author_verified and not re.fullmatch(
                rf"/user/profile/{re.escape(self.author_id)}/{re.escape(item_id)}/?",
                urlsplit(absolute_link).path):
            raise AdapterFailure("identity_mismatch")
        parser = _Body(source_url)
        parser.feed(body)
        words = "\n".join(" ".join(line.split()) for line in "".join(parser.words).splitlines() if line.strip())
        candidates: list[MediaCandidate] = []
        missing: list[str] = []
        seen = set()
        indexes = {"image": 0, "video": 0}
        for kind, media_url in parser.media + extra:
            if not isinstance(media_url, str) or not media_url or media_url in seen:
                continue
            seen.add(media_url)
            try:
                valid = self._media_url(urljoin(source_url, media_url))
            except ValueError:
                missing.append(f"{kind}_{indexes[kind]}_unsafe_source")
            else:
                candidates.append(MediaCandidate(kind, indexes[kind], valid))
            indexes[kind] += 1
        if not words:
            missing.append("body_missing")
        return {"item_id": item_id, "author_id": self.author_id,
                "title": title if isinstance(title, str) else "", "text": words,
                "published_at": _timestamp(published),
                "content_type": "video" if any(c.kind == "video" for c in candidates) else "image" if any(c.kind == "image" for c in candidates) else "unknown",
                "source": "http_feed", "observed_at": time.time(), "source_url": source_url,
                "media": candidates, "missing": missing, "metrics": {}}

    def _media_url(self, url: str) -> str:
        valid = validate_source_url(url)
        media, configured = urlsplit(valid), urlsplit(self.url)
        if media.hostname in {"localhost", "127.0.0.1"}:
            if ((media.scheme, media.hostname, media.port) !=
                    (configured.scheme, configured.hostname, configured.port)):
                raise ValueError("local_media_source_not_configured")
        return valid

    def _remember(self, parsed: _ParsedFeed):
        for detail in parsed.details:
            old = self._cache.get(detail["item_id"])
            if old and old["author_id"] != detail["author_id"]:
                raise AdapterFailure("identity_mismatch")
            self._cache[detail["item_id"]] = detail

    def page(self, author_id: str, cursor: str | None) -> Page:
        if author_id != self.author_id:
            raise AdapterFailure("identity_mismatch")
        parsed = self._load(cursor)
        if parsed.format != "json":
            raise AdapterFailure("missing_terminal_evidence")
        self._remember(parsed)
        items = tuple(Item(d["item_id"], author_id, d["published_at"]) for d in parsed.details)
        if parsed.next_url:
            if not items or parsed.next_url == (cursor or self.url):
                raise AdapterFailure("invalid_cursor")
            return Page(items, parsed.next_url, True)
        if parsed.history_complete:
            if cursor is None and not items:
                raise AdapterFailure("invalid_page")
            return Page(items, None, False, "json_feed: explicit history_complete=true")
        raise AdapterFailure("missing_terminal_evidence")

    def poll_latest(self) -> list[dict]:
        parsed = self._load(None)
        self._remember(parsed)
        return list(parsed.details)

    def detail(self, author_id: str, item_id: str, source_url: str = "") -> dict:
        if author_id != self.author_id:
            raise AdapterFailure("identity_mismatch")
        detail = self._cache.get(item_id)
        if not detail:
            raise AdapterFailure("item_unavailable")
        if source_url:
            requested_id, _ = _official_item(self.platform, source_url, author_id)
            if requested_id != item_id:
                raise AdapterFailure("identity_mismatch")
        return detail

    def prepare_page_details(self, author_id, item_ids):
        if author_id != self.author_id:
            raise AdapterFailure("identity_mismatch")
        return tuple(item for item in item_ids if item not in self._cache)

    def prepare_details(self, author_id, item_ids):
        missing = self.prepare_page_details(author_id, item_ids)
        if missing:
            self.poll_latest()
        return tuple(item for item in item_ids if item not in self._cache)

    def verify_author(self, author_id: str) -> dict:
        if author_id != self.author_id:
            raise AdapterFailure("identity_mismatch")
        self._load(None)
        return {"author_id": author_id, "display_name": author_id,
                "evidence": "official_profile_link_in_http_feed"}

    def download_media(self, candidate: MediaCandidate, target_dir: Path) -> dict:
        if candidate.kind not in {"image", "video"} or type(candidate.position) is not int or candidate.position < 0:
            raise AdapterFailure("media_failed")
        connection = None
        temp = None
        try:
            media_url = self._media_url(candidate.url)
            connection, response = _request(media_url)
            mime = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            length = response.getheader("Content-Length")
            expected = int(length) if length and length.isdigit() else None
            if expected is not None and expected > MAX_MEDIA_BYTES:
                raise AdapterFailure("media_failed")
            root = Path(target_dir).resolve()
            root.mkdir(parents=True, exist_ok=True)
            digest = sha256()
            size = 0
            header = b""
            with tempfile.NamedTemporaryFile(dir=root, suffix=".part", delete=False) as output:
                temp = Path(output.name)
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_MEDIA_BYTES:
                        raise AdapterFailure("media_failed")
                    header = (header + chunk)[:32]
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if not size or expected is not None and size != expected:
                raise AdapterFailure("media_failed")
            signatures = {"image/jpeg": (header.startswith(b"\xff\xd8\xff"), ".jpg"),
                          "image/png": (header.startswith(b"\x89PNG\r\n\x1a\n"), ".png"),
                          "image/webp": (header.startswith(b"RIFF") and header[8:12] == b"WEBP", ".webp"),
                          "video/mp4": (len(header) >= 12 and header[4:8] == b"ftyp", ".mp4")}
            valid, suffix = signatures.get(mime, (False, ""))
            if not valid or not mime.startswith(candidate.kind + "/"):
                raise AdapterFailure("media_failed")
            hexdigest = digest.hexdigest()
            path = root / f"{candidate.asset_id}-{hexdigest}{suffix}"
            if path.exists():
                if path.is_symlink() or path.stat().st_size != size or _file_digest(path) != hexdigest:
                    raise AdapterFailure("media_failed")
                reused = True
            else:
                os.link(temp, path)
                reused = False
            return {"path": str(path), "mime": mime, "size": size, "sha256": hexdigest, "reused": reused}
        except AdapterFailure:
            raise
        except (OSError, ValueError):
            raise AdapterFailure("media_failed") from None
        finally:
            if connection is not None:
                connection.close()
            if temp is not None:
                temp.unlink(missing_ok=True)


def _file_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
