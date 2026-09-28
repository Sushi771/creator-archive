"""Local archive workflow shared by platform adapters.

This module never obtains credentials or makes platform requests itself. An adapter
must supply verified author identity and pages with a trustworthy terminal signal.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from hashlib import sha256
from html import escape
from pathlib import Path
import json
import os
import re
import sqlite3
import tempfile
import time
from typing import Mapping
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .validation import AdapterFailure, HistoryAdapter, Page
from .metrics import migrate, read_metrics, read_snapshots, save_observation
from .folders import load_folders, mirror_author_to_obsidian, obsidian_author_dir


PLATFORMS = {"wechat", "xiaohongshu"}
MODES = {"full", "latest", "archive"}


def _id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_+/=-]{1,128}", value):
        raise ValueError("invalid_stable_id")
    return quote(value, safe="")


def _canonical_source_url(platform: str, value: str) -> str:
    """Retain only public article identity parameters, never share-session tokens."""
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("invalid_detail")
    try:
        if parsed.port not in (None, 443):
            raise ValueError("invalid_detail")
    except ValueError:
        raise ValueError("invalid_detail") from None
    host = parsed.hostname.lower()
    allowed = ({"mp.weixin.qq.com"} if platform == "wechat" else
               {"www.xiaohongshu.com", "xiaohongshu.com"})
    if host not in allowed:
        raise ValueError("invalid_detail")
    query = ""
    if platform == "wechat" and parsed.path == "/s":
        query = urlencode([(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                           if key in {"__biz", "mid", "idx", "sn"}])
    return urlunsplit(("https", host, parsed.path, query, ""))


def _managed_write(path: Path, content: bytes) -> Path:
    """Never overwrite a changed file, including a user's edits to a generated file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == content:
            return path
        digest = sha256(content).hexdigest()[:12]
        path = path.with_name(f"{path.stem}.{digest}{path.suffix}")
        if path.exists():
            if path.read_bytes() == content:
                return path
            raise ValueError("managed_file_conflict")
    temp = path.with_name(path.name + f".{os.getpid()}.part")
    try:
        with temp.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return path


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _managed_copy(path: Path, source: Path, mime: str) -> tuple[Path, int, str]:
    """Stream a local media file into managed storage with bounded memory."""
    signatures = {"image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG\r\n\x1a\n",
                  "image/webp": b"RIFF", "video/mp4": b""}
    if mime not in signatures:
        raise ValueError("unsupported_mime")
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = sha256()
    size = 0
    header = b""
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".part", prefix="media-",
                                         dir=path.parent, delete=False) as output, Path(source).open("rb") as input_file:
            temp = Path(output.name)
            while chunk := input_file.read(1024 * 1024):
                if len(header) < 12:
                    header = (header + chunk)[:12]
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        if not size or not header.startswith(signatures[mime]):
            raise ValueError("invalid_media_file")
        if mime == "image/webp" and header[8:12] != b"WEBP":
            raise ValueError("invalid_media_file")
        if mime == "video/mp4" and header[4:8] != b"ftyp":
            raise ValueError("invalid_media_file")
        hexdigest = digest.hexdigest()
        if path.exists():
            if path.stat().st_size == size and _file_sha256(path) == hexdigest:
                return path, size, hexdigest
            path = path.with_name(f"{path.stem}.{hexdigest[:12]}{path.suffix}")
            if path.exists():
                if path.stat().st_size == size and _file_sha256(path) == hexdigest:
                    return path, size, hexdigest
                raise ValueError("managed_file_conflict")
        os.replace(temp, path)
        temp = None
        return path, size, hexdigest
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


