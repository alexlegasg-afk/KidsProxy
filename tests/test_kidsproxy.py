"""Required test cases (spec item 15). Mocked upstream; no real 9router needed.

Live integration procedure: see README "Manual integration test".
"""

import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

import app.database as database
from app import billing, policies
from app.auth import authenticate, rotate_key, hash_password
from app.models import Balance, Child, LedgerEntry, Reservation, RequestRecord, Setting

from tests.conftest import auth, chat_body, completion_json, make_child, mock_upstream

# prompt estimate for these bodies: 2976/3 + 8 = 1000 tokens ("x" * 2976)
BIG = "x" * 2976


async def balances(cid):
    async with database.session_scope() as s:
        b = await s.get(Balance, cid)
        return b.available_micro, b.reserved_micro


async def _cid(slug):
    async with database.session_scope() as s:
        return (await s.execute(select(Child.id).where(Child.slug == slug))).scalar_one()


# --- 1. key resolves to correct child ---------------------------------------

async def test_key_resolves_to_owning_child():
    cid1, key1 = await make_child("kid1", 5_000_000)
    await make_child("kid2", 5_000_000)
    async with database.session_scope() as s:
        a1 = await authenticate(s, f"Bearer {key1}")
        assert a1 is not None and a1.child_id == cid1 and a1.slug == "kid1"


# --- 2. invalid / revoked keys rejected -------------------------------------

async def test_invalid_and_revoked_keys_rejected(client):
    cid, key = await make_child("kid1", 5_000_000)
    for bad in (None, "", "wrong-key-xyz"):
        headers = auth(f"Bearer {bad}") if bad else {}
        r = await client.post("/v1/chat/completions", headers=headers, json=chat_body())
        assert r.status_code == 401, repr(bad)
    async with database.session_scope() as s:  # rotate: old key dies
        new_key, _ = await rotate_key(s, cid, "kid1")
    r = await client.post("/v1/chat/completions", headers=auth(key), json=chat_body())
    assert r.status_code == 401
    mock_upstream(lambda req: httpx.Response(200, json=completion_json()))
    r = await client.post("/v1/chat/completions", headers=auth(new_key), json=chat_body())
    assert r.status_code == 200  # new key works


# --- 3. kid1 cannot spend kid2's credits ------------------------------------

async def test_cannot_settle_another_childs_request():
    cid1, _ = await make_child("kid1", 5_000_000)
    cid2, _ = await make_child("kid2", 5_000_000)
    async with database.session_scope() as s:
        await billing.reserve(s, cid2, "req-x", 1_000_000)
        s.add(RequestRecord(id="req-x", child_id=cid2, model="m",
                            created_at=billing.now_iso(), reserved_micro=1_000_000))
    rates = billing.Rates(500_000, 2_000_000, 100_000)
    async with database.session_scope() as s:
        with pytest.raises(billing.UnknownChild):
            await billing.settle(s, "req-x", cid1,
                                 {"prompt_tokens": 10, "completion_tokens": 1, "cached_tokens": 0}, rates)
    a2, r2 = await balances(cid2)
    assert (a2, r2) == (4_000_000, 1_000_000)  # untouched


# --- 4. two children concurrently -------------------------------------------

async def test_two_children_concurrent(client):
    _, key1 = await make_child("kid1", 5_000_000)
    _, key2 = await make_child("kid2", 5_000_000)
    mock_upstream(lambda req: httpx.Response(200, json=completion_json()))
    body = chat_body(content=BIG)
    r1, r2 = await asyncio.gather(
        client.post("/v1/chat/completions", headers=auth(key1), json=body),
        client.post("/v1/chat/completions", headers=auth(key2), json=body),
    )
    assert r1.status_code == r2.status_code == 200
    b1, _ = await balances(await _cid("kid1"))
    b2, _ = await balances(await _cid("kid2"))
    assert b1 == b2 == 5_000_000 - 1500  # each billed its own usage


# --- 5. concurrent reserve cannot overspend ---------------------------------

async def test_concurrent_reserve_no_overspend():
    cid, _ = await make_child("kid1", 1_000_000)
    results = []

    async def try_reserve(i):
        async with database.session_scope() as s:
            try:
                await billing.reserve(s, cid, f"race-{i}", 800_000)
                results.append("ok")
            except billing.NotFunded:
                results.append("funded-out")

    await asyncio.gather(*(try_reserve(i) for i in range(5)))
    assert results.count("ok") == 1
    avail, resv = await balances(cid)
    assert avail + resv == 1_000_000  # conservation of money
    assert resv == 800_000


