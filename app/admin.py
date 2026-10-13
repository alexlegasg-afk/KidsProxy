"""Parent dashboard: session auth, CSRF, children/credits/pricing/policies.

Separate trust domain from child API keys: cookie session (itsdangerous
signed), never accepted on /v1/*. All money forms carry an idempotency key
so a double-submit cannot double-credit.
"""

import asyncio
import hmac
import secrets
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import delete, func, select, text

from . import billing
from .auth import create_child, hash_password, rotate_key, verify_password
from .config import get_settings
from .database import session_scope
from .models import ApiKey, Balance, Child, LedgerEntry, Pricing, RequestRecord, Reservation, Setting

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

COOKIE = "kp_session"
FAILED_LOGINS: dict[str, list[float]] = {}  # ip -> failure timestamps (in-proc throttle)


def _serializer() -> URLSafeSerializer:
    secret = get_settings().session_secret
    if not secret:
        raise RuntimeError("KP_SESSION_SECRET unset; run `uv run python -m app.cli init-db`")
    return URLSafeSerializer(secret, salt="kidsproxy-session")


def current_user(request: Request) -> str | None:
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    try:
        return _serializer().loads(raw).get("admin")
    except BadSignature:
        return None


async def require_admin(request: Request) -> str:
    user = current_user(request)
    if user != "parent":
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})
    return user


def _tpl(request: Request, name: str, ctx: dict | None = None, status: int = 200):
    base = {"request": request, "csrf": csrf_token(request)}
    if ctx:
        base.update(ctx)
    return templates.TemplateResponse(request, name, base, status_code=status)


def csrf_token(request: Request) -> str:
    raw = request.cookies.get(COOKIE)
    if not raw:
        return ""
    try:
        return _serializer().loads(raw).get("csrf", "")
    except (BadSignature, ValueError):
        return ""


def check_csrf(request: Request, token: str) -> None:
    expected = csrf_token(request)
    if not expected or not hmac.compare_digest(expected, token or ""):
        raise HTTPException(status_code=403, detail="CSRF check failed. Reload the page and try again.")



def usd(micro: int) -> str:
    sign = "-" if micro < 0 else ""
    micro = abs(micro)
    return f"{sign}${micro // 1_000_000}.{micro % 1_000_000:06d}"


templates.env.filters["usd"] = usd


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return _tpl(request, "login.html", {"show_nav": False})


@router.post("/login")
async def login(request: Request, password: str = Form(...)):
    ip = request.client.host if request.client else "?"
    now = time.monotonic()
    fails = [t for t in FAILED_LOGINS.get(ip, []) if now - t < 300]
    FAILED_LOGINS[ip] = fails
    if len(fails) >= 8:
        return templates.TemplateResponse(
            request, "login.html",
            {"request": request, "error": "Too many failed attempts. Wait 5 minutes.", "show_nav": False},
            status_code=429,
        )
    ok = False
    async with session_scope() as s:
        stored = await billing.get_setting(s, "admin_password_hash")
    if stored and verify_password(password, stored):
        ok = True
    if not ok:
        FAILED_LOGINS[ip].append(now)
        await asyncio.sleep(0.5)  # slow brute force
        return templates.TemplateResponse(
            request, "login.html",
            {"request": request, "error": "Wrong password", "show_nav": False},
            status_code=401,
        )

    csrf = secrets.token_urlsafe(32)
    resp = RedirectResponse("/admin/", status_code=303)
    resp.set_cookie(
        COOKIE, _serializer().dumps({"admin": "parent", "csrf": csrf}),
        httponly=True, samesite="lax", secure=get_settings().cookie_secure,
        max_age=8 * 3600, path="/",
    )
    FAILED_LOGINS.pop(ip, None)
    return resp


@router.post("/logout")
async def logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return resp


async def _overview_data(s):
    children = (await s.execute(select(Child).order_by(Child.id))).scalars().all()
    balances = {b.child_id: b for b in (await s.execute(select(Balance))).scalars().all()}
    rates = await billing.get_rates(s)
    smap = {k: v for k, v in (await s.execute(select(Setting.key, Setting.value))).all()}
    tz = ZoneInfo(smap.get("tz", "UTC"))

    per_child = []
    for c in children:
        bal = balances.get(c.id)
        avail = bal.available_micro if bal else 0
        resv = bal.reserved_micro if bal else 0
        today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        month = today.replace(day=1)
        day_spent = -((await s.execute(
            select(func.coalesce(func.sum(LedgerEntry.amount_micro), 0)).where(
                LedgerEntry.child_id == c.id, LedgerEntry.type == "debit",
                LedgerEntry.created_at >= today.isoformat(timespec="seconds")))).scalar_one())
        month_spent = -((await s.execute(
            select(func.coalesce(func.sum(LedgerEntry.amount_micro), 0)).where(
                LedgerEntry.child_id == c.id, LedgerEntry.type == "debit",
                LedgerEntry.created_at >= month.isoformat(timespec="seconds")))).scalar_one())
        per_child.append({"c": c, "avail": avail, "resv": resv,
                          "day": day_spent, "month": month_spent})

    recent = (await s.execute(
        select(RequestRecord).order_by(RequestRecord.created_at.desc()).limit(25)
    )).scalars().all()
    names = {c.id: c.name for c in children}

    # daily family spend, last 14 days (all children)
    since = (datetime.now(tz) - timedelta(days=13)).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (await s.execute(text(
        "SELECT substr(created_at,1,10) d, -SUM(amount_micro) FROM ledger "
        "WHERE type='debit' AND created_at >= :since GROUP BY d ORDER BY d"
    ), {"since": since.astimezone(timezone.utc).isoformat(timespec="seconds")})).all()
    by_day = dict(rows)
    days = []
    cur = since
    while cur <= datetime.now(tz):
        d = cur.strftime("%Y-%m-%d")
        days.append({"d": d, "micro": int(by_day.get(d, 0))})
        cur += timedelta(days=1)
    return {"children": per_child, "recent": recent, "names": names,
            "rates": rates, "days": days, "smap": smap}


