"""Operator CLI: init-db, admin password, children, credits.

Run with: uv run python -m app.cli <command> ...
"""

import argparse
import asyncio
import os
import secrets
import sys

from sqlalchemy import select, text, update

from . import auth as A
from .config import get_settings
from .database import dispose_engine, init_engine, session_scope
from .models import Balance, Child, Pricing, RequestRecord, Setting


async def _init_db() -> None:
    from .models import Base

    engine = init_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    s = get_settings()
    async with session_scope() as sess:
        if await sess.get(Pricing, 1) is None:
            sess.add(Pricing(
                id=1, input_per_m=s.price_input_per_m, output_per_m=s.price_output_per_m,
                cached_input_per_m=s.price_cached_input_per_m, updated_at=A.now_iso(),
            ))
        existing = {k for k, in (await sess.execute(select(Setting.key))).all()}

        def seed(key: str, value) -> None:
            if key not in existing:
                sess.add(Setting(key=key, value=str(value)))

        seed("min_start_balance", s.min_start_balance)
        seed("max_output_tokens", s.max_output_tokens)
        seed("max_request_cost", s.max_request_cost)
        seed("daily_limit", s.daily_limit)
        seed("monthly_limit", s.monthly_limit)
        seed("allowed_hours", s.allowed_hours)
        seed("tz", s.tz)
        seed("allowed_models", s.allowed_models)
        seed("max_concurrent_per_child", s.max_concurrent_per_child)
        seed("uncertain_policy", s.uncertain_policy)
    await dispose_engine()


async def _set_admin(password: str) -> None:
    init_engine()
    async with session_scope() as sess:
        await sess.execute(
            text("INSERT INTO settings(key,value) VALUES('admin_password_hash',:v) "
                 "ON CONFLICT(key) DO UPDATE SET value=:v"),
            {"v": A.hash_password(password)},
        )
    await dispose_engine()


async def _ensure_secret() -> None:
    """Generate KP_SESSION_SECRET into .env if missing (0600)."""
    if get_settings().session_secret:
        return
    secret = secrets.token_urlsafe(48)
    with open(".env", "a") as f:
        f.write(f"KP_SESSION_SECRET={secret}\n")
    os.chmod(".env", 0o600)
    get_settings.cache_clear()
    print(f"generated KP_SESSION_SECRET into .env")


async def _add_child(slug: str, name: str, credit_usd: float) -> None:
    init_engine()
    async with session_scope() as sess:
        if (await sess.execute(select(Child).where(Child.slug == slug))).scalar_one_or_none():
            print(f"child '{slug}' already exists", file=sys.stderr)
            sys.exit(1)
        child = await A.create_child(sess, slug, name)
        full, key = await A.rotate_key(sess, child.id, slug)
        micro = round(credit_usd * 1_000_000)
        if micro:
            from . import billing

            await billing.add_credit(sess, child.id, micro, "cli-init", "initial credit",
                                     f"init:{slug}")
        print("\n=== CHILD CREATED ===")
        print(f"slug: {slug}")
        print(f"API key (shown ONCE, store it now): {full}")
    await dispose_engine()


async def _rotate(slug: str) -> None:
    init_engine()
    async with session_scope() as sess:
        c = (await sess.execute(select(Child).where(Child.slug == slug))).scalar_one_or_none()
        if c is None:
            print(f"no child '{slug}'", file=sys.stderr)
            sys.exit(1)
        full, _ = await A.rotate_key(sess, c.id, slug)
        print(f"new key for {slug} (shown ONCE): {full}")
    await dispose_engine()


async def _credit(slug: str, dollars: float, reason: str) -> None:
    init_engine()
    async with session_scope() as sess:
        c = (await sess.execute(select(Child).where(Child.slug == slug))).scalar_one_or_none()
        if c is None:
            print(f"no child '{slug}'", file=sys.stderr)
            sys.exit(1)
        from . import billing

        after = await billing.add_credit(sess, c.id, round(dollars * 1_000_000), "cli", reason,
                                         f"cli:{A.now_iso()}:{slug}:{secrets.token_hex(4)}")
        print(f"{slug} balance now ${after / 1_000_000:.6f}")
    await dispose_engine()



async def _release_uncertain(slug: str | None) -> None:
    from . import billing
    init_engine()
    async with session_scope() as sess:
        q = select(RequestRecord).where(RequestRecord.status == "uncertain")
        if slug:
            c = (await sess.execute(select(Child).where(Child.slug == slug))).scalar_one_or_none()
            if c is None:
                print(f"no child '{slug}'", file=sys.stderr)
                sys.exit(1)
            q = q.where(RequestRecord.child_id == c.id)
        rows = (await sess.execute(q)).scalars().all()
        if not rows:
            print("no uncertain requests")
        for rec in rows:
            await billing.release_reservation(
                sess, rec.request_id, reason="parent released uncertain via cli")
            await sess.execute(
                update(RequestRecord).where(RequestRecord.id == rec.id)
                .values(status="released"))
            print(f"released {rec.request_id} (child={rec.child_id})")
    await dispose_engine()

async def _status() -> None:
    init_engine()
    async with session_scope() as sess:
        for c in (await sess.execute(select(Child).order_by(Child.id))).scalars().all():
            b = await sess.get(Balance, c.id)
            print(f"{c.slug:12} active={c.active} available=${(b.available_micro if b else 0)/1e6:.6f} "
                  f"reserved=${(b.reserved_micro if b else 0)/1e6:.6f}")
    await dispose_engine()


def main() -> None:
    p = argparse.ArgumentParser(prog="app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    sp = sub.add_parser("set-admin-password")
    sp.add_argument("--password", default=None)
    sp = sub.add_parser("add-child")
    sp.add_argument("slug")
    sp.add_argument("--name", default=None)
    sp.add_argument("--credit", type=float, default=5.0)
    sp = sub.add_parser("rotate-key")
    sp.add_argument("slug")
    sp = sub.add_parser("credit")
    sp.add_argument("slug")
    sp.add_argument("dollars", type=float)
    sp.add_argument("--reason", default="manual credit")
    sp = sub.add_parser("release-uncertain")
    sp.add_argument("slug", nargs="?", default=None)
    sub.add_parser("status")

    args = p.parse_args()
    if args.cmd == "init-db":
        asyncio.run(_ensure_secret())
        asyncio.run(_init_db())
        print(f"database ready at {os.path.abspath(get_settings().db_path)}")
    elif args.cmd == "set-admin-password":
        pw = args.password or os.environ.get("KP_ADMIN_PASSWORD")
        if not pw:
            import getpass

            pw = getpass.getpass("admin password: ")
        asyncio.run(_set_admin(pw))
        print("admin password set")
    elif args.cmd == "add-child":
        asyncio.run(_add_child(args.slug, args.name or args.slug, args.credit))
    elif args.cmd == "rotate-key":
        asyncio.run(_rotate(args.slug))
    elif args.cmd == "credit":
        asyncio.run(_credit(args.slug, args.dollars, args.reason))
    elif args.cmd == "status":
        asyncio.run(_status())
    elif args.cmd == "release-uncertain":
        asyncio.run(_release_uncertain(args.slug))


if __name__ == "__main__":
    main()
