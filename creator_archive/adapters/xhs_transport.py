"""Experimental normal-browser XHS transport; no signatures or cookie import.

Uses Playwright's Apache-2.0 persistent context API. The existing pinned
xiaohongshu-mcp review supplies the INITIAL_STATE shape, not pagination code.
Only observed user_posted responses can establish a terminal page. Browser
restart recovery replays normal scrolling to the exact saved request cursor;
it does not modify the site's state or issue forged requests.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit

from .xhs import XhsPageAdapter
from creator_archive.validation import AdapterFailure


class TransportFailure(AdapterFailure):
    def __init__(self, category: str, reason: str, retry_after: float = 0):
        super().__init__(category, retry_after)
        self.reason = reason


def response_key(url: str) -> tuple[str, str] | None:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "edith.xiaohongshu.com"
            or parsed.path != "/api/sns/web/v1/user_posted" or parsed.port not in (None, 443)
            or parsed.username or parsed.password):
        return None
    query = parse_qs(parsed.query, keep_blank_values=True)
    authors, cursors = query.get("user_id", []), query.get("cursor", [""])
    if len(authors) != 1 or len(cursors) != 1 or not re.fullmatch(r"[0-9a-f]{24}", authors[0]):
        return None
    return authors[0], cursors[0]


def normalized_response(status: int, payload: object) -> dict:
    """Never persist headers, cookies, tokens, full request URLs or media URLs."""
    if status == 429:
        raise TransportFailure("rate_limited", "作者列表收到平台限流；已保留检查点，请至少等待一分钟再恢复。", 60)
    if status == 401:
        raise TransportFailure("needs_login", "作者列表登录已失效；请打开独立登录窗口完成登录后恢复。")
    if status in (403, 461, 471):
        raise TransportFailure("unavailable", "平台拒绝访问或要求安全验证；进度已保留，请在浏览器处理后再恢复。")
    if status != 200 or not isinstance(payload, dict):
        raise TransportFailure("unavailable", "作者列表响应异常，原因尚未确认；进度已保留，可稍后重试。")
    if (payload.get("success") is not True or type(payload.get("code")) is not int
            or payload.get("code") != 0):
        message = str(payload.get("msg", payload.get("message", "")))
        if "登录" in message or "login" in message.lower():
            raise TransportFailure("needs_login", "平台要求重新登录；已保留检查点，请打开登录窗口后恢复。")
        raise TransportFailure("unavailable", "平台未返回成功的作者列表；进度已保留，请查看浏览器并处理限制。")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("notes"), list):
        raise TransportFailure("unavailable", "作者列表字段不完整；未将此页当作末页，请稍后重试。")
    notes = []
    for raw in data["notes"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("user"), dict):
            raise TransportFailure("unavailable", "作品缺少作者归属，无法安全写入归档。")
        if (not isinstance(raw.get("note_id"), str)
                or not re.fullmatch(r"[0-9a-f]{24}", raw["note_id"])
                or not isinstance(raw["user"].get("user_id"), str)
                or not re.fullmatch(r"[0-9a-f]{24}", raw["user"]["user_id"])):
            raise TransportFailure("unavailable", "作品或作者标识缺失，无法安全写入归档。")
        notes.append({"note_id": raw.get("note_id"), "user": {"user_id": raw["user"].get("user_id")}})
    return {"success": True, "data": {"notes": notes, "cursor": data.get("cursor"), "has_more": data.get("has_more")}}


# Read-only page extraction: no page-state rewriting, signing, fetch or credentials.
INITIAL_STATE = """() => {
  const unwrap = v => v?.value ?? v?._value ?? v;
  const u = window.__INITIAL_STATE__?.user;
  const q = unwrap(u?.noteQueries)?.[0];
  const groups = unwrap(u?.notes);
  const raw = Array.isArray(groups) ? groups.flat() : [];
  const notes = raw.map(n => {const c=n.noteCard ?? n; return {
    note_id:n.id ?? n.noteId ?? c.noteId,
    user:{user_id:c.user?.userId ?? c.user?.user_id}};});
  return {author:q?.userId, cursor:q?.cursor, logged_in:unwrap(u?.loggedIn),
    has_more:q?.hasMore ?? q?.has_more, notes};
}"""


class XhsBrowserTransport:
    """One application-owned persistent context, serialized onto one thread.

    Profile must be a dedicated path outside the repository. A fresh profile
    requires the user to log in; open_login never claims login is complete.
    Missing Playwright/browser is a useful unavailable state, not an empty page.
    """
    version = "xhs-normal-browser-experimental-v1"

    def __init__(self, profile_root: Path, *, channel: str = "msedge", timeout: float = 30):
        self.profile_root = Path(profile_root).resolve()
        repo = Path(__file__).resolve().parents[2]
        if self.profile_root.is_relative_to(repo):
            raise ValueError("browser_profile_must_be_outside_repository")
        if timeout <= 0:
            raise ValueError("positive_timeout_required")
        self.channel, self.timeout = channel, timeout
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="xhs-browser")
        self._runtime = self._context = self._page = None
        self._author = None
        self._responses: dict[tuple[str, str], dict] = {}
        self._failure = None
        self._cooldown_until = 0.0
        self._closed = False

    def _call(self, function, *args):
        if self._closed:
            raise TransportFailure("unavailable", "平台浏览器已关闭，请重启应用后继续。")
        return self._worker.submit(function, *args).result()

    def page(self, author_id: str, cursor: str | None):
        return self._call(lambda: XhsPageAdapter(self._fetch).page(author_id, cursor))

    def verify_author(self, author_id: str) -> dict:
        def verify():
            page = XhsPageAdapter(self._fetch).page(author_id, None)
            if not page.items:
                raise TransportFailure("unavailable", "尚未观察到可验证作者归属的作品，请补充可访问主页。")
            return {"author_id": author_id, "display_name": author_id,
                    "evidence": "observed_browser_author_listing"}
        return self._call(verify)

    def open_login(self) -> dict:
        def open_page():
            self._ensure()
            if time.monotonic() < self._cooldown_until:
                raise self._failure
            self._failure = None
            self._author = None
            self._responses.clear()
            try:
                self._page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded", timeout=30000)
                self._page.bring_to_front()
            except Exception:
                raise TransportFailure("unavailable", "独立浏览器已启动但登录页未完成加载；请检查网络，在该窗口自行登录后重试。") from None
            return {"state": "needs_login", "message": "请在独立浏览器中自行登录，完成后回到应用验证作者或恢复任务。"}
        return self._call(open_page)

    def _ensure(self):
        if self._context is not None:
            if self._page is not None and not self._page.is_closed():
                return
            self._cleanup()
            self._author = None
            self._responses.clear()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise TransportFailure("unavailable", "未安装浏览器传输依赖，请运行平台环境安装入口后再打开登录窗口。") from None
        self.profile_root.mkdir(parents=True, exist_ok=True)
        try:
            self._runtime = sync_playwright().start()
            self._context = self._runtime.chromium.launch_persistent_context(
                str(self.profile_root), channel=self.channel, headless=False,
                accept_downloads=False, viewport={"width": 1280, "height": 900})
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self._page.on("response", self._observe)
        except Exception:
            self._cleanup()
            raise TransportFailure("unavailable", "独立浏览器启动失败；请确认 Edge 已安装、专用登录窗口未被其他进程占用，再重试。") from None

    def _observe(self, response):
        key = response_key(response.url)
        if key is None or key[0] != self._author or self._failure:
            return
        try:
            payload = response.json() if response.status == 200 else None
            normalized = normalized_response(response.status, payload)
            # Validate before caching so malformed pages never advance replay.
            XhsPageAdapter(lambda _a, _c: normalized).page(key[0], key[1] or None)
            if key in self._responses and self._responses[key] != normalized:
                raise TransportFailure("invalid_cursor", "同一游标收到不同页；已保留进度，请重新验证作者后继续。")
            self._responses[key] = normalized
        except AdapterFailure as exc:
            self._failure = exc
            self._cooldown_until = time.monotonic() + exc.retry_after
        except Exception:
            self._failure = TransportFailure("unavailable", "作者响应无法解析；进度已保留，请稍后重试。")

    def _check_wall(self):
        # A login button in the header alone is not proof of a login wall.
        text = self._page.locator("body").inner_text(timeout=3000)
        if any(s in text for s in ("安全验证", "访问频次异常", "请完成验证")):
            raise TransportFailure("unavailable", "作者页要求平台验证；进度已保留，请在浏览器完成处理后恢复。")
        if any(s in text for s in ("登录后查看", "登录后浏览", "手机号登录", "扫码登录")):
            raise TransportFailure("needs_login", "作者页要求登录；已保留检查点，请打开独立登录窗口后恢复。")

    def _fetch(self, author: str, cursor: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{24}", author):
            raise TransportFailure("unavailable", "作者 ID 格式无效，请补充完整小红书主页链接。")
        self._ensure()
        try:
            if self._failure:
                if time.monotonic() < self._cooldown_until:
                    raise self._failure
                # This is a new explicit call from a resumed task. Replay from
                # the homepage; no failure ever becomes a terminal empty page.
                self._failure = None
                self._author = None
            if not cursor:
                # A new initial scan must obtain fresh evidence. In particular,
                # never relabel a previous scan's cached terminal page as live.
                self._author = None
            if self._author != author:
                self._author = author
                self._responses.clear()
                self._page.goto(f"https://www.xiaohongshu.com/user/profile/{author}",
                                wait_until="domcontentloaded", timeout=30000)
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                if self._failure:
                    raise self._failure
                self._check_wall()
                if (author, cursor) in self._responses:
                    return self._responses[(author, cursor)]
                state = self._page.evaluate(INITIAL_STATE)
                if isinstance(state, dict) and state.get("logged_in") is False:
                    raise TransportFailure("needs_login", "独立浏览器尚未登录；作者页可能展示隐藏作品ID的预览，请登录后恢复，已有进度保留。")
                if isinstance(state, dict) and state.get("author") == author:
                    # SSR may seed a nonterminal first page, never prove completion.
                    if not cursor and state.get("has_more") is True and state.get("notes"):
                        seed = {"success": True, "data": {k: state[k] for k in ("notes", "cursor", "has_more")}}
                        seed = normalized_response(200, {"success": True, "code": 0, "data": seed["data"]})
                        XhsPageAdapter(lambda _a, _c: seed).page(author, None)
                        return seed
                    if state.get("has_more") is False:
                        raise TransportFailure("invalid_cursor", "浏览器已停在末页但未观察到所需游标响应；检查点保留，请核对作者后恢复。")
                # Normal scrolling can rejoin a saved cursor after browser restart.
                # No fixed page cap or DOM emptiness is interpreted as completion.
                self._page.mouse.wheel(0, 1800)
                self._page.wait_for_timeout(1200)
            raise TransportFailure("timeout", "暂未收到与检查点匹配的新分页响应；进度已保留，可恢复任务继续等待。")
        except AdapterFailure:
            raise
        except Exception:
            raise TransportFailure("unavailable", "作者页读取中断，具体原因尚未确认；已保留检查点，请重新打开登录窗口后恢复。") from None

    def _cleanup(self):
        try:
            if self._context:
                self._context.close()
        finally:
            self._context = self._page = None
            if self._runtime:
                self._runtime.stop()
                self._runtime = None

    def close(self):
        if not self._closed:
            try:
                self._call(self._cleanup)
            finally:
                self._closed = True
                self._worker.shutdown(wait=True)
