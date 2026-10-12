"""Money = integer microdollars (1 USD = 1_000_000). No floats in accounting.

Rounding policy: every derived charge rounds HALF-UP to the nearest microdollar.
Cached-token convention: usage.prompt_tokens_details.cached_tokens are billed at
the cached-input rate; (prompt_tokens - cached_tokens) at the standard input rate.
If upstream omits cached info, cached=0 (standard rate for all input).

All balance mutations are single conditional UPDATE statements — never
read-balance-then-write. Idempotency: settlement only proceeds while the
requests row is still 'pending' (guarded UPDATE), plus a partial unique index
guarantees at most one debit ledger row per request.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import select, text, update

from .auth import now_iso
from .models import Balance, LedgerEntry, Pricing, Reservation, RequestRecord

log = logging.getLogger("kidsproxy.billing")


class NotFunded(Exception):
    pass


class UnknownChild(Exception):
    pass


@dataclass(frozen=True)
class Rates:
    input_per_m: int
    output_per_m: int
    cached_input_per_m: int


def mul_div_round_half_up(amount_micro: int, numerator: int, denominator: int) -> int:
    """(amount * numerator) / denominator rounded half-up, pure integers."""
    q, r = divmod(amount_micro * numerator, denominator)
    if 2 * r >= denominator:
        q += 1
    return q


def estimate_prompt_tokens(body: dict) -> int:
    """Conservative input-token estimate when no upstream tokenizer is available.

    Ceiling assumption: at least 1 token per `chars_per_est` characters plus a
    per-message overhead. Underestimating input would under-reserve, so callers
    pair this with a hard per-request cost cap (max_request_cost): a request
    whose true input exceeds the estimate cannot be billed beyond that cap.
    """
    from .config import get_settings

    s = get_settings()
    chars = 0
    n_msgs = 0
    for m in body.get("messages", []):
        n_msgs += 1
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):  # multimodal parts
            for part in c:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chars += len(part["text"])
            chars += 500  # reserve budget per non-text part (images etc.)
    return max(1, -(-chars // s.chars_per_token_est) + 8 * n_msgs)


def cost_for(rates: Rates, prompt: int, completion: int, cached: int) -> tuple[int, int]:
    """Return (total_micro, overage_note_tokens). cached clamped to prompt."""
    cached = max(0, min(cached, prompt))
    total = (
        mul_div_round_half_up(prompt - cached, rates.input_per_m, 1_000_000)
        + mul_div_round_half_up(cached, rates.cached_input_per_m, 1_000_000)
        + mul_div_round_half_up(completion, rates.output_per_m, 1_000_000)
    )
    return total, 0


def extract_usage(upstream_json: dict) -> dict | None:
    """Normalize usage metadata; return None when it is absent/unusable."""
    u = upstream_json.get("usage")
    if not isinstance(u, dict):
        return None
    p, c = u.get("prompt_tokens"), u.get("completion_tokens")
    if not isinstance(p, int) or not isinstance(c, int) or p < 0 or c < 0:
        return None
    det = u.get("prompt_tokens_details")
    cached = 0
    if isinstance(det, dict) and isinstance(det.get("cached_tokens"), int) and det["cached_tokens"] >= 0:
        cached = det["cached_tokens"]
    return {"prompt_tokens": p, "completion_tokens": c, "cached_tokens": cached}


async def get_rates(session) -> Rates:
    row = await session.get(Pricing, 1)
    if row is None:
        raise RuntimeError("pricing table empty; run init-db")
    return Rates(row.input_per_m, row.output_per_m, row.cached_input_per_m)


async def reserve(
    session, child_id: int, request_id: str, amount_micro: int
) -> None:
    """Atomically move available->reserved. Raises NotFunded if unavailable.
    Same transaction as the caller's RequestRecord insert."""
    if amount_micro <= 0:
        raise ValueError("reservation must be positive")
    res = await session.execute(
        text(
            "UPDATE balances SET reserved_micro = reserved_micro + :amt, "
            "available_micro = available_micro - :amt "
            "WHERE child_id = :cid AND available_micro >= :amt"
        ),
        {"amt": amount_micro, "cid": child_id},
    )
    if res.rowcount != 1:
        raise NotFunded()
    session.add(
        Reservation(
            request_id=request_id,
            child_id=child_id,
            amount_micro=amount_micro,
            status="active",
            created_at=now_iso(),
        )
    )


