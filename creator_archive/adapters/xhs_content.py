"""Read-only detail projection and conservative count interpretation.

Field contract researched against xiaohongshu-mcp a5c8f779 (Apache-2.0).
No third-party scraper code, credentials or comments are retained here.
"""
from datetime import datetime, timezone
from decimal import Decimal
import re
import time
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

from .xhs import extract_detail_media
from creator_archive.validation import AdapterFailure


def parse_count(raw: object) -> dict:
    result = {"value": None, "quality": "unknown", "raw": None}
    if type(raw) is int and 0 <= raw <= 9223372036854775807:
        return {"value": raw, "quality": "exact", "raw": str(raw)}
    if not isinstance(raw, str):
        return result
    raw = raw.strip()
    result["raw"] = raw[:80]
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([万亿wWkK]?)(\+?)", raw.replace(",", ""))
    if not match or len(raw) > 80:
        return result
    digits, unit, plus = match.groups()
    if "." in digits and not unit:
        return result
    multiplier = {"": 1, "万": 10000, "亿": 100000000, "w": 10000, "k": 1000}[unit.lower()]
    value = int(Decimal(digits) * multiplier)
    if value > 9223372036854775807:
        return result
    return {"value": value, "quality": "lower_bound" if plus else "approximate" if unit else "exact", "raw": raw[:80]}


def detail_url(item_id: str, source_url: str = "") -> str:
    if not re.fullmatch(r"[0-9a-f]{24}", item_id):
        raise ValueError("invalid_item_id")
    if not source_url:
        return f"https://www.xiaohongshu.com/explore/{item_id}"
    try:
        parsed = urlsplit(source_url)
        valid = (parsed.scheme == "https" and parsed.hostname in {"www.xiaohongshu.com", "xiaohongshu.com"}
                 and not parsed.username and not parsed.password and parsed.port in (None, 443)
                 and (parsed.path in {f"/explore/{item_id}", f"/discovery/item/{item_id}"}
                      or re.fullmatch(rf"/user/profile/[0-9a-f]{{24}}/{item_id}", parsed.path)))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("invalid_detail_url")
    return urlunsplit(("https", "www.xiaohongshu.com", parsed.path, parsed.query, ""))


def project_detail(detail: Mapping, *, author_id: str, item_id: str, observed_at: float | None = None) -> dict:
    media = extract_detail_media(detail, author_id=author_id, item_id=item_id)
    note = detail["note"]
    metrics = note.get("interactInfo")
    metrics = metrics if isinstance(metrics, Mapping) else {}
    published = ""
    timestamp = note.get("time")
    if type(timestamp) in (int, float) and timestamp > 0:
        try:
            published = datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    candidates = list(media.images)
    if media.video_streams:
        # Prefer the broadly playable AVC stream; never invent an original URL.
        avc = [v for v in media.video_streams if v.quality and "h264" in v.quality.lower()]
        pool = avc or list(media.video_streams)
        candidates.append(max(pool, key=lambda v: (v.width if type(v.width) is int else 0,
                                                   v.size if type(v.size) is int else 0)))
    missing = list(media.missing)
    if "desc" not in note or not isinstance(note["desc"], str):
        missing.append("body_missing")
    if not media.images and note.get("type") != "video":
        missing.append("image_sources_missing")
    return {"item_id": item_id, "author_id": author_id,
            "title": note.get("title") if isinstance(note.get("title"), str) else "",
            "text": media.text, "published_at": published,
            "content_type": "video" if note.get("type") == "video" else "image" if note.get("type") == "normal" else "unknown",
            "source": "xhs_browser_initial_state", "observed_at": time.time() if observed_at is None else observed_at,
            "source_url": detail_url(item_id), "media": candidates, "missing": missing,
            "metrics": {key: parse_count(metrics.get(field)) for key, field in
                        (("likes", "likedCount"), ("collects", "collectedCount"), ("comments", "commentCount"))}}


# Explicit whitelist; platform state is data, never an instruction or stored blob.
DETAIL_STATE = """itemId => {
 const unwrap = v => v?.value ?? v?._value ?? v;
 const entry = unwrap(window.__INITIAL_STATE__?.note?.noteDetailMap)?.[itemId];
 const n = unwrap(entry?.note); if (!n) return null;
 const s = n.video?.media?.stream;
 const stream = {};
 if (s && typeof s === 'object') for (const [codec, variants] of Object.entries(s)) {
   if (Array.isArray(variants)) stream[codec] = variants.map(v => ({
     masterUrl:v.masterUrl,width:v.width,height:v.height,size:v.size,
     qualityType:codec + ':' + (v.qualityType ?? '')}));
 }
 return {note:{noteId:n.noteId,user:{userId:n.user?.userId},title:n.title,
   desc:n.desc,type:n.type,time:n.time,interactInfo:{likedCount:n.interactInfo?.likedCount,
   collectedCount:n.interactInfo?.collectedCount,commentCount:n.interactInfo?.commentCount},
   imageList:Array.isArray(n.imageList) ? n.imageList.map(v => ({urlDefault:v.urlDefault,
     urlPre:v.urlPre,width:v.width,height:v.height,livePhoto:v.livePhoto})) : [],
   video:{media:{stream}}}};
}"""
