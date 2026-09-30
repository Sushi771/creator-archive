"""Local product workspace: durable jobs and explicit evidence boundaries.

The existing archive workflow owns pages, checkpoints and exports. This service
adds a thin queue and never treats imported observations as a live transport.
"""
from __future__ import annotations

import errno
import json
from contextlib import closing
from datetime import date, timedelta
import os
import re
import shutil
from pathlib import Path
import sqlite3
from threading import Event, Lock, RLock, Thread
import time
from urllib.parse import quote, urlsplit

from .links import classify
from .workflow import ArchiveWorkflow, _id, _canonical_source_url
from .validation import AdapterFailure
from .metrics import FIELDS, read_metrics, read_snapshots
from .adapters.xhs_share import expand_share_link
from . import page_pipeline, network_safety
from hashlib import sha256
from contextvars import copy_context

XHS_HISTORY_PAGE_BUDGET = 100
XHS_RECENT_WINDOW_SIZE = 30
XHS_LEGACY_NETWORK_MODES = {"full", "author_archive", "page_archive", "demo_archive", "latest", "source_refresh"}
XHS_RECENT_WINDOW_WARNING = "最近内容同步 / 当前来源窗口约30篇；两次刷新间新增超过窗口可能遗漏，置顶或列表排序也可能缩小最近范围。"


def default_workspace() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share"))) / "CreatorArchive/workspace"


MESSAGES = {
    "manual_media_budget_exceeded": ("前三篇待归档媒体超过本次12次媒体请求预算，已停止。", "查看手动验收入口的所需数量；不自行提高预算，不重复重试。"),
    "recent_window_complete": ("最近第一页的新作品正文与当前可获取媒体已归档；已有作品保持。", XHS_RECENT_WINDOW_WARNING),
    "recent_window_unchanged": ("最近第一页没有尚未归档的新作品；已有正文与附件保持。", XHS_RECENT_WINDOW_WARNING),
    "recent_window_partial": ("最近第一页部分新作品正文或媒体仍有缺口；成功资源保持。", "可恢复本窗口未完成作品；不扫描旧历史。" + XHS_RECENT_WINDOW_WARNING),
    "legacy_history_retired": ("旧深分页任务已停用，代码与检查点保留作历史证据。", "请从作者卡片开始最近内容同步；旧页及 -100 不阻塞最近窗口版本。"),
    "network_paused": ("小红书联网因账号安全已暂停；已有资料和检查点保留。", "仅可离线阅读与导出；恢复须另有具体方案和用户明确批准。"),
    "unknown_business_error": ("来源返回未知业务失败；原始业务码与检查点关联保留，未判末页。", "停止该来源，不自动重试；仅使用已有证据离线排查。"),
    "account_safety_user_instruction_2026_09_30": ("小红书真实联网已持久暂停。", "本轮只做离线核对，不运行登录、验证或恢复。"),
    "user_paused": ("已按请求暂停，列表检查点、正文和已成功媒体保持。", "从原任务继续，系统只处理尚未完成的范围。"),
    "source_refresh_unchanged": ("后台来源没有新增、变化或待补内容；本次仅核对当前 Feed 所列范围。", "需要全历史时启动带可信分页的作者历史任务；本次不证明历史末页。"),
    "source_refresh_complete": ("当前 Feed 列出的待处理作品已保存；历史覆盖仍须另行核验。", "可本机导出；如需全历史，请使用支持可信分页的来源。"),
    "source_refresh_partial": ("当前 Feed 有正文或媒体缺口；已保存成功内容。", "查看失败作品和来源状态后恢复本任务。"),
    "demo_archive_complete": ("该作者本轮前10篇范围的可获取正文、媒体和已观察数据已归档；不足10篇时以可信末页为准。", "打开作者归档查看结果。已有成功资源已复用，本轮完成不代表全历史完成。"),
    "demo_archive_partial": ("该作者本轮前10篇范围内仍有未完成作品；固定目标和成功资源已保留，失败项不会被后续作品替换。", "查看未完成清单并处理具体原因，再恢复本父任务；只重试原范围内未完成作品。"),
    "author_archive_complete": ("该作者本次列表已到可信末页，所列作品正文和当前可获取媒体已归档。", "打开作者归档；此结果仅对应本次观察到的可获取范围，不代表双平台G1通过。"),
    "author_archive_partial": ("该作者本次列表已到可信末页，部分正文媒体仍未完成；成功资源与固定子任务已保留。", "查看未完成清单并处理具体原因，再恢复本父任务；只重试未完成作品。"),
    "repeated_cursor": ("作者列表返回了重复游标，无法确认后续页链；成功页面和资源已保留，未判定末页。", "恢复原父任务重试当前页；若仍重复，请保留检查点排查页链变化。"),
    "missing_cursor": ("作者列表仍有后续页面但缺少有效游标；已保存先前进度，未判定末页。", "核对后台来源页链后恢复原任务，系统会重试当前页。"),
    "empty_nonterminal_page": ("作者列表返回空页但未明确结束；先前页面和资源已保留，未判定末页。", "核对后台来源后恢复原任务，系统会重试当前页。"),
    "missing_terminal_evidence": ("作者列表缺少可信末页证据；先前页面和资源已保留，未判定完成。", "需要来源提供明确的历史结束证据；已有资料仍可导出。"),
    "invalid_page": ("作者列表返回的分页状态无效；先前页面和资源已保留。", "恢复原任务重试当前页；若持续发生，请保留资料排查平台响应。"),
    "identity_mismatch": ("当前页的作品作者身份与任务不符；该页未提交，旧资料及先前进度已保留。", "核对来源中每篇作品的稳定作者身份；修正来源后恢复原任务。"),
    "stale_checkpoint": ("当前页面未能匹配已保存的检查点；旧资料及先前进度已保留。", "恢复原任务重试当前页；若持续发生，请保留资料排查。"),
    "invalid_stable_id": ("当前页包含无效作品标识；该页未提交，旧资料及先前进度已保留。", "恢复原任务重试当前页；若持续发生，请保留资料排查。"),
    "invalid_response": ("当前页未通过数据校验，原因尚未确定；该页未提交，先前进度已保留。", "恢复原任务重试当前页；若持续发生，请保留资料排查。"),
    "adapter_version_changed": ("采集组件版本与原任务不一致，原页面及资源已保留。", "恢复至原组件版本后重试该任务；确认兼容前不会自动推进检查点。"),
    "page_archive_complete": ("本次最多两页的正文媒体闭环已归档；两页预算不代表全历史完成。", "打开作者归档；列表覆盖与正文媒体状态分别查看。"),
    "page_archive_partial": ("本次两页实验已归档现有资料，部分正文媒体未完成；原页与固定子任务已保留。", "查看未完成条目并处理具体原因，再恢复本父任务；成功作品和资源不会重做。"),
    "transport_unavailable": ("后台采集来源尚未配置；已保留作品及分页检查点。", "为作者配置本人授权的后台来源；现有作品可直接导出。"),
    "source_unconfigured": ("作者尚未配置后台来源，未启动历史采集；已有资料保持。", "在作者卡片配置并核验来源后再启动历史同步。"),
    "wechat_blocked": ("公众号历史来源未验证，当前无后台权限且站点访问受限；未发起网络采集。", "待提供经授权且可验证的全历史来源；已有资料仍可浏览和归档。"),
    "identity_unverified": ("已保存订阅意图，作者身份尚未经过平台核验。", "等待可用的平台核验通道；不要把链接中的候选ID当作已核验身份。"),
    "process_interrupted": ("上次进程退出，成功页面和作品已保留。", "点击恢复，从有效检查点继续；归档可安全重试。"),
    "needs_login": ("后台来源登录已失效或需要验证；已保存进度。", "在来源自身的本人授权入口完成登录后点击恢复。"),
    "verification_required": ("来源要求验证；本批成功项和待处理进度已保留。", "在来源自身的本人授权入口完成验证后恢复原任务。"),
    "rate_limited": ("平台要求冷却；已保存进度。", "等待冷却时间结束后点击恢复，不切换账号或IP。"),
    "adapter_unavailable": ("当前作者没有可用后台来源；已有资料保留。", "配置来源后再恢复；也可先导出已有资料。"),
    "page_budget_reached": ("本轮达到安全页数预算，尚未观察到末页；检查点已保存。", "点击恢复继续下一批页面。"),
    "content_retry_budget_reached": ("历史末页已观察，本轮补取作品访问引用达到页数预算；成功资源和失败清单已保存。", "点击恢复继续补取未处理作品。"),
    "unexpected_error": ("任务出现未分类错误，原因尚未确定；成功页面和已有文件保留。", "重试一次；若仍失败，请保留工作目录供排查，无需删库。"),
    "export_failed": ("该作者导出文件失败，具体文件或目录原因尚未确定；列表、正文进度和已成功文件保留。", "检查该作者归档目录的写入权限、同名文件和剩余空间后恢复原任务；系统重试导出，不重采成功作品。"),
    "archive_complete": ("现有资料已按作者导出；缺失正文与媒体在清单中明确标记。", "打开归档清单查看结果；列表完整不代表正文或媒体完整。"),
    "validation_import": ("已导入先前真实验证的历史列表；本次没有发起在线采集。", "可浏览和归档已有作品；正文及媒体缺失仍需后续补齐。"),
    "timeout": ("作者来源请求超时，原因尚未确认；已保存成功页面。", "核对后台来源状态后恢复。"),
    "unavailable": ("后台来源暂不可用，具体原因未知；已保存进度和旧指标。", "核对来源状态后恢复原任务；成功媒体和检查点保留。"),
    "invalid_cursor": ("当前页面未能衔接保存的游标；旧记录未细分具体原因，没有跳过未知页面。", "先核对后台来源页链，再恢复原任务一次；若持续发生，请保留检查点排查。"),
    "cursor_response_conflict": ("同一请求游标观察到不同列表页；先前页面和资源已保留，未判定末页。", "核对该作者专用浏览器中的列表状态；页面稳定后恢复原任务一次，若再冲突请保留检查点排查。"),
    "requested_response_missing": ("浏览器显示末页，但没有观察到检查点所需游标的列表响应；先前进度已保留，未判定末页。", "核对该作者页面和登录状态；确认可访问后恢复原任务一次，若仍缺响应请保留检查点排查。"),
    "content_partial": ("部分作品正文或媒体尚未保存，成功资源与指标已保留。", "查看任务未完成清单中的具体原因，处理后恢复原任务；成功媒体不会重复下载。"),
    "metrics_partial": ("部分作品指标未能更新；已有有效数值与原采集时间保留。", "查看独立浏览器提示后恢复；未知字段可能未由平台提供。"),
    "content_complete": ("本批作品正文和当前可获取媒体已保存。", "可查看离线附件或按作者导出；不代表所有历史作品均已获取。"),
    "metrics_complete": ("本批指标观察已保存，缺失字段仍标未知；历史快照保留。", "可按全库指标排序筛选，或查看作品的观察历史。"),
    "no_local_items": ("当前作者尚无本地作品，本批内容或指标范围为空。", "先获取作者历史列表，再新建内容或指标任务；当前任务不会把空范围当作完成。"),
    "reference_missing": ("本轮未取得部分历史作品的有效访问引用；旧资料和成功项保留。", "在任务的补充链接入口粘贴同篇完整官方链接后继续；不会重扫39/6页长链。"),
    "item_unavailable": ("该作品当前不可访问，可能是链接失效、删除或权限变化；旧资料保留。", "在官方页面确认可访问后，补充完整链接重试。其他作品可继续处理。"),
    "media_failed": ("部分媒体下载或校验失败，成功媒体和正文保留。", "恢复任务会刷新详情并只下载缺失资源。"),
    "media_write_failed": ("作品媒体未能写入本地目录；已取得的正文、指标及成功媒体保留。", "检查工作目录中的 downloads 和 archive 路径是否被同名文件占用、是否有写入权限及足够可用空间；处理后恢复原任务，成功项不会重做。"),
}

LOCAL_MEDIA_ERRNOS = {errno.EACCES, errno.EPERM, errno.ENOSPC, errno.EDQUOT,
                      errno.EROFS, errno.EEXIST, errno.ENOTDIR, errno.EISDIR, errno.ENOENT}


class PlatformCooldown(ValueError):
    def __init__(self, retry_at):
        self.retry_at = retry_at
        super().__init__("平台冷却尚未结束，已保存全部进度。请等待冷却结束后重试；已有资料仍可离线归档。")


