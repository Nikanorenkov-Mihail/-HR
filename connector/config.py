from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class ConnectorSettings:
    relay_base_url: str
    relay_token: str
    worker_id: str
    opencode_base_url: str
    opencode_password: str
    opencode_agent: str
    opencode_session_id: str
    opencode_session_file: Path
    opencode_check_interval: int
    request_timeout: int
    lease_heartbeat_seconds: int
    poll_wait_seconds: int


def load_settings() -> ConnectorSettings:
    relay_url = os.getenv("RELAY_BASE_URL", "").strip().rstrip("/")
    relay_token = os.getenv("RELAY_TOKEN", "").strip()
    if not relay_url:
        raise RuntimeError("RELAY_BASE_URL is required")
    if urlparse(relay_url).scheme != "https":
        raise RuntimeError("RELAY_BASE_URL must use HTTPS")
    if not relay_token or len(relay_token) < 32:
        raise RuntimeError("RELAY_TOKEN must match the VPS and be at least 32 characters")

    opencode_url = os.getenv("OPENCODE_BASE_URL", "http://127.0.0.1:4096").strip().rstrip("/")
    parsed = urlparse(opencode_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("OPENCODE_BASE_URL must point to localhost")
    opencode_password = os.getenv("OPENCODE_SERVER_PASSWORD", "").strip()
    if not opencode_password:
        raise RuntimeError("OPENCODE_SERVER_PASSWORD is required")

    return ConnectorSettings(
        relay_base_url=relay_url,
        relay_token=relay_token,
        worker_id=os.getenv("CONNECTOR_ID", "hr-opencode-pc-1").strip(),
        opencode_base_url=opencode_url,
        opencode_password=opencode_password,
        opencode_agent=os.getenv("OPENCODE_AGENT_ID", "hr-assistant").strip(),
        opencode_session_id=os.getenv("OPENCODE_SESSION_ID", "").strip(),
        opencode_session_file=Path(os.getenv("OPENCODE_SESSION_FILE", "connector-data/session_id.txt")),
        opencode_check_interval=max(10, int(os.getenv("OPENCODE_CHECK_INTERVAL_SECONDS", "30"))),
        request_timeout=max(60, int(os.getenv("OPENCODE_REQUEST_TIMEOUT_SECONDS", "900"))),
        lease_heartbeat_seconds=max(15, int(os.getenv("LEASE_HEARTBEAT_SECONDS", "30"))),
        poll_wait_seconds=min(25, max(0, int(os.getenv("RELAY_POLL_WAIT_SECONDS", "15")))),
    )