# --- 6. below minimum balance rejected --------------------------------------

async def test_below_min_start_rejected(client):
    _, key = await make_child("kid1", 500_000)  # $0.50 < $1.00 min
    r = await client.post("/v1/chat/completions", headers=auth(key), json=chat_body())
    assert r.status_code == 402
    assert "minimum" in r.json()["error"]["message"]


# --- 7. cannot reserve max permitted charge ---------------------------------

async def test_unreservable_max_charge_rejected(client):
    _, key = await make_child("kid1", 1_500_000)
    async with database.session_scope() as s:
        # widen caps so the request's true max cost is what gets reserved
        s.add(Setting(key="max_request_cost", value="3000000"))
        s.add(Setting(key="max_output_tokens", value="1000000"))
    body = chat_body(max_tokens=1_000_000)  # 1M out * $2/M = $2.00 > $1.50 available
    r = await client.post("/v1/chat/completions", headers=auth(key), json=body)
    assert r.status_code == 402
    assert "cannot cover" in r.json()["error"]["message"]


# --- 8. billed at configured rates ------------------------------------------

async def test_billed_at_configured_rates(client):
    cid, key = await make_child("kid1", 5_000_000)
    # prompt est 1000 * $0.5/M = 500u; 500 out * $2/M = 1000u -> 1500 micro
    mock_upstream(lambda req: httpx.Response(200, json=completion_json(prompt=1000, completion=500)))
    r = await client.post("/v1/chat/completions", headers=auth(key), json=chat_body(content=BIG))
    assert r.status_code == 200
    avail, resv = await balances(cid)
    assert avail == 5_000_000 - 1500 and resv == 0
    async with database.session_scope() as s:
        req = (await s.execute(select(RequestRecord).where(RequestRecord.child_id == cid))).scalar_one()
        assert req.status == "completed" and req.charge_micro == 1500
        debits = (await s.execute(select(LedgerEntry).where(
            LedgerEntry.child_id == cid, LedgerEntry.type == "debit"))).scalars().all()
        assert len(debits) == 1 and debits[0].amount_micro == -1500


# --- 9. cached tokens priced per convention ---------------------------------

async def test_cached_input_pricing():
    rates = billing.Rates(500_000, 2_000_000, 100_000)
    # 1000 prompt with 400 cached: 600*$0.5/M + 400*$0.1/M = 300+40 = 340u
    total, _ = billing.cost_for(rates, prompt=1000, completion=0, cached=400)
    assert total == 340
    # cached > prompt (bad upstream data) clamps to prompt, never negative-bills
    t2, _ = billing.cost_for(rates, prompt=100, completion=0, cached=500)
    assert t2 == 10  # all 100 tokens priced at cached rate: 100*$0.1/M


# --- 10. duplicate settlement no-op -----------------------------------------

async def test_duplicate_settlement_no_double_charge(client):
    cid, key = await make_child("kid1", 5_000_000)
    mock_upstream(lambda req: httpx.Response(200, json=completion_json(prompt=1000, completion=500)))
    r = await client.post("/v1/chat/completions", headers=auth(key), json=chat_body(content=BIG))
    rid = r.headers["x-request-id"]
    rates = billing.Rates(500_000, 2_000_000, 100_000)
    async with database.session_scope() as s:  # retried settle must be a no-op
        req = await billing.settle(s, rid, cid,
                                   {"prompt_tokens": 1000, "completion_tokens": 500, "cached_tokens": 0}, rates)
        assert req.charge_micro == 1500
    avail, resv = await balances(cid)
    assert avail == 5_000_000 - 1500 and resv == 0


# --- 11. interrupted stream -> uncertain ------------------------------------

class _TruncatingStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n'
        raise httpx.ReadError("connection dropped")


async def test_interrupted_stream_marked_uncertain(client):
    cid, key = await make_child("kid1", 5_000_000)
    mock_upstream(lambda req: httpx.Response(
        200, stream=_TruncatingStream(), headers={"content-type": "text/event-stream"}))
    r = await client.post("/v1/chat/completions", headers=auth(key),
                          json=chat_body(content=BIG, stream=True))
    assert r.status_code == 200 and "hel" in r.text
    async with database.session_scope() as s:
        req = (await s.execute(select(RequestRecord).where(RequestRecord.child_id == cid))).scalar_one()
        assert req.status == "uncertain" and req.error
        res = (await s.execute(select(Reservation).where(
            Reservation.request_id == req.id))).scalar_one()
        assert res.status == "active"  # policy 'hold': funds kept for parent review


