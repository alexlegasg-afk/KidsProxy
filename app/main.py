import asyncio
import logging
import re
from contextlib import asynccontextmanager

from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from . import billing
from .admin import router as admin_router
from .config import get_settings
from .database import dispose_engine, init_engine, session_scope
from .proxy import router as proxy_router
from .usage import router as usage_router

log = logging.getLogger("kidsproxy")


class RedactFilter(logging.Filter):
    """Scrub anything key-shaped from log lines: bearer tokens, sk-* keys."""

    PATTERNS = [
        re.compile(r"(?i)(authorization\s*[:=]\s*)\S+"),
        re.compile(r"Bearer\s+\S+"),
        re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
        re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)\S+"),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        for pat in self.PATTERNS:
            msg = pat.sub("[REDACTED]", msg)
        record.msg = msg
        record.args = ()
        return True


def setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(RedactFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


async def sweeper(app: FastAPI) -> None:
    """Periodic crash recovery for stale reservations."""
    s = get_settings()
    while True:
        await asyncio.sleep(max(30, s.stale_grace_seconds))
        try:
            async with session_scope() as sess:
                n = await billing.sweep_stale(sess, s.stale_grace_seconds)
                if n:
                    log.warning("swept %d stale requests to uncertain", n)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("sweeper error: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    s = get_settings()
    init_engine()
    app.state.http = httpx.AsyncClient(
        base_url=s.upstream_base_url,
        timeout=httpx.Timeout(s.upstream_timeout, connect=10.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    task = asyncio.create_task(sweeper(app))
    log.info("kidsproxy up; upstream=%s db=%s", s.upstream_base_url, s.db_path)
    try:
        yield
    finally:
        task.cancel()
        await app.state.http.aclose()
        await dispose_engine()


app = FastAPI(title="KidsProxy", lifespan=lifespan, docs_url=None, redoc_url=None)
app.include_router(proxy_router)
app.include_router(admin_router)
app.include_router(usage_router)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.middleware("http")
async def no_auth_leak_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("referrer-policy", "no-referrer")
    response.headers.setdefault("x-content-type-options", "nosniff")
    return response
