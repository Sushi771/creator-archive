"""Bounded official share redirects; no browser credentials or body parsing."""
import re
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from .xhs_content import detail_url
from .xhs_transport import TransportFailure


def _checked_url(url):
    try:
        parsed = urlsplit(url)
        valid = (len(url) <= 4096 and not re.search(r"[\s\\\x00-\x1f\x7f]", url)
                 and parsed.scheme == "https" and not parsed.username and not parsed.password
                 and parsed.port in (None, 443))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("分享链接跳转核验未完成：仅接受有效的 HTTPS 官方链接；旧资料保留，请重新复制作品链接。")
    return parsed


def expand_share_link(url: str) -> str:
    """Return a validated full note URL in memory, without fetching its body.

    Only short-link hosts are requested here. Each Location is checked before
    following it; the existing browser verifies the final exact note and author.
    Four requests and a 10-second per-operation timeout bound this small lookup.
    """
    parsed = _checked_url(url)
    if parsed.hostname not in {"xhslink.com", "xhslink.cn"}:
        raise ValueError("请提供 xhslink.com 或 xhslink.cn 官方分享短链")
    seen = set()
    try:
        with httpx.Client(follow_redirects=False, timeout=10) as client:
            for _ in range(4):
                parsed = _checked_url(url)
                if parsed.hostname not in {"xhslink.com", "xhslink.cn"} or not re.fullmatch(r"/[A-Za-z0-9/_-]+", parsed.path):
                    raise ValueError("分享短链跳转到未支持的地址；旧资料保留，请复制完整作品链接后重试。")
                if url in seen:
                    raise ValueError("分享短链出现循环跳转；旧资料保留，请重新复制作品链接。")
                seen.add(url)
                with client.stream("GET", url) as response:
                    if response.status_code == 429:
                        raise TransportFailure("rate_limited", "小红书分享短链解析触发限流；未收录或创建任务，旧资料保留，请等待至少一分钟后重试。", 60)
                    if response.status_code in {401, 403, 461, 471}:
                        raise TransportFailure("needs_login" if response.status_code == 401 else "verification_required", "小红书分享短链解析需要登录或平台验证；未创建任务，旧资料保留，请在官方页面处理提示后重试。")
                    if response.status_code not in {301, 302, 303, 307, 308} or not response.headers.get("location"):
                        raise ValueError("分享短链没有返回可核验的作品跳转，原因未知；旧资料保留，请重新复制短链或完整作品链接。")
                    target = urljoin(url, response.headers["location"])
                destination = _checked_url(target)
                if destination.hostname in {"www.xiaohongshu.com", "xiaohongshu.com"}:
                    item = destination.path.rstrip("/").split("/")[-1]
                    try:
                        target = detail_url(item, target)
                    except ValueError:
                        raise ValueError("分享短链未指向支持的作品页面；旧资料保留，请复制具体作品的完整链接。") from None
                    tokens = parse_qs(urlsplit(target).query).get("xsec_token", [])
                    if len(tokens) != 1 or not tokens[0].strip():
                        raise ValueError("分享短链目标缺少有效访问引用；旧资料保留，请从官方作品页面重新复制链接。")
                    return target
                url = target
    except httpx.TimeoutException:
        raise TransportFailure("timeout", "小红书分享短链解析超时，原因未知；未创建任务，旧资料保留，请稍后重试或复制完整作品链接。") from None
    except httpx.HTTPError:
        raise TransportFailure("unavailable", "小红书分享短链解析网络失败，原因未知；未创建任务，旧资料保留，请检查网络后重试。") from None
    raise ValueError("分享短链跳转次数超过核验上限；未创建任务，旧资料保留，请复制完整作品链接。")
