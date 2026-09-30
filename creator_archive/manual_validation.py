"""Private single-author, one-shot human validation plan. Never opens a browser."""
from hashlib import sha256
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .validation import AdapterFailure

PLAN_FILE = "xhs-manual-validation.json"
ACTION_BUDGETS = {"validate": 16}


class ManualScope:
    def __init__(self, author_id, action, persist):
        self.author_id = author_id
        self.action = action
        self.budget = ACTION_BUDGETS[action]
        self.persist = persist
        self.stopped = False
        self.reason = None
        self.counts = {"list": 0, "detail": 0, "media": 0}
        self.ids = set()
        self.seen = set()
        self.page = None
        self.details = {}
        self.media_required = 0

    def stop(self, reason):
        if not self.stopped:
            self.stopped = True
            self.reason = reason
            self.persist(self)

    def observe(self, ids):
        ids = tuple(ids)
        if len(ids) > 30 or any(not re.fullmatch(r"[0-9a-f]{24}", x) for x in ids):
            self.stop("invalid_response")
            raise AdapterFailure("invalid_response")
        self.ids.update(ids[:3])

    def consume(self, host, path):
        if self.stopped:
            raise AdapterFailure("network_paused")
        parsed = urlsplit(path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if host == "edith.xiaohongshu.com":
            valid = (parsed.path == "/api/sns/web/v1/user_posted" and
                     query.get("user_id") == [self.author_id] and
                     query.get("cursor") == [""] and query.get("num") == ["30"])
            kind, limit = "list", 1
        elif host == "www.xiaohongshu.com":
            match = re.fullmatch(r"/explore/([0-9a-f]{24})", parsed.path)
            valid = bool(match and match[1] in self.ids)
            kind, limit = "detail", 3
        else:
            valid = (self.counts["detail"] > 0
                     and (host == "xhscdn.com" or host.endswith(".xhscdn.com")))
            kind, limit = "media", 12
        key = sha256((host + (parsed.path if kind == "detail" else path)).encode()).hexdigest()
        if not valid or key in self.seen or self.counts[kind] >= limit or sum(self.counts.values()) >= self.budget:
            self.stop("manual_validation_budget_or_scope")
            raise AdapterFailure("network_paused")
        self.seen.add(key)
        self.counts[kind] += 1
        self.persist(self)


class ManualValidation:
    def __init__(self, service):
        self.service = service
        self.path = service.root / PLAN_FILE
        self.lock = threading.RLock()

    def _read(self):
        if not self.path.is_file():
            return None
        try:
            plan = json.loads(self.path.read_text(encoding="utf-8"))
            if (plan.get("schema") != 1 or not re.fullmatch(r"[0-9a-f]{24}", plan.get("author_id", ""))
                    or plan.get("account_normal_confirmed") is not True or plan.get("owner_authorized") is not True
                    or not isinstance(plan.get("actions"), dict)):
                raise ValueError()
            return plan
        except (OSError, ValueError, TypeError):
            raise ValueError("手动验收许可无法读取；联网保持暂停，原文件未改动") from None

    def _write(self, plan):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def status(self):
        with self.lock:
            plan = self._read()
            if plan is None:
                return {"configured": False}
            actions = plan["actions"]
            active = any(action.get("state") == "running" for action in actions.values())
            return {"configured": True, "author_id": plan["author_id"],
                    "blocked_reason": plan.get("blocked_reason"), "active": active,
                    "actions": {name: {"state": actions.get(name, {}).get("state", "pending"),
                                       "budget": budget, "counts": actions.get(name, {}).get("counts", {})}
                                for name,budget in ACTION_BUDGETS.items()},
                    "media_required": actions.get("validate", {}).get("media_required", 0),
                    "media_remaining": actions.get("validate", {}).get("media_remaining", 0),
                    "job_id": actions.get("validate", {}).get("job_id"),
                    "message": "仅此作者最新列表原顺序前三篇，一次验收共16 HTTP：列表1/详情3/媒体12。无第二轮刷新，其他联网与浏览器自动化仍暂停。"}

    def interrupt_pending(self):
        with self.lock:
            plan = self._read()
            if plan and any(x.get("state") == "running" for x in plan["actions"].values()):
                plan["blocked_reason"] = "process_interrupted"
                for action in plan["actions"].values():
                    if action.get("state") == "running":
                        action["state"] = "stopped"
                self._write(plan)

    def _persist(self, scope, *, done=False):
        with self.lock:
            plan = self._read()
            action = plan["actions"][scope.action]
            action["counts"] = dict(scope.counts)
            action["media_required"] = scope.media_required
            action["media_remaining"] = max(0, scope.media_required - scope.counts["media"])
            if scope.stopped:
                plan["blocked_reason"] = scope.reason
                action.update(state="stopped", reason=scope.reason)
            elif done:
                action["state"] = "succeeded"
            self._write(plan)

    def complete(self, scope, *, success, reason=None):
        if not success:
            scope.stop(reason or "manual_validation_failed")
        self._persist(scope, done=success)

    def begin(self, action, author_id, confirmed):
        if confirmed is not True or action not in ACTION_BUDGETS:
            raise ValueError("请本人勾选本次请求范围和预算，再点击对应手动验收按钮")
        with self.lock:
            plan = self._read()
            if plan is None or plan["author_id"] != author_id:
                raise ValueError("没有该作者的手动验收许可；未请求平台")
            if plan.get("blocked_reason"):
                raise ValueError("本次验收已停止：" + plan["blocked_reason"] + "。请反馈提示，不能重复点击重试")
            actions = plan["actions"]
            if any(x.get("state") == "running" for x in actions.values()) or actions.get(action, {}).get("state", "pending") != "pending":
                raise ValueError("该次操作已提交或已用完；不会重复请求")
            if self.service._source_kind("xiaohongshu", author_id) != "xhs_http":
                raise ValueError("尚未配置本机HTTP来源；不自动登录或切换来源")
            config = self.service.source_config("xiaohongshu", author_id)
            cookie_path = config.get("cookie_file")
            if (not isinstance(cookie_path, str) or not Path(cookie_path).is_absolute()
                    or not Path(cookie_path).resolve().is_relative_to(self.service.root)):
                raise ValueError("验收只允许使用当前工作区内已有的授权会话；不会读取其他目录的凭据")
            with self.service.workflow.connect() as db:
                if db.execute("SELECT 1 FROM jobs WHERE platform='xiaohongshu' AND state IN ('running','queued')").fetchone():
                    raise ValueError("已有任务未结束；未请求平台")
            actions[action] = {"state": "running", "counts": {"list": 0, "detail": 0, "media": 0}, "started_at": time.time()}
            self._write(plan)
            return ManualScope(author_id, action, self._persist)

    def perform(self, action, author_id, confirmed):
        from . import network_safety
        scope = self.begin(action, author_id, confirmed)
        with network_safety.manual_action(scope):
            try:
                # The one author/list request is reused by this same first-three job.
                self.service.subscribe("https://www.xiaohongshu.com/user/profile/" + author_id)
                self.service.verify("xiaohongshu", author_id)
                result = self.service.start("recent_window", "xiaohongshu", author_id)
                with self.lock:
                    plan = self._read()
                    plan["actions"][action]["job_id"] = result["job_id"]
                    self._write(plan)
                return result
            except Exception as error:
                self.complete(scope, success=False, reason=getattr(error, "category", "manual_validation_failed"))
                raise
