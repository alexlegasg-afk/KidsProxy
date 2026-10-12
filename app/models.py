from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import BigInteger, Index, String, Text


class Base(DeclarativeBase):
    pass


# INTEGER in SQLite is 64-bit; BigInteger needs the variant for strict DDL.
Int64 = BigInteger().with_variant(BigInteger, "sqlite")


class Child(Base):
    __tablename__ = "children"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(32), unique=True)  # e.g. "kid1"
    name: Mapped[str] = mapped_column(String(64))
    active: Mapped[int] = mapped_column(default=1)  # 0/1
    created_at: Mapped[str] = mapped_column(String(32))
    # Per-child policy overrides; NULL/empty = inherit global setting.
    daily_limit: Mapped[int | None] = mapped_column(Int64)
    monthly_limit: Mapped[int | None] = mapped_column(Int64)
    allowed_hours: Mapped[str | None] = mapped_column(String(32))
    allowed_models: Mapped[str | None] = mapped_column(Text)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[int] = mapped_column(primary_key=True)
    child_id: Mapped[int] = mapped_column(index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 hex
    prefix: Mapped[str] = mapped_column(String(48))  # display, e.g. "sk-kid1-Ab3..."
    active: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[str] = mapped_column(String(32))
    revoked_at: Mapped[str | None] = mapped_column(String(32))


class Balance(Base):
    __tablename__ = "balances"
    child_id: Mapped[int] = mapped_column(primary_key=True)
    available_micro: Mapped[int] = mapped_column(Int64, default=0)
    reserved_micro: Mapped[int] = mapped_column(Int64, default=0)


class Pricing(Base):
    __tablename__ = "pricing"
    id: Mapped[int] = mapped_column(primary_key=True)  # always 1
    input_per_m: Mapped[int] = mapped_column(Int64)
    output_per_m: Mapped[int] = mapped_column(Int64)
    cached_input_per_m: Mapped[int] = mapped_column(Int64)
    updated_at: Mapped[str] = mapped_column(String(32))


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(256))


class RequestRecord(Base):
    __tablename__ = "requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # uuid4 correlation id
    child_id: Mapped[int]
    key_id: Mapped[int | None]
    model: Mapped[str] = mapped_column(String(128))
    streamed: Mapped[int] = mapped_column(default=0)
    # pending -> completed | uncertain | upstream_error
    status: Mapped[str] = mapped_column(String(24), default="pending")
    created_at: Mapped[str] = mapped_column(String(32))
    settled_at: Mapped[str | None] = mapped_column(String(32))
    reserved_micro: Mapped[int] = mapped_column(Int64, default=0)
    charge_micro: Mapped[int] = mapped_column(Int64, default=0)
    overage_micro: Mapped[int] = mapped_column(Int64, default=0)  # actual-billed delta, audit only
    prompt_tokens: Mapped[int | None]
    completion_tokens: Mapped[int | None]
    cached_tokens: Mapped[int | None]
    # pricing snapshot so historical bills stay explainable after price changes
    price_input_per_m: Mapped[int | None] = mapped_column(Int64)
    price_output_per_m: Mapped[int | None] = mapped_column(Int64)
    price_cached_input_per_m: Mapped[int | None] = mapped_column(Int64)
    error: Mapped[str | None] = mapped_column(Text)


Index("ix_requests_child_created", "child_id", "created_at")
Index("ix_requests_status", "status")


class Reservation(Base):
    __tablename__ = "reservations"
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), unique=True)
    child_id: Mapped[int]
    amount_micro: Mapped[int] = mapped_column(Int64)
    # active -> settled | released | uncertain
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[str] = mapped_column(String(32))
    resolved_at: Mapped[str | None] = mapped_column(String(32))


class LedgerEntry(Base):
    __tablename__ = "ledger"
    id: Mapped[int] = mapped_column(primary_key=True)
    child_id: Mapped[int]
    request_id: Mapped[str | None] = mapped_column(String(36))
    # credit | debit | refund | adjustment
    type: Mapped[str] = mapped_column(String(16))
    amount_micro: Mapped[int] = mapped_column(Int64)  # signed; debits negative
    balance_after: Mapped[int] = mapped_column(Int64)
    admin: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[str] = mapped_column(String(32))


Index("ix_ledger_child_created", "child_id", "created_at", "type")
# One debit per request, DB-enforced (partial unique; NULLs exempt for credits).
Index("ix_ledger_request_debit", "request_id", unique=True, sqlite_where=(LedgerEntry.type == "debit"))
