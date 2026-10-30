from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    bot_token: str
    authorized_user_id: int
    relay_token: str
    database_path: Path
    api_host: str
    api_port: int
    job_lease_seconds: int
    pending_job_ttl_seconds: int
    delivery_poll_seconds: float
    max_delivery_attempts: int


def load_settings() -> Settings:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    relay_token = os.getenv("RELAY_TOKEN", "").strip()
    user_id = os.getenv("AUTHORIZED_TELEGRAM_USER_ID", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    if not relay_token or len(relay_token) < 32:
        raise RuntimeError("RELAY_TOKEN must be set to a random value of at least 32 characters")
    if not user_id:
        raise RuntimeError("AUTHORIZED_TELEGRAM_USER_ID is required")
    try:
        authorized_user_id = int(user_id)
    except ValueError as exc:
        raise RuntimeError("AUTHORIZED_TELEGRAM_USER_ID must be an integer") from exc

    database_path = Path(os.getenv("RELAY_DATABASE_PATH", "data/relay.sqlite3"))
    return Settings(
        bot_token=token,
        authorized_user_id=authorized_user_id,
        relay_token=relay_token,
        database_path=database_path,
        api_host=os.getenv("RELAY_API_HOST", "0.0.0.0"),
        api_port=int(os.getenv("RELAY_API_PORT", "8080")),
        job_lease_seconds=max(30, int(os.getenv("JOB_LEASE_SECONDS", "120"))),
        pending_job_ttl_seconds=max(60, int(os.getenv("PENDING_JOB_TTL_SECONDS", "300"))),
        delivery_poll_seconds=max(0.5, float(os.getenv("DELIVERY_POLL_SECONDS", "2"))),
        max_delivery_attempts=max(1, int(os.getenv("MAX_DELIVERY_ATTEMPTS", "8"))),
    )
