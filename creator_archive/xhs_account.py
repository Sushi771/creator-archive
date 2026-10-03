"""One private HTTP account; QR requests begin only after a local user action.

Endpoint sequence reviewed against hammershock/xhs-cli auth.py (MIT).
No browser/profile access, retries, account rotation or scheduled login.
"""
from __future__ import annotations

from datetime import timezone
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from io import BytesIO
import json
import os
from pathlib import Path
import re
import secrets
import shutil
from threading import RLock
import time
from urllib.parse import urlencode, quote, urlsplit

from .adapters.xhs_http import (_API_HOST, _COOKIE_NAME, _UA, _id, _read_cookies,
                                _signed_headers, _https_request)
from .validation import AdapterFailure
from .xhs_source_setup import configure_native_source

_ME = "/api/sns/web/v2/user/me"
_ACTIVATE = "/api/sns/web/v1/login/activate"
_CREATE = "/api/sns/web/v1/login/qrcode/create"
_STATUS = "/api/sns/web/v1/login/qrcode/status"


def _atomic_private(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    try:
        with os.fdopen(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class XhsAccount:
    def __init__(self, root: Path):
        self.root = root
        self.cookie_file = root / "private/xhs-session.cookie"
        self.metadata = root / "private/xhs-account.json"
        self.lock = RLock()
        self.pending = None
        self.last_request = 0.0
        self.last_error = None
        self.last_failure = None
        self.current_phase = "unknown"
        self.verified_in_process = False

    def status(self):
        # Opening/refreshing the page reads metadata only, never platform APIs.
        with self.lock:
            saved = {}
            if self.metadata.is_file():
                try:
                    data = json.loads(self.metadata.read_text(encoding="utf-8"))
                    saved = {key: data[key] for key in ("user_id", "nickname", "checked_at", "revision") if key in data}
                except (OSError, ValueError, TypeError):
                    pass
            return {**saved, "session_present": self.cookie_file.is_file(),
                    "state": self.pending["state"] if self.pending else
                             "connected" if self.verified_in_process and saved and self.cookie_file.is_file() and not self.last_error else
                             "not_checked" if self.cookie_file.is_file() and not self.last_error else
                             "disconnected",
                    "reason": self.last_error, "failure": self.last_failure, "qr_active": self.pending is not None,
                    "expires_at": self.pending["expires_at"] if self.pending else None,
                    "author_list_verified": False}

    def _call(self, cookies, method, uri, values=None):
        self.current_phase = {_ACTIVATE: "activate", _CREATE: "create_qr", _STATUS: "poll_qr", _ME: "identity"}.get(uri, "unknown")
        try:
            return self._request(cookies, method, uri, values)
        except AdapterFailure as error:
            error.diagnostics = {**error.diagnostics, "stage": "account", "phase": self.current_phase}
            raise

    def _request(self, cookies, method, uri, values=None):
        if uri not in {_ME, _ACTIVATE, _CREATE, _STATUS}:
            raise AdapterFailure("invalid_response")
        delay = 1.5 - (time.monotonic() - self.last_request)
        if delay > 0:
            time.sleep(delay)
        self.last_request = time.monotonic()
        headers = {"User-Agent": _UA, "Accept": "application/json", "Accept-Encoding": "identity",
                   "Origin": "https://www.xiaohongshu.com", "Referer": "https://www.xiaohongshu.com/",
                   "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items() if v)}
        headers.update(_signed_headers(cookies, values, uri=uri, method=method))
        body = None
        path = uri
        if method == "POST":
            headers["Content-Type"] = "application/json;charset=UTF-8"
            body = json.dumps(values or {}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        elif values:
            path += "?" + urlencode(values, safe=",", quote_via=quote)
        raw, received = _https_request(method, _API_HOST, path, headers, max_bytes=1024 * 1024, body=body)
        try:
            response = json.loads(raw)
            if not isinstance(response, dict):
                raise ValueError()
        except (UnicodeError, ValueError):
            raise AdapterFailure("invalid_response") from None
        code = response.get("code")
        if response.get("success") is not True or type(code) is not int or code != 0:
            category = ("verification_required" if code == 300011 else "rate_limited" if code == 300012
                        else "unknown_business_error")
            raise AdapterFailure(category, diagnostics={"http_status": 200,
                "success": response.get("success") if type(response.get("success")) is bool else None,
                "business_code": code if type(code) is int else None, "stage": "account",
                "message": "[platform account message redacted]"})
        data = response.get("data")
        if not isinstance(data, dict):
            raise AdapterFailure("invalid_response")
        # Only exact platform responses can update the in-memory candidate jar.
        for raw_cookie in received:
            # RFC6265: only the first name/value is a cookie. Unknown response
            # attributes (e.g. Priority) must not become additional credentials.
            first, *parts = raw_cookie.split(";")
            jar = SimpleCookie()
            try:
                jar.load(first)
            except Exception:
                raise AdapterFailure("invalid_response") from None
            if len(jar) != 1:
                raise AdapterFailure("invalid_response")
            attributes = {}
            for part in parts:
                key, separator, value = part.strip().partition("=")
                if separator:
                    attributes[key.lower()] = value.strip()
            for name, morsel in jar.items():
                domain = attributes.get("domain", "").lstrip(".").lower()
                if domain and domain != "xiaohongshu.com" and not domain.endswith(".xiaohongshu.com"):
                    continue
                value = morsel.value
                if not _COOKIE_NAME.fullmatch(name) or any(not 33 <= ord(c) <= 126 or c == ";" for c in value):
                    raise AdapterFailure("invalid_response")
                # A valid Max-Age takes precedence over Expires, including when
                # it revokes a nonempty value. Invalid attributes are ignored.
                max_age = attributes.get("max-age", "")
                expired = False
                if re.fullmatch(r"-?[0-9]+", max_age):
                    expired = max_age.startswith("-") or not max_age.strip("0")
                elif "expires" in attributes:
                    try:
                        expires = parsedate_to_datetime(attributes["expires"])
                        if expires.tzinfo is None:
                            expires = expires.replace(tzinfo=timezone.utc)
                        expired = expires.timestamp() <= time.time()
                    except (ValueError, TypeError, OverflowError):
                        pass
                if value and not expired:
                    cookies[name] = value
                else:
                    cookies.pop(name, None)
        return data

    def _identity(self, cookies):
        data = self._call(cookies, "GET", _ME)
        if data.get("guest") is not False:
            raise AdapterFailure("needs_login")
        user_id = _id(data.get("user_id", data.get("userId")))
        nickname = data.get("nickname")
        if not isinstance(nickname, str) or not nickname.strip() or len(nickname) > 160:
            raise AdapterFailure("invalid_response")
        if not cookies.get("a1") or not cookies.get("web_session"):
            raise AdapterFailure("needs_login")
        return {"user_id": user_id, "nickname": nickname, "checked_at": time.time(),
                "revision": secrets.token_hex(16)}

    def _save(self, cookies, identity):
        content = ("; ".join(f"{k}={v}" for k, v in cookies.items() if v) + "\n").encode("ascii")
        old = self.cookie_file.read_bytes() if self.cookie_file.exists() else None
        old_meta = self.metadata.read_bytes() if self.metadata.exists() else None
        source_file = self.root / "sources.json"
        old_sources = source_file.read_bytes() if source_file.exists() else None
        if old is not None:
            backup = self.root / "backups" / f"session-before-update-{time.time_ns()}"
            backup.mkdir(parents=True)
            shutil.copy2(self.cookie_file, backup / "xhs-session.cookie")
            if (backup / "xhs-session.cookie").read_bytes() != old:
                raise OSError("private_session_backup_failed")
            if old_meta is not None:
                shutil.copy2(self.metadata, backup / "xhs-account.json")
        try:
            _atomic_private(self.cookie_file, content)
            configure_native_source(self.root, self.cookie_file, replace=True)
            _atomic_private(self.metadata, json.dumps(identity, ensure_ascii=False).encode("utf-8"))
        except Exception:
            if old is not None:
                _atomic_private(self.cookie_file, old)
            else:
                self.cookie_file.unlink(missing_ok=True)
            if old_meta is not None:
                _atomic_private(self.metadata, old_meta)
            else:
                self.metadata.unlink(missing_ok=True)
            if old_sources is not None:
                _atomic_private(source_file, old_sources)
            else:
                source_file.unlink(missing_ok=True)
            raise
        self.pending = None
        self.last_error = None
        self.last_failure = None
        self.verified_in_process = True
        return self.status()

    def _fail(self, error):
        self.pending = None
        self.last_error = getattr(error, "category", "local_session_write_failed")
        self.verified_in_process = False
        diagnostic = getattr(error, "diagnostics", {})
        self.last_failure = {"phase": self.current_phase,
                             "http_status": diagnostic.get("http_status") if type(diagnostic.get("http_status")) is int else None,
                             "business_code": diagnostic.get("business_code") if type(diagnostic.get("business_code")) is int else None}

    def start(self):
        with self.lock:
            import qrcode  # Check the renderer before requesting a platform QR.
            self.pending = None
            self.last_error = None
            # These are unauthenticated client identifiers for guest activation;
            # no authenticated web_session or user identity is invented.
            seed = (f"{int(time.time() * 1000):x}" + secrets.token_hex(20))[:52].ljust(52, "0")
            cookies = {"a1": seed, "webId": secrets.token_hex(16)}
            try:
                self._call(cookies, "POST", _ACTIVATE, {})
                data = self._call(cookies, "POST", _CREATE, {"qr_type": 1})
                for key in ("qr_id", "code", "url"):
                    if not isinstance(data.get(key), str) or not data[key] or len(data[key]) > 4096:
                        raise AdapterFailure("invalid_response")
                url = urlsplit(data["url"])
                if url.scheme != "https" or not url.hostname or (url.hostname != "xiaohongshu.com"
                        and not url.hostname.endswith(".xiaohongshu.com")) or url.username or url.password:
                    raise AdapterFailure("invalid_response")
                self.pending = {"cookies": cookies, "qr_id": data["qr_id"], "code": data["code"],
                                "url": data["url"], "expires_at": time.time() + 240, "state": "waiting_scan"}
                return self.status()
            except Exception as error:
                self._fail(error)
                raise

    def poll(self):
        with self.lock:
            if self.pending is None:
                raise ValueError("当前没有等待登录的二维码；没有请求平台")
            if time.time() >= self.pending["expires_at"]:
                self.pending = None
                self.last_error = "qr_expired"
                return self.status()
            try:
                candidate = self.pending
                data = self._call(candidate["cookies"], "GET", _STATUS,
                                  {"qr_id": candidate["qr_id"], "code": candidate["code"]})
                state = data.get("code_status")
                if type(state) is not int or state not in {-1, 0, 1, 2}:
                    raise AdapterFailure("invalid_response")
                if state == -1:
                    self.pending = None
                    self.last_error = "qr_expired"
                elif state == 2:
                    return self._save(candidate["cookies"], self._identity(candidate["cookies"]))
                else:
                    candidate["state"] = "waiting_scan" if state == 0 else "waiting_confirmation"
                return self.status()
            except Exception as error:
                self._fail(error)
                raise

    def verify(self):
        with self.lock:
            self.pending = None
            try:
                _, cookies = _read_cookies(self.cookie_file)
                return self._save(cookies, self._identity(cookies))
            except Exception as error:
                self._fail(error)
                raise

    def cancel(self):
        with self.lock:
            self.pending = None
            return self.status()

    def qr_image(self):
        with self.lock:
            if self.pending is None or time.time() >= self.pending["expires_at"]:
                raise KeyError("no_active_qr")
            import qrcode
            from qrcode.image.svg import SvgPathImage
            output = BytesIO()
            qrcode.make(self.pending["url"], image_factory=SvgPathImage, border=4).save(output)
            return output.getvalue()
