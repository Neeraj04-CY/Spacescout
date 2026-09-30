"""Application entry point: FastAPI app, request IDs, JSON logging, static UI.

Run:  uvicorn app.main:app --reload
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.config import get_settings
from app.data.repository import load_listings
from app.services.llm_client import LLMClient
from app.services.search import SearchService

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


class JsonFormatter(logging.Formatter):
    """One JSON object per log line. Extra fields passed via `extra=` are included."""

    _skip = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": round(record.created, 3), "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        payload.update({k: v for k, v in record.__dict__.items() if k not in self._skip})
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("spacescout")
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    root.propagate = False


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(
        title="SpaceScout API",
        version="1.0.0",
        description="Natural-language search over coworking listings. Recommendations come only from the listing data.",
    )
    app.state.search_service = SearchService(settings, load_listings(), LLMClient(settings))
    log = logging.getLogger("spacescout.http")

    hits: dict[str, deque] = defaultdict(deque)

    def client_ip(request: Request) -> str:
        fwd = request.headers.get("x-forwarded-for")
        return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        request.state.request_id = rid
        start = time.perf_counter()
        if request.method == "POST" and request.url.path == "/api/search" and settings.rate_limit_per_minute > 0:
            # Simple sliding-window limit per client IP (in-memory: per process, resets on restart).
            q, now = hits[client_ip(request)], time.monotonic()
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= settings.rate_limit_per_minute:
                retry = int(60 - (now - q[0])) + 1
                log.info("rate_limited", extra={"request_id": rid})
                return JSONResponse(
                    {"error": "rate_limited", "message": f"Too many searches. Try again in {retry} second{'' if retry == 1 else 's'}.", "request_id": rid},
                    status_code=429, headers={"retry-after": str(retry), "x-request-id": rid},
                )
            q.append(now)
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled_error", extra={"request_id": rid, "path": request.url.path})
            response = JSONResponse({"error": "internal_error", "request_id": rid}, status_code=500)
        response.headers["x-request-id"] = rid
        response.headers["x-content-type-options"] = "nosniff"
        response.headers["referrer-policy"] = "same-origin"
        if request.url.path.startswith("/api"):
            log.info("http", extra={"request_id": rid, "method": request.method, "path": request.url.path,
                                     "status": response.status_code, "ms": round((time.perf_counter() - start) * 1000, 1)})
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        errors = [{"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse({"error": "invalid_request", "details": errors, "request_id": getattr(request.state, "request_id", None)}, status_code=422)

    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
