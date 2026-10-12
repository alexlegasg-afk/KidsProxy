import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ApiKey, Balance, Child

KEY_BYTES = 48  # 384 bits of randomness (> required 256)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_key(child_slug: str) -> tuple[str, str]:
    """Return (full_key, sha256_hex). Full key shown ONCE; only hash stored."""
    secret = secrets.token_urlsafe(KEY_BYTES)
    full = f"sk-{child_slug}-{secret}"
    return full, hash_key(full)


def verify_password(password: str, stored: str) -> bool:
    """stored = 'salt$sha256hex' (iterated). Constant-time compare."""
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return hmac.compare_digest(calc, digest)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    return salt.hex() + "$" + hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000).hex()


@dataclass
class AuthedChild:
    child_id: int
    slug: str
    name: str
    key_id: int


async def authenticate(session: AsyncSession, authorization: str | None) -> AuthedChild | None:
    """Resolve Bearer key -> (child, key). Never trusts identity from body/query."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:].strip()
    if not token:
        return None
    row = (
        await session.execute(
            select(ApiKey, Child)
            .join(Child, Child.id == ApiKey.child_id)
            .where(ApiKey.key_hash == hash_key(token), ApiKey.active == 1, Child.active == 1)
        )
    ).first()
    if row is None:
        return None
    key, child = row
    return AuthedChild(child_id=child.id, slug=child.slug, name=child.name, key_id=key.id)


async def create_child(session: AsyncSession, slug: str, name: str) -> Child:
    child = Child(slug=slug, name=name, created_at=now_iso())
    session.add(child)
    await session.flush()
    session.add(Balance(child_id=child.id, available_micro=0, reserved_micro=0))
    return child


async def rotate_key(session: AsyncSession, child_id: int, slug: str) -> tuple[str, ApiKey]:
    """Issue new key and revoke all previous ones atomically (caller commits)."""
    await session.execute(
        update(ApiKey)
        .where(ApiKey.child_id == child_id, ApiKey.active == 1)
        .values(active=0, revoked_at=now_iso())
    )
    full, digest = generate_key(slug)
    key = ApiKey(child_id=child_id, key_hash=digest, prefix=full[:16] + "...", created_at=now_iso())
    session.add(key)
    return full, key
