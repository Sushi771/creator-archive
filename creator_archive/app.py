"""Loopback workspace API with isolated synthetic validation compatibility."""
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
import os

from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .links import classify
from .validation import AdapterFailure, Store, SyntheticAdapter, scan
from .service import PlatformCooldown, WorkspaceService, default_workspace


class LinkInput(BaseModel):
    text: str = Field(min_length=1, max_length=4096)


class DemoInput(BaseModel):
    inject_failures: bool = True


class SubscriptionInput(LinkInput):
    display_name: str | None = Field(default=None, max_length=160)


class AuthorInput(BaseModel):
    platform: str
    author_id: str


class ToggleInput(AuthorInput):
    enabled: bool


class JobInput(BaseModel):
    mode: str = "full"
    platform: str | None = None
    author_id: str | None = None
    item_id: str | None = None
    source_url: str | None = Field(default=None,max_length=4096)
    item_ids: list[str] | None = Field(default=None,max_length=200)


class ResumeInput(BaseModel):
    source_url: str | None = Field(default=None,max_length=4096)
    source_urls: list[str] | None = Field(default=None,max_length=200)


def create_app(data_dir: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        yield
        service.close()

    app = FastAPI(title="Creator Archive · 本地工作台", version=__version__, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    root = data_dir or Path(os.environ.get("CREATOR_ARCHIVE_DATA_DIR", str(default_workspace())))
    store = Store(root / "state.sqlite3")
    service = WorkspaceService(root)
    app.state.service = service
    lock = Lock()
    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if request.headers.get("x-creator-archive") != "local-validation" or (origin and origin != str(request.base_url).rstrip("/")):
                return JSONResponse({"detail": "仅允许本地验证页面发起操作"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.get("/")
    def home():
        return FileResponse(static / "index.html")

    @app.get("/api/status")
    def status():
        return {"version": __version__, "mode": "local_mvp", "g1_passed": False,
                "platforms": [{"platform": p, "creator_resolution": False, "history_pagination": p == "xiaohongshu",
                               "detail": p == "xiaohongshu", "media": p == "xiaohongshu", "metrics": p == "xiaohongshu",
                               "experimental": True, "known_limits": "G1未通过；字段表示实验接口已实现，不代表全库已采集或双平台已验收。旧作品可能需补充有效原文链接。"}
                              for p in ("wechat", "xiaohongshu")], "runs": store.all()}

    @app.exception_handler(ValueError)
    async def invalid_input(request, error):
        return JSONResponse({"detail": str(error)}, status_code=422)

    @app.exception_handler(KeyError)
    async def missing_record(request, error):
        return JSONResponse({"detail": "未找到对应作者、作品或任务"}, status_code=404)

    @app.exception_handler(AdapterFailure)
    async def platform_failure(request, error):
        return JSONResponse({"detail": getattr(error, "reason", "平台暂不可用，进度已保留。请检查独立浏览器的登录和验证提示后重试。"), "reason": error.category}, status_code=409)

    @app.exception_handler(PlatformCooldown)
    async def platform_cooldown(request, error):
        return JSONResponse({"detail": str(error), "reason": "rate_limited", "retry_at": error.retry_at}, status_code=429)

    @app.get("/api/workspace")
    def workspace():
        return service.workspace()

    @app.post("/api/subscriptions")
    def subscribe(body: SubscriptionInput):
        return service.subscribe(body.text, body.display_name)

    @app.post("/api/subscriptions/toggle")
    def toggle(body: ToggleInput):
        return service.toggle(body.platform, body.author_id, body.enabled)

    @app.post("/api/subscriptions/cancel")
    def cancel_subscription(body: AuthorInput):
        return service.cancel_subscription(body.platform, body.author_id)

    @app.post("/api/subscriptions/resubscribe")
    def resubscribe(body: AuthorInput):
        return service.resubscribe(body.platform, body.author_id)

    @app.post("/api/subscriptions/confirm")
    def confirm_subscription(body: AuthorInput):
        return service.confirm_subscription(body.platform, body.author_id)

    @app.post("/api/subscriptions/verify")
    def verify(body: AuthorInput):
        return service.verify(body.platform, body.author_id)

    @app.post("/api/platforms/xiaohongshu/login")
    def login():
        return service.open_login()

    @app.post("/api/server/stop")
    def stop_server():
        callback = getattr(app.state, "request_shutdown", None)
        if callback is None:
            raise HTTPException(status_code=409, detail="当前启动方式未提供停止入口，请通过启动进程正常退出")
        callback()
        return {"state": "stopping"}

    @app.post("/api/jobs")
    def start_job(body: JobInput):
        return service.start(body.mode, body.platform, body.author_id, body.item_id, body.source_url, body.item_ids)

    @app.post("/api/jobs/{job_id}/resume")
    def resume(job_id: int, body: ResumeInput | None = None):
        return service.resume(job_id,body.source_url if body else None,body.source_urls if body else None)

    @app.post("/api/archive-batches/{batch_id}/resume")
    def resume_archive_batch(batch_id: int):
        return service.resume_archive_batch(batch_id)

    @app.get("/api/jobs/{job_id}/failures")
    def job_failures(job_id: int, offset: int = Query(default=0,ge=0), limit: int = Query(default=50,ge=1,le=200)):
        return service.job_failures(job_id,offset,limit)

    @app.get("/api/jobs/{job_id}/scan")
    def job_scan(job_id: int):
        return service.job_scan(job_id)

    @app.post("/api/items/resolve")
    def resolve_item(body: SubscriptionInput):
        return service.resolve_item(body.text)

    @app.get("/api/items")
    def items(platform: str | None = None, author_id: str | None = None,
              offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=200), has_assets: bool = False,
              sort: str = "published_at", order: str = "desc", min_likes: int | None = Query(default=None,ge=0),
              min_collects: int | None = Query(default=None,ge=0), min_comments: int | None = Query(default=None,ge=0),
              date_from: str | None = None, date_to: str | None = None, content_type: str | None = None,
              missing_metric: str | None = None):
        return service.items(platform, author_id, offset, limit, has_assets,sort=sort,order=order,min_likes=min_likes,
                             min_collects=min_collects,min_comments=min_comments,date_from=date_from,date_to=date_to,
                             content_type=content_type,missing_metric=missing_metric)

    @app.get("/api/items/{platform}/{item_id}")
    def item(platform: str, item_id: str):
        return service.item(platform, item_id)

    @app.post("/api/import/validation")
    def import_validation():
        return service.import_validation()

    @app.get("/archive/{relative_path:path}")
    def archive_file(relative_path: str):
        archive = (service.root / "archive").resolve()
        target = (archive / relative_path).resolve()
        if not target.is_relative_to(archive) or not target.is_file() or target.suffix.lower() not in {".html", ".md", ".json", ".jsonl", ".jpg", ".png", ".webp", ".mp4"}:
            raise HTTPException(status_code=404, detail="归档文件不存在或不可访问")
        if service.workflow.archive_asset_valid(target.relative_to(archive).as_posix()) is False:
            raise HTTPException(status_code=404, detail="附件缺失或完整性校验失败；已保留登记，请恢复原文件或重新保存缺失媒体")
        return FileResponse(target)

    @app.post("/api/links/classify")
    def classify_link(body: LinkInput):
        try:
            return classify(body.text)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.post("/api/demo/run")
    def demo(body: DemoInput):
        if not lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="已有验证任务运行中")
        try:
            results = []
            # This fixed scope intentionally includes a synthetic paused subscription.
            for platform in ("wechat", "xiaohongshu"):
                for author_id in ("demo-a", "demo-b-paused"):
                    adapter = SyntheticAdapter()
                    if body.inject_failures and author_id == "demo-b-paused":
                        adapter = SyntheticAdapter(2, "needs_login" if platform == "wechat" else "rate_limited")
                    run_id = store.create(platform, author_id, adapter.version)
                    results.append(scan(store, run_id, adapter, login_confirmed=not body.inject_failures))
            return {"evidence_level": "synthetic", "g1_passed": False, "runs": results}
        finally:
            lock.release()

    return app