class PlatformAccessPaused(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__("小红书真实联网已因账号安全暂停。旧资料和检查点保留；本轮仅可离线阅读与导出，恢复须另有方案和用户明确批准。")


class WorkspaceService:
    def __init__(self, root: Path, *, validation_root: Path | None = None, adapter_factory=None):
        self.root = Path(root).resolve()
        self.workflow = ArchiveWorkflow(self.root)
        self.validation_root = validation_root or default_workspace().parent / "private-validation/G1-2026-09-26"
        self.adapter_factory = adapter_factory
        self._lock = Lock()
        self._transport_lock = Lock()
        self._network_lock = RLock()
        self._transport = None
        self._source_transports = {}
        self._threads: set[Thread] = set()
        self._stopping = Event()
        self._schedule_lock = Lock()
        self._schedule_wakeup = Event()
        self._scheduler = None
        self._pause_lock = Lock()
        self._pause_requests: set[int] = set()
        self._job_sources = {} # Temporary navigation parameters never enter SQLite or exports.
        from .manual_validation import ManualValidation
        self.manual_validation = ManualValidation(self)
        self.manual_validation.interrupt_pending()
        with self.workflow.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS subscription_intents (
                    platform TEXT NOT NULL, author_id TEXT NOT NULL, display_name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
                    PRIMARY KEY(platform,author_id));
                CREATE TABLE IF NOT EXISTS subscription_confirmations (
                    platform TEXT NOT NULL, author_id TEXT NOT NULL, confirmed_at REAL NOT NULL,
                    PRIMARY KEY(platform,author_id));
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY, platform TEXT NOT NULL, author_id TEXT NOT NULL,
                    mode TEXT NOT NULL, state TEXT NOT NULL, run_id INTEGER,
                    reason TEXT, export_json TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS validation_imports (
                    source TEXT PRIMARY KEY, imported_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS platform_cooldowns (
                    platform TEXT PRIMARY KEY, retry_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS request_failures (
                    id INTEGER PRIMARY KEY, job_id INTEGER, run_id INTEGER, platform TEXT NOT NULL,
                    author_id TEXT NOT NULL, stage TEXT NOT NULL, checkpoint_pages INTEGER,
                    checkpoint_cursor_sha256 TEXT, diagnostic_json TEXT NOT NULL, observed_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS platform_access_pauses (
                    platform TEXT PRIMARY KEY, reason TEXT NOT NULL, paused_at REAL NOT NULL);
            """)
        self.pipeline_migration_backup = page_pipeline.initialize(self.workflow)
        self.batch_migration_backup = self._initialize_archive_batches()
        self.recent_window_migration_backup = self._initialize_recent_window_observations()
        with self.workflow.connect() as db:
            db.execute("UPDATE jobs SET state='interrupted',reason='process_interrupted',updated_at=? WHERE state IN ('running','queued')", (time.time(),))
            db.execute("UPDATE runs SET state='interrupted',reason='process_interrupted',updated_at=? WHERE state IN ('running','queued')", (time.time(),))
        if network_safety.xhs_network_paused():
            marker = self.root / "xhs-network-paused.json"
            if not marker.exists():
                marker.write_text(json.dumps({"paused": True, "reason": network_safety.SAFETY_REASON,
                                              "automatic_release": False}), encoding="utf-8")
            with self.workflow.connect() as db:
                db.execute("INSERT OR IGNORE INTO platform_access_pauses VALUES(?,?,?)",
                           ("xiaohongshu", network_safety.SAFETY_REASON, time.time()))
        if self.refresh_schedule()["enabled"] and not network_safety.xhs_network_paused():
            self._start_scheduler()

    def refresh_schedule(self):
        """Read a private local schedule; absence means no background requests."""
        path = self.root / "refresh-schedule.json"
        if not path.is_file():
            return {"enabled": False, "interval_minutes": 1440, "next_at": None,
                    "last_triggered_at": None, "last_started_jobs": 0}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or type(data.get("enabled")) is not bool
                    or type(data.get("interval_minutes")) is not int
                    or not 15 <= data["interval_minutes"] <= 10080
                    or data.get("next_at") is not None and type(data["next_at"]) not in {int, float}
                    or data["enabled"] and data.get("next_at") is None):
                raise ValueError()
            return data
        except (OSError, ValueError) as error:
            raise ValueError("本机定时刷新配置无法读取；未启动后台刷新，请检查 refresh-schedule.json") from error

    def _save_refresh_schedule(self, data):
        path = self.root / "refresh-schedule.json"
        temporary = path.with_name(f".refresh-schedule-{time.time_ns()}.tmp")
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def set_refresh_schedule(self, enabled: bool, interval_minutes: int = 1440):
        if enabled:
            self._check_platform_access("xiaohongshu")
        if type(enabled) is not bool or type(interval_minutes) is not int or not 15 <= interval_minutes <= 10080:
            raise ValueError("定时刷新间隔须为15至10080分钟；原设置未改动")
        with self._schedule_lock:
            old = self.refresh_schedule()
            if (old["enabled"] == enabled and old["interval_minutes"] == interval_minutes):
                return old
            data = {"enabled": enabled, "interval_minutes": interval_minutes,
                    "next_at": time.time() + interval_minutes * 60 if enabled else None,
                    "last_triggered_at": old.get("last_triggered_at"),
                    "last_started_jobs": old.get("last_started_jobs", 0)}
            self._save_refresh_schedule(data)
        self._schedule_wakeup.set()
        if enabled:
            self._start_scheduler()
        return data

    def _start_scheduler(self):
        with self._schedule_lock:
            if self._scheduler is None or not self._scheduler.is_alive():
                self._scheduler = Thread(target=self._scheduler_loop, daemon=True, name="xhs-refresh-schedule")
                self._scheduler.start()

    def _scheduler_loop(self):
        while not self._stopping.is_set():
            try:
                schedule = self.refresh_schedule()
            except ValueError:
                # A malformed private setting never triggers network traffic.
                return
            if not schedule["enabled"]:
                return
            now = time.time()
            if schedule["next_at"] is not None and schedule["next_at"] <= now:
                self._run_due_refresh(now)
                continue
            wait = max(1, min(60, (schedule["next_at"] or now + 60) - now))
            self._schedule_wakeup.wait(wait)
            self._schedule_wakeup.clear()

    def _run_due_refresh(self, now=None):
        if network_safety.xhs_network_paused():
            return 0
        now = time.time() if now is None else now
        with self._schedule_lock:
            schedule = self.refresh_schedule()
            if not schedule["enabled"] or schedule["next_at"] is None or schedule["next_at"] > now:
                return 0
            schedule["next_at"] = now + schedule["interval_minutes"] * 60
            schedule["last_triggered_at"] = now
            schedule["last_started_jobs"] = 0
            self._save_refresh_schedule(schedule)
        with self.workflow.connect() as db:
            authors = [tuple(row) for row in db.execute("""SELECT s.platform,s.author_id FROM subscriptions s
                WHERE s.platform='xiaohongshu' AND s.enabled=1 AND NOT EXISTS
                (SELECT 1 FROM subscription_cancellations c WHERE c.platform=s.platform AND c.author_id=s.author_id)
                ORDER BY s.author_id""")]
        started = 0
        for platform, author_id in authors:
            if self._stopping.is_set() or not self.refresh_schedule()["enabled"]:
                break
            if self._source_kind(platform, author_id) != "xhs_http":
                continue
            try:
                self.start("source_refresh", platform, author_id)
                started += 1
            except (ValueError, PlatformCooldown):
                # Existing jobs and platform cooldowns retain their progress.
                continue
        with self._schedule_lock:
            schedule = self.refresh_schedule()
            if schedule.get("last_triggered_at") == now:
                schedule["last_started_jobs"] = started
                self._save_refresh_schedule(schedule)
        return started

    def _initialize_archive_batches(self):
        """Add a fixed all-author membership without changing legacy jobs or runs."""
        with closing(sqlite3.connect(self.workflow.db_path)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='archive_batch_members'").fetchone():
                return None
            backup = None
            if db.execute("SELECT 1 FROM jobs UNION ALL SELECT 1 FROM runs UNION ALL SELECT 1 FROM items UNION ALL SELECT 1 FROM subscriptions LIMIT 1").fetchone():
                directory = self.workflow.db_path.parent / "backups"
                directory.mkdir(exist_ok=True)
                backup = directory / f"archive-before-all-batch-{time.time_ns()}.sqlite3"
                with closing(sqlite3.connect(backup)) as target:
                    db.backup(target)
                    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("数据库备份校验失败，未执行全订阅批次升级")
            db.execute("""CREATE TABLE archive_batch_members (
                batch_id INTEGER NOT NULL REFERENCES batches(id),
                platform TEXT NOT NULL, author_id TEXT NOT NULL,
                job_id INTEGER UNIQUE REFERENCES jobs(id), reason TEXT,
                PRIMARY KEY(batch_id,platform,author_id))""")
            db.commit()
            return str(backup) if backup else None

    def _initialize_recent_window_observations(self):
        """Add window observations after verifying a backup of any populated DB."""
        with closing(sqlite3.connect(self.workflow.db_path)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='recent_window_observations'").fetchone():
                return None
            backup = None
            if db.execute("SELECT 1 FROM jobs UNION ALL SELECT 1 FROM runs UNION ALL SELECT 1 FROM items UNION ALL SELECT 1 FROM subscriptions LIMIT 1").fetchone():
                directory = self.workflow.db_path.parent / "backups"
                directory.mkdir(exist_ok=True)
                backup = directory / f"archive-before-recent-window-{time.time_ns()}.sqlite3"
                with closing(sqlite3.connect(backup)) as target:
                    db.backup(target)
                    if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise ValueError("数据库备份校验失败，未执行最近窗口观察表升级")
            db.execute("""CREATE TABLE recent_window_observations (
                job_id INTEGER NOT NULL, item_id TEXT NOT NULL, disposition TEXT NOT NULL,
                difference_json TEXT, PRIMARY KEY(job_id,item_id))""")
            db.commit()
            return str(backup) if backup else None

    def _cooldown_until(self, platform, db):
        return db.execute("SELECT coalesce(max(retry_at),0) FROM (SELECT retry_at FROM platform_cooldowns WHERE platform=? UNION ALL SELECT retry_at FROM runs WHERE platform=?)", (platform, platform)).fetchone()[0]

    def _check_cooldown(self, platform, db=None):
        if db is None:
            with self.workflow.connect() as connection:
                return self._check_cooldown(platform, connection)
        retry_at = self._cooldown_until(platform, db)
        if retry_at > time.time():
            raise PlatformCooldown(retry_at)

    def _record_cooldown(self, platform, *, retry_after=0, retry_at=None):
        until = retry_at if retry_at is not None else time.time() + max(retry_after, 5)
        with self.workflow.connect() as db:
            db.execute("INSERT INTO platform_cooldowns VALUES(?,?) ON CONFLICT(platform) DO UPDATE SET retry_at=max(platform_cooldowns.retry_at,excluded.retry_at)", (platform, until))

    def _platform_access_reason(self, platform, db=None):
        if platform != "xiaohongshu":
            return None
        if db is None:
            with self.workflow.connect() as connection:
                return self._platform_access_reason(platform, connection)
        row = db.execute("SELECT reason FROM platform_access_pauses WHERE platform=?", (platform,)).fetchone()
        return row[0] if row else None

    def _check_platform_access(self, platform, db=None):
        if platform == "xiaohongshu" and network_safety.xhs_network_paused():
            raise PlatformAccessPaused(network_safety.SAFETY_REASON)
        reason = self._platform_access_reason(platform, db)
        if reason and not (reason == network_safety.SAFETY_REASON and network_safety.manual_scope() is not None):
            raise PlatformAccessPaused(reason)

    def _record_platform_access_pause(self, platform, author_id, reason):
        if (platform != "xiaohongshu" or self._source_kind(platform, author_id) != "xhs_http"
                or reason not in {"needs_login", "verification_required", "rate_limited", "unknown_business_error", "unavailable",
                                  "identity_mismatch", "invalid_response", "timeout", "invalid_stable_id"}):
            return
        with self.workflow.connect() as db:
            db.execute("INSERT INTO platform_access_pauses VALUES(?,?,?) ON CONFLICT(platform) DO UPDATE SET reason=excluded.reason,paused_at=excluded.paused_at",
                       (platform, reason, time.time()))

    def _source_call(self, job, operation, *args, **kwargs):
        """Latch an unknown source failure before another author takes the lock."""
        with self._network_lock:
            network_safety.require_manual_author(job["author_id"])
            self._check_platform_access(job["platform"])
            try:
                return operation(*args, **kwargs)
            except AdapterFailure as error:
                self._record_platform_access_pause(job["platform"], job["author_id"], error.category)
                raise

    def _record_request_failure(self, job, error, stage):
        if error.category == "network_paused" or (not error.diagnostics and self._source_kind(job["platform"], job["author_id"]) != "xhs_http"):
            return
        with self.workflow.connect() as db:
            run = db.execute("SELECT pages,cursor FROM runs WHERE id=?", (job.get("run_id"),)).fetchone()
            allowed = {key: error.diagnostics.get(key) for key in
                       ("http_status", "success", "business_code", "message", "message_sha256", "stage", "request_cursor_sha256")}
            allowed["category"] = error.category
            if run and allowed.get("request_cursor_sha256"):
                for saved in db.execute("SELECT page_number,request_cursor FROM pages WHERE run_id=?", (job.get("run_id"),)):
                    if sha256((saved[1] or "").encode()).hexdigest() == allowed["request_cursor_sha256"]:
                        allowed["saved_page_number"] = saved[0]
                        break
            db.execute("INSERT INTO request_failures(job_id,run_id,platform,author_id,stage,checkpoint_pages,checkpoint_cursor_sha256,diagnostic_json,observed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (job["id"], job.get("run_id"), job["platform"], job["author_id"], stage,
                        run[0] if run else None, sha256((run[1] or "").encode()).hexdigest() if run else None,
                        json.dumps(allowed, ensure_ascii=False), time.time()))

    def _clear_platform_access_pause(self, platform):
        if platform == "xiaohongshu" and network_safety.manual_scope() is not None:
            return  # A manual action never removes the global safety pause.
        if platform == "xiaohongshu" and network_safety.xhs_network_paused():
            raise PlatformAccessPaused(network_safety.SAFETY_REASON)
        with self.workflow.connect() as db:
            db.execute("DELETE FROM platform_access_pauses WHERE platform=?", (platform,))

    def transport(self):
        self._check_platform_access("xiaohongshu")
        with self._transport_lock:
            if self._transport is None:
                if self.adapter_factory:
                    self._transport = self.adapter_factory(self.root)
                else:
                    raise AdapterFailure("unavailable")
            return self._transport

    def source_config(self, platform, author_id):
        path = self.root / "sources.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError("私有来源配置无法读取；原文件未改动，请检查 sources.json") from error
        if not isinstance(data, dict):
            raise ValueError("私有来源配置格式错误；原文件未改动")
        config = data.get(f"{platform}/{author_id}")
        if config is None and platform == "xiaohongshu":
            config = data.get("xiaohongshu/*")
        if config is not None and not isinstance(config, dict):
            raise ValueError("私有来源配置格式错误；原文件未改动")
        return config

    def _source_kind(self, platform, author_id):
        config = self.source_config(platform, author_id)
        if config is None:
            return None
        kind = config.get("kind", "feed_http")
        if kind not in {"feed_http", "xhs_http"} or kind == "xhs_http" and platform != "xiaohongshu":
            raise ValueError("私有来源类型无效；请检查 sources.json")
        return kind

    def set_source(self, platform, author_id, url):
        self._check_platform_access(platform)
        from .adapters.feed_http import validate_source_url
        if platform not in {"xiaohongshu", "wechat"}:
            raise ValueError("仅支持公众号和小红书来源")
        _id(author_id)
        url = validate_source_url(url)
        with self.workflow.connect() as db:
            if not db.execute("""SELECT 1 FROM subscriptions WHERE platform=? AND author_id=?
                UNION ALL SELECT 1 FROM subscription_intents WHERE platform=? AND author_id=? LIMIT 1""",
                (platform, author_id, platform, author_id)).fetchone():
                raise KeyError("subscription_not_found")
        path = self.root / "sources.json"
        with self._lock:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("私有来源配置格式错误；未覆盖原文件")
            else:
                data = {}
            data[f"{platform}/{author_id}"] = {"url": url, "format": "auto"}
            temporary = path.with_name(f".sources-{time.time_ns()}.tmp")
            try:
                temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                if path.exists():
                    backup = self.root / "backups" / f"sources-before-change-{time.time_ns()}.json"
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, backup)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            with self._transport_lock:
                self._source_transports.pop((platform, author_id), None)
        return {"platform": platform, "author_id": author_id, "source_configured": True,
                "message": "后台来源已保存于本机私有配置；尚未请求上游或证明作者身份与历史范围。"}

    def transport_for(self, platform, author_id):
        network_safety.require_manual_author(author_id)
        self._check_platform_access(platform)
        config = self.source_config(platform, author_id)
        if self.adapter_factory and config is None and platform == "xiaohongshu":
            return self.transport()
        if config is None:
            raise AdapterFailure("unavailable")
        key = (platform, author_id)
        with self._transport_lock:
            if key not in self._source_transports:
                kind = self._source_kind(platform, author_id)
                if kind == "xhs_http":
                    from .adapters.xhs_http import XhsHttpTransport
                    self._source_transports[key] = XhsHttpTransport(config, platform, author_id)
                else:
                    from .adapters.feed_http import FeedHttpTransport
                    self._source_transports[key] = FeedHttpTransport(config, platform, author_id)
            return self._source_transports[key]

    def open_login(self):
        self._check_platform_access("xiaohongshu")
        if not self.adapter_factory:
            raise ValueError("正式采集已停用旧浏览器传输。请为作者配置本人授权的后台来源；必要登录请在来源自身入口完成。")
        with self._network_lock:
            self._check_cooldown("xiaohongshu")
            with self.workflow.connect() as db:
                if db.execute("SELECT 1 FROM jobs WHERE platform='xiaohongshu' AND mode!='archive' AND state IN ('running','queued') LIMIT 1").fetchone():
                    raise ValueError("当前小红书采集任务仍在运行；请等待任务暂停，或先用Stop停止再Start启动，然后打开登录窗口。已保存进度会保留。")
            self._job_sources.clear()
            try:
                return self.transport().open_login()
            except AdapterFailure as error:
                if error.category == "rate_limited":
                    self._record_cooldown("xiaohongshu", retry_after=error.retry_after)
                    raise
                raise ValueError(getattr(error, "reason", "采集浏览器未就绪，请检查启动自检并重试")) from None

    def verify(self, platform, author_id):
        network_safety.require_manual_author(author_id)
        self._check_platform_access(platform)
        if platform not in {"xiaohongshu", "wechat"}:
            raise ValueError("不支持的平台")
        with self.workflow.connect() as db:
            intent = db.execute("SELECT * FROM subscription_intents WHERE platform=? AND author_id=?", (platform, author_id)).fetchone()
            existing = db.execute("SELECT * FROM subscriptions WHERE platform=? AND author_id=?", (platform, author_id)).fetchone()
        if not intent and not existing:
            raise KeyError("subscription_not_found")
        with self._network_lock:
            self._check_cooldown(platform)
            try:
                transport = self.transport_for(platform, author_id)
                if network_safety.manual_scope() is not None:
                    page = network_safety.bound_manual_page(author_id, transport.recent_page(author_id))
                    observed = {"author_id": author_id, "display_name": author_id}
                else:
                    observed = transport.verify_author(author_id)
            except AdapterFailure as error:
                self._record_request_failure({"id": None, "run_id": None, "platform": platform,
                                              "author_id": author_id}, error, "verify_author")
                if error.category == "rate_limited":
                    self._record_cooldown(platform, retry_after=error.retry_after)
                self._record_platform_access_pause(platform, author_id, error.category)
                raise
        if observed.get("author_id") != author_id:
            raise ValueError("平台返回的作者身份与候选不符；原订阅保留")
        saved_name = (existing or intent)["display_name"]
        observed_name = observed.get("display_name")
        name = observed_name if saved_name.startswith("待核验作者 · ") and observed_name and observed_name != author_id else saved_name
        evidence = ("observed_xhs_http_author" if self._source_kind(platform, author_id) == "xhs_http"
                    else "observed_browser_author_listing" if self.adapter_factory else "observed_http_author_feed")
        self.workflow.subscribe(platform, author_id, name, verified=True, evidence=evidence)
        if self._source_kind(platform, author_id) == "xhs_http":
            self._clear_platform_access_pause(platform)
        if intent:
            self.workflow.set_enabled(platform, author_id, bool(intent["enabled"]))
        return {"platform": platform, "author_id": author_id, "identity_verified": True,
                "message": "作者身份已通过后台来源核验，可同步最近内容。" + XHS_RECENT_WINDOW_WARNING}

    def close(self):
        self._stopping.set()
        self._schedule_wakeup.set()
        if self._transport is not None and hasattr(self._transport, "close"):
            self._transport.close()
        for source in self._source_transports.values():
            if hasattr(source, "close"):
                source.close()

    def _pause_requested(self, job_id):
        with self._pause_lock:
            return job_id in self._pause_requests

    def pause(self, job_id):
        with self.workflow.connect() as db:
            row = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError("job_not_found")
            if row["state"] not in {"queued", "running"}:
                raise ValueError("任务不在运行或排队状态，现有进度未改动")
        with self._pause_lock:
            self._pause_requests.add(job_id)
        return {"job_id": job_id, "state": "pausing",
                "message": "已请求在当前作品或页面边界暂停；已保存进度与文件保持。"}

    def subscribe(self, text: str, display_name: str | None = None) -> dict:
        result = classify(text)
        if result["platform"] == "xiaohongshu" and result["kind"] == "short_link":
            url = re.findall(r"https?://[^\s<>\"'，。；）]+", text)[0]
            result = classify(expand_share_link(url))
        author_id = result["candidate_author_id"]
        if not author_id:
            raise ValueError("当前无法从这个链接核验作者。请提供小红书作者主页或带 __biz 的公众号链接；小红书作品及短链请使用“按分享链接保存一篇”入口。")
        _id(author_id)
        platform = result["platform"]
        name = (display_name or f"待核验作者 · {author_id[-6:]}").strip()
        if not name or len(name) > 160:
            raise ValueError("作者名称须为1至160个字符")
        with self.workflow.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            was_cancelled = self._is_cancelled(db, platform, author_id)
            existing = db.execute("SELECT * FROM subscriptions WHERE platform=? AND author_id=?", (platform, author_id)).fetchone()
            if existing:
                if display_name:
                    db.execute("UPDATE subscriptions SET display_name=? WHERE platform=? AND author_id=?", (name, platform, author_id))
                if was_cancelled and not self._confirmation_required(existing, db):
                    db.execute("UPDATE subscriptions SET enabled=1 WHERE platform=? AND author_id=?", (platform, author_id))
            else:
                db.execute("INSERT INTO subscription_intents VALUES(?,?,?,1,?) ON CONFLICT(platform,author_id) DO UPDATE SET display_name=excluded.display_name", (platform, author_id, name, time.time()))
                if was_cancelled:
                    db.execute("UPDATE subscription_intents SET enabled=1 WHERE platform=? AND author_id=?", (platform, author_id))
            db.execute("DELETE FROM subscription_cancellations WHERE platform=? AND author_id=?", (platform, author_id))
        verification_issue = None
        if platform == "xiaohongshu" and self._source_kind(platform, author_id) == "xhs_http":
            verification_issue = self._platform_access_reason(platform)
            if not verification_issue and network_safety.manual_scope() is None:
                try:
                    self.verify(platform, author_id)
                except (AdapterFailure, PlatformCooldown) as error:
                    verification_issue = error.category if isinstance(error, AdapterFailure) else "rate_limited"
        result = next(s for s in self.workspace()["subscriptions"] if s["platform"] == platform and s["author_id"] == author_id)
        if verification_issue:
            result["verification_issue"] = verification_issue
            result["message"] = MESSAGES.get(verification_issue, MESSAGES["unavailable"])[0]
        elif result["identity_verified"] and platform == "xiaohongshu":
            result["message"] = "已通过本机后台来源核验并订阅该小红书作者；尚未开始历史同步。"
        elif not result["identity_verified"] and platform == "xiaohongshu" and not result["source_connected"]:
            result["message"] = "已保留候选订阅；请先运行本机 authorize-xhs.cmd 完成本人授权，再核验作者。"
        return result

    def resolve_item(self, text: str) -> dict:
        """Experimental exact-note registration; no history/content job implied."""
        classified = classify(text)
        if classified["platform"] != "xiaohongshu" or classified["kind"] not in {"item", "short_link"}:
            raise ValueError("此验证入口仅接受小红书作品链接或官方分享短链")
        url = re.findall(r"https?://[^\s<>\"'，。；）]+", text)[0]
        with self._network_lock:
            self._check_cooldown("xiaohongshu")
            try:
                if classified["kind"] == "short_link":
                    url = expand_share_link(url)
                canonical = _canonical_source_url("xiaohongshu", url)
                item_id = urlsplit(canonical).path.rstrip("/").split("/")[-1]
                observed = self.transport().resolve_item(url)
            except AdapterFailure as error:
                if error.category == "rate_limited":
                    self._record_cooldown("xiaohongshu", retry_after=error.retry_after)
                raise
        author = observed.get("author_id")
        if observed.get("item_id") != item_id or not isinstance(author,str) or not re.fullmatch(r"[0-9a-f]{24}",author):
            raise ValueError("目标作品或作者身份不匹配；原资料未改动")
        with self.workflow.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT author_id FROM items WHERE platform='xiaohongshu' AND item_id=?",(item_id,)).fetchone()
            if old and old[0] != author:
                raise ValueError("作品与已有作者归属冲突；原资料未改动")
            # Keep existing names, pause state and content. A new author starts
            # paused; one observed note proves no historical coverage.
            db.execute("INSERT OR IGNORE INTO subscriptions(platform,author_id,display_name,identity_evidence,enabled,created_at) VALUES('xiaohongshu',?,?,?,0,?)",
                       (author, f"作品核验作者 · {author[-6:]}", "observed_browser_exact_note", time.time()))
            db.execute("INSERT OR IGNORE INTO items(platform,author_id,item_id,published_at,source_url,title,content_type) VALUES('xiaohongshu',?,?,?,?,?,?)",
                       (author,item_id,observed.get("published_at") or "",canonical,observed.get("title") or "",observed.get("content_type") or "unknown"))
            subscription = db.execute("SELECT * FROM subscriptions WHERE platform='xiaohongshu' AND author_id=?", (author,)).fetchone()
            confirmation_required = self._confirmation_required(subscription, db)
        return {"platform":"xiaohongshu","author_id":author,"item_id":item_id,"identity_verified":True,
                "author_display_name":subscription["display_name"],"subscription_confirmation_required":confirmation_required,
                "subscription_enabled":bool(subscription["enabled"]),
                "resolved_from_short_link":classified["kind"] == "short_link",
                "message":"已从目标作品核验归属并收录该作品；新作者待确认订阅，历史与内容完整性尚未验证。"}

    @staticmethod
    def _confirmation_required(subscription, db) -> bool:
        if subscription["identity_evidence"] != "observed_browser_exact_note" or subscription["enabled"]:
            return False
        return db.execute("SELECT 1 FROM subscription_confirmations WHERE platform=? AND author_id=?",
                          (subscription["platform"], subscription["author_id"])).fetchone() is None

    def confirm_subscription(self, platform: str, author_id: str) -> dict:
        if platform != "xiaohongshu":
            raise ValueError("此入口仅确认已核验作品的小红书作者订阅")
        _id(author_id)
        with self.workflow.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            subscription = db.execute("SELECT * FROM subscriptions WHERE platform=? AND author_id=?", (platform, author_id)).fetchone()
            if self._is_cancelled(db, platform, author_id):
                raise ValueError("该作者已取消订阅；请先在作者卡片重新订阅，旧作品和任务已保留")
            if not subscription or subscription["identity_evidence"] != "observed_browser_exact_note":
                raise ValueError("没有待确认的作品作者；请先在作品入口核验目标作品与作者")
            if not db.execute("SELECT 1 FROM items WHERE platform=? AND author_id=? LIMIT 1", (platform, author_id)).fetchone():
                raise ValueError("未找到该作者的已核验作品，原订阅状态未改动")
            if self._confirmation_required(subscription, db):
                db.execute("INSERT INTO subscription_confirmations VALUES(?,?,?)", (platform, author_id, time.time()))
                db.execute("UPDATE subscriptions SET enabled=1 WHERE platform=? AND author_id=?", (platform, author_id))
                enabled = True
            else:
                enabled = bool(subscription["enabled"])
        return {"platform":platform,"author_id":author_id,"subscription_confirmed":True,"enabled":enabled,
                "message":"已确认订阅该作者；原作品和任务保持不变。历史尚未自动扫描，可在作者操作中单独启动。"}

    def toggle(self, platform: str, author_id: str, enabled: bool) -> dict:
        with self.workflow.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._is_cancelled(db, platform, author_id):
                raise ValueError("该作者已取消订阅；请先重新订阅，旧归档和检查点已保留")
            subscription = db.execute("SELECT * FROM subscriptions WHERE platform=? AND author_id=?", (platform, author_id)).fetchone()
            if subscription and self._confirmation_required(subscription, db) and enabled:
                raise ValueError("这位作品作者尚未确认订阅；请在作者卡片点击“确认订阅”，旧资料已保留")
            if subscription and subscription["identity_evidence"] == "observed_browser_exact_note" and subscription["enabled"]:
                # Legacy v0.4.7 users may already have enabled this author.
                db.execute("INSERT OR IGNORE INTO subscription_confirmations VALUES(?,?,?)", (platform, author_id, time.time()))
            count = 0
            for table in ("subscriptions", "subscription_intents"):
                count += db.execute(f"UPDATE {table} SET enabled=? WHERE platform=? AND author_id=?", (int(enabled), platform, author_id)).rowcount
            if not count:
                raise KeyError("subscription_not_found")
        return {"platform": platform, "author_id": author_id, "enabled": enabled}

    @staticmethod
    def _is_cancelled(db, platform, author_id):
        return db.execute("SELECT 1 FROM subscription_cancellations WHERE platform=? AND author_id=?",
                          (platform, author_id)).fetchone() is not None

    def cancel_subscription(self, platform: str, author_id: str) -> dict:
        _id(author_id)
        with self.workflow.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            found = db.execute("""SELECT 1 FROM subscriptions WHERE platform=? AND author_id=?
                UNION ALL SELECT 1 FROM subscription_intents WHERE platform=? AND author_id=? LIMIT 1""",
                (platform, author_id, platform, author_id)).fetchone()
            if not found:
                raise KeyError("subscription_not_found")
            db.execute("INSERT OR IGNORE INTO subscription_cancellations VALUES(?,?,?)",
                       (platform, author_id, time.time()))
        return {"platform": platform, "author_id": author_id, "subscribed": False,
                "message": "已取消订阅；作者归档、手工资料、旧任务和检查点保留。已有批次仍按原固定范围显示和恢复。"}

    def resubscribe(self, platform: str, author_id: str) -> dict:
        _id(author_id)
        with self.workflow.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            subscription = db.execute("SELECT * FROM subscriptions WHERE platform=? AND author_id=?",
                                      (platform, author_id)).fetchone()
            intent = db.execute("SELECT 1 FROM subscription_intents WHERE platform=? AND author_id=?",
                                (platform, author_id)).fetchone()
            if not subscription and not intent:
                raise KeyError("subscription_not_found")
            if not self._is_cancelled(db, platform, author_id):
                raise ValueError("该作者当前未取消订阅；如需暂停或启用，请使用作者卡片的订阅开关")
            db.execute("DELETE FROM subscription_cancellations WHERE platform=? AND author_id=?", (platform, author_id))
            pending = bool(subscription and self._confirmation_required(subscription, db))
            if not pending:
                for table in ("subscriptions", "subscription_intents"):
                    db.execute(f"UPDATE {table} SET enabled=1 WHERE platform=? AND author_id=?",
                               (platform, author_id))
        return {"platform": platform, "author_id": author_id, "subscribed": True,
                "enabled": False if pending else True,
                "message": "已恢复订阅意图；请先确认作品作者订阅。旧资料和任务保留。" if pending else
                           "已重新订阅；旧归档和任务检查点保留，本次未自动扫描历史。"}

    def set_subscription_tags(self, platform: str, author_id: str, tags: list[str]) -> dict:
        _id(author_id)
        if not isinstance(tags, list) or len(tags) > 10:
            raise ValueError("每位作者最多设置10个标签；原标签已保留")
        cleaned = []
        seen = set()
        for value in tags:
            if not isinstance(value, str):
                raise ValueError("标签须为文字；原标签已保留")
            tag = value.strip()
            if not tag or len(tag) > 32 or any(ord(char) < 32 or char in ",，" for char in tag):
                raise ValueError("每个标签须为1至32个字符，不能包含逗号或换行；原标签已保留")
            if tag.casefold() in seen:
                continue
            seen.add(tag.casefold())
            cleaned.append(tag)
        with self.workflow.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            found = db.execute("""SELECT 1 FROM subscriptions WHERE platform=? AND author_id=?
                UNION ALL SELECT 1 FROM subscription_intents WHERE platform=? AND author_id=? LIMIT 1""",
                (platform, author_id, platform, author_id)).fetchone()
            if not found:
                raise KeyError("subscription_not_found")
            db.execute("DELETE FROM subscription_tags WHERE platform=? AND author_id=?", (platform, author_id))
            db.executemany("INSERT INTO subscription_tags VALUES(?,?,?,?)",
                           [(platform, author_id, tag, position) for position, tag in enumerate(cleaned)])
        return {"platform": platform, "author_id": author_id, "tags": cleaned,
                "message": "作者标签已保存；归档、手工资料及任务进度保留。"}

    def _public_job(self, row: dict, db) -> dict:
        run = db.execute("SELECT * FROM runs WHERE id=?", (row["run_id"],)).fetchone() if row["run_id"] else None
        count = db.execute("SELECT count(*) FROM items WHERE platform=? AND author_id=?", (row["platform"], row["author_id"])).fetchone()[0]
        run_ids = set()
        if run:
            for page in db.execute("SELECT item_ids FROM pages WHERE run_id=?", (row["run_id"],)):
                run_ids.update(json.loads(page[0]))
        exported = json.loads(row["export_json"]) if row["export_json"] else None
        item_count = (sum(author["items"] for author in exported["authors"]) if row["mode"] == "archive" and exported else len(run_ids))
        result = {k: row[k] for k in ("id", "platform", "author_id", "mode", "state", "reason", "run_id", "created_at", "updated_at")}
        group = db.execute("SELECT batch_id FROM archive_batch_members WHERE job_id=?", (row["id"],)).fetchone()
        if group:
            result["archive_batch_id"] = group[0]
        cooldown = self._cooldown_until(row["platform"], db) if row["mode"] != "archive" else 0
        result.update(pages=run["pages"] if run else 0, item_count=item_count, library_item_count=count,
                      coverage=run["coverage"] if run else "unknown", retry_at=cooldown,
                      can_resume=row["state"] not in {"succeeded", "running", "queued"} and cooldown <= time.time()
                      and (row["mode"] == "archive" or not self._platform_access_reason(row["platform"], db)))
        # A current source selector does not rewrite the meaning of an older
        # list-only run. Use the adapter version captured by this run instead.
        native_full = (row["mode"] == "full" and
                       (bool(run and run["adapter_version"] == "xhs-author-http-v1") or
                        bool(db.execute("SELECT 1 FROM job_items WHERE job_id=? LIMIT 1", (row["id"],)).fetchone())))
        if row["mode"] in {"content", "metrics", "source_refresh", "recent_window"} or native_full:
            progress = db.execute("SELECT count(*),coalesce(sum(state='succeeded'),0),coalesce(sum(state='partial'),0) FROM job_items WHERE job_id=?", (row["id"],)).fetchone()
            result.update(target_count=progress[0],item_count=progress[1],failed_count=progress[2],pending_count=progress[0]-progress[1]-progress[2],
                          coverage="not_applicable" if row["mode"] != "full" else result["coverage"])
            if row["mode"] in {"full", "recent_window"}:
                result.update(listed_count=len(run_ids), list_finished=bool(run and run["terminal_evidence"]),
                              scan_url=f"/api/jobs/{row['id']}/scan")
            observed = db.execute("""SELECT coalesce(sum(i.detail_state='complete'),0),
                coalesce(sum(i.media_state='complete_for_observed_detail'),0),
                coalesce(sum(i.media_state='partial'),0),
                coalesce(sum(i.media_state='unknown'),0)
                FROM job_items j JOIN items i USING(platform,item_id) WHERE j.job_id=?""", (row["id"],)).fetchone()
            result.update(body_saved_count=observed[0],media_observed_complete_count=observed[1],
                          media_partial_item_count=observed[2],media_unknown_item_count=observed[3])
            result["registered_media"] = {kind: number for kind, number in db.execute("""
                SELECT a.kind,count(*) FROM assets a JOIN job_items j
                ON a.platform=j.platform AND a.item_id=j.item_id
                WHERE j.job_id=? GROUP BY a.kind""", (row["id"],))}
            result["failed_items"] = [dict(r) for r in db.execute("SELECT item_id,reason FROM job_items WHERE job_id=? AND state='partial' ORDER BY item_id LIMIT 20", (row["id"],))]
        if row["mode"] == "recent_window":
            skipped = db.execute("SELECT count(*) FROM recent_window_observations WHERE job_id=? AND disposition='existing_preserved'", (row["id"],)).fetchone()[0]
            differences = db.execute("SELECT count(*) FROM recent_window_observations WHERE job_id=? AND difference_json IS NOT NULL", (row["id"],)).fetchone()[0]
            result.update(sync_scope="recent_window", window_size=XHS_RECENT_WINDOW_SIZE,
                          window_warning=XHS_RECENT_WINDOW_WARNING, coverage="recent_window_only",
                          page_limit=1, list_finished=False, until_terminal=False,
                          window_list_saved=bool(run and run["pages"] == 1),
                          skipped_existing_count=skipped, listed_difference_count=differences,
                          update_detection="existing_content_preserved_update_unchecked")
        elif (row["platform"] == "xiaohongshu" and row["mode"] in XHS_LEGACY_NETWORK_MODES
              and self._source_kind(row["platform"], row["author_id"]) == "xhs_http"):
            result.update(can_resume=False, retired=True, retirement_reason="legacy_history_retired")
        association = db.execute("SELECT parent_job_id FROM pipeline_pages WHERE child_job_id=?", (row["id"],)).fetchone()
        if association:
            result.update(parent_job_id=association[0],can_resume=False)
        if row["mode"] in page_pipeline.MODES:
            checkpoint = db.execute("SELECT * FROM page_pipelines WHERE parent_job_id=?", (row["id"],)).fetchone()
            checkpoint = checkpoint or {"stage": "not_started", "page_limit": None}
            progress = page_pipeline.progress(db,row["id"])
            success = sum(item["state"] == "succeeded" for item in progress)
            failures = [item for item in progress if item["state"] == "partial"]
            result.update(page_limit=checkpoint["page_limit"] if row["mode"] == "page_archive" else None,
                          item_limit=page_pipeline.DEMO_ITEM_LIMIT if row["mode"] == "demo_archive" else None,
                          until_terminal=row["mode"] == "author_archive",list_finished=bool(run and run["terminal_evidence"]),
                          stage=checkpoint["stage"],listed_count=len(run_ids),
                          target_count=len(progress),item_count=success,failed_count=len(failures),
                          pending_count=len(progress)-success-len(failures),failed_items=failures[:20],
                          children=[dict(r) for r in db.execute("SELECT p.page_number,j.id,j.state,j.reason FROM pipeline_pages p JOIN jobs j ON j.id=p.child_job_id WHERE p.parent_job_id=? ORDER BY p.page_number", (row["id"],))])
            if row["mode"] == "author_archive":
                result["scan_url"] = f"/api/jobs/{row['id']}/scan"
        message, next_step = MESSAGES.get(row["reason"], ("任务正在处理，成功进度持续保存。" if row["state"] in {"queued", "running"} else "请查看最近窗口正文与媒体状态。" if row["mode"] == "recent_window" else "请查看历史覆盖与正文状态。", "等待任务结束，或查看已保存作品。"))
        if row["mode"] in {"content", "metrics", "source_refresh"} and row["reason"] in {"unavailable", "timeout"}:
            message = "作品详情或媒体暂未能获取，具体原因尚未确认；已有正文、成功媒体及指标原值与时间保留。"
            next_step = "核对该作者的后台来源及访问权限后恢复原任务；成功媒体会复用。"
        if row["mode"] in page_pipeline.MODES and row["reason"] in {"reference_missing","item_unavailable","unavailable","timeout"}:
            if row["reason"] in {"unavailable","timeout"} and checkpoint["stage"] == "content":
                message = "该作者的作品详情或媒体暂未能获取，具体原因尚未确认；原列表检查点、固定子任务及成功资源已保留。"
            next_step = "核对该作者后台来源及页链后恢复本父任务；系统沿用原检查点重新观察所需列表页，只重试未完成作品，成功资源会复用。若仍失败，保留原进度排查，无需新建任务。"
        if row["state"] == "succeeded" and not row["reason"] and run and run["coverage"] == "complete_for_accessible_scope":
            message = "本次列表扫描已到当前可获取范围的明确末页；正文和媒体完整性单独核验。"
            next_step = "查看已保存作品，或按作者归档现有资料。"
        if result.get("retired"):
            message, next_step = MESSAGES["legacy_history_retired"]
        result.update(message=message, next_step=next_step)
        if exported:
            result["export"] = exported
        return result

    def workspace(self) -> dict:
        with self.workflow.connect() as db:
            author_tags = {}
            for row in db.execute("SELECT platform,author_id,tag FROM subscription_tags ORDER BY position"):
                author_tags.setdefault((row["platform"], row["author_id"]), []).append(row["tag"])
            subscriptions = [dict(r, identity_verified=True, evidence_level="observed_platform_identity",
                                  subscription_confirmation_required=self._confirmation_required(r, db))
                             for r in db.execute("SELECT * FROM subscriptions ORDER BY created_at")]
            known = {(s["platform"], s["author_id"]) for s in subscriptions}
            subscriptions.extend(dict(r, identity_verified=False, evidence_level="unverified_link",
                                      subscription_confirmation_required=False)
                                 for r in db.execute("SELECT * FROM subscription_intents ORDER BY created_at")
                                 if (r["platform"], r["author_id"]) not in known)
            jobs = [self._public_job(dict(r), db) for r in db.execute("SELECT * FROM jobs ORDER BY id DESC")]
            archive_batches = self._archive_batches(db, jobs)
            for sub in subscriptions:
                args = (sub["platform"], sub["author_id"])
                sub["source_kind"] = self._source_kind(*args)
                sub["source_connected"] = sub["source_kind"] is not None
                sub["source_configured"] = sub["source_connected"]
                sub["tags"] = author_tags.get(args, [])
                sub["enabled"] = bool(sub["enabled"])
                sub["subscribed"] = not self._is_cancelled(db, *args)
                counts = db.execute("""SELECT count(*),coalesce(sum(detail_state='complete'),0),
                    coalesce(sum(media_state='complete_for_observed_detail'),0),
                    coalesce(sum(media_state='partial'),0),
                    coalesce(sum(media_state='unknown'),0)
                    FROM items WHERE platform=? AND author_id=?""", args).fetchone()
                sub.update(item_count=counts[0], detail_count=counts[1],
                           media_observed_complete_count=counts[2],media_partial_item_count=counts[3],
                           media_unknown_item_count=counts[4])
                sub["registered_media"] = {kind: number for kind, number in db.execute("""
                    SELECT a.kind,count(*) FROM assets a JOIN items i USING(platform,item_id)
                    WHERE i.platform=? AND i.author_id=? GROUP BY a.kind""", args)}
                source_version = None
                if sub["source_kind"] == "feed_http":
                    source_version = "http-feed-v1"
                elif sub["source_kind"] == "xhs_http":
                    from .adapters.xhs_http import XhsHttpTransport
                    source_version = XhsHttpTransport.version
                source_run = db.execute("""SELECT coverage,terminal_evidence FROM runs
                    WHERE platform=? AND author_id=? AND adapter_version=?
                    AND mode IN ('author_archive', 'full') ORDER BY id DESC LIMIT 1""", (*args, source_version)).fetchone()
                sub["source_history_coverage"] = source_run["coverage"] if source_run else "unknown"
                sub["source_terminal_observed"] = bool(source_run and source_run["terminal_evidence"])
                run = db.execute("SELECT coverage FROM runs WHERE platform=? AND author_id=? ORDER BY CASE WHEN coverage='complete_for_accessible_scope' THEN 0 ELSE 1 END,id DESC LIMIT 1", args).fetchone()
                sub["coverage"] = run[0] if run else "unknown"
                if sub["source_kind"] == "xhs_http":
                    sub.update(sync_scope="recent_window", window_size=XHS_RECENT_WINDOW_SIZE,
                               window_warning=XHS_RECENT_WINDOW_WARNING,
                               coverage="recent_window_only", source_history_coverage="not_applicable",
                               source_terminal_observed=False)
                latest = next((j for j in jobs if (j["platform"], j["author_id"]) == args and not j.get("parent_job_id")), None)
                latest_source_job = next((j for j in jobs if (j["platform"], j["author_id"]) == args
                                          and j["mode"] in {"full", "source_refresh", "recent_window"}
                                          and not j.get("parent_job_id")), None)
                latest_source_version = None
                if latest_source_job and latest_source_job.get("run_id"):
                    version_row = db.execute("SELECT adapter_version FROM runs WHERE id=?", (latest_source_job["run_id"],)).fetchone()
                    latest_source_version = version_row[0] if version_row else None
                sub.update(latest_state=latest["state"] if latest else "pending", reason=latest["reason"] if latest else "identity_unverified")
                platform_access = self._platform_access_reason(sub["platform"], db)
                sub["source_health"] = ("not_configured" if not sub["source_configured"] else
                                        platform_access if platform_access else
                                        "rate_limited" if latest and latest["reason"] == "rate_limited" else
                                        "last_run_succeeded" if latest_source_job and latest_source_job["mode"] in {"full", "recent_window"}
                                            and latest_source_job["state"] == "succeeded"
                                            and latest_source_version == source_version else
                                        "not_checked")
                sub["source_health_checked_at"] = (latest_source_job["updated_at"] if latest_source_job
                                                    and sub["source_health"] == "last_run_succeeded" else None)
                sub["message"] = (latest["message"] if latest else
                                  "作者身份已核验；尚未开始最近内容同步。" if sub["identity_verified"] else
                                  MESSAGES["identity_unverified"][0])
                exports = next((j["export"] for j in jobs if (j["platform"], j["author_id"]) == args and j.get("export")), None)
                if exports:
                    author_export = exports["authors"][0]
                    sub["archive_url"] = author_export.get("index_url", author_export["manifest_url"])
                for j in jobs:
                    if (j["platform"], j["author_id"]) == args:
                        j["display_name"] = sub["display_name"]
            stats = {"subscriptions": len(subscriptions), "items": db.execute("SELECT count(*) FROM items").fetchone()[0], "details": db.execute("SELECT count(*) FROM items WHERE detail_state='complete'").fetchone()[0], "running": sum(j["state"] in {"queued", "running"} for j in jobs)}
        return {"subscriptions": subscriptions, "runs": jobs, "archive_batches": archive_batches, "stats": stats,
                "platforms": [{"platform": "wechat", "available": any(s["platform"] == "wechat" and s["source_connected"] for s in subscriptions), "status": "deferred", "message": "公众号本轮暂缓；现有来源和资料保留。"},
                              {"platform": "xiaohongshu", "available": any(s["platform"] == "xiaohongshu" and s["source_connected"] for s in subscriptions), "status": "recent_window", "message": XHS_RECENT_WINDOW_WARNING}],
                "data_dir": str(self.root), "archive_dir": str(self.workflow.archive_root),
                "obsidian_dir": str(self.workflow.obsidian_root) if self.workflow.obsidian_root else None,
                "refresh_schedule": self.refresh_schedule(),
                "g1_passed": False}

    def _archive_batches(self, db, jobs):
        by_id = {job["id"]: job for job in jobs}
        result = []
        for batch in db.execute("SELECT id,mode,created_at FROM batches WHERE mode IN ('all_archive','selected_archive') ORDER BY id DESC"):
            members = []
            for row in db.execute("SELECT * FROM archive_batch_members WHERE batch_id=? ORDER BY platform,author_id", (batch["id"],)):
                job = by_id.get(row["job_id"])
                members.append({"platform": row["platform"], "author_id": row["author_id"],
                                "job_id": row["job_id"], "state": job["state"] if job else "blocked",
                                "reason": job["reason"] if job else row["reason"],
                                "reused_existing_job": row["reason"] == "existing_checkpoint",
                                "pages": job["pages"] if job else 0,
                                "list_finished": job.get("list_finished", False) if job else False,
                                "target_count": job.get("target_count", 0) if job else 0,
                                "item_count": job.get("item_count", 0) if job else 0,
                                "failed_count": job.get("failed_count", 0) if job else 0,
                                "pending_count": job.get("pending_count", 0) if job else 0,
                                "message": job["message"] if job else MESSAGES["wechat_blocked"][0],
                                "next_step": job["next_step"] if job else MESSAGES["wechat_blocked"][1],
                                "can_resume": job["can_resume"] if job else False})
                if job:
                    members[-1]["scan_url"] = f"/api/jobs/{job['id']}/scan"
                    for field in ("sync_scope", "window_size", "window_warning", "retired", "retirement_reason"):
                        if field in job:
                            members[-1][field] = job[field]
                if job and job.get("export"):
                    exported_author = next((author for author in job["export"]["authors"]
                                            if (author["platform"], author["author_id"]) ==
                                            (row["platform"], row["author_id"])), None)
                    if exported_author:
                        members[-1]["scan_manifest_url"] = exported_author.get("scan_manifest_url")
            running = any(m["state"] in {"queued", "running"} for m in members)
            complete = sum(m["state"] == "succeeded" for m in members)
            blocked = sum(m["job_id"] is None for m in members)
            result.append({"id": batch["id"], "mode": batch["mode"], "created_at": batch["created_at"],
                           "state": "running" if running else "succeeded" if complete == len(members) else "partial",
                           "total": len(members), "complete": complete, "blocked": blocked,
                           "unfinished": len(members) - complete - blocked,
                           "pending": sum(m["state"] in {"queued", "running", "interrupted"} for m in members),
                           "partial": sum(m["state"] == "partial" for m in members),
                           "failed": sum(m["job_id"] is not None and m["state"] in {"failed", "blocked", "needs_login", "rate_limited"} for m in members),
                           "items_complete": sum(m["item_count"] for m in members),
                           "items_target": sum(m["target_count"] for m in members),
                           "items_failed": sum(m["failed_count"] for m in members),
                           "items_pending": sum(m["pending_count"] for m in members),
                           "members": members})
        return result

    def job_scan(self, job_id: int) -> dict:
        with self.workflow.connect() as db:
            job = db.execute("SELECT run_id,mode,platform,author_id FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError("job_not_found")
            if job["mode"] not in {"author_archive", "full", "recent_window"}:
                raise ValueError("仅单作者归档或最近窗口任务有本轮列表清单")
            if not job["run_id"]:
                library_only = [row[0] for row in db.execute(
                    "SELECT item_id FROM items WHERE platform=? AND author_id=? ORDER BY published_at,item_id",
                    (job["platform"], job["author_id"]))]
                return {"schema_version": 2, "kind": "author_scan", "platform": job["platform"],
                        "author_id": job["author_id"], "parent_job_id": job_id,
                        "pages_scanned": 0, "list_finished": False,
                        "unseen_items": "unknown_not_enumerable", "status": "scan_not_started",
                        "counts": {"observed_unique": 0, "targeted_unique": 0, "complete": 0,
                                   "partial": 0, "pending": 0, "not_targeted": 0,
                                   "library_only": len(library_only)},
                        "observed_items": [], "library_only_item_ids": library_only}
            run = dict(db.execute("SELECT * FROM runs WHERE id=?", (job["run_id"],)).fetchone())
            items = [dict(row) for row in db.execute("SELECT * FROM items WHERE platform=? AND author_id=? ORDER BY published_at,item_id",
                                                  (job["platform"], job["author_id"]))]
            if job["mode"] in {"full", "recent_window"}:
                observed = {}
                for page in db.execute("SELECT page_number,item_ids FROM pages WHERE run_id=? ORDER BY page_number", (job["run_id"],)):
                    for item_id in json.loads(page["item_ids"]):
                        observed.setdefault(item_id, []).append(page["page_number"])
                targets = {row["item_id"]: dict(row) for row in db.execute(
                    "SELECT item_id,state,reason FROM job_items WHERE job_id=?", (job_id,))}
                library = {item["item_id"]: item for item in items}
                observed_items = []
                for item_id, page_numbers in observed.items():
                    target = targets.get(item_id)
                    item = library.get(item_id)
                    state = ("complete" if target and target["state"] == "succeeded" else
                             "partial" if target and target["state"] == "partial" else
                             "pending" if target else "not_targeted")
                    observed_items.append({"item_id": item_id, "pages": page_numbers,
                                           "download_state": state,
                                           "reason": target["reason"] if target else None,
                                           "detail_state": item["detail_state"] if item else "missing",
                                           "media_state": item["media_state"] if item else "unknown"})
                library_only = [item["item_id"] for item in items if item["item_id"] not in observed]
                result = {"schema_version": 2, "kind": "author_scan", "platform": job["platform"],
                        "author_id": job["author_id"], "run_id": job["run_id"],
                        "parent_job_id": job_id, "pages_scanned": run["pages"],
                        "coverage": run["coverage"], "list_finished": bool(run["terminal_evidence"]),
                        "terminal_evidence": run["terminal_evidence"],
                        "unseen_items": "unknown_not_enumerable",
                        "counts": {"observed_unique": len(observed_items), "targeted_unique": len(targets),
                                   "complete": sum(item["download_state"] == "complete" for item in observed_items),
                                   "partial": sum(item["download_state"] == "partial" for item in observed_items),
                                   "pending": sum(item["download_state"] == "pending" for item in observed_items),
                                   "not_targeted": sum(item["download_state"] == "not_targeted" for item in observed_items),
                                   "library_only": len(library_only)},
                        "observed_items": observed_items, "library_only_item_ids": library_only}
                if job["mode"] == "recent_window":
                    result.update(kind="recent_window_scan", sync_scope="recent_window",
                                  coverage="recent_window_only", list_finished=False,
                                  terminal_evidence=None, window_size=XHS_RECENT_WINDOW_SIZE,
                                  window_warning=XHS_RECENT_WINDOW_WARNING)
                return result
            return self.workflow._author_scan_manifest(db, run, items)

    def _archive_status(self, db, item):
        """Classify saved local evidence, independent of author list coverage."""
        assets = db.execute("SELECT asset_id FROM assets WHERE platform=? AND item_id=?",
                            (item["platform"], item["item_id"])).fetchall()
        has_body = item["detail_state"] == "complete" and bool((item["detail_text"] or "").strip())
        if not has_body:
            return "partial" if assets else "missing"
        if any(not self.workflow.asset_valid(item["platform"], item["item_id"], asset["asset_id"])
               for asset in assets):
            return "partial"
        if item["media_state"] == "partial":
            return "partial"
        if item["media_state"] != "complete_for_observed_detail":
            return "unknown"
        return "complete"

    def items(self, platform=None, author_id=None, offset=0, limit=50, has_assets=False,
              *, sort="published_at", order="desc", min_likes=None, min_collects=None, min_comments=None,
              date_from=None, date_to=None, content_type=None, missing_metric=None,
              text=None, archive_status=None) -> dict:
        if sort not in (*FIELDS,"published_at") or order not in {"asc","desc"}:
            raise ValueError("无效排序字段或方向")
        if content_type not in {None,"image","video","unknown"} or missing_metric not in {None,*FIELDS}:
            raise ValueError("无效作品类型或未知指标")
        if archive_status not in {None,"complete","partial","missing","unknown"}:
            raise ValueError("无效本机归档状态")
        text = text.strip() if text is not None else None
        if text and len(text) > 200:
            raise ValueError("搜索文字最多200字符；本机资料未修改")
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("无效分页范围")
        clauses, params = [], []
        for key, value in (("platform", platform), ("author_id", author_id)):
            if value:
                clauses.append(f"i.{key}=?")
                params.append(value)
        if has_assets:
            clauses.append("EXISTS (SELECT 1 FROM assets a WHERE a.platform=i.platform AND a.item_id=i.item_id)")
        if content_type:
            clauses.append("i.content_type=?")
            params.append(content_type)
        if text:
            pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            clauses.append("(i.title LIKE ? ESCAPE '\\' OR i.detail_text LIKE ? ESCAPE '\\' "
                           "OR s.display_name LIKE ? ESCAPE '\\' OR i.author_id LIKE ? ESCAPE '\\' "
                           "OR i.item_id LIKE ? ESCAPE '\\')")
            params.extend([pattern] * 5)
        for key, value in (("date_from",date_from),("date_to",date_to)):
            if value:
                try:
                    parsed = date.fromisoformat(value)
                except ValueError:
                    raise ValueError("发布日期须为 YYYY-MM-DD") from None
                clauses.append("i.published_at>=?" if key == "date_from" else "i.published_at<?")
                params.append(parsed.isoformat() if key == "date_from" else (parsed+timedelta(days=1)).isoformat())
                clauses.append("i.published_at!=''")
        for field, value in (("likes",min_likes),("collects",min_collects),("comments",min_comments)):
            if value is not None:
                if type(value) is not int or value < 0 or value > 9223372036854775807:
                    raise ValueError("指标最小值须为非负整数")
                clauses.append(f"{field}.value>=?")
                params.append(value)
        if missing_metric:
            clauses.append(f"{missing_metric}.value IS NULL")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        joins = " LEFT JOIN subscriptions s ON s.platform=i.platform AND s.author_id=i.author_id" + "".join(f" LEFT JOIN item_metrics {f} ON {f}.platform=i.platform AND {f}.item_id=i.item_id AND {f}.field='{f}'" for f in FIELDS)
        sort_expr = "NULLIF(i.published_at,'')" if sort == "published_at" else f"{sort}.value"
        sort_sql = f" ORDER BY {sort_expr} IS NULL, {sort_expr} {order}, i.item_id, i.platform"
        with self.workflow.connect() as db:
            select = "SELECT i.platform,i.item_id,i.author_id,i.published_at,i.detail_state,i.detail_text,i.source_url,i.content_type,i.title,i.media_state,s.display_name,(SELECT count(*) FROM assets a WHERE a.platform=i.platform AND a.item_id=i.item_id) AS asset_count FROM items i"
            if archive_status:
                rows, total = [], 0
                for candidate in db.execute(select + joins + where + sort_sql, params):
                    row = dict(candidate)
                    row["archive_status"] = self._archive_status(db, row)
                    if row["archive_status"] == archive_status:
                        if offset <= total < offset + limit:
                            rows.append(row)
                        total += 1
            else:
                total = db.execute("SELECT count(*) FROM items i" + joins + where, params).fetchone()[0]
                rows = [dict(r) for r in db.execute(select + joins + where + sort_sql + " LIMIT ? OFFSET ?", params + [limit, offset])]
                for row in rows:
                    row["archive_status"] = self._archive_status(db, row)
            for row in rows:
                row.pop("detail_text")
                row["metrics"] = read_metrics(db,row["platform"],row["item_id"])
        return {"items": rows, "total": total, "offset": offset, "limit": limit}

    def item(self, platform, item_id) -> dict:
        with self.workflow.connect() as db:
            row = db.execute("SELECT * FROM items WHERE platform=? AND item_id=?", (platform, item_id)).fetchone()
            if not row:
                raise KeyError("item_not_found")
            result = dict(row)
            result["metrics"] = read_metrics(db, platform, item_id)
            result["metric_snapshots"] = read_snapshots(db, platform, item_id)
            result["snapshot_total"] = len(result["metric_snapshots"])
            result["assets"] = [dict(r) for r in db.execute("SELECT asset_id,kind,mime,relative_path,bytes FROM assets WHERE platform=? AND item_id=? ORDER BY position", (platform, item_id))]
            result["archive_status"] = self._archive_status(db, result)
        for asset in result["assets"]:
            asset["state"] = "complete" if self.workflow.asset_valid(platform,item_id,asset["asset_id"]) else "missing"
            asset["url"] = "/archive/" + quote(Path(asset["relative_path"]).as_posix(), safe="/") if asset["state"] == "complete" else None
        result["media_coverage"] = "unknown_expected_count"
        return result

    def _validate_source(self, source_url, platform, item_id, author_id=None):
        if not source_url:
            return None
        if not item_id or platform != "xiaohongshu" or len(source_url) > 4096:
            raise ValueError("补充原文链接只用于小红书单作品任务")
        try:
            canonical = _canonical_source_url(platform,source_url)
        except ValueError:
            raise ValueError("请粘贴该作品的小红书完整 HTTPS 原文链接") from None
        if urlsplit(canonical).path.rstrip("/") not in {f"/explore/{item_id}",f"/discovery/item/{item_id}",f"/user/profile/{author_id}/{item_id}"}:
            raise ValueError("原文链接的作品ID与当前作品不匹配")
        return source_url

    def start(self, mode, platform=None, author_id=None, item_id=None, source_url=None, item_ids=None, selected_authors=None) -> dict:
        if mode in {"all_archive", "selected_archive"}:
            if any(value is not None for value in (platform, author_id, item_id, source_url, item_ids)) or (mode == "all_archive" and selected_authors is not None):
                raise ValueError("全订阅归档只接受固定的当前订阅范围，不接受单作者或作品参数")
            if mode == "selected_archive" and selected_authors is None:
                raise ValueError("所选作者归档需要明确的作者范围；未创建任务")
            return self.start_archive_batch(selected_authors)
        if (mode in XHS_LEGACY_NETWORK_MODES
                and platform == "xiaohongshu" and author_id and self._source_kind(platform, author_id) == "xhs_http"):
            mode = "recent_window"
        if mode == "latest" and not self.adapter_factory:
            mode = "source_refresh"
        if selected_authors is not None and mode not in {"archive", "source_refresh", "recent_window"}:
            raise ValueError("所选作者范围仅用于后台来源刷新或本机已有资料导出")
        selected_export = self._selected_author_keys(selected_authors) if selected_authors is not None else None
        if selected_export is not None and any(value is not None for value in (platform, author_id, item_id, source_url, item_ids)):
            raise ValueError("所选作者本机导出只接受作者选择范围")
        if mode not in {"full", "latest", "source_refresh", "recent_window", "archive", "content", "metrics", *page_pipeline.MODES} or bool(platform) != bool(author_id):
            raise ValueError("请选择有效模式；指定作者时须同时提供平台和作者ID")
        if mode in {"page_archive", "author_archive"} and (not author_id or
                (platform != "xiaohongshu" and not self.source_config(platform, author_id))):
            raise ValueError("完整历史任务须指定已配置的作者来源")
        if mode == "demo_archive" and author_id and platform != "xiaohongshu":
            raise ValueError("前10篇Demo仅支持已核验并已确认订阅的小红书作者")
        if item_id and (not author_id or mode not in {"content","metrics"}):
            raise ValueError("单作品任务须提供平台、作者及内容或指标模式")
        if item_ids is not None and (not item_ids or len(item_ids)>200 or item_id or not author_id or mode not in {"content","metrics"} or source_url):
            raise ValueError("指定作品批次须提供同一作者的1至200个ID，且不能同时提供单篇参数；全部作者范围不受此限制")
        selected = list(dict.fromkeys(item_ids)) if item_ids is not None else None
        source_url = self._validate_source(source_url,platform,item_id,author_id)
        scope = [s for s in self.workspace()["subscriptions"]
                 if (not author_id and s["subscribed"] and not s["subscription_confirmation_required"])
                 or (author_id and (s["platform"], s["author_id"]) == (platform, author_id))]
        if selected_export is not None:
            scope = [s for s in scope if (s["platform"], s["author_id"]) in selected_export
                     and s["identity_verified"]]
            if {(s["platform"], s["author_id"]) for s in scope} != selected_export:
                raise ValueError("所选作者须已核验、确认且仍订阅；请刷新选择。旧资料保持不变")
        if mode in {"source_refresh", "recent_window"}:
            scope = [s for s in scope if s["identity_verified"] and self.source_config(s["platform"], s["author_id"])]
            if mode == "recent_window" and any(s["source_kind"] != "xhs_http" for s in scope):
                raise ValueError("最近窗口同步需要已配置的小红书后台 HTTP 来源")
            if selected_export is not None and {(s["platform"], s["author_id"]) for s in scope} != selected_export:
                raise ValueError("所选作者必须已核验并配置后台来源")
        if author_id and scope and not scope[0]["subscribed"] and mode != "archive":
            raise ValueError("该作者已取消订阅；请先重新订阅。原归档和任务检查点保留；已有任务可从历史与任务恢复")
        if mode == "demo_archive" and not author_id:
            scope = [s for s in scope if s["platform"] == "xiaohongshu" and s["identity_verified"]
                     and not s["subscription_confirmation_required"]]
        if mode == "archive" and not author_id and selected_export is None:
            scope = [s for s in scope if s["identity_verified"]]
        if not scope:
            raise ValueError("没有可处理的订阅作者；待确认作者请先确认，已取消作者请先重新订阅。旧归档和任务检查点保留")
        if mode in {"full", "latest", "source_refresh", "recent_window", *page_pipeline.MODES} and any(s["subscription_confirmation_required"] for s in scope):
            raise ValueError("该作品作者尚未确认订阅；请先在作者卡片确认，原历史和作品保持不变")
        if mode == "archive" and any(s["subscription_confirmation_required"] or not s["subscribed"] or not s["identity_verified"] for s in scope):
            raise ValueError("本机导出仅支持已核验、已确认且仍订阅的作者；原资料保持")
        if mode in page_pipeline.MODES and any(not s["identity_verified"] for s in scope):
            raise ValueError("按页闭环实验需要已核验的订阅作者")
        ids = []
        with self.workflow.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if item_id and not db.execute("SELECT 1 FROM items WHERE platform=? AND author_id=? AND item_id=?", (platform,author_id,item_id)).fetchone():
                raise KeyError("item_not_found")
            if selected and any(not db.execute("SELECT 1 FROM items WHERE platform=? AND author_id=? AND item_id=?", (platform,author_id,i)).fetchone() for i in selected):
                raise ValueError("指定作品不属于该作者的本地资料；未创建任务")
            for sub in scope:
                if mode != "archive":
                    self._check_cooldown(sub["platform"], db)
                    self._check_platform_access(sub["platform"], db)
                active = db.execute("SELECT id FROM jobs WHERE platform=? AND author_id=? AND state IN ('queued','running')", (sub["platform"], sub["author_id"])).fetchone()
                if active and mode != "archive":
                    raise ValueError("该作者已有任务排队或运行，请等待完成")
            for sub in scope:
                author_mode = ("recent_window" if mode in {*XHS_LEGACY_NETWORK_MODES, "recent_window"}
                               and self._source_kind(sub["platform"], sub["author_id"]) == "xhs_http" else mode)
                cur = db.execute("INSERT INTO jobs(platform,author_id,mode,state,created_at,updated_at) VALUES(?,?,?,'queued',?,?)", (sub["platform"], sub["author_id"], author_mode, time.time(), time.time()))
                ids.append(cur.lastrowid)
                if author_mode in page_pipeline.MODES:
                    db.execute("INSERT INTO page_pipelines(parent_job_id,page_limit) VALUES(?,2)", (cur.lastrowid,))
                if mode in {"content","metrics"}:
                    clause = " AND item_id IN (" + ",".join("?" for _ in selected) + ")" if selected else " AND item_id=?" if item_id else ""
                    db.execute("INSERT INTO job_items(job_id,platform,item_id) SELECT ?,platform,item_id FROM items WHERE platform=? AND author_id=?" + clause,
                               [cur.lastrowid,sub["platform"],sub["author_id"]] + (selected or ([item_id] if item_id else [])))
        for job_id in ids:
            if source_url:
                self._job_sources[job_id] = source_url
            self._spawn(job_id)
        return {"job_id": ids[0], "job_ids": ids, "state": "queued", "includes_paused": True}

    @staticmethod
    def _selected_author_keys(selected_authors):
        if selected_authors is None:
            return None
        if not isinstance(selected_authors, list) or not selected_authors:
            raise ValueError("请先选择至少一位作者；未创建任务，旧资料与进度保留")
        keys = []
        for author in selected_authors:
            if not isinstance(author, dict) or set(author) != {"platform", "author_id"} or not all(isinstance(author[k], str) and author[k] for k in ("platform", "author_id")):
                raise ValueError("所选作者须提供稳定的平台和作者ID；未创建任务")
            keys.append((author["platform"], author["author_id"]))
        if len(set(keys)) != len(keys):
            raise ValueError("所选作者有重复项；请检查后重试，未创建任务")
        return set(keys)

    def archive_batch_preview(self, selected_authors):
        """Resolve the exact manual scope without creating jobs or calling a platform."""
        keys = self._selected_author_keys(selected_authors)
        with self.workflow.connect() as db:
            scope = self._archive_batch_scope(db, keys)
        return {"mode": "selected_archive", "total": len(scope),
                "members": [{"platform": s["platform"], "author_id": s["author_id"],
                             "display_name": s["display_name"], "paused": not bool(s["enabled"]),
                             "platform_available": self.source_config(s["platform"], s["author_id"]) is not None} for s in scope],
                "message": "预览仅核对当前可选择作者；执行时再次核对并固定范围。小红书只同步最近第一页，未配置来源的作者保留明确缺口。"}

    def _archive_batch_scope(self, db, selected_keys):
        eligible = {(row["platform"], row["author_id"]): dict(row)
                    for row in db.execute("SELECT * FROM subscriptions ORDER BY platform,author_id")
                    if not self._is_cancelled(db, row["platform"], row["author_id"])
                    and not self._confirmation_required(row, db)}
        if selected_keys is not None:
            missing = selected_keys - eligible.keys()
            if missing:
                label = ", ".join(f"{platform}/{author_id}" for platform, author_id in sorted(missing))
                raise ValueError(f"所选作者不在已核验、已确认且仍订阅范围：{label}；请刷新选择。旧资料和任务进度保留")
            return [eligible[key] for key in sorted(selected_keys)]
        if not eligible:
            raise ValueError("没有已核验并确认且仍订阅的作者；待确认作者请先确认，已取消作者请先重新订阅。旧归档和检查点保留")
        return [eligible[key] for key in sorted(eligible)]

    def _archive_job_compatible(self, db, job_id, platform, author_id):
        """A saved page cursor belongs to the adapter that produced it."""
        config = self.source_config(platform, author_id)
        if config is not None:
            if self._source_kind(platform, author_id) == "xhs_http":
                from .adapters.xhs_http import XhsHttpTransport
                version = XhsHttpTransport.version
            else:
                from .adapters.feed_http import FeedHttpTransport
                version = FeedHttpTransport.version
        elif platform == "xiaohongshu" and self.adapter_factory:
            version = self.transport().version
        else:
            return False
        row = db.execute("""SELECT r.adapter_version,j.mode FROM jobs j LEFT JOIN runs r ON r.id=j.run_id
            WHERE j.id=?""", (job_id,)).fetchone()
        if row and self._source_kind(platform, author_id) == "xhs_http" and row[1] in XHS_LEGACY_NETWORK_MODES:
            return False
        return bool(row) and row[0] in {None, "pending", version}

    def start_archive_batch(self, selected_authors=None):
        """Snapshot subscriptions, retaining each author's latest unfinished checkpoint."""
        selected_keys = self._selected_author_keys(selected_authors)
        batch_mode = "selected_archive" if selected_keys is not None else "all_archive"
        with self.workflow.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior_batches = db.execute("SELECT id FROM batches WHERE mode=? ORDER BY id DESC", (batch_mode,)).fetchall()
            for prior in prior_batches[:1] if selected_keys is None else prior_batches:
                prior_members = [dict(row) for row in db.execute(
                    "SELECT platform,author_id,job_id FROM archive_batch_members WHERE batch_id=?", (prior[0],))]
                if selected_keys is not None and {
                        (row["platform"], row["author_id"]) for row in prior_members} != selected_keys:
                    continue
                if any(row["job_id"] is not None and not self._archive_job_compatible(
                        db, row["job_id"], row["platform"], row["author_id"]) for row in prior_members):
                    continue
                if any(row["job_id"] is None and self.source_config(row["platform"], row["author_id"])
                       for row in prior_members):
                    continue
                unfinished = db.execute("""SELECT 1 FROM archive_batch_members m LEFT JOIN jobs j ON j.id=m.job_id
                    WHERE m.batch_id=? AND (j.state!='succeeded' OR (?='selected_archive' AND m.job_id IS NULL)) LIMIT 1""",
                    (prior[0], batch_mode)).fetchone()
                if unfinished:
                    return {"batch_id": prior[0], "reused": True,
                            "message": f"已复用逐作者归档批次 #{prior[0]} 的固定作者范围；从历史与任务恢复未完成作者，成功资源保持。"}
            scope = self._archive_batch_scope(db, selected_keys)
            for sub in scope:
                if sub["platform"] == "xiaohongshu" and db.execute("""SELECT 1 FROM jobs WHERE platform=? AND author_id=?
                    AND state IN ('queued','running') LIMIT 1""", (sub["platform"],sub["author_id"])).fetchone():
                    raise ValueError(f"作者 {sub['display_name']} 已有任务排队或运行，请等待完成后创建逐作者归档批次；已有进度保留")
            batch_id = db.execute("INSERT INTO batches(mode,created_at) VALUES(?,?)", (batch_mode,time.time())).lastrowid
            ids = []
            reused_ids = []
            for sub in scope:
                job_id = None
                history_mode = "recent_window" if self._source_kind(sub["platform"], sub["author_id"]) == "xhs_http" else "author_archive"
                reason = ("wechat_blocked" if sub["platform"] == "wechat" else "source_unconfigured") if (
                    not self.source_config(sub["platform"], sub["author_id"]) and
                    not (sub["platform"] == "xiaohongshu" and self.adapter_factory)) else None
                if reason is None:
                    latest = db.execute("""SELECT j.id,j.state,m.batch_id FROM jobs j
                        LEFT JOIN archive_batch_members m ON m.job_id=j.id
                        WHERE j.platform=? AND j.author_id=? AND j.mode=?
                        ORDER BY j.id DESC LIMIT 1""", (sub["platform"],sub["author_id"],history_mode)).fetchone()
                    if latest and latest["state"] != "succeeded" and self._archive_job_compatible(
                            db, latest["id"], sub["platform"], sub["author_id"]):
                        if latest["batch_id"] is not None:
                            raise ValueError(f"作者 {sub['display_name']} 的未完成历史任务已属于批次 #{latest['batch_id']}；请恢复原批次")
                        job_id = latest["id"]
                        reason = "existing_checkpoint"
                        reused_ids.append(job_id)
                    else:
                        job_id = db.execute("""INSERT INTO jobs(platform,author_id,mode,state,created_at,updated_at)
                            VALUES(?,? ,?,'queued',?,?)""", (sub["platform"],sub["author_id"],history_mode,time.time(),time.time())).lastrowid
                        if history_mode == "author_archive":
                            db.execute("INSERT INTO page_pipelines(parent_job_id,page_limit) VALUES(?,2)", (job_id,))
                        ids.append(job_id)
                db.execute("INSERT INTO archive_batch_members VALUES(?,?,?,?,?)",
                           (batch_id,sub["platform"],sub["author_id"],job_id,reason))
        for job_id in ids:
            self._spawn(job_id)
        return {"batch_id": batch_id, "job_ids": ids, "reused_job_ids": reused_ids,
                "reused": False, "includes_paused": True,
                "message": f"已建立{'所选作者' if selected_keys is not None else '全订阅'}归档批次 #{batch_id}；接入 {len(reused_ids)} 个已有检查点，新建 {len(ids)} 个作者任务。已有未完成任务不会自动重试；未配置来源的成员保留缺口。"}

    def resume_archive_batch(self, batch_id):
        with self.workflow.connect() as db:
            members = [tuple(row) for row in db.execute("SELECT job_id FROM archive_batch_members WHERE batch_id=? ORDER BY platform,author_id", (batch_id,))]
        if not members:
            raise KeyError("batch_not_found")
        resumed, waiting = [], []
        for (job_id,) in members:
            if job_id is None:
                continue
            with self.workflow.connect() as db:
                state = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
            if state in {"queued", "running", "succeeded"}:
                continue
            try:
                self.resume(job_id)
                resumed.append(job_id)
            except (ValueError, PlatformCooldown) as error:
                waiting.append({"job_id": job_id, "message": str(error)})
        return {"batch_id": batch_id, "resumed_job_ids": resumed, "waiting": waiting,
                "message": f"批次 #{batch_id} 已恢复 {len(resumed)} 位作者；其余状态与固定范围保持。"}

    def _batch_sources(self, db, job, source_urls):
        if not source_urls:
            return {}
        if job["mode"] not in {"content", "metrics"} or job["platform"] != "xiaohongshu" or len(source_urls) > 200:
            raise ValueError("每次可为小红书内容或指标任务补充至多200条完整链接，可分批补充")
        result = {}
        for url in source_urls:
            if not isinstance(url, str):
                raise ValueError("请每行粘贴一条完整官方作品链接")
            item_id = urlsplit(url).path.rstrip("/").split("/")[-1]
            value = self._validate_source(url,job["platform"],item_id,job["author_id"])
            target = db.execute("SELECT state FROM job_items WHERE job_id=? AND item_id=?", (job["id"],item_id)).fetchone()
            if not target:
                raise ValueError("补充链接包含原批次以外的作品；原任务与资料未改动")
            if item_id in result and result[item_id] != value:
                raise ValueError("同一作品提供了不同链接，请每篇保留一条")
            if target[0] != "succeeded":
                result[item_id] = value
        return result

    def job_failures(self, job_id, offset=0, limit=50):
        with self.workflow.connect() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                raise KeyError("job_not_found")
            total = db.execute("SELECT count(*) FROM job_items WHERE job_id=? AND state='partial'", (job_id,)).fetchone()[0]
            rows = [dict(r) for r in db.execute("SELECT item_id,reason FROM job_items WHERE job_id=? AND state='partial' ORDER BY item_id LIMIT ? OFFSET ?", (job_id,limit,offset))]
            if job["mode"] in page_pipeline.MODES:
                failures = [item for item in page_pipeline.progress(db,job_id) if item["state"] == "partial"]
                total, rows = len(failures), failures[offset:offset+limit]
        for row in rows:
            row["message"], row["next_step"] = MESSAGES.get(row["reason"], MESSAGES["unexpected_error"])
            if job["mode"] in page_pipeline.MODES and row["reason"] in {"reference_missing","item_unavailable","unavailable","timeout"}:
                row["next_step"] = "在专用浏览器确认该作品可访问后恢复本父任务；系统重新观察原列表页，只重试未完成作品。若仍取不到引用，会保留部分归档与原进度。"
        return {"total": total,"items":rows,"platform":job["platform"],"author_id":job["author_id"]}

    def resume(self, job_id, source_url=None, source_urls=None) -> dict:
        with self.workflow.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                raise KeyError("job_not_found")
            if (job["mode"] in XHS_LEGACY_NETWORK_MODES and
                    self._source_kind(job["platform"], job["author_id"]) == "xhs_http"):
                raise ValueError(MESSAGES["legacy_history_retired"][0] + MESSAGES["legacy_history_retired"][1])
            association = db.execute("SELECT parent_job_id FROM pipeline_pages WHERE child_job_id=?", (job_id,)).fetchone()
            if association:
                raise ValueError(f"该内容子任务属于按页实验，请恢复父任务 {association[0]}，避免丢失列表检查点")
            if source_url and source_urls:
                raise ValueError("请使用单篇链接或多篇链接其中一种方式")
            sources = self._batch_sources(db,job,source_urls)
            if source_url:
                targets = db.execute("SELECT item_id FROM job_items WHERE job_id=?", (job_id,)).fetchall()
                if len(targets) != 1 or job["mode"] not in {"content","metrics"}:
                    raise ValueError("补充原文链接只用于单作品内容或指标任务")
                source_url = self._validate_source(source_url,job["platform"],targets[0][0],job["author_id"])
            if job["mode"] != "archive":
                self._check_cooldown(job["platform"], db)
                self._check_platform_access(job["platform"], db)
            if job["state"] in {"queued", "running", "succeeded"}:
                raise ValueError("该任务运行中或已成功，无需恢复")
            if db.execute("SELECT 1 FROM jobs WHERE platform=? AND author_id=? AND state IN ('queued','running') AND id!=? AND id NOT IN (SELECT child_job_id FROM pipeline_pages WHERE parent_job_id=?)", (job["platform"], job["author_id"], job_id,job_id)).fetchone():
                raise ValueError("该作者已有其他任务运行，请等待后恢复")
            if job["run_id"]:
                run = db.execute("SELECT retry_at FROM runs WHERE id=?", (job["run_id"],)).fetchone()
                if run and run[0] > time.time():
                    raise ValueError("平台冷却尚未结束，请等待后再恢复")
            db.execute("UPDATE jobs SET state='queued',reason=NULL,updated_at=? WHERE id=?", (time.time(), job_id))
        if source_url:
            self._job_sources[job_id] = source_url
        elif sources:
            self._job_sources[job_id] = {**(self._job_sources.get(job_id) if isinstance(self._job_sources.get(job_id),dict) else {}), **sources}
        with self._pause_lock:
            self._pause_requests.discard(job_id)
        self._spawn(job_id)
        return {"job_id": job_id, "state": "queued"}

    def _spawn(self, job_id):
        # Only the explicitly submitted job inherits its bounded manual scope.
        context = copy_context()
        thread = Thread(target=context.run, args=(self._execute, job_id), daemon=True)
        self._threads.add(thread)
        thread.start()

    def wait(self, timeout=30):
        for thread in list(self._threads):
            thread.join(timeout)
            if not thread.is_alive():
                self._threads.discard(thread)

    def _finish(self, job_id, state, reason=None, export=None):
        scope = network_safety.manual_scope()
        if scope is not None and state not in {"queued", "running"}:
            self.manual_validation.complete(scope, success=state == "succeeded", reason=reason)
        with self.workflow.connect() as db:
            db.execute("UPDATE jobs SET state=?,reason=?,export_json=?,updated_at=? WHERE id=?", (state, reason, json.dumps(export) if export else None, time.time(), job_id))
            job = db.execute("SELECT mode,run_id FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job and job["mode"] == "recent_window" and job["run_id"]:
                db.execute("UPDATE runs SET state=?,reason=?,coverage='recent_window_only',cursor=NULL,terminal_evidence=NULL,updated_at=? WHERE id=?",
                           (state, reason, time.time(), job["run_id"]))
            if job and job["mode"] in page_pipeline.MODES and state not in {"queued","running","succeeded"}:
                # A failure may occur after the page transaction but before
                # content starts (e.g. hashing an existing unreadable asset).
                # Close only this parent's active children, allowing login and
                # recovery without leaving a phantom running task behind.
                db.execute("UPDATE jobs SET state=?,reason=?,updated_at=? WHERE state IN ('queued','running') AND id IN (SELECT child_job_id FROM pipeline_pages WHERE parent_job_id=?)", (state,reason,time.time(),job_id))
                if job["run_id"]:
                    # Content interruption does not invalidate an observed
                    # terminal list response, nor advance any page checkpoint.
                    db.execute("""UPDATE runs SET
                        state=CASE WHEN terminal_evidence IS NOT NULL THEN 'succeeded' ELSE ? END,
                        coverage=CASE WHEN terminal_evidence IS NOT NULL THEN 'complete_for_accessible_scope' ELSE 'partial' END,
                        reason=CASE WHEN terminal_evidence IS NOT NULL THEN NULL ELSE ? END,
                        updated_at=? WHERE id=?""", (state,reason,time.time(),job["run_id"]))
        if state == "succeeded":
            self._job_sources.pop(job_id,None)
        if state not in {"queued", "running"}:
            with self._pause_lock:
                self._pause_requests.discard(job_id)

    def _execute(self, job_id):
        with self._lock:
            if self._pause_requested(job_id):
                self._finish(job_id, "interrupted", "user_paused")
                return
            if self._stopping.is_set():
                self._finish(job_id,"interrupted","process_interrupted")
                return
            try:
                with self.workflow.connect() as db:
                    job = dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
                    if (job["mode"] in XHS_LEGACY_NETWORK_MODES and
                            self._source_kind(job["platform"], job["author_id"]) == "xhs_http"):
                        self._finish(job_id, "blocked", "legacy_history_retired")
                        return
                    db.execute("UPDATE jobs SET state='running',updated_at=? WHERE id=?", (time.time(), job_id))
                    verified = db.execute("SELECT 1 FROM subscriptions WHERE platform=? AND author_id=?", (job["platform"], job["author_id"])).fetchone()
                if job["mode"] != "archive":
                    self._check_cooldown(job["platform"])
                    self._check_platform_access(job["platform"])
                if not verified:
                    if job["mode"] != "archive" and (self.adapter_factory or self.source_config(job["platform"], job["author_id"])):
                        self.verify(job["platform"], job["author_id"])
                    else:
                        self._finish(job_id, "blocked", "wechat_blocked" if job["platform"] == "wechat" else "identity_unverified")
                        return
                if job["mode"] == "source_refresh":
                    self._execute_source_refresh(job)
                    return
                if job["mode"] in {"content","metrics"}:
                    self._execute_content(job)
                    return
                if job["mode"] in page_pipeline.MODES:
                    page_pipeline.execute(self,job)
                    return
                if not job["run_id"]:
                    with self.workflow.connect() as db:
                        batch = db.execute("INSERT INTO batches(mode,created_at) VALUES(?,?)", (job["mode"], time.time())).lastrowid
                        run_id = db.execute("INSERT INTO runs(batch_id,platform,author_id,mode,adapter_version,updated_at) VALUES(?,?,?,?,?,?)", (batch, job["platform"], job["author_id"], job["mode"], "pending", time.time())).lastrowid
                        db.execute("UPDATE jobs SET run_id=? WHERE id=?", (run_id, job_id))
                else:
                    run_id = job["run_id"]
                    with self.workflow.connect() as db:
                        batch = db.execute("SELECT batch_id FROM runs WHERE id=?", (run_id,)).fetchone()[0]
                job["run_id"] = run_id
                if job["mode"] == "archive":
                    with self.workflow.connect() as db:
                        known = db.execute("SELECT coverage,terminal_evidence FROM runs WHERE platform=? AND author_id=? AND id!=? ORDER BY CASE WHEN coverage='complete_for_accessible_scope' THEN 0 ELSE 1 END,id DESC LIMIT 1", (job["platform"], job["author_id"], run_id)).fetchone()
                        recent_source = self._source_kind(job["platform"], job["author_id"]) == "xhs_http"
                        db.execute("UPDATE runs SET state='succeeded',coverage=?,terminal_evidence=?,updated_at=? WHERE id=?",
                                   ("recent_window_only" if recent_source else known[0] if known else "unknown",
                                    None if recent_source else known[1] if known else None, time.time(), run_id))
                    exported = self.workflow.export_all(batch_id=batch,
                        recent_window_authors={(job["platform"], job["author_id"])} if self._source_kind(job["platform"], job["author_id"]) == "xhs_http" else ())
                    for author in exported["authors"]:
                        for key in ("manifest", "corpus", "index", "failures"):
                            author[key + "_url"] = "/archive/" + quote(Path(author[key]).relative_to(self.workflow.archive_root).as_posix(), safe="/")
                    self._finish(job_id, "succeeded", "archive_complete", exported)
                    return
                if not self.source_config(job["platform"], job["author_id"]) and (job["platform"] == "wechat" or not self.adapter_factory):
                    self._finish(job_id, "blocked", "wechat_blocked")
                    return
                if job["mode"] == "recent_window":
                    self._execute_xhs_recent_window(job, run_id, batch)
                    return
                with self._network_lock:
                    self._check_cooldown(job["platform"])
                    adapter = self.transport_for(job["platform"], job["author_id"])
                    result = self.workflow.run_all({job["platform"]: adapter}, mode=job["mode"], batch_id=batch, login_confirmed=True)
                    run = result["runs"][0]
                    if run["state"] == "rate_limited":
                        self._record_cooldown(job["platform"], retry_at=run["retry_at"])
                run = result["runs"][0]
                self._finish(job_id, run["state"], run["reason"])
            except PlatformCooldown:
                self._finish(job_id, "rate_limited", "rate_limited")
            except PlatformAccessPaused as error:
                if job.get("run_id"):
                    with self.workflow.connect() as db:
                        current = db.execute("SELECT state FROM runs WHERE id=?", (job["run_id"],)).fetchone()
                    if current and current[0] in {"queued", "running"}:
                        self.workflow._stop(job["run_id"], "interrupted", "network_paused")
                self._finish(job_id, "blocked", error.reason)
            except AdapterFailure as error:
                self._record_request_failure(job, error, "job_preparation")
                if error.category == "rate_limited":
                    self._record_cooldown(job["platform"], retry_after=error.retry_after)
                if error.category in {"needs_login", "verification_required", "rate_limited"}:
                    self._job_sources.clear()
                    self._record_platform_access_pause(job["platform"], job["author_id"], error.category)
                # Preparation can fail before any item is attempted. Preserve
                # the typed cause instead of claiming the transport is absent.
                category = error.category
                state = category if category in {"needs_login", "rate_limited"} else "partial" if category == "reference_missing" and job["mode"] == "recent_window" else "blocked"
                self._finish(job_id, state, category)
            except Exception:
                self._finish(job_id, "failed", "unexpected_error")

    def _execute_xhs_recent_window(self, job, run_id, batch_id):
        """Archive new IDs from one first-page snapshot; never follow its cursor."""
        with self._network_lock:
            transport = self.transport_for(job["platform"], job["author_id"])
            with self.workflow.connect() as db:
                run = dict(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
                saved = db.execute("SELECT * FROM pages WHERE run_id=? ORDER BY page_number LIMIT 1", (run_id,)).fetchone()
            if run["adapter_version"] not in {"pending", transport.version}:
                self._finish(job["id"], "partial", "adapter_version_changed")
                return
            if not saved:
                if self._pause_requested(job["id"]):
                    self._finish(job["id"], "interrupted", "user_paused")
                    return
                self._check_cooldown(job["platform"])
                scope = network_safety.manual_scope()
                if scope is not None:
                    if scope.page is None:
                        raise AdapterFailure("network_paused")
                    page = scope.page
                elif hasattr(transport, "recent_page"):
                    page = self._source_call(job, transport.recent_page, job["author_id"])
                else:
                    page = self._source_call(job, transport.page, job["author_id"], None)
                if not page.items or len(page.items) > XHS_RECENT_WINDOW_SIZE:
                    raise AdapterFailure("invalid_page")
                ids = [item.item_id for item in page.items]
                if len(set(ids)) != len(ids):
                    raise AdapterFailure("invalid_stable_id")
                for item in page.items:
                    if item.author_id != job["author_id"]:
                        raise AdapterFailure("identity_mismatch")
                    if not isinstance(item.item_id, str) or not re.fullmatch(r"[0-9a-f]{24}", item.item_id):
                        raise AdapterFailure("invalid_stable_id")
                with self.workflow.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    for item in page.items:
                        previous = db.execute("SELECT author_id,title,published_at FROM items WHERE platform=? AND item_id=?",
                                              (job["platform"], item.item_id)).fetchone()
                        if previous and previous["author_id"] != job["author_id"]:
                            raise AdapterFailure("identity_mismatch")
                        title = getattr(item, "title", "") or ""
                        if not isinstance(title, str) or not isinstance(item.published_at, str):
                            raise AdapterFailure("invalid_response")
                        difference = {}
                        if previous:
                            for field, observed in (("title", title), ("published_at", item.published_at)):
                                if observed and observed != (previous[field] or ""):
                                    difference[field] = {"saved": previous[field], "listed": observed}
                        else:
                            db.execute("INSERT INTO items(platform,item_id,author_id,published_at,title) VALUES(?,?,?,?,?)",
                                       (job["platform"], item.item_id, job["author_id"], item.published_at, title))
                            db.execute("INSERT INTO job_items(job_id,platform,item_id) VALUES(?,?,?)",
                                       (job["id"], job["platform"], item.item_id))
                        db.execute("INSERT INTO recent_window_observations VALUES(?,?,?,?)",
                                   (job["id"], item.item_id, "existing_preserved" if previous else "new",
                                    json.dumps(difference, ensure_ascii=False) if difference else None))
                    # The saved row is a window snapshot, not evidence of a history end.
                    db.execute("INSERT INTO pages VALUES(?,1,'',NULL,?,NULL,?)", (run_id, json.dumps(ids), time.time()))
                    db.execute("UPDATE runs SET adapter_version=?,pages=1,cursor=NULL,state='running',coverage='recent_window_only',terminal_evidence=NULL,updated_at=? WHERE id=?",
                               (transport.version, time.time(), run_id))
                    saved = dict(db.execute("SELECT * FROM pages WHERE run_id=?", (run_id,)).fetchone())
                    if network_safety.manual_scope() is None:
                        self._save_xhs_page_references(job, run_id, transport, saved)
            else:
                # A retry uses private references from this fixed first-page snapshot.
                # Missing or expired references remain a gap; no old page is reread.
                self._load_xhs_page_references(job, run_id, transport, dict(saved))
        with self.workflow.connect() as db:
            target_count = db.execute("SELECT count(*) FROM job_items WHERE job_id=?", (job["id"],)).fetchone()[0]
        if target_count:
            scope = network_safety.manual_scope()
            if scope is not None:
                # Fetch at most the original three details before any media. This
                # exposes the full required media count without spending >12.
                with self.workflow.connect() as db:
                    targets = {r[0] for r in db.execute("SELECT item_id FROM job_items WHERE job_id=?", (job["id"],))}
                for item in scope.page.items:
                    if item.item_id not in targets:
                        continue
                    detail = self._source_call(job, transport.detail, job["author_id"], item.item_id)
                    if detail.get("author_id") != job["author_id"] or detail.get("item_id") != item.item_id:
                        raise AdapterFailure("identity_mismatch")
                    scope.details[item.item_id] = detail
                    if detail.get("text", "").strip():
                        self.workflow.save_detail(job["platform"], item.item_id, job["author_id"], detail["text"], detail["source_url"])
                    scope.media_required += sum(not self.workflow.candidate_saved(job["platform"], item.item_id, c) for c in detail.get("media", []))
                scope.persist(scope)
                if scope.media_required > 12:
                    scope.stop("manual_media_budget_exceeded")
                    self._finish(job["id"], "blocked", "manual_media_budget_exceeded")
                    return
            pending = self._execute_content(job, finalize=False)
            with self.workflow.connect() as db:
                current = db.execute("SELECT state FROM jobs WHERE id=?", (job["id"],)).fetchone()[0]
            if current != "running":
                return
        else:
            pending = 0
        exported = self.workflow.export_all(batch_id=batch_id,
            recent_window_authors={(job["platform"], job["author_id"])})
        for author in exported["authors"]:
            for key in ("manifest", "corpus", "index", "failures"):
                author[key + "_url"] = "/archive/" + quote(Path(author[key]).relative_to(self.workflow.archive_root).as_posix(), safe="/")
        self._finish(job["id"], "partial" if pending else "succeeded",
                     "recent_window_partial" if pending else "recent_window_complete" if target_count else "recent_window_unchanged", exported)

    def _execute_xhs_history(self, job, run_id, batch_id):
        """Scan the full page chain while recording individual content gaps."""
        with self._network_lock:
            transport = self.transport_for(job["platform"], job["author_id"])
        incompatible = False
        with self.workflow.connect() as db:
            run = db.execute("SELECT adapter_version FROM runs WHERE id=?", (run_id,)).fetchone()
            if run["adapter_version"] == "pending":
                db.execute("UPDATE runs SET adapter_version=? WHERE id=?", (transport.version, run_id))
            elif run["adapter_version"] != transport.version:
                incompatible = True
            else:
                incompatible = False
        if incompatible:
            self.workflow._stop(run_id, "partial", "adapter_version_changed")
            self._finish(job["id"], "partial", "adapter_version_changed")
            return
        last_staged_page = 0
        attempted_ids = set()
        pages_requested = 0
        while True:
            if self._pause_requested(job["id"]) or self._stopping.is_set():
                self._finish(job["id"], "interrupted", "user_paused" if self._pause_requested(job["id"]) else "process_interrupted")
                return
            # A crash can happen between committing the page and registering
            # its content targets. Rebuild the fixed scope from saved pages.
            with self.workflow.connect() as db:
                pages = db.execute("SELECT page_number,item_ids FROM pages WHERE run_id=? AND page_number>? ORDER BY page_number", (run_id, last_staged_page)).fetchall()
                for page in pages:
                    for item_id in json.loads(page["item_ids"]):
                        row = db.execute("SELECT detail_state,media_state FROM items WHERE platform=? AND item_id=? AND author_id=?",
                                         (job["platform"], item_id, job["author_id"])).fetchone()
                        if row is None:
                            raise AdapterFailure("identity_mismatch")
                        db.execute("INSERT OR IGNORE INTO job_items(job_id,platform,item_id) VALUES(?,?,?)",
                                   (job["id"], job["platform"], item_id))
                        if row["detail_state"] != "complete" or row["media_state"] != "complete_for_observed_detail":
                            db.execute("UPDATE job_items SET state='partial' WHERE job_id=? AND platform=? AND item_id=? AND state='succeeded'",
                                       (job["id"], job["platform"], item_id))
                    last_staged_page = page["page_number"]
                pending_ids = [row[0] for row in db.execute(
                    "SELECT item_id FROM job_items WHERE job_id=? AND state!='succeeded'", (job["id"],))]
                run = dict(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            targets = set(pending_ids) - attempted_ids
            deferred = set()
            if targets:
                try:
                    replayed, deferred = self._restore_xhs_page_references(
                        job, run_id, transport, targets,
                        max_pages=XHS_HISTORY_PAGE_BUDGET - pages_requested)
                    pages_requested += replayed
                except AdapterFailure as error:
                    self._record_request_failure(job, error, "restore_page_references")
                    state = error.category if error.category in {"needs_login", "rate_limited"} else "partial"
                    retry_at = time.time() + max(error.retry_after, 5) if state == "rate_limited" else 0
                    if run["state"] != "succeeded":
                        self.workflow._stop(run_id, state, error.category, retry_at)
                    if state == "rate_limited":
                        self._record_cooldown(job["platform"], retry_at=retry_at)
                    self._record_platform_access_pause(job["platform"], job["author_id"], error.category)
                    self._finish(job["id"], state, error.category)
                    return
                ready_targets = targets - deferred
                if ready_targets:
                    self._execute_content(job, finalize=False, only_item_ids=ready_targets)
                    attempted_ids.update(ready_targets)
                with self.workflow.connect() as db:
                    state = db.execute("SELECT state FROM jobs WHERE id=?", (job["id"],)).fetchone()[0]
                if state != "running":
                    if run["state"] != "succeeded":
                        with self.workflow.connect() as db:
                            reason = db.execute("SELECT reason FROM jobs WHERE id=?", (job["id"],)).fetchone()[0]
                        self.workflow._stop(run_id, state, reason or "content_partial")
                    return
            if run["state"] == "succeeded":
                with self.workflow.connect() as db:
                    pending = db.execute("SELECT count(*) FROM job_items WHERE job_id=? AND state!='succeeded'", (job["id"],)).fetchone()[0]
                exported = self.workflow.export_all(batch_id=batch_id)
                for author in exported["authors"]:
                    for key in ("manifest", "corpus", "index", "failures"):
                        author[key + "_url"] = "/archive/" + quote(Path(author[key]).relative_to(self.workflow.archive_root).as_posix(), safe="/")
                reason = ("content_retry_budget_reached" if deferred
                          else "author_archive_partial" if pending else "author_archive_complete")
                self._finish(job["id"], "partial" if pending else "succeeded", reason, exported)
                return
            if pages_requested >= XHS_HISTORY_PAGE_BUDGET:
                self.workflow._stop(run_id, "partial", "page_budget_reached")
                self._finish(job["id"], "partial", "page_budget_reached")
                return
            with self._network_lock:
                self._check_cooldown(job["platform"])
                try:
                    pages_requested += 1
                    page = self._source_call(job, transport.page, job["author_id"], run["cursor"])
                    # Persist private detail references before the checkpoint
                    # transaction commits. A failed write rolls back the page;
                    # an orphan file cannot be used without a matching DB page.
                    with self.workflow.connect() as db:
                        db.execute("BEGIN IMMEDIATE")
                        self.workflow._commit_page_in_db(db, run_id, run["cursor"], page)
                        saved = dict(db.execute("SELECT * FROM pages WHERE run_id=? ORDER BY page_number DESC LIMIT 1", (run_id,)).fetchone())
                        self._save_xhs_page_references(job, run_id, transport, saved)
                except AdapterFailure as error:
                    self._record_request_failure(job, error, "list")
                    state = error.category if error.category in {"needs_login", "rate_limited"} else "partial"
                    retry_at = time.time() + max(error.retry_after, 5) if state == "rate_limited" else 0
                    self.workflow._stop(run_id, state, error.category, retry_at)
                    if state == "rate_limited":
                        self._record_cooldown(job["platform"], retry_at=retry_at)
                    self._record_platform_access_pause(job["platform"], job["author_id"], error.category)
                    self._finish(job["id"], state, error.category)
                    return
                except ValueError as error:
                    reason = str(error)
                    if reason not in {"stale_checkpoint", "invalid_page", "missing_cursor", "repeated_cursor",
                                      "empty_nonterminal_page", "missing_terminal_evidence", "identity_mismatch", "invalid_stable_id"}:
                        reason = "invalid_response"
                    self.workflow._stop(run_id, "partial", reason)
                    self._finish(job["id"], "partial", reason)
                    return

    def _xhs_reference_record(self, job, run_id, saved):
        scope = {"run_id": run_id, "author_id": job["author_id"],
                 "page_number": saved["page_number"], "item_ids": json.loads(saved["item_ids"]),
                 "request_cursor_sha256": sha256((saved["request_cursor"] or "").encode()).hexdigest(),
                 "next_cursor_sha256": sha256(saved["next_cursor"].encode()).hexdigest() if saved["next_cursor"] is not None else None,
                 "terminal_evidence": saved["terminal_evidence"]}
        path = self.root / "private" / "xhs-page-references" / str(run_id) / (scope["request_cursor_sha256"] + ".json")
        return path, scope

    def _save_xhs_page_references(self, job, run_id, transport, saved):
        if not hasattr(transport, "export_page_references"):
            return
        path, scope = self._xhs_reference_record(job, run_id, saved)
        references = transport.export_page_references(job["author_id"], scope["item_ids"])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + f".{time.time_ns()}.part")
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as output:
                json.dump({"scope": scope, "references": references}, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_xhs_page_references(self, job, run_id, transport, saved):
        if not hasattr(transport, "restore_page_references"):
            return
        path, scope = self._xhs_reference_record(job, run_id, saved)
        if not path.exists():
            return  # Legacy checkpoints have no saved tokens; do not invent them.
        try:
            if path.stat().st_size > 1024 * 1024:
                raise ValueError()
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or record.get("scope") != scope:
                raise ValueError()
            references = record["references"]
            if not isinstance(references, dict) or set(references) - set(scope["item_ids"]):
                raise ValueError()
        except (OSError, UnicodeError, ValueError, KeyError):
            raise AdapterFailure("reference_missing") from None
        transport.restore_page_references(job["author_id"], references)

    def _restore_xhs_page_references(self, job, run_id, transport, target_ids=None, *, max_pages=XHS_HISTORY_PAGE_BUDGET):
        """Reuse durable references; re-read a legacy page only if tokens are absent."""
        with self.workflow.connect() as db:
            pending_ids = [row[0] for row in db.execute(
                "SELECT item_id FROM job_items WHERE job_id=? AND state!='succeeded'", (job["id"],))
                if target_ids is None or row[0] in target_ids]
            pages = [dict(row) for row in db.execute(
                "SELECT page_number,request_cursor,next_cursor,item_ids,terminal_evidence FROM pages WHERE run_id=? ORDER BY page_number", (run_id,))]
        with self._network_lock:
            for saved in pages:
                if set(pending_ids).intersection(json.loads(saved["item_ids"])):
                    self._load_xhs_page_references(job, run_id, transport, saved)
            missing = set(transport.prepare_page_details(job["author_id"], pending_ids))
            if not missing:
                return 0, set()
            pages_requested = 0
            deferred = set()
            for saved in pages:
                expected = json.loads(saved["item_ids"])
                if not missing.intersection(expected):
                    continue
                if pages_requested >= max_pages:
                    deferred.update(missing.intersection(expected))
                    continue
                pages_requested += 1
                observed = self._source_call(job, transport.page, job["author_id"], saved["request_cursor"] or None)
                actual = [item.item_id for item in observed.items]
                if any(item.author_id != job["author_id"] for item in observed.items):
                    raise AdapterFailure("identity_mismatch")
                if (actual != expected or observed.next_cursor != saved["next_cursor"]
                        or observed.has_more != (saved["next_cursor"] is not None)
                        or observed.terminal_evidence != saved["terminal_evidence"]):
                    raise AdapterFailure("stale_checkpoint")
                self._save_xhs_page_references(job, run_id, transport, saved)
                missing = set(transport.prepare_page_details(job["author_id"], tuple(missing)))
            # A missing token for one saved item is an item-level content gap.
            # detail() records that failure once; it must not truncate the
            # otherwise valid history page chain.
            return pages_requested, deferred

    def _execute_source_refresh(self, job):
        """Poll only a configured author feed. Absence of pagination is not history completion."""
        with self.workflow.connect() as db:
            existing = db.execute("SELECT count(*) FROM job_items WHERE job_id=?", (job["id"],)).fetchone()[0]
        if not existing:
            if self._pause_requested(job["id"]):
                self._finish(job["id"], "interrupted", "user_paused")
                return
            with self._network_lock:
                adapter = self.transport_for(job["platform"], job["author_id"])
                entries = self._source_call(job, adapter.poll_latest)
            if not entries:
                self._finish(job["id"], "partial", "invalid_response")
                return
            seen = set()
            for entry in entries:
                item_id = entry.get("item_id")
                if (entry.get("author_id") != job["author_id"] or not isinstance(item_id, str)
                        or item_id in seen):
                    raise AdapterFailure("identity_mismatch")
                _id(item_id)
                seen.add(item_id)
            with self.workflow.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                for entry in entries:
                    item_id = entry["item_id"]
                    previous = db.execute("SELECT author_id,detail_text,detail_state,media_state,title FROM items WHERE platform=? AND item_id=?",
                                          (job["platform"], item_id)).fetchone()
                    if previous and previous[0] != job["author_id"]:
                        raise AdapterFailure("identity_mismatch")
                    db.execute("""INSERT OR IGNORE INTO items(platform,item_id,author_id,published_at,source_url,title,content_type)
                        VALUES(?,?,?,?,?,?,?)""",
                        (job["platform"], item_id, job["author_id"], entry.get("published_at") or "",
                         entry.get("source_url") or "", entry.get("title") or "",
                         entry.get("content_type") or "unknown"))
                    # For the native source, revisit the bounded latest window
                    # so edited bodies and pinned notes are checked as well.
                    native = self._source_kind(job["platform"], job["author_id"]) == "xhs_http"
                    if (native or not previous or previous["detail_state"] != "complete"
                            or previous["media_state"] != "complete_for_observed_detail"
                            or entry.get("text") is not None and previous["detail_text"] != entry["text"]
                            or previous["title"] != (entry.get("title") or previous["title"])):
                        db.execute("INSERT OR IGNORE INTO job_items(job_id,platform,item_id) VALUES(?,?,?)",
                                   (job["id"], job["platform"], item_id))
        with self.workflow.connect() as db:
            pending = db.execute("SELECT count(*) FROM job_items WHERE job_id=?", (job["id"],)).fetchone()[0]
        if not pending:
            self._finish(job["id"], "succeeded", "source_refresh_unchanged")
            return
        self._execute_content(job)

    def _execute_content(self, job, *, page_scoped=False, parent_id=None, finalize=True, only_item_ids=None):
        """Run a fixed local item snapshot, independent of history pagination."""
        if job["platform"] != "xiaohongshu" and not self.source_config(job["platform"], job["author_id"]):
            self._finish(job["id"], "blocked", "wechat_blocked")
            return
        with self.workflow.connect() as db:
            targets = [dict(r) for r in db.execute("SELECT i.* FROM job_items j JOIN items i USING(platform,item_id) WHERE j.job_id=? AND j.state!='succeeded' ORDER BY i.item_id", (job["id"],))]
            scope_count = db.execute("SELECT count(*) FROM job_items WHERE job_id=?", (job["id"],)).fetchone()[0]
        if only_item_ids is not None:
            targets = [item for item in targets if item["item_id"] in only_item_ids]
        if not scope_count:
            self._finish(job["id"],"blocked","no_local_items")
            return
        sources = self._job_sources.get(job["id"], {})
        # The scope lives in job_items; ephemeral links can be renewed without
        # changing membership, replaying successful items or advancing pages.
        with self._network_lock:
            self._check_cooldown(job["platform"])
            transport = self.transport_for(job["platform"], job["author_id"])
            if page_scoped:
                transport.prepare_page_details(job["author_id"], [i["item_id"] for i in targets])
            elif job["mode"] not in {"full", "recent_window"} and hasattr(transport, "prepare_details") and not isinstance(sources,str):
                self._source_call(job, transport.prepare_details, job["author_id"], [i["item_id"] for i in targets if i["item_id"] not in sources])
        for item in targets:
            if self._pause_requested(job["id"]) or (parent_id is not None and self._pause_requested(parent_id)):
                self._finish(job["id"], "interrupted", "user_paused")
                return
            if self._stopping.is_set():
                self._finish(job["id"],"interrupted","process_interrupted")
                return
            observed = False
            try:
                with self._network_lock:
                    self._check_cooldown(job["platform"])
                    transport = self.transport_for(job["platform"], job["author_id"])
                    source = sources if isinstance(sources,str) else sources.get(item["item_id"],item["source_url"] or "")
                    scope = network_safety.manual_scope()
                    detail = (scope.details[item["item_id"]] if scope is not None and item["item_id"] in scope.details
                              else self._source_call(job, transport.detail, job["author_id"], item["item_id"], source_url=source))
                    if detail.get("item_id") != item["item_id"] or detail.get("author_id") != job["author_id"]:
                        raise AdapterFailure("unavailable")
                    self.workflow.save_metrics(job["platform"], item["item_id"], detail.get("metrics",{}),
                        source=detail["source"], collected_at=detail["observed_at"],
                        observation_key=f"job:{job['id']}:{item['item_id']}:{detail['source']}:{detail['observed_at']}")
                    observed = True
                    with self.workflow.connect() as db:
                        db.execute("UPDATE items SET title=coalesce(nullif(?,''),title),content_type=CASE WHEN ? IN ('image','video') THEN ? ELSE content_type END,published_at=CASE WHEN ?!='' THEN ? ELSE published_at END WHERE platform=? AND item_id=?",
                                   (detail.get("title"),detail.get("content_type"),detail.get("content_type"),detail.get("published_at") or "",detail.get("published_at") or "",job["platform"],item["item_id"]))
                    complete = True
                    incomplete_reason = "content_partial"
                    if job["mode"] in {"content", "source_refresh", "full", "recent_window"}:
                        content_changed = (job["mode"] == "source_refresh" and
                                           (item["detail_text"] != detail.get("text") or
                                            item["title"] != (detail.get("title") or item["title"])))
                        if detail.get("text","").strip() and not (job["mode"] != "source_refresh" and bool((item["detail_text"] or "").strip())):
                            self.workflow.save_detail(job["platform"],item["item_id"],job["author_id"],detail["text"],detail["source_url"])
                        else:
                            complete = item["detail_state"] == "complete"
                        media_failed = False
                        for candidate in detail.get("media",[]):
                            if self._pause_requested(job["id"]) or (parent_id is not None and self._pause_requested(parent_id)):
                                self._finish(job["id"], "interrupted", "user_paused")
                                return
                            if self._stopping.is_set():
                                self._finish(job["id"],"interrupted","process_interrupted")
                                return
                            if not content_changed and self.workflow.candidate_saved(job["platform"],item["item_id"],candidate):
                                continue
                            try:
                                fetched = self._source_call(job, transport.download_media, candidate, self.root / "downloads" / job["platform"] / _id(item["item_id"]))
                                self.workflow.attach_media(job["platform"],item["item_id"],candidate.asset_id,Path(fetched["path"]),position=candidate.position,kind=candidate.kind,mime=fetched["mime"])
                            except AdapterFailure as error:
                                self._record_request_failure(job, error, "media")
                                if network_safety.manual_scope() is not None or error.category in {"needs_login","rate_limited","unavailable","verification_required","unknown_business_error","network_paused"}:
                                    raise
                                media_failed = True
                                if error.category == "media_failed":
                                    incomplete_reason = "media_failed"
                            except OSError as error:
                                if network_safety.manual_scope() is not None:
                                    raise AdapterFailure("media_failed") from None
                                media_failed = True
                                if error.errno in LOCAL_MEDIA_ERRNOS:
                                    incomplete_reason = "media_write_failed"
                            except ValueError:
                                if network_safety.manual_scope() is not None:
                                    raise AdapterFailure("media_failed") from None
                                media_failed = True
                        media_complete = bool(detail.get("media")) and not detail.get("missing") and not media_failed
                        complete = complete and media_complete
                        with self.workflow.connect() as db:
                            db.execute("UPDATE items SET media_state=? WHERE platform=? AND item_id=?", ("complete_for_observed_detail" if media_complete else "partial",job["platform"],item["item_id"]))
                with self.workflow.connect() as db:
                    db.execute("UPDATE job_items SET state=?,reason=? WHERE job_id=? AND platform=? AND item_id=?", ("succeeded" if complete else "partial",None if complete else incomplete_reason,job["id"],job["platform"],item["item_id"]))
                if complete and isinstance(sources,dict):
                    sources.pop(item["item_id"],None)
                if not complete and network_safety.manual_scope() is not None:
                    self._finish(job["id"], "partial", incomplete_reason)
                    return
            except PlatformCooldown:
                raise
            except Exception as error:
                category = error.category if isinstance(error,AdapterFailure) else "unexpected_error"
                if isinstance(error, AdapterFailure):
                    self._record_request_failure(job, error, "detail")
                if not observed:
                    attempt_source = ("xhs_http_detail_attempt" if self._source_kind(job["platform"], job["author_id"]) == "xhs_http"
                                      else "xhs_browser_detail_attempt")
                    self.workflow.save_metrics(job["platform"],item["item_id"],{},source=attempt_source,collected_at=time.time(),
                        observation_key=f"job:{job['id']}:{item['item_id']}:failed:{time.time_ns()}",status="failed",reason=category)
                with self.workflow.connect() as db:
                    db.execute("UPDATE job_items SET state='partial',reason=? WHERE job_id=? AND platform=? AND item_id=?", (category,job["id"],job["platform"],item["item_id"]))
                if (network_safety.manual_scope() is not None
                        or category in {"rate_limited","needs_login","verification_required","unavailable","unknown_business_error","network_paused"}
                        or job["mode"] == "recent_window" and category in {"identity_mismatch", "invalid_response", "timeout", "invalid_stable_id"}):
                    if category in {"needs_login","verification_required"}:
                        self._job_sources.clear()
                    if category == "rate_limited":
                        self._record_cooldown(job["platform"],retry_after=error.retry_after)
                    self._record_platform_access_pause(job["platform"], job["author_id"], category)
                    self._finish(job["id"],category if category in {"rate_limited","needs_login"} else "blocked",category)
                    return
        with self.workflow.connect() as db:
            pending = db.execute("SELECT count(*) FROM job_items WHERE job_id=? AND state!='succeeded'", (job["id"],)).fetchone()[0]
        if finalize:
            self._finish(job["id"], "partial" if pending else "succeeded", job["mode"] + ("_partial" if pending else "_complete"))
        return pending

    def import_validation(self) -> dict:
        """Whitelist import from fixed local sources; never writes source databases.

        No HAR, raw responses, profile, cookies or unrelated user files are copied.
        Only explicitly recorded attachments are copied after identity/hash checks.
        Imported list records preserve evidence and are separate from online jobs.
        """
        imported, skipped = [], []
        with self._lock:
            from .validation_assets import import_verified_assets, validation_display_names
            names = validation_display_names(self.validation_root)
            for source_name in ("xhs-live-validation", "xhs-live-validation-b"):
                source_path = (self.validation_root / source_name / "archive.sqlite3").resolve()
                if not source_path.is_file():
                    skipped.append(source_name)
                    continue
                if not source_path.is_relative_to(self.validation_root.resolve()) or source_path == self.workflow.db_path.resolve():
                    raise ValueError("验证来源路径不安全")
                with self.workflow.connect() as db:
                    if db.execute("SELECT 1 FROM validation_imports WHERE source=?", (source_name,)).fetchone():
                        skipped.append(source_name)
                        continue
                source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
                source.row_factory = sqlite3.Row
                try:
                    with source:
                        subs = list(source.execute("SELECT * FROM subscriptions WHERE platform='xiaohongshu'"))
                        with self.workflow.connect() as db:
                            count = 0
                            for sub in subs:
                                _id(sub["author_id"])
                                db.execute("INSERT OR IGNORE INTO subscriptions VALUES(?,?,?,?,?,?)", ("xiaohongshu", sub["author_id"], names.get(sub["author_id"], sub["display_name"]), "prior_live_validation", sub["enabled"], time.time()))
                                for item in source.execute("SELECT item_id,author_id,published_at FROM items WHERE platform='xiaohongshu' AND author_id=?", (sub["author_id"],)):
                                    _id(item["item_id"])
                                    old = db.execute("SELECT author_id FROM items WHERE platform='xiaohongshu' AND item_id=?", (item["item_id"],)).fetchone()
                                    if old and old[0] != item["author_id"]:
                                        raise ValueError("导入作品作者身份冲突，原资料已保留")
                                    count += db.execute("INSERT OR IGNORE INTO items(platform,item_id,author_id,published_at) VALUES('xiaohongshu',?,?,?)", tuple(item)).rowcount
                                previous = source.execute("SELECT * FROM runs WHERE platform='xiaohongshu' AND author_id=? ORDER BY CASE WHEN coverage='complete_for_accessible_scope' THEN 0 ELSE 1 END,id DESC LIMIT 1", (sub["author_id"],)).fetchone()
                                batch = db.execute("INSERT INTO batches(mode,created_at) VALUES('full',?)", (time.time(),)).lastrowid
                                coverage = previous["coverage"] if previous else "unknown"
                                terminal = previous["terminal_evidence"] if previous else None
                                if coverage == "complete_for_accessible_scope" and not terminal:
                                    coverage = "unknown"
                                run_id = db.execute("INSERT INTO runs(batch_id,platform,author_id,mode,adapter_version,cursor,pages,state,coverage,terminal_evidence,updated_at) VALUES(?,'xiaohongshu',?,'full','imported_observation',?,?,'succeeded',?,?,?)", (batch, sub["author_id"], previous["cursor"] if previous else None, previous["pages"] if previous else 0, coverage, terminal, time.time())).lastrowid
                                if previous:
                                    for page in source.execute("SELECT page_number,request_cursor,next_cursor,item_ids,terminal_evidence,observed_at FROM pages WHERE run_id=?", (previous["id"],)):
                                        db.execute("INSERT INTO pages VALUES(?,?,?,?,?,?,?)", (run_id, *tuple(page)))
                                db.execute("INSERT INTO jobs(platform,author_id,mode,state,run_id,reason,created_at,updated_at) VALUES('xiaohongshu',?,'full','succeeded',?,'validation_import',?,?)", (sub["author_id"], run_id, time.time(), time.time()))
                            db.execute("INSERT INTO validation_imports VALUES(?,?)", (source_name, time.time()))
                            imported.append({"source": source_name, "authors": len(subs), "items": count})
                finally:
                    source.close()
            assets = import_verified_assets(self.workflow, self.validation_root)
        return {"imported": imported, "skipped": skipped, "assets": assets, "evidence_level": "prior_live_observation", "message": "原验证资料未修改；已导入作者、作品ID、页链及通过哈希核验的已有附件。缺失正文和其余媒体明确保留为待补齐。", "g1_passed": False}
