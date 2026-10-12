import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Env-driven config (prefix KP_). Runtime-editable policy lives in the DB
    `settings` table, seeded from these defaults at init-db."""

    model_config = SettingsConfigDict(env_prefix="KP_", env_file=".env", extra="ignore")

    db_path: str = "./kidsproxy.db"
    upstream_base_url: str = "http://127.0.0.1:20128"
    upstream_api_key: str = ""
    session_secret: str = ""
    cookie_secure: bool = False

    price_input_per_m: int = 500_000
    price_output_per_m: int = 2_000_000
    price_cached_input_per_m: int = 100_000

    min_start_balance: int = 1_000_000
    max_output_tokens: int = 4096
    max_request_cost: int = 250_000
    max_body_bytes: int = 1 << 20
    chars_per_token_est: int = 3

    upstream_timeout: float = 120.0
    stale_grace_seconds: int = 300

    daily_limit: int = 0
    monthly_limit: int = 0
    allowed_hours: str = ""
    tz: str = "America/Los_Angeles"
    allowed_models: str = ""
    max_concurrent_per_child: int = 2

    uncertain_policy: str = "hold"  # hold | release

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{os.path.abspath(self.db_path)}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