# --- 12. missing usage never silently free ----------------------------------

async def test_missing_usage_not_zero_billed(client):
    cid, key = await make_child("kid1", 5_000_000)
    data = completion_json(prompt=1000, completion=500)
    del data["usage"]
    mock_upstream(lambda req: httpx.Response(200, json=data))
    r = await client.post("/v1/chat/completions", headers=auth(key), json=chat_body(content=BIG))
    assert r.status_code == 200  # answer delivered...
    assert r.headers["x-kidsproxy-billing"] == "uncertain"  # ...flagged, not free
    avail, resv = await balances(cid)
    assert avail == 5_000_000 - resv and resv > 0  # still reserved, pending review


# --- 13. credit + ledger atomic, idempotent ---------------------------------

async def test_credit_atomic_and_idempotent():
    cid, _ = await make_child("kid1", 0)
    async with database.session_scope() as s:
        after = await billing.add_credit(s, cid, 2_500_000, "parent", "allowance", "idem-1")
    assert after == 2_500_000
    async with database.session_scope() as s:  # duplicate submit = no-op
        again = await billing.add_credit(s, cid, 2_500_000, "parent", "allowance", "idem-1")
    assert again == 2_500_000
    avail, _ = await balances(cid)
    assert avail == 2_500_000
    async with database.session_scope() as s:
        rows = (await s.execute(select(LedgerEntry).where(
            LedgerEntry.idempotency_key == "idem-1"))).scalars().all()
        assert len(rows) == 1


# --- 14. hours + budget policies --------------------------------------------

async def test_hours_window():
    hm = datetime.now(timezone.utc).strftime("%H:%M")
    assert policies.hours_ok("", "UTC") is True
    assert policies.hours_ok("00:00-23:59", "UTC") is True
    assert policies.hours_ok("25:99-26:00", "UTC") is False  # malformed -> deny
    h = int(hm[:2])
    assert policies.hours_ok(f"{(h + 1) % 24:02d}:00-{(h + 2) % 24:02d}:00", "UTC") is False
    assert policies.hours_ok(f"{h:02d}:00-{(h + 1) % 24:02d}:00", "UTC") is True


async def test_daily_limit_enforced(client):
    _, key = await make_child("kid1", 5_000_000)
    async with database.session_scope() as s:
        s.add(Setting(key="daily_limit", value="1000"))  # $0.001
    mock_upstream(lambda req: httpx.Response(200, json=completion_json()))
    r = await client.post("/v1/chat/completions", headers=auth(key), json=chat_body(content=BIG))
    assert r.status_code == 200  # passes: limit checked before spend
    r = await client.post("/v1/chat/completions", headers=auth(key), json=chat_body(content=BIG))
    assert r.status_code == 403
    assert "daily_limit_reached" in r.json()["error"]["code"]


# --- 15. dashboard shows balances and usage ---------------------------------

async def test_dashboard_renders(client):
    cid, key = await make_child("kid1", 5_000_000)
    mock_upstream(lambda req: httpx.Response(200, json=completion_json(prompt=1000, completion=500)))
    await client.post("/v1/chat/completions", headers=auth(key), json=chat_body(content=BIG))

    r = await client.get("/admin/")  # not logged in
    assert r.status_code in (303, 401)
    # GET login page with NO session cookie must render (csrf_token used to crash = 500)
    r = await client.get("/admin/login")
    assert r.status_code == 200

    async with database.session_scope() as s:
        s.add(Setting(key="admin_password_hash", value=hash_password("hunter2")))
    r = await client.post("/admin/login", data={"password": "wrong"}, follow_redirects=False)
    assert r.status_code == 401
    r = await client.post("/admin/login", data={"password": "hunter2"}, follow_redirects=False)
    assert r.status_code == 303

    r = await client.get("/admin/")
    assert r.status_code == 200 and "kid1" in r.text
    assert "$4.998500" in r.text  # available after 1500u spend
    assert "test-model" in r.text  # recent request row
    r = await client.get("/admin/children")
    assert r.status_code == 200 and "kid1" in r.text
    r = await client.get("/admin/data/usage.json")
    assert r.status_code == 200 and sum(r.json()["family"]) > 0

    # credit form without CSRF token -> rejected
    r = await client.post(f"/admin/child/{cid}/credit",
                          data={"dollars": "1", "idem": "x", "csrf": "bogus"})
    assert r.status_code == 403
