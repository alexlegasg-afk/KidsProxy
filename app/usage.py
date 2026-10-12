"""Public (ungated) usage board: per-child credits + token usage by period.

Shows which kids used what; no secrets, so no password. Read-only aggregation
over requests/balances. Period: today | week | month | all | custom (start/end).
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from .config import get_settings
from .database import session_scope
from .models import Balance, Child, RequestRecord

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

PERIODS = ("today", "week", "month", "all")


def usd(micro: int) -> str:
    return f"${micro / 1_000_000:,.4f}"


@router.get("/usage")
async def usage(request: Request):
    s = get_settings()
    tz = ZoneInfo(s.tz)
    now = datetime.now(tz)

    period = request.query_params.get("period", "today")
    start = request.query_params.get("start", "")
    end = request.query_params.get("end", "")

    start_dt = end_dt = None
    if period == "today":
        start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start_dt = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        start_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "all":
        pass
    else:  # custom
        period = "custom"
        try:
            start_dt = datetime.fromisoformat(start).replace(tzinfo=tz) if start else None
            end_dt = datetime.fromisoformat(end).replace(tzinfo=tz, hour=23, minute=59, second=59) if end else None
        except ValueError:
            start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
            period = "today"

    def iso(dt: datetime | None) -> str | None:
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None

    async with session_scope() as sess:
        children = (await sess.execute(select(Child).order_by(Child.slug))).scalars().all()
        balances = {b.child_id: b for b in (await sess.execute(select(Balance))).scalars()}

        q = select(
            RequestRecord.child_id,
            func.coalesce(func.sum(RequestRecord.prompt_tokens), 0),
            func.coalesce(func.sum(RequestRecord.completion_tokens), 0),
            func.coalesce(func.sum(RequestRecord.cached_tokens), 0),
            func.coalesce(func.sum(RequestRecord.charge_micro), 0),
            func.count(),
        ).where(RequestRecord.status == "completed").group_by(RequestRecord.child_id)
        if start_dt:
            q = q.where(RequestRecord.created_at >= iso(start_dt))
        if end_dt:
            q = q.where(RequestRecord.created_at <= iso(end_dt))
        agg = {r[0]: r for r in (await sess.execute(q))}

    rows = []
    for c in children:
        a = agg.get(c.id)
        bal = balances.get(c.id)
        rows.append({
            "name": c.name or c.slug,
            "slug": c.slug,
            "active": bool(c.active),
            "credits": usd(bal.available_micro if bal else 0),
            "low": (bal.available_micro if bal else 0) < 500_000,
            "in": (a[1] if a else 0) - (a[3] if a else 0),  # fresh input = prompt - cached
            "cached": a[3] if a else 0,
            "out": a[2] if a else 0,
            "cost": usd(a[4] if a else 0),
            "reqs": a[5] if a else 0,
        })

    return templates.TemplateResponse(request, "usage.html", {
        "rows": rows, "period": period, "start": start, "end": end,
        "range_label": _label(period, start_dt, end_dt, tz),
        "generated": now.strftime("%H:%M:%S"),
    })


def _label(period: str, start_dt, end_dt, tz) -> str:
    if period == "all":
        return "all time"
    fmt = "%b %d, %Y"
    if end_dt:
        return f"{start_dt.strftime(fmt) if start_dt else '…'} → {end_dt.strftime(fmt)}"
    return f"{start_dt.astimezone(tz).strftime(fmt)} (today)" if period == "today" \
        else start_dt.astimezone(tz).strftime(fmt)