async def settle(
    session,
    request_id: str,
    child_id: int,
    usage: dict,
    rates: Rates,
) -> RequestRecord:
    """Debit actual charge + release reservation + ledger + status, atomically.

    Idempotent: the guarded UPDATE on requests.status='pending' means a retried
    settle is a no-op (returns the already-settled row). Ownership is enforced
    by the child_id predicate: kid1's settle can never touch kid2's request.
    """
    actual, _ = cost_for(rates, usage["prompt_tokens"], usage["completion_tokens"], usage["cached_tokens"])
    req = await session.get(RequestRecord, request_id)
    if req is None or req.child_id != child_id:
        raise UnknownChild(request_id)
    if req.status == "completed":
        return req  # already settled (duplicate settlement attempt)
    if req.status != "pending":
        raise RuntimeError(f"request {request_id} is {req.status}, cannot settle")

    res = await session.execute(
        update(RequestRecord)
        .where(RequestRecord.id == request_id, RequestRecord.status == "pending")
        .values(
            status="completed",
            settled_at=now_iso(),
            charge_micro=actual,
            overage_micro=max(0, actual - req.reserved_micro),
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            cached_tokens=usage["cached_tokens"],
            price_input_per_m=rates.input_per_m,
            price_output_per_m=rates.output_per_m,
            price_cached_input_per_m=rates.cached_input_per_m,
        )
    )
    if res.rowcount != 1:
        # Lost a race with a concurrent settle of the same request.
        return await session.get(RequestRecord, request_id)

    # Reservation already sits OUT of available. Return the unspent remainder
    # and consume `actual` from reserved: available += (reserved - actual).
    # If actual exceeds reserved (max_request_cost should prevent this),
    # available goes negative — recorded as overage, caught at next admission.
    bal = (
        await session.execute(
            text(
                "UPDATE balances SET available_micro = available_micro + :delta, "
                "reserved_micro = reserved_micro - :resv "
                "WHERE child_id = :cid AND reserved_micro >= :resv "
                "RETURNING available_micro"
            ),
            {"delta": req.reserved_micro - actual, "resv": req.reserved_micro, "cid": child_id},
        )
    ).first()
    if bal is None:
        raise RuntimeError(f"reservation missing/foreign for request {request_id}")
    balance_after = bal[0]

    await session.execute(
        update(Reservation)
        .where(Reservation.request_id == request_id, Reservation.status == "active")
        .values(status="settled", resolved_at=now_iso())
    )
    session.add(
        LedgerEntry(
            child_id=child_id,
            request_id=request_id,
            type="debit",
            amount_micro=-actual,
            balance_after=balance_after,
            reason=f"model={req.model}",
            created_at=now_iso(),
        )
    )
    return req


async def mark_uncertain(session, request_id: str, error: str) -> None:
    """Usage unrecoverable. Policy 'hold' keeps funds reserved for parent review;
    'release' refunds them. Never bills a fabricated amount, never silently zeroes."""
    req = await session.get(RequestRecord, request_id)
    if req is None or req.status != "pending":
        return
    from .config import get_settings

    policy = await get_setting(session, "uncertain_policy", get_settings().uncertain_policy)
    await session.execute(
        update(RequestRecord)
        .where(RequestRecord.id == request_id, RequestRecord.status == "pending")
        .values(status="uncertain", settled_at=now_iso(), error=error[:2000])
    )
    if policy == "release":
        await release_reservation(session, request_id, reason="uncertain:released")


async def release_reservation(session, request_id: str, reason: str = "") -> None:
    """Return reserved funds to available; close reservation row."""
    r = (
        await session.execute(
            select(Reservation).where(
                Reservation.request_id == request_id, Reservation.status == "active"
            )
        )
    ).scalar_one_or_none()
    if r is None:
        return  # already settled/released — idempotent no-op
    await session.execute(
        update(Reservation)
        .where(Reservation.id == r.id, Reservation.status == "active")
        .values(status="released", resolved_at=now_iso())
    )
    await session.execute(
        text(
            "UPDATE balances SET reserved_micro = reserved_micro - :amt, "
            "available_micro = available_micro + :amt WHERE child_id = :cid"
        ),
        {"amt": r.amount_micro, "cid": r.child_id},
    )
    if reason:
        log.info("released reservation req=%s child=%s amt=%d (%s)", request_id, r.child_id, r.amount_micro, reason)


async def add_credit(
    session, child_id: int, amount_micro: int, admin: str, reason: str, idempotency_key: str
) -> int:
    """Manual credit/refund with duplicate-submission protection.
    Returns balance_after. Duplicate idempotency_key -> existing balance (no-op)."""
    from sqlalchemy.exc import IntegrityError

    try:
        async with session.begin_nested():
            bal = (
                await session.execute(
                    text(
                        "UPDATE balances SET available_micro = available_micro + :amt "
                        "WHERE child_id = :cid RETURNING available_micro"
                    ),
                    {"amt": amount_micro, "cid": child_id},
                )
            ).first()
            if bal is None:
                raise UnknownChild(str(child_id))
            session.add(
                LedgerEntry(
                    child_id=child_id,
                    type="credit" if amount_micro >= 0 else "adjustment",
                    amount_micro=amount_micro,
                    balance_after=bal[0],
                    admin=admin,
                    reason=reason,
                    idempotency_key=idempotency_key,
                    created_at=now_iso(),
                )
            )
        return bal[0]
    except IntegrityError:
        cur = await session.execute(
            select(LedgerEntry.balance_after).where(LedgerEntry.idempotency_key == idempotency_key)
        )
        row = cur.first()
        if row is None:
            raise
        return row[0]


async def get_setting(session, key: str, default: str | None = None) -> str | None:
    from .models import Setting

    row = await session.get(Setting, key)
    return row.value if row else default


async def sweep_stale(session, grace_seconds: int) -> int:
    """Crash recovery: 'pending' requests older than grace had their in-flight
    coroutine die with the process. Their true upstream cost is unknown: mark
    uncertain (never assume zero). Reservation follows uncertain_policy."""
    from datetime import datetime, timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=grace_seconds)).isoformat(timespec="seconds")
    ids = (
        await session.execute(
            select(RequestRecord.id).where(
                RequestRecord.status == "pending", RequestRecord.created_at < cutoff
            )
        )
    ).scalars().all()
    for rid in ids:
        await mark_uncertain(session, rid, "stale after process restart/interruption; true cost unknown")
    return len(ids)
