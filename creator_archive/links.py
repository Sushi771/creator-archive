"""Classify links without requests, redirects, credentials or identity guesses."""
import re
from urllib.parse import parse_qs, urlsplit


def classify(text: str) -> dict:
    urls = re.findall(r"https?://[^\s<>\"'，。；）]+", text)
    if len(urls) != 1:
        raise ValueError("请提供恰好一个公众号或小红书链接")
    try:
        url = urlsplit(urls[0])
        port = url.port
    except ValueError:
        raise ValueError("链接格式不正确") from None
    if url.username or url.password or port not in (None, 443, 80):
        raise ValueError("不支持带登录信息或自定义端口的链接")
    host = (url.hostname or "").lower()
    candidate = None
    kind = None
    if host in ("www.xiaohongshu.com", "xiaohongshu.com"):
        platform = "xiaohongshu"
        profile = re.fullmatch(r"/user/profile/([0-9a-fA-F]{24})/?", url.path)
        note = re.fullmatch(r"/(?:explore|discovery/item|user/profile/[0-9a-fA-F]{24})/([0-9a-fA-F]{24})/?", url.path)
        if profile:
            kind, candidate = "profile", profile[1].lower()
        elif note:
            kind = "item"
    elif host in ("xhslink.com", "xhslink.cn"):
        platform, kind = "xiaohongshu", "short_link"
    elif host == "mp.weixin.qq.com":
        platform = "wechat"
        if url.path.startswith("/s/") or url.path == "/s":
            kind = "item"
        elif url.path in ("/mp/profile_ext", "/mp/appmsgalbum"):
            kind = "profile" if url.path == "/mp/profile_ext" else "collection"
        values = parse_qs(url.query).get("__biz", [])
        if len(values) == 1 and re.fullmatch(r"[A-Za-z0-9+/=]{4,128}", values[0]):
            candidate = values[0]
    else:
        raise ValueError("仅支持公众号与小红书官方链接")
    if kind is None:
        raise ValueError("暂不识别此链接路径，请提供作品或博主主页链接")
    return {"platform": platform, "kind": kind, "candidate_author_id": candidate,
            "identity_verified": False, "can_subscribe": False,
            "status": "needs_live_adapter", "message": "仅完成链接分类；需真实组件核对作者身份，尚未建立订阅"}
