"""Local product workspace: durable jobs and explicit evidence boundaries.

The existing archive workflow owns pages, checkpoints and exports. This service
adds a thin queue and never treats imported observations as a live transport.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
import os
from pathlib import Path
import sqlite3
from threading import Event, Lock, RLock, Thread
import time
from urllib.parse import quote, urlsplit

from .links import classify
from .workflow import ArchiveWorkflow, _id, _canonical_source_url
from .validation import AdapterFailure
from .metrics import FIELDS, read_metrics, read_snapshots


def default_workspace() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share"))) / "CreatorArchive/workspace"


MESSAGES = {
    "transport_unavailable": ("小红书自运行采集通道尚未配置；已保留作品及分页检查点。", "配置并登录独立采集浏览器后重试；现有作品可直接归档。"),
    "wechat_blocked": ("公众号历史来源未验证，当前无后台权限且站点访问受限；未发起网络采集。", "待提供经授权且可验证的全历史来源；已有资料仍可浏览和归档。"),
    "identity_unverified": ("已保存订阅意图，作者身份尚未经过平台核验。", "等待可用的平台核验通道；不要把链接中的候选ID当作已核验身份。"),
    "process_interrupted": ("上次进程退出，成功页面和作品已保留。", "点击恢复，从有效检查点继续；归档可安全重试。"),
    "needs_login": ("采集登录已失效或需要验证；已保存进度。", "在独立采集浏览器完成登录后点击恢复。"),
    "rate_limited": ("平台要求冷却；已保存进度。", "等待冷却时间结束后点击恢复，不切换账号或IP。"),
    "adapter_unavailable": ("当前平台没有可用采集适配器；已有资料保留。", "待平台接入后恢复；也可先归档已有资料。"),
    "page_budget_reached": ("本轮达到安全页数预算，尚未观察到末页；检查点已保存。", "点击恢复继续下一批页面。"),
    "unexpected_error": ("任务出现未分类错误，原因尚未确定；成功页面和已有文件保留。", "重试一次；若仍失败，请保留工作目录供排查，无需删库。"),
    "archive_complete": ("现有资料已按作者导出；缺失正文与媒体在清单中明确标记。", "打开归档清单查看结果；列表完整不代表正文或媒体完整。"),
    "validation_import": ("已导入先前真实验证的历史列表；本次没有发起在线采集。", "可浏览和归档已有作品；正文及媒体缺失仍需后续补齐。"),
    "timeout": ("作者列表等待超时，原因尚未确认；已保存成功页面。", "查看独立浏览器的登录或验证提示，处理后恢复。"),
    "unavailable": ("平台页面暂不可用，具体原因未知；已保存进度和旧指标。", "查看独立浏览器提示；作品任务可在详情粘贴该作品的完整原文链接后重试。重启后需重新粘贴，成功媒体和检查点保留。"),
    "invalid_cursor": ("当前页面未能衔接保存的游标；没有跳过未知页面。", "重新登录后恢复；若持续发生，请保留当前资料排查页链变化。"),
    "content_partial": ("部分作品正文或媒体尚未保存，成功资源与指标已保留。", "查看作品详情的缺失状态，处理浏览器提示后点击恢复；成功媒体不会重复下载。"),
    "metrics_partial": ("部分作品指标未能更新；已有有效数值与原采集时间保留。", "查看独立浏览器提示后恢复；未知字段可能未由平台提供。"),
    "content_complete": ("本批作品正文和当前可获取媒体已保存。", "可查看离线附件或按作者导出；不代表所有历史作品均已获取。"),
    "metrics_complete": ("本批指标观察已保存，缺失字段仍标未知；历史快照保留。", "可按全库指标排序筛选，或查看作品的观察历史。"),
    "no_local_items": ("当前作者尚无本地作品，本批内容或指标范围为空。", "先获取作者历史列表，再新建内容或指标任务；当前任务不会把空范围当作完成。"),
    "reference_missing": ("本轮未取得部分历史作品的有效访问引用；旧资料和成功项保留。", "在任务的补充链接入口粘贴同篇完整官方链接后继续；不会重扫39/6页长链。"),
    "item_unavailable": ("该作品当前不可访问，可能是链接失效、删除或权限变化；旧资料保留。", "在官方页面确认可访问后，补充完整链接重试。其他作品可继续处理。"),
    "media_failed": ("部分媒体下载或校验失败，成功媒体和正文保留。", "恢复任务会刷新详情并只下载缺失资源。"),
}


class PlatformCooldown(ValueError):
    def __init__(self, retry_at):
        self.retry_at = retry_at
        super().__init__("平台冷却尚未结束，已保存全部进度。请等待冷却结束后重试；已有资料仍可离线归档。")


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
        self._threads: set[Thread] = set()
        self._stopping = Event()
        self._job_sources = {} # Temporary navigation parameters never enter SQLite or exports.
        with self.workflow.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS subscription_intents (
                    platform TEXT NOT NULL, author_id TEXT NOT NULL, display_name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
                    PRIMARY KEY(platform,author_id));
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY, platform TEXT NOT NULL, author_id TEXT NOT NULL,
                    mode TEXT NOT NULL, state TEXT NOT NULL, run_id INTEGER,
                    reason TEXT, export_json TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS validation_imports (
                    source TEXT PRIMARY KEY, imported_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS platform_cooldowns (
                    platform TEXT PRIMARY KEY, retry_at REAL NOT NULL);
            """)
            db.execute("UPDATE jobs SET state='interrupted',reason='process_interrupted',updated_at=? WHERE state IN ('running','queued')", (time.time(),))
            db.execute("UPDATE runs SET state='interrupted',reason='process_interrupted',updated_at=? WHERE state IN ('running','queued')", (time.time(),))

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

    def transport(self):
        with self._transport_lock:
            if self._transport is None:
                if self.adapter_factory:
                    self._transport = self.adapter_factory(self.root)
                else:
                    from .adapters.xhs_transport import XhsBrowserTransport
                    self._transport = XhsBrowserTransport(self.root.parent / "browser-profile-xhs")
            return self._transport

    def open_login(self):
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
        if platform != "xiaohongshu":
            raise ValueError(MESSAGES["wechat_blocked"][0])
        with self.workflow.connect() as db:
            intent = db.execute("SELECT * FROM subscription_intents WHERE platform=? AND author_id=?", (platform, author_id)).fetchone()
            existing = db.execute("SELECT * FROM subscriptions WHERE platform=? AND author_id=?", (platform, author_id)).fetchone()
        if not intent and not existing:
            raise KeyError("subscription_not_found")
        with self._network_lock:
            self._check_cooldown(platform)
            try:
                observed = self.transport().verify_author(author_id)
            except AdapterFailure as error:
                if error.category == "rate_limited":
                    self._record_cooldown(platform, retry_after=error.retry_after)
                raise
        if observed.get("author_id") != author_id:
            raise ValueError("平台返回的作者身份与候选不符；原订阅保留")
        saved_name = (existing or intent)["display_name"]
        observed_name = observed.get("display_name")
        name = observed_name if saved_name.startswith("待核验作者 · ") and observed_name and observed_name != author_id else saved_name
        self.workflow.subscribe(platform, author_id, name, verified=True, evidence="observed_browser_author_listing")
        if intent:
            self.workflow.set_enabled(platform, author_id, bool(intent["enabled"]))
        return {"platform": platform, "author_id": author_id, "identity_verified": True, "message": "作者身份已通过当前浏览器页面核验；历史完整性须另行采集验证。"}

    def close(self):
        self._stopping.set()
        if self._transport is not None and hasattr(self._transport, "close"):
            self._transport.close()

    def subscribe(self, text: str, display_name: str | None = None) -> dict:
        result = classify(text)
        author_id = result["candidate_author_id"]
        if not author_id:
            raise ValueError("当前无法从这个链接核验作者。请提供小红书作者主页或带 __biz 的公众号链接；短链和单篇解析尚未接通。")
        _id(author_id)
        platform = result["platform"]
        name = (display_name or f"待核验作者 · {author_id[-6:]}").strip()
        if not name or len(name) > 160:
            raise ValueError("作者名称须为1至160个字符")
        with self.workflow.connect() as db:
            existing = db.execute("SELECT 1 FROM subscriptions WHERE platform=? AND author_id=?", (platform, author_id)).fetchone()
            if existing:
                if display_name:
                    db.execute("UPDATE subscriptions SET display_name=? WHERE platform=? AND author_id=?", (name, platform, author_id))
            else:
                db.execute("INSERT INTO subscription_intents VALUES(?,?,?,1,?) ON CONFLICT(platform,author_id) DO UPDATE SET display_name=excluded.display_name", (platform, author_id, name, time.time()))
        return next(s for s in self.workspace()["subscriptions"] if s["platform"] == platform and s["author_id"] == author_id)

    def toggle(self, platform: str, author_id: str, enabled: bool) -> dict:
        with self.workflow.connect() as db:
            count = 0
            for table in ("subscriptions", "subscription_intents"):
                count += db.execute(f"UPDATE {table} SET enabled=? WHERE platform=? AND author_id=?", (int(enabled), platform, author_id)).rowcount
            if not count:
                raise KeyError("subscription_not_found")
        return {"platform": platform, "author_id": author_id, "enabled": enabled}

    def _public_job(self, row: dict, db) -> dict:
        run = db.execute("SELECT * FROM runs WHERE id=?", (row["run_id"],)).fetchone() if row["run_id"] else None
        count = db.execute("SELECT count(*) FROM items WHERE platform=? AND author_id=?", (row["platform"], row["author_id"])).fetchone()[0]
        run_ids = set()
        if run:
            for page in db.execute("SELECT item_ids FROM pages WHERE run_id=?", (row["run_id"],)):
                run_ids.update(json.loads(page[0]))
        exported = json.loads(row["export_json"]) if row["export_json"] else None
        item_count = (sum(author["items"] for author in exported["authors"]) if row["mode"] == "archive" and exported else len(run_ids))
        result = {k: row[k] for k in ("id", "platform", "author_id", "mode", "state", "reason", "created_at", "updated_at")}
        cooldown = self._cooldown_until(row["platform"], db) if row["mode"] != "archive" else 0
        result.update(pages=run["pages"] if run else 0, item_count=item_count, library_item_count=count,
                      coverage=run["coverage"] if run else "unknown", retry_at=cooldown,
                      can_resume=row["state"] not in {"succeeded", "running", "queued"} and cooldown <= time.time())
        if row["mode"] in {"content", "metrics"}:
            progress = db.execute("SELECT count(*),coalesce(sum(state='succeeded'),0),coalesce(sum(state='partial'),0) FROM job_items WHERE job_id=?", (row["id"],)).fetchone()
            result.update(target_count=progress[0],item_count=progress[1],failed_count=progress[2],pending_count=progress[0]-progress[1]-progress[2],coverage="not_applicable")
            result["failed_items"] = [dict(r) for r in db.execute("SELECT item_id,reason FROM job_items WHERE job_id=? AND state='partial' ORDER BY item_id LIMIT 20", (row["id"],))]
        message, next_step = MESSAGES.get(row["reason"], ("任务正在处理，成功进度持续保存。" if row["state"] in {"queued", "running"} else "请查看历史覆盖与正文状态。", "等待任务结束，或查看已保存作品。"))
        if row["mode"] in {"content", "metrics"} and row["reason"] in {"unavailable", "timeout"}:
            message = "作品详情或媒体暂未能获取，具体原因尚未确认；已有正文、成功媒体及指标原值与时间保留。"
            next_step = "查看专用浏览器的提示；可在对应作品详情补充完整原文链接后重试，成功媒体会复用。"
        if row["state"] == "succeeded" and not row["reason"] and run and run["coverage"] == "complete_for_accessible_scope":
            message = "本次列表扫描已到当前可获取范围的明确末页；正文和媒体完整性单独核验。"
            next_step = "查看已保存作品，或按作者归档现有资料。"
        result.update(message=message, next_step=next_step)
        if exported:
            result["export"] = exported
        return result

    def workspace(self) -> dict:
        with self.workflow.connect() as db:
            subscriptions = [dict(r, identity_verified=True, evidence_level="observed_platform_identity") for r in db.execute("SELECT * FROM subscriptions ORDER BY created_at")]
            known = {(s["platform"], s["author_id"]) for s in subscriptions}
            subscriptions.extend(dict(r, identity_verified=False, evidence_level="unverified_link") for r in db.execute("SELECT * FROM subscription_intents ORDER BY created_at") if (r["platform"], r["author_id"]) not in known)
            jobs = [self._public_job(dict(r), db) for r in db.execute("SELECT * FROM jobs ORDER BY id DESC")]
            for sub in subscriptions:
                args = (sub["platform"], sub["author_id"])
                sub["enabled"] = bool(sub["enabled"])
                counts = db.execute("SELECT count(*),coalesce(sum(detail_state='complete'),0) FROM items WHERE platform=? AND author_id=?", args).fetchone()
                sub.update(item_count=counts[0], detail_count=counts[1])
                run = db.execute("SELECT coverage FROM runs WHERE platform=? AND author_id=? ORDER BY CASE WHEN coverage='complete_for_accessible_scope' THEN 0 ELSE 1 END,id DESC LIMIT 1", args).fetchone()
                sub["coverage"] = run[0] if run else "unknown"
                latest = next((j for j in jobs if (j["platform"], j["author_id"]) == args), None)
                sub.update(latest_state=latest["state"] if latest else "pending", reason=latest["reason"] if latest else "identity_unverified")
                sub["message"] = latest["message"] if latest else MESSAGES["identity_unverified"][0]
                exports = next((j["export"] for j in jobs if (j["platform"], j["author_id"]) == args and j.get("export")), None)
                if exports:
                    author_export = exports["authors"][0]
                    sub["archive_url"] = author_export.get("index_url", author_export["manifest_url"])
                for j in jobs:
                    if (j["platform"], j["author_id"]) == args:
                        j["display_name"] = sub["display_name"]
            stats = {"subscriptions": len(subscriptions), "items": db.execute("SELECT count(*) FROM items").fetchone()[0], "details": db.execute("SELECT count(*) FROM items WHERE detail_state='complete'").fetchone()[0], "running": sum(j["state"] in {"queued", "running"} for j in jobs)}
        return {"subscriptions": subscriptions, "runs": jobs, "stats": stats,
                "platforms": [{"platform": "wechat", "available": False, "status": "blocked", "message": MESSAGES["wechat_blocked"][0]},
                              {"platform": "xiaohongshu", "available": True, "status": "experimental", "message": "列表、内容保存和互动指标为实验接入；旧作品可能需补充有效完整链接。样本通过不代表全库正文媒体已保存，长期登录与双平台G1仍未通过。"}],
                "data_dir": str(self.root), "archive_dir": str(self.root / "archive"), "g1_passed": False}

    def items(self, platform=None, author_id=None, offset=0, limit=50, has_assets=False,
              *, sort="published_at", order="desc", min_likes=None, min_collects=None, min_comments=None,
              date_from=None, date_to=None, content_type=None, missing_metric=None) -> dict:
        if sort not in (*FIELDS,"published_at") or order not in {"asc","desc"}:
            raise ValueError("无效排序字段或方向")
        if content_type not in {None,"image","video","unknown"} or missing_metric not in {None,*FIELDS}:
            raise ValueError("无效作品类型或未知指标")
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
        joins = "".join(f" LEFT JOIN item_metrics {f} ON {f}.platform=i.platform AND {f}.item_id=i.item_id AND {f}.field='{f}'" for f in FIELDS)
        sort_expr = "NULLIF(i.published_at,'')" if sort == "published_at" else f"{sort}.value"
        sort_sql = f" ORDER BY {sort_expr} IS NULL, {sort_expr} {order}, i.item_id, i.platform"
        with self.workflow.connect() as db:
            total = db.execute("SELECT count(*) FROM items i" + joins + where, params).fetchone()[0]
            rows = [dict(r) for r in db.execute("SELECT i.platform,i.item_id,i.author_id,i.published_at,i.detail_state,i.source_url,i.content_type,i.title,i.media_state,(SELECT count(*) FROM assets a WHERE a.platform=i.platform AND a.item_id=i.item_id) AS asset_count FROM items i" + joins + where + sort_sql + " LIMIT ? OFFSET ?", params + [limit, offset])]
            for row in rows:
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
        for asset in result["assets"]:
            asset["url"] = "/archive/" + quote(Path(asset["relative_path"]).as_posix(), safe="/")
            asset["state"] = "complete" if self.workflow.asset_valid(platform,item_id,asset["asset_id"]) else "missing"
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

    def start(self, mode, platform=None, author_id=None, item_id=None, source_url=None, item_ids=None) -> dict:
        if mode not in {"full", "latest", "archive", "content", "metrics"} or bool(platform) != bool(author_id):
            raise ValueError("请选择有效模式；指定作者时须同时提供平台和作者ID")
        if item_id and (not author_id or mode not in {"content","metrics"}):
            raise ValueError("单作品任务须提供平台、作者及内容或指标模式")
        if item_ids is not None and (not item_ids or len(item_ids)>200 or item_id or not author_id or mode not in {"content","metrics"} or source_url):
            raise ValueError("指定作品批次须提供同一作者的1至200个ID，且不能同时提供单篇参数；全部作者范围不受此限制")
        selected = list(dict.fromkeys(item_ids)) if item_ids is not None else None
        source_url = self._validate_source(source_url,platform,item_id,author_id)
        scope = [s for s in self.workspace()["subscriptions"] if not author_id or (s["platform"], s["author_id"]) == (platform, author_id)]
        if not scope:
            raise ValueError("没有可处理的订阅，请先添加作者或导入已有验证资料")
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
                active = db.execute("SELECT id FROM jobs WHERE platform=? AND author_id=? AND state IN ('queued','running')", (sub["platform"], sub["author_id"])).fetchone()
                if active:
                    raise ValueError("该作者已有任务排队或运行，请等待完成")
            for sub in scope:
                cur = db.execute("INSERT INTO jobs(platform,author_id,mode,state,created_at,updated_at) VALUES(?,?,?,'queued',?,?)", (sub["platform"], sub["author_id"], mode, time.time(), time.time()))
                ids.append(cur.lastrowid)
                if mode in {"content","metrics"}:
                    clause = " AND item_id IN (" + ",".join("?" for _ in selected) + ")" if selected else " AND item_id=?" if item_id else ""
                    db.execute("INSERT INTO job_items(job_id,platform,item_id) SELECT ?,platform,item_id FROM items WHERE platform=? AND author_id=?" + clause,
                               [cur.lastrowid,sub["platform"],sub["author_id"]] + (selected or ([item_id] if item_id else [])))
        for job_id in ids:
            if source_url:
                self._job_sources[job_id] = source_url
            self._spawn(job_id)
        return {"job_id": ids[0], "job_ids": ids, "state": "queued", "includes_paused": True}

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
        for row in rows:
            row["message"], row["next_step"] = MESSAGES.get(row["reason"], MESSAGES["unexpected_error"])
        return {"total": total,"items":rows,"platform":job["platform"],"author_id":job["author_id"]}

    def resume(self, job_id, source_url=None, source_urls=None) -> dict:
        with self.workflow.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                raise KeyError("job_not_found")
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
            if job["state"] in {"queued", "running", "succeeded"}:
                raise ValueError("该任务运行中或已成功，无需恢复")
            if db.execute("SELECT 1 FROM jobs WHERE platform=? AND author_id=? AND state IN ('queued','running') AND id!=?", (job["platform"], job["author_id"], job_id)).fetchone():
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
        self._spawn(job_id)
        return {"job_id": job_id, "state": "queued"}

    def _spawn(self, job_id):
        thread = Thread(target=self._execute, args=(job_id,), daemon=True)
        self._threads.add(thread)
        thread.start()

    def wait(self, timeout=30):
        for thread in list(self._threads):
            thread.join(timeout)
            if not thread.is_alive():
                self._threads.discard(thread)

    def _finish(self, job_id, state, reason=None, export=None):
        with self.workflow.connect() as db:
            db.execute("UPDATE jobs SET state=?,reason=?,export_json=?,updated_at=? WHERE id=?", (state, reason, json.dumps(export) if export else None, time.time(), job_id))
        if state == "succeeded":
            self._job_sources.pop(job_id,None)

    def _execute(self, job_id):
        with self._lock:
            if self._stopping.is_set():
                self._finish(job_id,"interrupted","process_interrupted")
                return
            try:
                with self.workflow.connect() as db:
                    job = dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
                    db.execute("UPDATE jobs SET state='running',updated_at=? WHERE id=?", (time.time(), job_id))
                    verified = db.execute("SELECT 1 FROM subscriptions WHERE platform=? AND author_id=?", (job["platform"], job["author_id"])).fetchone()
                if job["mode"] != "archive":
                    self._check_cooldown(job["platform"])
                if not verified:
                    if job["platform"] == "xiaohongshu" and job["mode"] != "archive":
                        self.verify(job["platform"], job["author_id"])
                    else:
                        self._finish(job_id, "blocked", "wechat_blocked" if job["platform"] == "wechat" else "identity_unverified")
                        return
                if job["mode"] in {"content","metrics"}:
                    self._execute_content(job)
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
                if job["mode"] == "archive":
                    with self.workflow.connect() as db:
                        known = db.execute("SELECT coverage,terminal_evidence FROM runs WHERE platform=? AND author_id=? AND id!=? ORDER BY CASE WHEN coverage='complete_for_accessible_scope' THEN 0 ELSE 1 END,id DESC LIMIT 1", (job["platform"], job["author_id"], run_id)).fetchone()
                        db.execute("UPDATE runs SET state='succeeded',coverage=?,terminal_evidence=?,updated_at=? WHERE id=?", (known[0] if known else "unknown", known[1] if known else None, time.time(), run_id))
                    exported = self.workflow.export_all(batch_id=batch)
                    for author in exported["authors"]:
                        for key in ("manifest", "corpus", "index"):
                            author[key + "_url"] = "/archive/" + quote(Path(author[key]).relative_to(self.root / "archive").as_posix(), safe="/")
                    self._finish(job_id, "succeeded", "archive_complete", exported)
                    return
                if job["platform"] == "wechat":
                    self._finish(job_id, "blocked", "wechat_blocked")
                    return
                with self._network_lock:
                    self._check_cooldown(job["platform"])
                    adapter = self.transport()
                    result = self.workflow.run_all({job["platform"]: adapter}, mode=job["mode"], batch_id=batch, login_confirmed=True)
                    run = result["runs"][0]
                    if run["state"] == "rate_limited":
                        self._record_cooldown(job["platform"], retry_at=run["retry_at"])
                run = result["runs"][0]
                self._finish(job_id, run["state"], run["reason"])
            except PlatformCooldown:
                self._finish(job_id, "rate_limited", "rate_limited")
            except AdapterFailure as error:
                if error.category == "rate_limited":
                    self._record_cooldown(job["platform"], retry_after=error.retry_after)
                if error.category in {"needs_login", "verification_required"}:
                    self._job_sources.clear()
                category = error.category if error.category in {"needs_login", "rate_limited"} else "transport_unavailable"
                self._finish(job_id, category if category != "transport_unavailable" else "blocked", category)
            except Exception:
                self._finish(job_id, "failed", "unexpected_error")

    def _execute_content(self, job):
        """Run a fixed local item snapshot, independent of history pagination."""
        if job["platform"] != "xiaohongshu":
            self._finish(job["id"], "blocked", "wechat_blocked")
            return
        with self.workflow.connect() as db:
            targets = [dict(r) for r in db.execute("SELECT i.* FROM job_items j JOIN items i USING(platform,item_id) WHERE j.job_id=? AND j.state!='succeeded' ORDER BY i.item_id", (job["id"],))]
            scope_count = db.execute("SELECT count(*) FROM job_items WHERE job_id=?", (job["id"],)).fetchone()[0]
        if not scope_count:
            self._finish(job["id"],"blocked","no_local_items")
            return
        sources = self._job_sources.get(job["id"], {})
        # The scope lives in job_items; ephemeral links can be renewed without
        # changing membership, replaying successful items or advancing pages.
        with self._network_lock:
            self._check_cooldown(job["platform"])
            transport = self.transport()
            if hasattr(transport, "prepare_details") and not isinstance(sources,str):
                transport.prepare_details(job["author_id"], [i["item_id"] for i in targets if i["item_id"] not in sources])
        for item in targets:
            if self._stopping.is_set():
                self._finish(job["id"],"interrupted","process_interrupted")
                return
            observed = False
            try:
                with self._network_lock:
                    self._check_cooldown(job["platform"])
                    transport = self.transport()
                    source = sources if isinstance(sources,str) else sources.get(item["item_id"],item["source_url"] or "")
                    detail = transport.detail(job["author_id"], item["item_id"], source_url=source)
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
                    if job["mode"] == "content":
                        if detail.get("text","").strip():
                            self.workflow.save_detail(job["platform"],item["item_id"],job["author_id"],detail["text"],detail["source_url"])
                        else:
                            complete = item["detail_state"] == "complete"
                        media_failed = False
                        for candidate in detail.get("media",[]):
                            if self._stopping.is_set():
                                self._finish(job["id"],"interrupted","process_interrupted")
                                return
                            if self.workflow.candidate_saved(job["platform"],item["item_id"],candidate):
                                continue
                            try:
                                fetched = transport.download_media(candidate,self.root / "downloads" / job["platform"] / _id(item["item_id"]))
                                self.workflow.attach_media(job["platform"],item["item_id"],candidate.asset_id,Path(fetched["path"]),position=candidate.position,kind=candidate.kind,mime=fetched["mime"])
                            except AdapterFailure as error:
                                if error.category in {"needs_login","rate_limited","unavailable","verification_required"}:
                                    raise
                                media_failed = True
                            except (ValueError,OSError):
                                media_failed = True
                        media_complete = bool(detail.get("media")) and not detail.get("missing") and not media_failed
                        complete = complete and media_complete
                        with self.workflow.connect() as db:
                            db.execute("UPDATE items SET media_state=? WHERE platform=? AND item_id=?", ("complete_for_observed_detail" if media_complete else "partial",job["platform"],item["item_id"]))
                with self.workflow.connect() as db:
                    db.execute("UPDATE job_items SET state=?,reason=? WHERE job_id=? AND platform=? AND item_id=?", ("succeeded" if complete else "partial",None if complete else "content_partial",job["id"],job["platform"],item["item_id"]))
                if complete and isinstance(sources,dict):
                    sources.pop(item["item_id"],None)
            except PlatformCooldown:
                raise
            except Exception as error:
                category = error.category if isinstance(error,AdapterFailure) else "unexpected_error"
                if not observed:
                    self.workflow.save_metrics(job["platform"],item["item_id"],{},source="xhs_browser_detail_attempt",collected_at=time.time(),
                        observation_key=f"job:{job['id']}:{item['item_id']}:failed:{time.time_ns()}",status="failed",reason=category)
                with self.workflow.connect() as db:
                    db.execute("UPDATE job_items SET state='partial',reason=? WHERE job_id=? AND platform=? AND item_id=?", (category,job["id"],job["platform"],item["item_id"]))
                if category in {"rate_limited","needs_login","unavailable","verification_required"}:
                    if category in {"needs_login","verification_required"}:
                        self._job_sources.clear()
                    if category == "rate_limited":
                        self._record_cooldown(job["platform"],retry_after=error.retry_after)
                    self._finish(job["id"],category if category in {"rate_limited","needs_login"} else "blocked",category)
                    return
        with self.workflow.connect() as db:
            pending = db.execute("SELECT count(*) FROM job_items WHERE job_id=? AND state!='succeeded'", (job["id"],)).fetchone()[0]
        self._finish(job["id"], "partial" if pending else "succeeded", job["mode"] + ("_partial" if pending else "_complete"))

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
