"""Local validation API. Live platform actions deliberately remain unavailable."""
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .links import classify
from .validation import Store, SyntheticAdapter, scan


class LinkInput(BaseModel):
    text: str = Field(min_length=1, max_length=4096)


class DemoInput(BaseModel):
    inject_failures: bool = True


def create_app(data_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="Creator Archive · 验证模式", version=__version__)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    store = Store((data_dir or Path("data/validation")) / "state.sqlite3")
    lock = Lock()
    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.method == "POST":
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
        return {"version": __version__, "mode": "validation_only", "g1_passed": False,
                "platforms": [{"platform": p, "creator_resolution": False, "history_pagination": False,
                               "detail": False, "media": False, "known_limits": "真实组件与授权样本尚未接入"}
                              for p in ("wechat", "xiaohongshu")], "runs": store.all()}

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
