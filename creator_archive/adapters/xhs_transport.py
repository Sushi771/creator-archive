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
from urllib.parse import parse_qs, urlencode, urlsplit

from .xhs import XhsPageAdapter
from .xhs_content import DETAIL_STATE, detail_url, project_detail
from .xhs_media import download_media, MediaFailure
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
    xsec_token:n.xsecToken,
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
        self._runtime = self._context = self._page = self._detail_page = None
        self._author = None
        self._responses: dict[tuple[str, str], dict] = {}
        self._failure = None
        self._cooldown_until = 0.0
        self._closed = False
        self._detail_links: dict[tuple[str, str], str] = {}
        self._verified_detail_links: dict[tuple[str, str], str] = {}
        self._detail_authors: set[str] = set()

    def _profile_links(self, author_id, *, page=None, item_ids=None):
        """Only retain actual profile anchors, after listing identity validation."""
        page = self._page if page is None else page
        links = page.locator('a[href]').evaluate_all("nodes => nodes.map(n => n.href)")
        for link in links:
            parsed = urlsplit(link)
            match = re.fullmatch(rf"/(?:explore|user/profile/{author_id})/([0-9a-f]{{24}})", parsed.path)
            if match and (item_ids is None or match[1] in item_ids) and parse_qs(parsed.query).get("xsec_token"):
                try:
                    self._detail_links[(author_id, match[1])] = detail_url(match[1], link)
                except ValueError:
                    pass

    def prepare_page_details(self, author_id, item_ids):
        """Consume observed page references without resetting the listing.

        Return missing IDs for diagnostics; detail() fails those items separately
        so a missing reference cannot discard the rest of the fixed page batch.
        A resumed process must first replay page() to its saved request cursor.
        No navigation references are persisted by this operation.
        """
        targets = set(item_ids)

        def prepare():
            if not targets:
                return ()
            self._ensure()
            if self._failure:
                raise self._failure
            # Also disables the legacy first-screen fallback for missing items.
            self._detail_authors.add(author_id)
            if self._author == author_id:
                self._check_wall()
                if not all((author_id, item) in self._detail_links for item in targets):
                    self._page.wait_for_timeout(300)
                    self._profile_links(author_id, item_ids=targets)
            return tuple(sorted(item for item in targets if (author_id, item) not in self._detail_links))

        return self._call(prepare)

    def prepare_details(self, author_id, item_ids):
        """Reuse verified session links; refresh missing references in three pages.

        This does not run or advance a historical scan. Missing references remain
        explicit gaps; no token, browser credential or response blob is persisted.
        """
        targets = set(item_ids)
        def prepare():
            if not targets:
                return
            self._ensure()
            if time.monotonic() < self._cooldown_until and self._failure:
                raise self._failure
            self._failure = None
            self._detail_links = {key: value for key, value in self._detail_links.items() if key[0] != author_id}
            self._detail_links.update(self._verified_detail_links)
            if all((author_id, item) in self._detail_links for item in targets):
                self._detail_authors.add(author_id)
                return
            cursor = None
            seen = set()
            for _ in range(3):
                page = XhsPageAdapter(self._fetch).page(author_id, cursor)
                self._page.wait_for_timeout(300)
                self._profile_links(author_id)
                if all((author_id, item) in self._detail_links for item in targets) or not page.has_more:
                    break
                if page.next_cursor in seen:
                    raise TransportFailure("invalid_cursor", "引用刷新出现重复游标；原历史检查点保留，请稍后重试。")
                seen.add(page.next_cursor)
                cursor = page.next_cursor
            self._detail_authors.add(author_id)
        return self._call(prepare)

    def _call(self, function, *args):
        if self._closed:
            raise TransportFailure("unavailable", "平台浏览器已关闭，请重启应用后继续。")
        def invoke():
            try:
                return function(*args)
            except AdapterFailure as error:
                if error.category in {"needs_login", "verification_required"}:
                    self._detail_links.clear()
                    self._verified_detail_links.clear()
                    self._detail_authors.clear()
                raise
        return self._worker.submit(invoke).result()

    def page(self, author_id: str, cursor: str | None):
        return self._call(lambda: XhsPageAdapter(self._fetch).page(author_id, cursor))

    def detail(self, author_id: str, item_id: str, source_url: str = "") -> dict:
        """One fresh normal detail navigation; never repeats author history scans."""
        if not re.fullmatch(r"[0-9a-f]{24}", author_id):
            raise TransportFailure("unavailable", "作者标识无效；已保留资料，请核对作者主页后重试。")
        return self._detail(author_id, item_id, source_url)

    def resolve_item(self, source_url: str) -> dict:
        """Resolve a full note link from the exact observed note only."""
        item_id = urlsplit(source_url).path.rstrip("/").split("/")[-1]
        url = detail_url(item_id, source_url)
        if not parse_qs(urlsplit(url).query).get("xsec_token"):
            raise ValueError("请提供从官方作品页面复制的完整访问链接")
        return self._detail(None, item_id, url)

    def _detail(self, author_id: str | None, item_id: str, source_url: str) -> dict:
        try:
            url = detail_url(item_id, source_url)
            if author_id and urlsplit(url).path.startswith("/user/profile/") and urlsplit(url).path != f"/user/profile/{author_id}/{item_id}":
                raise ValueError("detail_author_mismatch")
            supplied_token = bool(parse_qs(urlsplit(url).query).get("xsec_token"))
        except ValueError:
            raise TransportFailure("unavailable", "作品链接与作品 ID 不匹配；已保留资料，请补充该作品的完整链接。") from None

        def fetch_detail():
            self._ensure()
            if time.monotonic() < self._cooldown_until and self._failure:
                raise self._failure
            try:
                # An existing profile card may carry a short-lived navigation
                # token. Use only its actual href, never manufacture a token.
                if not supplied_token:
                    href = self._detail_links.get((author_id, item_id))
                    if not href and author_id not in self._detail_authors:
                        self._detail_page.goto(f"https://www.xiaohongshu.com/user/profile/{author_id}", wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
                        self._detail_page.wait_for_timeout(1000)
                        self._check_wall(self._detail_page)
                        # First-screen links only: no repeat of the validated
                        # historical page chain just to obtain navigation tokens.
                        links = self._detail_page.locator('a[href]').evaluate_all("nodes => nodes.map(n => n.href)")
                        for link in links:
                            parsed_link = urlsplit(link)
                            match = re.fullmatch(rf"/(?:explore|user/profile/{author_id})/([0-9a-f]{{24}})", parsed_link.path)
                            if match and parse_qs(parsed_link.query).get("xsec_token"):
                                try:
                                    self._detail_links[(author_id, match[1])] = detail_url(match[1], link)
                                except ValueError:
                                    pass
                        self._detail_authors.add(author_id)
                        href = self._detail_links.get((author_id, item_id))
                    if href:
                        url_to_open = detail_url(item_id, "https://www.xiaohongshu.com" + href if href.startswith("/") else href)
                    else:
                        raise TransportFailure("reference_missing", "本轮未取得该历史作品的有效访问引用；已保留旧资料，请补充同篇完整官方链接。未重扫历史长链。")
                else:
                    url_to_open = url
                # Detail navigation stays in a second tab of the same context.
                # The listing tab, its scroll position and responses stay intact.
                response = self._detail_page.goto(url_to_open, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
                if response is not None and response.status == 429:
                    failure = TransportFailure("rate_limited", "作品详情收到限流；正文、指标与成功媒体已保留，请等待冷却后恢复。", 60)
                    self._failure = failure
                    self._cooldown_until = time.monotonic() + 60
                    raise failure
                if response is not None and response.status in (401, 403, 461, 471):
                    raise TransportFailure("needs_login" if response.status == 401 else "unavailable", "作品详情需要登录或平台验证；旧指标和成功媒体已保留，请处理浏览器提示后恢复。")
                deadline = time.monotonic() + self.timeout
                hydration_deadline = None
                projected = None
                while time.monotonic() < deadline:
                    state = self._detail_page.evaluate(DETAIL_STATE, item_id)
                    if isinstance(state, dict):
                        observed_author = state.get("note", {}).get("user", {}).get("userId")
                        if not isinstance(observed_author, str) or not re.fullmatch(r"[0-9a-f]{24}", observed_author):
                            raise TransportFailure("unavailable", "目标作品未返回有效作者身份；未收录，请稍后重试。")
                        resolved_author = author_id or observed_author
                        if urlsplit(url).path.startswith("/user/profile/") and urlsplit(url).path != f"/user/profile/{resolved_author}/{item_id}":
                            raise TransportFailure("unavailable", "作品页面与链接中的作者不符；未收录。")
                        current = project_detail(state, author_id=resolved_author, item_id=item_id)
                        if projected is not None:
                            # Hydration can temporarily omit a field that was
                            # already observed on this same navigation.
                            for key, previous in projected["metrics"].items():
                                if current["metrics"][key]["value"] is None and previous["value"] is not None:
                                    current["metrics"][key] = previous
                        projected = current
                        # A successful explicit navigation can be reused for a
                        # canonical metrics-only refresh in this same session.
                        if parse_qs(urlsplit(url_to_open).query).get("xsec_token"):
                            self._detail_links[(resolved_author, item_id)] = url_to_open
                            self._verified_detail_links[(resolved_author, item_id)] = url_to_open
                        if all(m["value"] is not None for m in projected["metrics"].values()):
                            return projected
                        # Counters can hydrate at different times, including
                        # after one has already appeared. Re-read this page
                        # briefly; missing values never become zero.
                        if hydration_deadline is None:
                            hydration_deadline = min(deadline, time.monotonic() + 2.0)
                    text = self._detail_page.locator("body").inner_text(timeout=3000)
                    if any(s in text for s in ("访问频次异常", "操作频繁")):
                        failure = TransportFailure("rate_limited", "作品详情触发平台频次限制；已保留旧资料，请等待冷却后恢复。", 60)
                        self._failure, self._cooldown_until = failure, time.monotonic() + 60
                        raise failure
                    if any(s in text for s in ("手机号登录", "扫码登录", "登录后查看")):
                        raise TransportFailure("needs_login", "作品详情要求登录；旧指标与成功媒体已保留，请在独立窗口登录后恢复。")
                    if any(s in text for s in ("安全验证", "请完成验证")):
                        raise TransportFailure("needs_login", "作品详情要求平台安全验证；已保留旧资料，请在独立浏览器处理后恢复任务。")
                    if any(s in text for s in ("当前笔记暂时无法浏览", "内容不存在", "该笔记已被删除", "私密笔记")):
                        # resolve_item has no author until the page is verified.
                        for cache in (self._detail_links, self._verified_detail_links):
                            for key in list(cache):
                                if key[1] == item_id and (author_id is None or key[0] == author_id):
                                    cache.pop(key)
                        raise TransportFailure("item_unavailable", "作品详情暂不可访问，可能受链接或平台权限限制；已保留旧资料，请在浏览器确认并补充可访问的完整作品链接后重试。")
                    if projected is not None and time.monotonic() >= hydration_deadline:
                        return projected
                    self._detail_page.wait_for_timeout(300)
                if projected is not None:
                    return projected
                raise TransportFailure("timeout", "作品详情未在等待时间内返回，原因未知；旧指标与成功媒体已保留，可补充完整作品链接后重试。")
            except AdapterFailure:
                raise
            except Exception:
                raise TransportFailure("unavailable", "作品详情读取中断，原因未知；旧指标与成功媒体已保留，请检查浏览器或补充完整作品链接后重试。") from None
        return self._call(fetch_detail)

    def download_media(self, candidate, target_dir):
        if time.monotonic() < self._cooldown_until and self._failure:
            raise self._failure
        try:
            return download_media(candidate, target_dir)
        except MediaFailure as exc:
            if str(exc) == "media_rate_limited":
                failure = TransportFailure("rate_limited", "媒体下载收到限流；成功资源已保留，请等待冷却后仅重试缺失资源。", 60)
                self._failure, self._cooldown_until = failure, time.monotonic() + 60
                raise failure from None
            raise TransportFailure("media_failed", "媒体下载或文件校验失败；成功资源已保留，请刷新详情后重试缺失媒体。") from None

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
            self._detail_links.clear()
            self._verified_detail_links.clear()
            self._detail_authors.clear()
            try:
                self._page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded", timeout=30000)
                self._page.bring_to_front()
            except Exception:
                raise TransportFailure("unavailable", "独立浏览器已启动但登录页未完成加载；请检查网络，在该窗口自行登录后重试。") from None
            return {"state": "needs_login", "message": "请在独立浏览器中自行登录，完成后回到应用验证作者或恢复任务。"}
        return self._call(open_page)

    def _ensure(self):
        if self._context is not None:
            try:
                if self._page is None or self._page.is_closed():
                    self._page = self._context.new_page()
                    self._page.on("response", self._observe)
                    self._author = None
                    self._responses.clear()
                if self._detail_page is None or self._detail_page.is_closed():
                    self._detail_page = self._context.new_page()
                return
            except Exception:
                # A closed browser/context needs a fresh dedicated session.
                pass
            self._cleanup()
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
            # Playwright supports independent tabs in one persistent context;
            # only the listing tab may contribute pagination observations.
            # https://playwright.dev/python/docs/pages#multiple-pages
            self._detail_page = self._context.new_page()
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
            # Listing cards can be virtualized before their anchor is mounted.
            # The observed card token is a navigation reference, not a session
            # cookie. Keep it only in process memory, outside normalized pages,
            # database evidence, exports and logs. Never sign or invent tokens.
            # Field contract: pinned MCP Feed.xsecToken and MediaCrawler's
            # creator-note xsec_token field (schema research only, no code reuse).
            self._cache_references(key[0], payload["data"]["notes"])
            self._responses[key] = normalized
        except AdapterFailure as exc:
            self._failure = exc
            self._cooldown_until = time.monotonic() + exc.retry_after
        except Exception:
            self._failure = TransportFailure("unavailable", "作者响应无法解析；进度已保留，请稍后重试。")

    def _cache_references(self, author_id, notes):
        """Memory-only navigation references from an already validated full page.

        Both the API and SSR callers must validate every item's identity and the
        page cursor before this method can retain any navigation references.
        """
        for note in notes:
            token = note.get("xsec_token", note.get("xsecToken"))
            if isinstance(token, str) and 0 < len(token) <= 2048 and not any(ord(c) < 32 for c in token):
                query = {"xsec_token": token}
                source = note.get("xsec_source", note.get("xsecSource"))
                if isinstance(source, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", source):
                    query["xsec_source"] = source
                self._detail_links[(author_id, note["note_id"])] = detail_url(note["note_id"]) + "?" + urlencode(query)

    def _check_wall(self, page=None):
        # A login button in the header alone is not proof of a login wall.
        page = self._page if page is None else page
        text = page.locator("body").inner_text(timeout=3000)
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
                        # SSR cards can exist before virtualized anchors mount.
                        # Retain their observed top-level xsecToken only after
                        # the same identity/cursor checks as an API response.
                        self._cache_references(author, state["notes"])
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
            self._context = self._page = self._detail_page = None
            self._author = None
            self._responses.clear()
            self._detail_links.clear()
            self._verified_detail_links.clear()
            self._detail_authors.clear()
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
