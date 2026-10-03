"""Copy only the explicitly recorded, hash-verified local validation attachments."""
from hashlib import sha256
import json
from pathlib import Path

from .workflow import ArchiveWorkflow


def validation_display_names(source_root: Path) -> dict[str, str]:
    source = Path(source_root) / "samples.json"
    if not source.is_file():
        return {}
    samples = json.loads(source.read_text(encoding="utf-8-sig"))
    names = {}
    for alias in ("XHS-A", "XHS-B"):
        sample = samples.get(alias, {})
        author, name = sample.get("author_id"), sample.get("name")
        if isinstance(author, str) and isinstance(name, str) and 0 < len(name.strip()) <= 160:
            names[author] = name.strip()
    return names


def import_verified_assets(workflow: ArchiveWorkflow, source_root: Path) -> dict:
    root = Path(source_root).resolve()
    samples_file = root / "samples.json"
    if not samples_file.is_file():
        return {"copied": 0, "skipped": [], "media_coverage": "unknown_expected_count"}
    samples = json.loads(samples_file.read_text(encoding="utf-8-sig"))
    records = []
    images_file = root / "image-download-results.json"
    if images_file.is_file():
        images = json.loads(images_file.read_text(encoding="utf-8-sig"))
        for record in images:
            alias = record.get("sample")
            if alias not in {"XHS-A-image-1", "XHS-B-image-1", "XHS-B-image-2"}:
                continue
            if record.get("decoded") is not True:
                continue
            records.append((alias, alias[:5], record.get("file"), record.get("sha256"),
                            record.get("bytes"), record.get("position"), "image", record.get("mime")))
    video_file = root / "video-resume.json"
    if video_file.is_file():
        video = json.loads(video_file.read_text(encoding="utf-8-sig"))
        if video.get("offline_playback_checked") is True:
            records.append(("XHS-A-video-1", "XHS-A-video-1", "XHS-A-video-1.mp4", video.get("sha256"),
                            video.get("total_bytes"), 0, "video", video.get("mime")))
    copied, skipped = 0, []
    for alias, sample_key, filename, digest, size, position, kind, mime in records:
        sample = samples.get(sample_key, {})
        if not isinstance(filename, str) or not isinstance(position, int) or position < 0:
            skipped.append({"sample": alias, "reason": "invalid_record"})
            continue
        source = (root / filename).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            skipped.append({"sample": alias, "reason": "source_unavailable"})
            continue
        with workflow.connect() as db:
            item = db.execute("SELECT author_id FROM items WHERE platform='xiaohongshu' AND item_id=?",
                              (sample.get("item_id"),)).fetchone()
        if not item or item[0] != sample.get("author_id"):
            skipped.append({"sample": alias, "reason": "identity_mismatch"})
            continue
        if source.stat().st_size != size or sha256(source.read_bytes()).hexdigest() != digest:
            skipped.append({"sample": alias, "reason": "hash_mismatch"})
            continue
        workflow.attach_media("xiaohongshu", sample["item_id"], alias, source,
                              position=position, kind=kind, mime=mime)
        copied += 1
    return {"copied": copied, "skipped": skipped, "media_coverage": "unknown_expected_count"}