@router.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    user = current_user(request)
    if user != "parent":
        return RedirectResponse("/admin/login", status_code=303)
    async with session_scope() as s:
        data = await _overview_data(s)
    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request, "csrf": csrf_token(request), "usd": usd, **data})


@router.get("/children", response_class=HTMLResponse)
async def children_page(request: Request):
    user = current_user(request)
    if user != "parent":
        return RedirectResponse("/admin/login", status_code=303)
    async with session_scope() as s:
        children = (await s.execute(select(Child).order_by(Child.id))).scalars().all()
        balances = {b.child_id: b for b in (await s.execute(select(Balance))).scalars().all()}
        keys = {}
        for k in (await s.execute(select(ApiKey).order_by(ApiKey.id))).scalars().all():
            keys.setdefault(k.child_id, []).append(k)
    rows = [{"c": c, "bal": balances.get(c.id), "api_keys": keys.get(c.id, []),
             "idem": secrets.token_urlsafe(12)} for c in children]
    return templates.TemplateResponse(request, "children.html", {
        "request": request, "csrf": csrf_token(request), "rows": rows, "usd": usd,
        "newkey": request.query_params.get("newkey", "")})


@router.post("/children/create")
async def child_create(request: Request, slug: str = Form(...), name: str = Form(""), csrf: str = Form("")):
    await require_admin(request)
    check_csrf(request, csrf)
    if not re.fullmatch(r"[a-z0-9]{1,20}", slug):
        return HTMLResponse("Slug must be lowercase letters/digits, max 20.", status_code=400)
    async with session_scope() as s:
        if (await s.execute(select(Child).where(Child.slug == slug))).scalar_one_or_none():
            return HTMLResponse("Child already exists.", status_code=409)
        await create_child(s, slug, name.strip() or slug)
    return RedirectResponse(f"/admin/children?created={slug}", status_code=303)

@router.post("/child/{child_id}/delete")
async def child_delete(request: Request, child_id: int, csrf: str = Form(...), confirm: str = Form(...)):
    await require_admin(request)
    check_csrf(request, csrf)
    async with session_scope() as s:
        c = await s.get(Child, child_id)
        if c is None:
            return HTMLResponse("No such child", status_code=404)
        if confirm != c.slug:
            return HTMLResponse("Type the child's slug exactly to confirm deletion.", status_code=400)
        slug = c.slug
        # No FK constraints exist; purge the child's rows in one transaction.
        for tbl in (ApiKey, Balance, RequestRecord, Reservation, LedgerEntry):
            await s.execute(delete(tbl).where(tbl.child_id == child_id))
        await s.delete(c)
    return RedirectResponse(f"/admin/children?deleted={slug}", status_code=303)

@router.post("/child/{child_id}/key")
async def child_key(request: Request, child_id: int, csrf: str = Form(...)):
    await require_admin(request)
    check_csrf(request, csrf)
    async with session_scope() as s:
        c = await s.get(Child, child_id)
        if c is None:
            return HTMLResponse("No such child", status_code=404)
        full, _ = await rotate_key(s, c.id, c.slug)
    return RedirectResponse(f"/admin/children?newkey={full}", status_code=303)


@router.post("/child/{child_id}/revoke/{key_id}")
async def key_revoke(request: Request, child_id: int, key_id: int, csrf: str = Form(...)):
    await require_admin(request)
    check_csrf(request, csrf)
    async with session_scope() as s:
        await s.execute(
            text("UPDATE api_keys SET active=0, revoked_at=:t WHERE id=:k AND child_id=:c"),
            {"t": billing.now_iso(), "k": key_id, "c": child_id},
        )
    return RedirectResponse(f"/admin/children#{child_id}", status_code=303)


