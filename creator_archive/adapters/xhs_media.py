"""Bounded, credential-free CDN downloads with verified reuse and atomic files."""
from hashlib import sha256
import ipaddress
import json
import os
from pathlib import Path
import socket
import tempfile
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .xhs import MediaCandidate
from creator_archive import network_safety


class MediaFailure(ValueError):
    def __init__(self, reason, *, http_status=None):
        super().__init__(reason)
        self.diagnostics = {"http_status": http_status, "success": None, "business_code": None,
                            "stage": "media", "message": "[media error; credential URLs omitted]"}


def safe_media_url(url: str, *, resolve: bool = True) -> str:
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        valid = (p.scheme in {"http", "https"} and p.port in (None, 443)
                 and not p.username and not p.password
                 and (host == "xhscdn.com" or host.endswith(".xhscdn.com")))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise MediaFailure("media_host_not_allowed")
    if resolve:
        network_safety.require_xhs_network()
        try:
            addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                raise MediaFailure("media_address_not_public")
        except OSError:
            raise MediaFailure("media_dns_failed") from None
    return urlunsplit(("https", host, p.path, p.query, ""))


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, safe_media_url(newurl))


def _mime(header: bytes, kind: str) -> tuple[str, str]:
    if kind == "video" and len(header) >= 12 and header[4:8] == b"ftyp":
        return "video/mp4", ".mp4"
    if kind == "image":
        if header.startswith(b"\xff\xd8\xff"):
            return "image/jpeg", ".jpg"
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png", ".png"
        if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            return "image/webp", ".webp"
    raise MediaFailure("media_content_invalid_or_unsupported")


def _digest(path: Path) -> str:
    h = sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def download_media(candidate: MediaCandidate, target_dir: Path, *, timeout: float = 60,
                   max_bytes: int = 1024 * 1024 * 1024) -> dict:
    """Retry only failed resources; no byte-range claim or authenticated requests.

    Complete media is content addressed and never overwritten. Interrupted .part
    files are disposable; metadata stores a URL digest, never a signed URL.
    """
    if candidate.kind not in {"image", "video"} or type(candidate.position) is not int or candidate.position < 0:
        raise MediaFailure("invalid_media_candidate")
    url = safe_media_url(candidate.url, resolve=False)
    p = urlsplit(url)
    key = sha256(f"{candidate.asset_id}|{p.hostname}{p.path}".encode()).hexdigest()
    root = Path(target_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    sidecar = root / f"{key}.json"
    if sidecar.exists():
        try:
            saved = json.loads(sidecar.read_text(encoding="utf-8"))
            path = (root / saved["filename"]).resolve()
            if (path.parent == root and path.is_file() and not path.is_symlink()
                    and path.stat().st_size == saved["size"] and _digest(path) == saved["sha256"]):
                with path.open("rb") as source:
                    mime, _ = _mime(source.read(32), candidate.kind)
                return {"path": str(path), "mime": mime, "size": saved["size"], "sha256": saved["sha256"], "reused": True}
        except (ValueError, KeyError, TypeError, OSError):
            pass
    network_safety.require_xhs_network()
    temp = None
    try:
        url = safe_media_url(url)
        request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.xiaohongshu.com/", "Accept-Encoding": "identity"})
        with build_opener(_SafeRedirect()).open(request, timeout=timeout) as response:
            if response.status != 200:
                raise MediaFailure("media_http_failed", http_status=response.status)
            safe_media_url(response.geturl())
            length = response.headers.get("Content-Length")
            expected = int(length) if length and length.isdigit() else None
            if expected is not None and expected > max_bytes:
                raise MediaFailure("media_too_large")
            digest, size, header = sha256(), 0, b""
            with tempfile.NamedTemporaryFile(dir=root, suffix=".part", delete=False) as output:
                temp = Path(output.name)
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise MediaFailure("media_too_large")
                    header = (header + chunk)[:32] if len(header) < 32 else header
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if not size or (expected is not None and size != expected):
                raise MediaFailure("media_length_mismatch")
            mime, suffix = _mime(header, candidate.kind)
            hexdigest = digest.hexdigest()
            path = root / f"{candidate.asset_id}-{hexdigest}{suffix}"
            if path.exists():
                if path.is_symlink() or path.stat().st_size != size or _digest(path) != hexdigest:
                    raise MediaFailure("media_existing_file_conflict")
            else:
                # Same-volume hard link publishes the fully flushed file
                # atomically and fails if another writer created this name.
                os.link(temp, path)
            metadata = {"filename": path.name, "mime": mime, "sha256": hexdigest, "size": size}
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, suffix=".part", delete=False) as out:
                meta_temp = Path(out.name)
                json.dump(metadata, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(meta_temp, sidecar)
            return {"path": str(path), "mime": mime, "sha256": hexdigest, "size": size, "reused": False}
    except MediaFailure:
        raise
    except HTTPError as exc:
        raise MediaFailure("media_rate_limited" if exc.code == 429 else "media_url_expired_or_unavailable", http_status=exc.code) from None
    except Exception:
        raise MediaFailure("media_download_interrupted") from None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
