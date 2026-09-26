"""Local product workspace: durable jobs and explicit evidence boundaries.

The existing archive workflow owns pages, checkpoints and exports. This service
adds a thin queue and never treats imported observations as a live transport.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
from threading import Lock, RLock, Thread
import time
from urllib.parse import quote

from .links import classify
from .workflow import ArchiveWorkflow, _id
from .validation import AdapterFailure


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
    "unavailable": ("作者列表暂不可用或受到平台限制；成功进度保留。", "查看独立浏览器中的提示，解除限制后再恢复。"),
    "invalid_cursor": ("当前页面未能衔接保存的游标；没有跳过未知页面。", "重新登录后恢复；若持续发生，请保留当前资料排查页链变化。"),
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
        message, next_step = MESSAGES.get(row["reason"], ("任务正在处理，成功进度持续保存。" if row["state"] in {"queued", "running"} else "请查看历史覆盖与正文状态。", "等待任务结束，或查看已保存作品。"))
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
                              {"platform": "xiaohongshu", "available": True, "status": "experimental", "message": "已完成本机样本自动列表及浏览器重启续扫验证；仍为实验接入，长期登录、全部正文和媒体尚未验收，G1未通过。"}],
                "data_dir": str(self.root), "archive_dir": str(self.root / "archive"), "g1_passed": False}

    def items(self, platform=None, author_id=None, offset=0, limit=50, has_assets=False) -> dict:
        clauses, params = [], []
        for key, value in (("platform", platform), ("author_id", author_id)):
            if value:
                clauses.append(f"{key}=?")
                params.append(value)
        if has_assets:
            clauses.append("EXISTS (SELECT 1 FROM assets a WHERE a.platform=items.platform AND a.item_id=items.item_id)")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.workflow.connect() as db:
            total = db.execute("SELECT count(*) FROM items" + where, params).fetchone()[0]
            rows = [dict(r) for r in db.execute("SELECT platform,item_id,author_id,published_at,detail_state,source_url,(SELECT count(*) FROM assets a WHERE a.platform=items.platform AND a.item_id=items.item_id) AS asset_count FROM items" + where + " ORDER BY published_at DESC,item_id LIMIT ? OFFSET ?", params + [limit, offset])]
        return {"items": rows, "total": total, "offset": offset, "limit": limit}

    def item(self, platform, item_id) -> dict:
        with self.workflow.connect() as db:
            row = db.execute("SELECT * FROM items WHERE platform=? AND item_id=?", (platform, item_id)).fetchone()
            if not row:
                raise KeyError("item_not_found")
            result = dict(row)
            result["assets"] = [dict(r) for r in db.execute("SELECT asset_id,kind,mime,relative_path,bytes FROM assets WHERE platform=? AND item_id=? ORDER BY position", (platform, item_id))]
        for asset in result["assets"]:
            asset["url"] = "/archive/" + quote(Path(asset["relative_path"]).as_posix(), safe="/")
        result["media_coverage"] = "unknown_expected_count"
        return result

    def start(self, mode, platform=None, author_id=None) -> dict:
        if mode not in {"full", "latest", "archive"} or bool(platform) != bool(author_id):
            raise ValueError("请选择有效模式；指定作者时须同时提供平台和作者ID")
        scope = [s for s in self.workspace()["subscriptions"] if not author_id or (s["platform"], s["author_id"]) == (platform, author_id)]
        if not scope:
            raise ValueError("没有可处理的订阅，请先添加作者或导入已有验证资料")
        ids = []
        with self.workflow.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for sub in scope:
                if mode != "archive":
                    self._check_cooldown(sub["platform"], db)
                active = db.execute("SELECT id FROM jobs WHERE platform=? AND author_id=? AND state IN ('queued','running')", (sub["platform"], sub["author_id"])).fetchone()
                if active:
                    raise ValueError("该作者已有任务排队或运行，请等待完成")
            for sub in scope:
                cur = db.execute("INSERT INTO jobs(platform,author_id,mode,state,created_at,updated_at) VALUES(?,?,?,'queued',?,?)", (sub["platform"], sub["author_id"], mode, time.time(), time.time()))
                ids.append(cur.lastrowid)
        for job_id in ids:
            self._spawn(job_id)
        return {"job_id": ids[0], "job_ids": ids, "state": "queued", "includes_paused": True}

    def resume(self, job_id) -> dict:
        with self.workflow.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                raise KeyError("job_not_found")
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

    def _execute(self, job_id):
        with self._lock:
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
                category = error.category if error.category in {"needs_login", "rate_limited"} else "transport_unavailable"
                self._finish(job_id, category if category != "transport_unavailable" else "blocked", category)
            except Exception:
                self._finish(job_id, "failed", "unexpected_error")

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
