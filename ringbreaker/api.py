"""FastAPI backend + web UI.

  python -m ringbreaker serve      # http://127.0.0.1:8000  (docs at /docs)
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import investigator as INV
from .store import Store

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
DATA_DIR = os.environ.get("RINGBREAKER_DATA_DIR", "data")
SITE_DIR = os.environ.get("RINGBREAKER_SITE_DIR", "dist/site")


class InvestigationLimiter:
    """Protects a shared LLM key on a public deployment: one investigation at a
    time and a cap per rolling hour (RINGBREAKER_MAX_INVESTIGATIONS_PER_HOUR)."""

    def __init__(self, per_hour: int):
        self.per_hour, self.lock, self.recent = per_hour, threading.Lock(), deque()
        self.running = threading.Lock()

    def acquire(self) -> str | None:
        with self.lock:
            now = time.time()
            while self.recent and now - self.recent[0] > 3600:
                self.recent.popleft()
            if len(self.recent) >= self.per_hour:
                wait = int(3600 - (now - self.recent[0])) // 60 + 1
                return f"Investigation limit reached for this hour. Try again in about {wait} min."
            if not self.running.acquire(blocking=False):
                return "Another investigation is running. Try again in a minute."
            self.recent.append(now)
            return None

    def release(self):
        if self.running.locked():
            self.running.release()


class Action(BaseModel):
    action: str = Field(pattern="^(start|escalate|freeze|str|false_positive|close|reopen)$")
    note: str = Field(default="", max_length=1000)


def create_app(data_dir: str = DATA_DIR) -> FastAPI:
    store = Store(data_dir)
    limiter = InvestigationLimiter(int(os.environ.get("RINGBREAKER_MAX_INVESTIGATIONS_PER_HOUR", "1000")))
    # Behind the proxy the app lives at /live (prefix stripped by Caddy). The prefix is only
    # advertised in the OpenAPI "servers" list; routing itself stays prefix-free.
    prefix = os.environ.get("RINGBREAKER_ROOT_PATH", "").rstrip("/")
    app = FastAPI(title="RingBreaker API", version="1.0.0", docs_url=None, redoc_url=None,
                  servers=[{"url": prefix}] if prefix else None,
                  description="Mule-network detection, case management and AI investigation.")

    @app.get("/docs", include_in_schema=False)
    def docs():
        from fastapi.openapi.docs import get_swagger_ui_html
        return get_swagger_ui_html(openapi_url="openapi.json", title="RingBreaker API")
    # Public read-only demo snapshot (served without auth behind the proxy at "/").
    if os.path.isdir(SITE_DIR):
        app.mount("/site", StaticFiles(directory=SITE_DIR, html=True), name="site")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        # The server is for local use: reject other hosts (DNS rebinding) and cross-site writes.
        host = (request.headers.get("host") or "").split(":")[0]
        allowed = {"127.0.0.1", "localhost", *filter(None, os.environ.get("RINGBREAKER_ALLOWED_HOSTS", "").split(","))}
        if host not in allowed:
            return JSONResponse({"error": "forbidden host"}, status_code=403)
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD") and origin and origin.split("//")[-1].split(":")[0] not in allowed:
            return JSONResponse({"error": "cross-origin request blocked"}, status_code=403)
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
        return resp

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(os.path.join(WEB, "index.html"))

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/config")
    def config():
        llm = INV.llm_available()
        return {"llm": llm, "live": True}

    @app.get("/api/overview")
    def overview():
        return store.overview()

    @app.get("/api/cases")
    def cases():
        return store.cases()

    @app.get("/api/cases/{case_id}")
    def case(case_id: str):
        c = store.case(case_id)
        if not c:
            raise HTTPException(404, "case not found")
        return c

    @app.get("/api/accounts/{account_id}")
    def account(account_id: str):
        a = store.account(account_id)
        if not a:
            raise HTTPException(404, "account not in the alerting population")
        return a

    @app.get("/api/alerts")
    def alerts():
        return store.alerts()

    @app.get("/api/model")
    def model():
        return store.model()

    @app.get("/api/network")
    def network():
        return store.network()

    @app.get("/api/pipeline")
    def pipeline():
        return store.pipeline()

    @app.post("/api/cases/{case_id}/actions")
    def act(case_id: str, a: Action):
        if not store.case(case_id):
            raise HTTPException(404, "case not found")
        return {"status": store.act(case_id, a.action, a.note)}

    @app.get("/api/cases/{case_id}/investigate")
    def investigate(case_id: str):
        if not store.case(case_id):
            raise HTTPException(404, "case not found")

        blocked = limiter.acquire()

        def stream():
            if blocked:
                yield f"data: {json.dumps({'type': 'error', 'error': blocked})}\n\n"
                return
            try:
                steps = []
                for ev in INV.investigate(store, case_id, cache_dir=os.path.join(store.state_dir, ".llm_cache")):
                    if ev["type"] == "step":
                        steps.append(ev)
                    if ev["type"] == "final":
                        store.save_investigation(case_id, ev["report"], ev["engine"], steps)
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
                yield "data: {\"type\": \"done\"}\n\n"
            finally:
                limiter.release()

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


def serve(port: int = 8000, data_dir: str = DATA_DIR, host: str = "127.0.0.1"):
    import uvicorn
    shown = "127.0.0.1" if host in ("127.0.0.1", "0.0.0.0") else host
    print(f"RingBreaker running at http://{shown}:{port}   (API docs: /docs, Ctrl+C to stop)")
    uvicorn.run(create_app(data_dir), host=host, port=port, log_level="warning")