@router.post("/child/{child_id}/active")
async def child_active(request: Request, child_id: int, csrf: str = Form(...), active: int = Form(...)):
    await require_admin(request)
    check_csrf(request, csrf)
    async with session_scope() as s:
        await s.execute(text("UPDATE children SET active=:a WHERE id=:c"),
                        {"a": 1 if active else 0, "c": child_id})
    return RedirectResponse("/admin/children", status_code=303)


@router.post("/child/{child_id}/credit")
async def child_credit(request: Request, child_id: int, csrf: str = Form(...),
                       dollars: str = Form(...), reason: str = Form(""),
                       idem: str = Form(...)):
    await require_admin(request)
    check_csrf(request, csrf)
    try:
        micro = round(float(dollars) * 1_000_000)
    except ValueError:
        return HTMLResponse("Enter a dollar amount like 5.00", status_code=400)
    if micro == 0:
        return HTMLResponse("Amount is zero.", status_code=400)
    if not idem:
        idem = secrets.token_urlsafe(16)
    async with session_scope() as s:
        try:
            after = await billing.add_credit(s, child_id, micro, "parent",
                                             reason.strip() or "manual credit", idem)
        except billing.UnknownChild:
            return HTMLResponse("No such child", status_code=404)
    return RedirectResponse(f"/admin/children?credited={usd(micro)}#child-{child_id}", status_code=303)


@router.post("/settings")
async def settings_save(request: Request):
    await require_admin(request)
    form = await request.form()
    check_csrf(request, form.get("csrf", ""))
    int_keys = {
        "min_start_balance_usd": ("min_start_balance", 1_000_000),
        "max_request_cost_usd": ("max_request_cost", 250_000),
        "daily_limit_usd": ("daily_limit", 0),
        "monthly_limit_usd": ("monthly_limit", 0),
    }
    plain = {"max_output_tokens": lambda v: str(int(v)),
             "allowed_hours": str, "allowed_models": str, "tz": str}
    async with session_scope() as s:
        for field, (key, scale) in int_keys.items():
            v = form.get(field, "")
            if v == "":
                await s.execute(text("INSERT INTO settings(key,value) VALUES(:k,'0') "
                                     "ON CONFLICT(key) DO UPDATE SET value='0'"), {"k": key})
                continue
            await s.execute(
                text("INSERT INTO settings(key,value) VALUES(:k,:v) ON CONFLICT(key) DO UPDATE SET value=:v"),
                {"k": key, "v": str(round(float(v) * scale))},
            )
        for field, conv in plain.items():
            if field in form:
                await s.execute(
                    text("INSERT INTO settings(key,value) VALUES(:k,:v) ON CONFLICT(key) DO UPDATE SET value=:v"),
                    {"k": field, "v": conv(form[field])},
                )
        await _save_pricing(s, form)
    return RedirectResponse("/admin/settings?saved=1", status_code=303)


async def _save_pricing(s, form) -> None:
    vals = {}
    for field, key in (("price_input_usd", "input_per_m"),
                       ("price_output_usd", "output_per_m"),
                       ("price_cached_usd", "cached_input_per_m")):
        if field in form:
            vals[key] = round(float(form[field]) * 1_000_000)
    if not vals:
        return
    cur = (await s.execute(select(Pricing).where(Pricing.id == 1))).scalar_one_or_none()
    if cur is None:
        s.add(Pricing(id=1, updated_at=billing.now_iso(), **vals))
    else:
        for k, v in vals.items():
            setattr(cur, k, v)
        cur.updated_at = billing.now_iso()


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = current_user(request)
    if user != "parent":
        return RedirectResponse("/admin/login", status_code=303)
    async with session_scope() as s:
        rates = await billing.get_rates(s)
        smap = {k: v for k, v in (await s.execute(select(Setting.key, Setting.value))).all()}

    def usd_in(key, default):
        return f"{int(smap.get(key, default)) / 1_000_000:.6f}"

    return templates.TemplateResponse(request, "settings.html", {
        "request": request, "csrf": csrf_token(request),
        "price_input": f"{rates.input_per_m / 1_000_000:.6f}",
        "price_output": f"{rates.output_per_m / 1_000_000:.6f}",
        "price_cached": f"{rates.cached_input_per_m / 1_000_000:.6f}",
        "min_start": usd_in("min_start_balance", 1_000_000),
        "max_cost": usd_in("max_request_cost", 250_000),
        "daily": usd_in("daily_limit", 0),
        "monthly": usd_in("monthly_limit", 0),
        "max_output": smap.get("max_output_tokens", "4096"),
        "allowed_hours": smap.get("allowed_hours", ""),
        "allowed_models": smap.get("allowed_models", ""),
        "tz": smap.get("tz", "UTC"),
    })


@router.get("/data/usage.json")
async def usage_json(request: Request):
    user = current_user(request)
    if user != "parent":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    async with session_scope() as s:
        data = await _overview_data(s)
    return JSONResponse({
        "days": [d["d"] for d in data["days"]],
        "family": [d["micro"] / 1_000_000 for d in data["days"]],
    })