class ArchiveWorkflow:
    """Durable per-author scans and offline exports; one local caller at a time."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.archive_root, self.obsidian_root = load_folders(self.root)
        if (self.root / "folders.json").exists() and not self.archive_root.is_dir():
            raise ValueError("已配置归档目录不可访问；原数据库未改动。请恢复该目录，或停机后核对folders.json及备份。")
        self.db_path = self.root / "archive.sqlite3"
        existing_database = self.db_path.is_file()
        with self.connect() as db:
            db.executescript("""
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS subscriptions (
                    platform TEXT NOT NULL, author_id TEXT NOT NULL,
                    display_name TEXT NOT NULL, identity_evidence TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
                    PRIMARY KEY(platform,author_id));
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL, platform TEXT NOT NULL,
                    author_id TEXT NOT NULL, mode TEXT NOT NULL, adapter_version TEXT NOT NULL,
                    cursor TEXT, pages INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'queued', coverage TEXT NOT NULL DEFAULT 'unknown',
                    reason TEXT, terminal_evidence TEXT, retry_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    UNIQUE(batch_id,platform,author_id));
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY, mode TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS items (
                    platform TEXT NOT NULL, item_id TEXT NOT NULL, author_id TEXT NOT NULL,
                    published_at TEXT NOT NULL, detail_text TEXT, source_url TEXT,
                    detail_state TEXT NOT NULL DEFAULT 'missing',
                    PRIMARY KEY(platform,item_id));
                CREATE TABLE IF NOT EXISTS pages (
                    run_id INTEGER NOT NULL, page_number INTEGER NOT NULL,
                    request_cursor TEXT NOT NULL, next_cursor TEXT, item_ids TEXT NOT NULL,
                    terminal_evidence TEXT, observed_at REAL NOT NULL,
                    PRIMARY KEY(run_id,page_number), UNIQUE(run_id,request_cursor));
                CREATE TABLE IF NOT EXISTS assets (
                    platform TEXT NOT NULL, item_id TEXT NOT NULL, asset_id TEXT NOT NULL,
                    position INTEGER NOT NULL, kind TEXT NOT NULL, relative_path TEXT NOT NULL,
                    bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, mime TEXT NOT NULL,
                    PRIMARY KEY(platform,item_id,asset_id));
            """)
        self.migration_backup = migrate(self.db_path, backup_required=existing_database)
        self.cancellation_migration_backup = self._initialize_subscription_cancellations(existing_database)
        self.tags_migration_backup = self._initialize_subscription_tags(existing_database)

    def _initialize_subscription_tags(self, backup_required):
        """Add local author labels without rewriting subscriptions or archive data."""
        with self.connect() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='subscription_tags'").fetchone():
                return None
            backup = None
            if backup_required:
                backups = self.db_path.parent / "backups"
                backups.mkdir(exist_ok=True)
                backup = backups / f"archive-before-subscription-tags-{time.time_ns()}.sqlite3"
                with closing(sqlite3.connect(backup)) as target:
                    db.backup(target)
                    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("数据库备份校验失败，未执行作者标签升级")
            db.execute("""CREATE TABLE subscription_tags (
                platform TEXT NOT NULL, author_id TEXT NOT NULL, tag TEXT NOT NULL,
                position INTEGER NOT NULL,
                PRIMARY KEY(platform,author_id,tag))""")
            return str(backup) if backup else None

    def _initialize_subscription_cancellations(self, backup_required):
        """Keep archived authors and old rows intact while adding cancel state."""
        with self.connect() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='subscription_cancellations'").fetchone():
                return None
            backup = None
            if backup_required:
                backups = self.db_path.parent / "backups"
                backups.mkdir(exist_ok=True)
                backup = backups / f"archive-before-subscription-cancel-{time.time_ns()}.sqlite3"
                with closing(sqlite3.connect(backup)) as target:
                    db.backup(target)
                    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("数据库备份校验失败，未执行取消订阅升级")
            db.execute("""CREATE TABLE subscription_cancellations (
                platform TEXT NOT NULL, author_id TEXT NOT NULL, cancelled_at REAL NOT NULL,
                PRIMARY KEY(platform,author_id))""")
            return str(backup) if backup else None

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    def subscribe(self, platform: str, author_id: str, display_name: str,
                  *, verified: bool, evidence: str) -> None:
        if platform not in PLATFORMS or not verified or not re.fullmatch(r"[A-Za-z0-9:_ -]{1,128}", evidence):
            raise ValueError("verified_identity_required")
        _id(author_id)
        if not display_name.strip():
            raise ValueError("display_name_required")
        with self.connect() as db:
            db.execute("""INSERT INTO subscriptions VALUES(?,?,?,?,1,?)
                ON CONFLICT(platform,author_id) DO UPDATE SET display_name=excluded.display_name,
                identity_evidence=excluded.identity_evidence""",
                (platform, author_id, display_name.strip(), evidence.strip(), time.time()))

    def set_enabled(self, platform: str, author_id: str, enabled: bool) -> None:
        with self.connect() as db:
            result = db.execute("UPDATE subscriptions SET enabled=? WHERE platform=? AND author_id=?",
                                (int(enabled), platform, author_id))
            if result.rowcount != 1:
                raise KeyError((platform, author_id))

    def subscriptions(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM subscriptions ORDER BY platform,author_id")]

    def _batch(self, mode: str, batch_id: int | None, include_paused: bool) -> tuple[int, list[dict]]:
        if mode not in MODES:
            raise ValueError("invalid_mode")
        with self.connect() as db:
            if batch_id is None:
                cur = db.execute("INSERT INTO batches(mode,created_at) VALUES(?,?)", (mode, time.time()))
                batch_id = cur.lastrowid
                scope = db.execute("""SELECT s.platform,s.author_id FROM subscriptions s
                    WHERE NOT EXISTS (SELECT 1 FROM subscription_cancellations c
                                      WHERE c.platform=s.platform AND c.author_id=s.author_id)""" +
                    ("" if include_paused else " AND s.enabled=1") +
                    " ORDER BY s.platform,s.author_id").fetchall()
                for row in scope:
                    db.execute("""INSERT INTO runs(batch_id,platform,author_id,mode,adapter_version,updated_at)
                        VALUES(?,?,?,?,?,?)""",
                        (batch_id, row["platform"], row["author_id"], mode, "pending", time.time()))
            else:
                row = db.execute("SELECT mode FROM batches WHERE id=?", (batch_id,)).fetchone()
                if row is None or row["mode"] != mode:
                    raise ValueError("batch_mode_mismatch")
            runs = [dict(row) for row in db.execute(
                "SELECT * FROM runs WHERE batch_id=? ORDER BY platform,author_id", (batch_id,))]
        return batch_id, runs

    def _stop(self, run_id: int, state: str, reason: str, retry_at: float = 0) -> None:
        coverage = "blocked" if state in {"needs_login", "rate_limited", "failed"} else "partial"
        with self.connect() as db:
            db.execute("UPDATE runs SET state=?,coverage=?,reason=?,retry_at=?,updated_at=? WHERE id=?",
                       (state, coverage, reason, retry_at, time.time(), run_id))

    def _commit_page(self, run_id: int, cursor: str | None, page: Page) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._commit_page_in_db(db, run_id, cursor, page)

    def _commit_page_in_db(self, db, run_id, cursor, page):
        """Validate and commit a page inside the caller-owned transaction."""
        row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row["cursor"] != cursor or row["state"] == "succeeded":
            raise ValueError("stale_checkpoint")
        seen = {r[0] for r in db.execute("SELECT request_cursor FROM pages WHERE run_id=?", (run_id,))}
        requested = cursor or ""
        if type(page.has_more) is not bool:
            raise ValueError("invalid_page")
        if page.has_more:
            if not page.items:
                raise ValueError("empty_nonterminal_page")
            if not isinstance(page.next_cursor, str) or not page.next_cursor:
                raise ValueError("missing_cursor")
            if page.next_cursor == requested or page.next_cursor in seen:
                raise ValueError("repeated_cursor")
        elif page.next_cursor is not None or not page.terminal_evidence:
            raise ValueError("missing_terminal_evidence")
        for item in page.items:
            if not item.item_id or item.author_id != row["author_id"]:
                raise ValueError("identity_mismatch")
            _id(item.item_id)
            old = db.execute("SELECT author_id FROM items WHERE platform=? AND item_id=?",
                             (row["platform"], item.item_id)).fetchone()
            if old and old[0] != item.author_id:
                raise ValueError("identity_mismatch")
            db.execute("""INSERT INTO items(platform,item_id,author_id,published_at)
                VALUES(?,?,?,?) ON CONFLICT(platform,item_id)
                DO UPDATE SET published_at=CASE WHEN excluded.published_at!='' THEN excluded.published_at ELSE items.published_at END""",
                (row["platform"], item.item_id, item.author_id, item.published_at))
        db.execute("INSERT INTO pages VALUES(?,?,?,?,?,?,?)",
                   (run_id, row["pages"] + 1, requested, page.next_cursor,
                    json.dumps([i.item_id for i in page.items]), page.terminal_evidence, time.time()))
        db.execute("""UPDATE runs SET cursor=?,pages=pages+1,state=?,coverage=?,reason=NULL,
            terminal_evidence=?,retry_at=0,updated_at=? WHERE id=?""",
            (page.next_cursor, "running" if page.has_more else "succeeded",
             "scanning" if page.has_more else "complete_for_accessible_scope",
             page.terminal_evidence, time.time(), run_id))

    def run_all(self, adapters: Mapping[str, HistoryAdapter], *, mode: str = "full",
                batch_id: int | None = None, include_paused: bool = True,
                login_confirmed: bool = False, max_pages: int = 100,
                now: float | None = None) -> dict:
        """Scan a fixed subscription snapshot. A failed author does not stop others."""
        batch_id, runs = self._batch(mode, batch_id, include_paused)
        fixed_now = now
        now = time.time() if fixed_now is None else fixed_now
        for run in runs:
            run_id = run["id"]
            current_time = time.time() if fixed_now is None else fixed_now
            if run["state"] == "succeeded" or run["retry_at"] > current_time:
                continue
            with self.connect() as db:
                platform_retry = db.execute("SELECT coalesce(max(retry_at),0) FROM runs WHERE platform=?", (run["platform"],)).fetchone()[0]
            if platform_retry > current_time:
                self._stop(run_id, "rate_limited", "rate_limited", platform_retry)
                continue
            if run["state"] == "needs_login" and not login_confirmed:
                continue
            adapter = adapters.get(run["platform"])
            if adapter is None:
                self._stop(run_id, "partial", "adapter_unavailable")
                continue
            if run["adapter_version"] == "pending":
                with self.connect() as db:
                    db.execute("UPDATE runs SET adapter_version=? WHERE id=?", (adapter.version, run_id))
            elif run["adapter_version"] != adapter.version:
                self._stop(run_id, "partial", "adapter_version_changed")
                continue
            for _ in range(max_pages):
                with self.connect() as db:
                    current = db.execute("SELECT cursor FROM runs WHERE id=?", (run_id,)).fetchone()[0]
                try:
                    page = adapter.page(run["author_id"], current)
                    self._commit_page(run_id, current, page)
                except AdapterFailure as error:
                    state = error.category if error.category in {"needs_login", "rate_limited"} else "partial"
                    failure_time = time.time() if fixed_now is None else fixed_now
                    retry_at = failure_time + max(error.retry_after, 5) if state == "rate_limited" else 0
                    self._stop(run_id, state, error.category, retry_at)
                    break
                except ValueError as error:
                    allowed = {"stale_checkpoint", "invalid_page", "missing_cursor", "repeated_cursor",
                               "empty_nonterminal_page", "missing_terminal_evidence", "identity_mismatch",
                               "invalid_stable_id"}
                    self._stop(run_id, "partial", str(error) if str(error) in allowed else "invalid_response")
                    break
                except Exception:
                    self._stop(run_id, "failed", "unexpected_adapter_error")
                    break
                if not page.has_more:
                    break
            else:
                self._stop(run_id, "partial", "page_budget_reached")
        result = self.status(batch_id)
        if mode == "archive":
            result["export"] = self.export_all(batch_id=batch_id)
        return result

    def status(self, batch_id: int | None = None) -> dict:
        with self.connect() as db:
            if batch_id is None:
                # All-author orchestration batches have fixed members but no
                # workflow runs of their own; keep the legacy default on the
                # latest batch that actually owns a run.
                row = db.execute("SELECT max(batch_id) FROM runs").fetchone()
                batch_id = row[0]
            if batch_id is None:
                return {"batch_id": None, "runs": [], "evidence_level": "adapter_supplied"}
            runs = [dict(row) for row in db.execute("""SELECT r.*,
                (SELECT count(*) FROM items i WHERE i.platform=r.platform AND i.author_id=r.author_id) AS item_count
                FROM runs r WHERE batch_id=? ORDER BY platform,author_id""", (batch_id,))]
            return {"batch_id": batch_id, "runs": runs, "evidence_level": "adapter_supplied"}

    def save_detail(self, platform: str, item_id: str, author_id: str,
                    text: str, source_url: str) -> None:
        """Record adapter supplied plain text; blank detail remains retryable."""
        if platform not in PLATFORMS or not text.strip():
            raise ValueError("invalid_detail")
        source_url = _canonical_source_url(platform, source_url)
        with self.connect() as db:
            old = db.execute("SELECT author_id FROM items WHERE platform=? AND item_id=?",
                             (platform, item_id)).fetchone()
            if old is None or old[0] != author_id:
                raise ValueError("identity_mismatch")
            db.execute("UPDATE items SET detail_text=?,source_url=?,detail_state='complete' WHERE platform=? AND item_id=?",
                       (text, source_url, platform, item_id))

    def attach_media(self, platform: str, item_id: str, asset_id: str,
                     source: Path, *, position: int, kind: str, mime: str) -> Path:
        """Import an already fetched local file without trusting its basename."""
        _id(asset_id)
        if kind not in {"image", "video"} or position < 0:
            raise ValueError("invalid_asset")
        with self.connect() as db:
            row = db.execute("SELECT author_id FROM items WHERE platform=? AND item_id=?",
                             (platform, item_id)).fetchone()
        if row is None:
            raise KeyError((platform, item_id))
        suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "video/mp4": ".mp4"}[mime]
        relative = Path(platform) / _id(row[0]) / _id(item_id) / "assets" / f"{_id(asset_id)}{suffix}"
        target, size, digest = _managed_copy(self.archive_root / relative, Path(source), mime)
        with self.connect() as db:
            db.execute("""INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(platform,item_id,asset_id) DO UPDATE SET
                position=excluded.position,kind=excluded.kind,relative_path=excluded.relative_path,
                bytes=excluded.bytes,sha256=excluded.sha256,mime=excluded.mime""",
                (platform, item_id, asset_id, position, kind, str(target.relative_to(self.archive_root)),
                 size, digest, mime))
        return target

    def save_metrics(self, platform, item_id, metrics, **observation):
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM items WHERE platform=? AND item_id=?", (platform,item_id)).fetchone():
                raise KeyError("item_not_found")
            return save_observation(db, platform, item_id, metrics, **observation)

    def asset_valid(self, platform, item_id, asset_id):
        with self.connect() as db:
            row = db.execute("SELECT relative_path,bytes,sha256 FROM assets WHERE platform=? AND item_id=? AND asset_id=?", (platform,item_id,asset_id)).fetchone()
        if not row:
            return False
        path = self.archive_root / row["relative_path"]
        return path.is_file() and path.stat().st_size == row["bytes"] and _file_sha256(path) == row["sha256"]

    def archive_asset_valid(self, relative_path: str) -> bool | None:
        """Return None for non-asset files; verify every registration for an asset path."""
        windows_path = relative_path.replace("/", "\\")
        with self.connect() as db:
            rows = db.execute("""SELECT platform,item_id,asset_id FROM assets
                WHERE relative_path=? COLLATE NOCASE OR relative_path=? COLLATE NOCASE""",
                (relative_path, windows_path)).fetchall()
        if not rows:
            return None
        return all(self.asset_valid(row["platform"], row["item_id"], row["asset_id"]) for row in rows)

    def candidate_saved(self, platform, item_id, candidate):
        """Reuse verified legacy files at the same item/kind/position, without aliases.

        Legacy imports used different asset names. Their stored position is used
        verbatim; no one-based/zero-based conversion or quality equivalence is
        guessed. A video can never be satisfied by its image cover.
        """
        with self.connect() as db:
            exact = db.execute("SELECT kind,position FROM assets WHERE platform=? AND item_id=? AND asset_id=?", (platform,item_id,candidate.asset_id)).fetchone()
            legacy = [r[0] for r in db.execute("SELECT asset_id FROM assets WHERE platform=? AND item_id=? AND kind=? AND position=?", (platform,item_id,candidate.kind,candidate.position))] if not exact else []
        if exact:
            return exact["kind"] == candidate.kind and exact["position"] == candidate.position and self.asset_valid(platform,item_id,candidate.asset_id)
        return any(self.asset_valid(platform,item_id,asset_id) for asset_id in legacy)

    def export_all(self, *, batch_id: int | None = None) -> dict:
        """Write offline artifacts for the fixed batch scope, preserving edited files."""
        with self.connect() as db:
            if batch_id is None:
                scope = db.execute("SELECT platform,author_id,display_name FROM subscriptions ORDER BY platform,author_id").fetchall()
            else:
                scope = db.execute("""SELECT s.platform,s.author_id,s.display_name FROM runs r
                    JOIN subscriptions s USING(platform,author_id) WHERE r.batch_id=?
                    ORDER BY s.platform,s.author_id""", (batch_id,)).fetchall()
            run_map = {(r["platform"], r["author_id"]): dict(r) for r in db.execute(
                "SELECT * FROM runs WHERE batch_id=?", (batch_id,))} if batch_id is not None else {}
            result = []
            for author in scope:
                platform, author_id = author["platform"], author["author_id"]
                base = self.archive_root / platform / _id(author_id)
                items = [dict(r) for r in db.execute("SELECT * FROM items WHERE platform=? AND author_id=? ORDER BY published_at,item_id",
                                                   (platform, author_id))]
                manifest_items = []
                corpus = []
                missing_assets = 0
                for item in items:
                    metrics = read_metrics(db, platform, item["item_id"])
                    snapshots = read_snapshots(db, platform, item["item_id"])
                    item_dir = base / _id(item["item_id"])
                    assets = [dict(r) for r in db.execute("SELECT * FROM assets WHERE platform=? AND item_id=? ORDER BY position,asset_id",
                                                       (platform, item["item_id"]))]
                    media_md = []
                    media_html = []
                    for asset in assets:
                        local = self.archive_root / asset["relative_path"]
                        asset["state"] = ("complete" if local.is_file() and local.stat().st_size == asset["bytes"]
                                          and _file_sha256(local) == asset["sha256"] else "missing")
                        asset["relative_path"] = Path(asset["relative_path"]).as_posix()
                        if asset["state"] != "complete":
                            missing_assets += 1
                            continue
                        relative = local.relative_to(item_dir).as_posix()
                        label = escape(asset["asset_id"])
                        if asset["kind"] == "image":
                            media_md.append(f"![{asset['asset_id']}]({relative})")
                            media_html.append(f'<figure><img src="{escape(relative, quote=True)}" alt="{label}" loading="lazy">'
                                              f'<figcaption>图片 {asset["position"] + 1}</figcaption></figure>')
                        else:
                            media_md.append(f"[视频 {asset['asset_id']}]({relative})")
                            media_html.append(f'<figure><video controls preload="metadata" src="{escape(relative, quote=True)}"></video>'
                                              f'<figcaption>视频 {asset["position"] + 1}</figcaption></figure>')
                    files = {}
                    if item["detail_state"] == "complete":
                        body = item["detail_text"]
                        title = item["title"] or item["item_id"]
                        md = (f"# {title}\n\n平台：{platform} · 作者ID：{author_id} · 作品ID：{item['item_id']}\n\n"
                              f"{body}\n\n" + "\n\n".join(media_md) +
                              f"\n\n来源：{item['source_url']}\n").encode("utf-8")
                        html = ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
                                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                                f"<title>{escape(title)}</title>"
                                "<style>body{margin:0;background:#f7f8f6;color:#1d2924;font:17px/1.85 system-ui,'Microsoft YaHei',sans-serif}"
                                "main{max-width:760px;margin:0 auto;padding:42px 24px 96px}h1{font-size:1.7em;line-height:1.4}"
                                ".meta,figcaption{font-size:.78em;color:#647369}.body{white-space:pre-wrap;overflow-wrap:anywhere;margin:32px 0}"
                                "figure{margin:28px 0}img,video{max-width:100%;height:auto;border-radius:8px;display:block}"
                                "a{color:#285d4a}footer{margin-top:42px;border-top:1px solid #d7dfd8;padding-top:20px;overflow-wrap:anywhere}"
                                "@media(prefers-color-scheme:dark){body{background:#17201b;color:#e8eee8}.meta,figcaption{color:#acb9ad}"
                                "a{color:#a5d7b8}footer{border-color:#3d4d40}}</style></head><body><main>"
                                f"<h1>{escape(title)}</h1><p class='meta'>{escape(platform)} · {escape(author_id)} · {escape(item['item_id'])}</p>"
                                f"<div class='body'>{escape(body)}</div>" + "".join(media_html) +
                                f"<footer>来源：<a href='{escape(item['source_url'], quote=True)}'>{escape(item['source_url'])}</a></footer>"
                                "</main></body></html>").encode("utf-8")
                        for name, content in (("article.md", md), ("index.html", html)):
                            files[name] = _managed_write(item_dir / name, content).relative_to(base).as_posix()
                        corpus.append({"schema_version": 2, "platform": platform, "author_id": author_id,
                                       "item_id": item["item_id"], "source_url": item["source_url"],
                                       "published_at": item["published_at"], "text": body,
                                       "metrics": metrics, "metric_snapshots": snapshots, "content_type": item["content_type"],
                                       "content_hash": sha256(body.encode("utf-8")).hexdigest(),
                                       "media_refs": [a["relative_path"] for a in assets if a["state"] == "complete"],
                                       "status": "detail_complete"})
                    manifest_items.append({"item_id": item["item_id"], "detail_state": item["detail_state"],
                                           "title": item["title"], "content_type": item["content_type"], "published_at": item["published_at"],
                                           "metrics": metrics, "metric_snapshots": snapshots, "media_state": item["media_state"],
                                           "files": files, "assets": assets})
                run = run_map.get((platform, author_id))
                scan_path = None
                if run and run["mode"] == "author_archive":
                    scan = self._author_scan_manifest(db, run, items)
                    if scan is not None:
                        scan_path = _managed_write(base / f"scan-run-{run['id']}.json",
                                                   json.dumps(scan, ensure_ascii=False, indent=2).encode("utf-8"))
                manifest = {"schema_version": 2, "platform": platform, "author_id": author_id,
                            "display_name": author["display_name"],
                            "coverage": run["coverage"] if run else "unknown",
                            "terminal_evidence": run["terminal_evidence"] if run else None,
                            "missing_registered_assets": missing_assets,
                            "media_coverage": "unknown_expected_count",
                            "items": manifest_items}
                manifest_path = _managed_write(base / "manifest.json",
                                               json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
                corpus_path = _managed_write(base / "corpus.jsonl",
                                             "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in corpus).encode("utf-8"))
                failures = [{"item_id": item["item_id"], "reason": "detail_missing"}
                            for item in manifest_items if item["detail_state"] != "complete"]
                failures.extend({"item_id": item["item_id"], "asset_id": asset["asset_id"],
                                 "reason": "registered_asset_missing_or_corrupt"}
                                for item in manifest_items for asset in item["assets"]
                                if asset["state"] != "complete")
                failures_path = _managed_write(base / "failures.json",
                                               json.dumps({"scope": "saved_local_items", "entries": failures},
                                                          ensure_ascii=False, indent=2).encode("utf-8"))
                # A complete author index is independent of the interactive API's
                # page size. Link actual managed filenames, including hash-suffixed
                # replacements when an earlier export was edited by the user.
                index_rows = []
                for item in manifest_items:
                    label = escape(item["item_id"])
                    article = item["files"].get("index.html")
                    if article:
                        href = escape(quote(Path(article).as_posix(), safe="/"), quote=True)
                        entry = f'<a href="{href}">{label}</a> · 正文已保存'
                    else:
                        entry = f"{label} · 正文缺失，待补齐"
                    for asset in item["assets"]:
                        if asset["state"] != "complete":
                            continue
                        local = self.archive_root / asset["relative_path"]
                        href = escape(quote(local.relative_to(base).as_posix(), safe="/"), quote=True)
                        asset_label = "图片" if asset["kind"] == "image" else "视频"
                        entry += f' · <a href="{href}">{asset_label} {escape(asset["asset_id"])}</a>'
                    index_rows.append(f"<li>{entry}</li>")
                coverage_text = ("已观察到当前可获取范围的明确末页" if
                                 manifest["coverage"] == "complete_for_accessible_scope" and manifest["terminal_evidence"]
                                 else "历史覆盖尚未确认，不能据此认为已到末页")
                scan_link = (f' · <a href="{escape(quote(scan_path.name), quote=True)}">本轮扫描清单</a>'
                             if scan_path is not None else "")
                scan_notice = (f'<p>本轮观察 {scan["counts"]["observed_unique"]} 个唯一作品；'
                               f'库中但本轮未观察 {scan["counts"]["library_only"]} 个。未见作品无法枚举；'
                               '本轮完成数含复用资源，不等于新下载数。</p>' if scan_path is not None else "")
                index_html = (
                    '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width, initial-scale=1">'
                    f'<title>{escape(author["display_name"])} · 作者归档</title></head><body>'
                    f'<main><h1>{escape(author["display_name"])} · 作者归档</h1>'
                    f'<p>平台：{escape(platform)} · 作者ID：{escape(author_id)}</p>'
                    f'<p>已保存 {len(items)} 条作品记录；正文已保存 {len(corpus)} 条，缺失 {len(items) - len(corpus)} 条。</p>'
                    f'<p>历史覆盖：{coverage_text}。此状态仅针对列表，不代表正文或媒体完整。</p>'
                    f'<p>媒体预期总数未知；已登记但缺失或校验失败的附件：{missing_assets} 个。</p>'
                    + scan_notice +
                    f'<nav><a href="{escape(quote(manifest_path.name), quote=True)}">归档清单</a> · '
                    f'<a href="{escape(quote(corpus_path.name), quote=True)}">正文语料 JSONL</a> · '
                    f'<a href="{escape(quote(failures_path.name), quote=True)}">失败清单</a>{scan_link}</nav>'
                    '<h2>全部已保存作品</h2><ol>' + "".join(index_rows) + '</ol></main></body></html>'
                )
                index_path = _managed_write(base / "index.html", index_html.encode("utf-8"))
                result.append({"platform": platform, "author_id": author_id, "items": len(items),
                               "details": len(corpus), "manifest": str(manifest_path), "corpus": str(corpus_path),
                               "failures": str(failures_path), "index": str(index_path),
                               "coverage": manifest["coverage"], "missing_registered_assets": missing_assets,
                               "media_coverage": "unknown_expected_count"})
                if scan_path is not None:
                    result[-1]["scan_manifest"] = str(scan_path)
                if self.obsidian_root and self.obsidian_root != self.archive_root:
                    result[-1]["obsidian_copied_files"] = mirror_author_to_obsidian(
                        base, obsidian_author_dir(self.obsidian_root, platform, _id(author_id)))
        return {"authors": result, "archive_root": str(self.archive_root),
                "obsidian_root": str(self.obsidian_root) if self.obsidian_root else None}

    def _author_scan_manifest(self, db, run: dict, library_items: list[dict]) -> dict | None:
        """Describe one durable author scan without changing the all-library schema-2 export."""
        parent = db.execute("SELECT id FROM jobs WHERE run_id=? AND mode='author_archive'", (run["id"],)).fetchone()
        if parent is None:
            return None
        observed = {}
        for page in db.execute("SELECT page_number,item_ids FROM pages WHERE run_id=? ORDER BY page_number", (run["id"],)):
            for item_id in json.loads(page["item_ids"]):
                observed.setdefault(item_id, []).append(page["page_number"])
        target_rows = db.execute("""SELECT j.item_id,j.state,j.reason FROM pipeline_pages p
            JOIN job_items j ON j.job_id=p.child_job_id WHERE p.parent_job_id=?""", (parent["id"],))
        targets = {}
        for row in target_rows:
            targets.setdefault(row["item_id"], []).append(dict(row))
        library = {item["item_id"]: item for item in library_items}
        observed_items = []
        for item_id, pages in observed.items():
            states = targets.get(item_id, [])
            if any(row["state"] == "succeeded" for row in states):
                state, reason = "complete", None
            elif any(row["state"] == "partial" for row in states):
                state = "partial"
                reason = next((row["reason"] for row in states if row["state"] == "partial" and row["reason"]), None)
            else:
                state, reason = "pending" if states else "not_targeted", None
            item = library.get(item_id)
            observed_items.append({"item_id": item_id, "pages": pages, "download_state": state,
                                   "reason": reason, "detail_state": item["detail_state"] if item else "missing",
                                   "media_state": item["media_state"] if item else "unknown"})
        library_only = [item["item_id"] for item in library_items if item["item_id"] not in observed]
        return {"schema_version": 2, "kind": "author_scan", "platform": run["platform"],
                "author_id": run["author_id"], "run_id": run["id"], "parent_job_id": parent["id"],
                "pages_scanned": run["pages"], "coverage": run["coverage"],
                "list_finished": bool(run["terminal_evidence"]),
                "unseen_items": "unknown_not_enumerable",
                "download_note": "complete is the content task state and includes reused resources; inspect the full-library manifest for offline asset validation; it does not count new downloads",
                "counts": {"observed_unique": len(observed_items), "targeted_unique": len(targets),
                           "complete": sum(item["download_state"] == "complete" for item in observed_items),
                           "partial": sum(item["download_state"] == "partial" for item in observed_items),
                           "pending": sum(item["download_state"] == "pending" for item in observed_items),
                           "not_targeted": sum(item["download_state"] == "not_targeted" for item in observed_items),
                           "library_only": len(library_only)},
                "observed_items": observed_items, "library_only_item_ids": library_only}
