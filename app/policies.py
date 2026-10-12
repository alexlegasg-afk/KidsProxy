"""Admission policy: model, hours, budgets, concurrency, balance.

Effective value precedence: per-child override (children.* columns) > global
DB `settings` row > env default. Budget checks sum SETTLED debits only;
in-flight reservations are enforced through the reserve() funding check and
the concurrency limit, so a pending request never double-counts spend.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from .billing import Rates, estimate_prompt_tokens, mul_div_round_half_up
from .models import Child, LedgerEntry, Reservation, Setting


@dataclass(frozen=True)
class Policy:
    min_start_balance: int
    max_output_tokens: int
    max_request_cost: int
    daily_limit: int          # 0 = unlimited
    monthly_limit: int        # 0 = unlimited
    allowed_hours: str        # "" = any; "HH:MM-HH:MM" local tz
    allowed_models: tuple[str, ...]  # () = all
    max_concurrent: int       # 0 = unlimited
    tz: str


async def load_settings_map(session) -> dict[str, str]:
    rows = await session.execute(select(Setting.key, Setting.value))
    return {k: v for k, v in rows.all()}


def effective_policy(smap: dict[str, str], child: Child) -> Policy:
    def num(key: str, default: int) -> int:
        return int(smap.get(key, default))

    hours = child.allowed_hours if child.allowed_hours is not None else smap.get("allowed_hours", "")
    models_raw = child.allowed_models if child.allowed_models is not None else smap.get("allowed_models", "")
    models = tuple(m.strip() for m in models_raw.split(",") if m.strip())
    return Policy(
        min_start_balance=num("min_start_balance", 1_000_000),
        max_output_tokens=num("max_output_tokens", 4096),
        max_request_cost=num("max_request_cost", 250_000),
        daily_limit=child.daily_limit if child.daily_limit is not None else num("daily_limit", 0),
        monthly_limit=child.monthly_limit if child.monthly_limit is not None else num("monthly_limit", 0),
        allowed_hours=hours or "",
        allowed_models=models,
        max_concurrent=num("max_concurrent_per_child", 2),
        tz=smap.get("tz", "UTC"),
    )


def hours_ok(window: str, tz: str) -> bool:
    if not window:
        return True
    local = datetime.now(ZoneInfo(tz))
    try:
        spans = []
        for part in window.split(","):
            a, b = part.strip().split("-")
            spans.append((a.strip(), b.strip()))
    except ValueError:
        return False  # malformed window = deny (fail closed)
    hm = local.strftime("%H:%M")
    for a, b in spans:
        if a <= b:
            if a <= hm < b:
                return True
        elif hm >= a or hm < b:  # overnight window
            return True
    return False


async def spent_since(session, child_id: int, since_iso: str) -> int:
    """Total settled debit (microdollars) since timestamp."""
    total = await session.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount_micro), 0)).where(
            LedgerEntry.child_id == child_id,
            LedgerEntry.type == "debit",
            LedgerEntry.created_at >= since_iso,
        )
    )
    return -total.scalar_one()


async def active_reservation_count(session, child_id: int) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(Reservation).where(
                Reservation.child_id == child_id, Reservation.status == "active"
            )
        )
    ).scalar_one()


def reservation_amount(body: dict, rates: Rates, policy: Policy) -> int:
    """Conservative upper bound on this request's permitted charge.

    input = char estimate (see estimate_prompt_tokens), output = client
    max_tokens clamped to policy.max_output_tokens. The reservation equals the
    maximum permitted charge, so settlement can never exceed it; if the true
    input exceeds the estimate, billing is still capped at max_request_cost.
    """
    est_in = estimate_prompt_tokens(body)
    try:
        out = max(0, min(int(body.get("max_tokens") or body.get("max_completion_tokens")
                              or policy.max_output_tokens), policy.max_output_tokens))
    except (TypeError, ValueError):
        out = policy.max_output_tokens
    full = (
        mul_div_round_half_up(est_in, rates.input_per_m, 1_000_000)
        + mul_div_round_half_up(out, rates.output_per_m, 1_000_000)
    )
    return max(1, min(full, policy.max_request_cost))


async def check_admission(session, child: Child, body: dict, rates: Rates) -> tuple[Policy, int] | str:
    """Return (policy, reservation_micro) or an error string with code+message.

    Error strings: "code|message". Caller maps to HTTP JSON error."""
    smap = await load_settings_map(session)
    policy = effective_policy(smap, child)

    model = body.get("model", "")
    if not isinstance(model, str) or not model:
        return "invalid_request|Missing model"
    if policy.allowed_models and model not in policy.allowed_models:
        return f"model_not_allowed|Model '{model}' is not permitted"
    if not hours_ok(policy.allowed_hours, policy.tz):
        return "outside_allowed_hours|Requests are only allowed within permitted hours"

    if policy.max_concurrent > 0:
        n = await active_reservation_count(session, child.id)
        if n >= policy.max_concurrent:
            return "concurrency_limit|Too many simultaneous requests for this account"

    now = datetime.now(timezone.utc)
    if policy.daily_limit > 0:
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        if await spent_since(session, child.id, day_start) >= policy.daily_limit:
            return "daily_limit_reached|Daily spending limit reached; ask a parent"
    if policy.monthly_limit > 0:
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        if await spent_since(session, child.id, month_start) >= policy.monthly_limit:
            return "monthly_limit_reached|Monthly spending limit reached; ask a parent"

    from .models import Balance

    bal = await session.get(Balance, child.id)
    available = bal.available_micro if bal else 0
    if available < policy.min_start_balance:
        return "insufficient_balance|Balance is below the required minimum to start a request. Ask a parent to add credits."

    reserve_amt = reservation_amount(body, rates, policy)
    if available < reserve_amt:
        return "insufficient_balance|Available balance cannot cover this request's maximum cost. Ask a parent to add credits."
    return policy, reserve_amt
