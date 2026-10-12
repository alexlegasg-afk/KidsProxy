import asyncio
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="kidsproxy-test-")
os.environ["KP_DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["KP_SESSION_SECRET"] = "test-session-secret"
os.environ["KP_UPSTREAM_API_KEY"] = "master-key-should-never-leak"
os.environ["KP_UPSTREAM_BASE_URL"] = "http://upstream.test"
os.environ["KP_TZ"] = "UTC"

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from asgi_lifespan import LifespanManager  # noqa: E402
from httpx import ASGITransport  # noqa: E402
from sqlalchemy import text  # noqa: E402

import app.database as database  # noqa: E402
from app import billing  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models import Base, Pricing  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():  # sync: avoids pytest-asyncio session/function runner clash
    async def create():
        eng = database.init_engine()
        async with eng.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        await database.dispose_engine()
    asyncio.run(create())


@pytest_asyncio.fixture(autouse=True)
async def _lifespan():
    async with LifespanManager(fastapi_app):
        yield


@pytest_asyncio.fixture(autouse=True)
async def _clean_db(_lifespan):  # explicit dep: engine must be live first
    async with database.session_scope() as s:
        for t in ("ledger", "reservations", "requests", "api_keys", "balances", "children", "settings", "pricing"):
            await s.execute(text(f"DELETE FROM {t}"))
        s.add(Pricing(id=1, input_per_m=500_000, output_per_m=2_000_000,
                      cached_input_per_m=100_000, updated_at=billing.now_iso()))
    yield


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=ASGITransport(app=fastapi_app),
                                base_url="http://testserver") as c:
        yield c


def mock_upstream(handler):
    """Replace the app's upstream client; the real one stays unused."""
    fastapi_app.state.http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://upstream.test")


def completion_json(prompt=1000, completion=500, cached=None):
    usage = {"prompt_tokens": prompt, "completion_tokens": completion}
    if cached is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    return {"id": "chatcmpl-1", "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": usage}


async def make_child(slug, credit_micro=0):
    """Create child + key (optionally funded). Returns (child_id, full_key)."""
    from app.auth import create_child, rotate_key
    async with database.session_scope() as s:
        child = await create_child(s, slug, slug)
        await s.flush()
        cid = child.id
    if credit_micro:
        async with database.session_scope() as s:
            await billing.add_credit(s, cid, credit_micro, "test", "seed", f"seed-{slug}")
    async with database.session_scope() as s:
        full, _ = await rotate_key(s, cid, slug)
    return cid, full


def chat_body(content="hi", **kw):
    body = {"model": "test-model", "messages": [{"role": "user", "content": content}],
            "max_tokens": 500}
    body.update(kw)
    return body


def auth(key):
    return {"authorization": f"Bearer {key}"}
