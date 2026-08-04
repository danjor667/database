"""Environment-driven configuration.

Deliberately dependency-free: values come from the process environment with
defaults matching docker-compose.yml, so a fresh clone runs without a .env file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_DSN = "postgresql://rr_app:rr_password@localhost:5433/restaurant_reservations"


@dataclass(frozen=True)
class Settings:
    dsn: str
    pool_min: int
    pool_max: int
    pool_acquire_timeout: float
    app_name: str
    booking_max_attempts: int
    redis_url: str
    mongo_url: str


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def load_settings() -> Settings:
    pool_min = _int_env("POOL_MIN", 1)
    pool_max = _int_env("POOL_MAX", 10)
    if pool_min < 1:
        raise ValueError("POOL_MIN must be >= 1")
    if pool_max < pool_min:
        raise ValueError("POOL_MAX must be >= POOL_MIN")

    return Settings(
        dsn=os.environ.get("DATABASE_URL", DEFAULT_DSN),
        pool_min=pool_min,
        pool_max=pool_max,
        pool_acquire_timeout=float(os.environ.get("POOL_ACQUIRE_TIMEOUT", "10") or 10),
        app_name=os.environ.get("APP_NAME", "rr-backend"),
        booking_max_attempts=_int_env("BOOKING_MAX_ATTEMPTS", 8),
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        mongo_url=os.environ.get(
            "MONGO_URL",
            "mongodb://rr_app:rr_password@localhost:27017/"
            "restaurant_feedback?authSource=admin",
        ),
    )