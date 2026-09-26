"""Strict XHS response adapters. Network access and credentials belong to the caller.

The page shape follows the user_posted response documented in the pinned
MediaCrawler review. This module does not sign requests or infer pagination
from browser DOM windows. Its output remains validation-only until real API
pages and a terminal response have been observed for the target authors.
"""

from dataclasses import dataclass
import ipaddress
from typing import Callable, Mapping
from urllib.parse import urlsplit

from creator_archive.validation import AdapterFailure, Item, Page


def _mapping(value: object) -> Mapping:
    if not isinstance(value, Mapping):
        raise AdapterFailure("unavailable")
    return value


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


class XhsPageAdapter:
    """Consume one authenticated user_posted API response per call.

    ``fetch_page`` must request the supplied author's published-notes endpoint
    with the supplied cursor and return the decoded response. It must not turn
    transport errors, login walls or partial DOM results into empty pages.
    The caller owns its browser/session and supplies typed AdapterFailure for
    known login, rate-limit and timeout conditions.
    """

    version = "xhs-user-posted-contract-v1"

    def __init__(self, fetch_page: Callable[[str, str], Mapping]):
        self.fetch_page = fetch_page

    def page(self, author_id: str, cursor: str | None) -> Page:
        if not _nonempty(author_id) or (cursor is not None and not _nonempty(cursor)):
            raise AdapterFailure("invalid_cursor")
        response = _mapping(self.fetch_page(author_id, cursor or ""))
        if response.get("success") is False:
            raise AdapterFailure("unavailable")
        data = _mapping(response["data"] if "data" in response else response)
        notes = data.get("notes")
        has_more = data.get("has_more")
        next_cursor = data.get("cursor")
        if not isinstance(notes, list) or type(has_more) is not bool:
            raise AdapterFailure("unavailable")
        if has_more and (not _nonempty(next_cursor) or not notes):
            raise AdapterFailure("unavailable")
        if has_more and next_cursor == cursor:
            raise AdapterFailure("invalid_cursor")
        if not has_more and not notes and cursor is None:
            # An initial empty response could be a blocked account or a login
            # interstitial. Require an independent live check before completion.
            raise AdapterFailure("unavailable")
        items = []
        for raw in notes:
            note = _mapping(raw)
            note_id = note.get("note_id")
            if not _nonempty(note_id):
                raise AdapterFailure("unavailable")
            user = note.get("user")
            if isinstance(user, Mapping):
                observed_author = user.get("user_id", user.get("userId"))
                if observed_author is not None and observed_author != author_id:
                    raise AdapterFailure("unavailable")
            # Published time is not guaranteed in listing cards. Detail fetching
            # fills it later; an empty value does not invent a timestamp.
            items.append(Item(note_id, author_id, ""))
        if has_more:
            return Page(tuple(items), next_cursor, True)
        # Only an explicit false from the API is terminal. The API may retain a
        # cursor even on its last page; that value must not be sent to Store.
        return Page(tuple(items), None, False, "xhs user_posted: explicit has_more=false")


@dataclass(frozen=True)
class MediaCandidate:
    kind: str
    position: int
    url: str
    width: int | None = None
    height: int | None = None
    size: int | None = None
    quality: str | None = None


@dataclass(frozen=True)
class DetailMedia:
    item_id: str
    author_id: str
    text: str
    images: tuple[MediaCandidate, ...]
    video_streams: tuple[MediaCandidate, ...]
    missing: tuple[str, ...]


def _https_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "local"} or host.endswith((".localhost", ".local")):
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return None
    return value


def extract_detail_media(detail: Mapping, *, author_id: str, item_id: str) -> DetailMedia:
    """Map the pinned xiaohongshu-mcp detail shape without downloading files.

    Candidate URLs are short-lived and must be rechecked by a separate media
    downloader before use. Missing video/image sources remain explicit.
    """
    note = _mapping(detail.get("note"))
    user = _mapping(note.get("user"))
    if note.get("noteId") != item_id or user.get("userId") != author_id:
        raise AdapterFailure("unavailable")
    images_raw = note.get("imageList", [])
    if not isinstance(images_raw, list):
        raise AdapterFailure("unavailable")
    images: list[MediaCandidate] = []
    missing: list[str] = []
    for index, raw in enumerate(images_raw):
        image = _mapping(raw)
        url = _https_url(image.get("urlDefault")) or _https_url(image.get("urlPre"))
        if url is None:
            missing.append(f"image_{index}_source_missing")
        else:
            images.append(MediaCandidate("image", index, url, image.get("width"), image.get("height")))
        if image.get("livePhoto") is True:
            missing.append(f"live_photo_{index}_video_unresolved")

    video_streams: list[MediaCandidate] = []
    if note.get("type") == "video":
        video = note.get("video")
        media = video.get("media") if isinstance(video, Mapping) else None
        streams = media.get("stream") if isinstance(media, Mapping) else None
        if isinstance(streams, Mapping):
            for codec, variants in streams.items():
                if not isinstance(variants, list):
                    continue
                for variant in variants:
                    if not isinstance(variant, Mapping):
                        continue
                    url = _https_url(variant.get("masterUrl"))
                    if url:
                        video_streams.append(MediaCandidate(
                            "video", 0, url, variant.get("width"), variant.get("height"),
                            variant.get("size"), str(variant.get("qualityType") or codec),
                        ))
        if not video_streams:
            missing.append("video_stream_missing")
    text = note.get("desc")
    if not isinstance(text, str):
        text = ""
    return DetailMedia(item_id, author_id, text, tuple(images), tuple(video_streams), tuple(missing))
