"""Settings loaded from environment variables.

Validation errors name the variable but never echo its value.
"""

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
MIN_SECRET_LEN = 32


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    database_url: str
    phone_ingest_secret: str
    mcp_auth_secret: str
    oauth_client_id: str
    oauth_client_secret: str
    public_base_url: str  # e.g. https://fitbit-mcp-xyz.a.run.app (no trailing slash)
    user_tz: str = "UTC"
    active_source: str = "health_connect"
    allowed_redirect_uris: tuple[str, ...] = (CLAUDE_CALLBACK,)
    extra_allowed_hosts: tuple[str, ...] = ()
    trust_proxy: bool = False  # True on Cloud Run: client IP = last X-Forwarded-For entry
    refresh_reuse_grace_seconds: int = 30
    access_token_ttl: int = 3600
    refresh_token_ttl: int = 90 * 24 * 3600
    scopes: tuple[str, ...] = field(default=("health.read",))

    @property
    def issuer(self) -> str:
        return self.public_base_url

    @property
    def resource_url(self) -> str:
        return f"{self.public_base_url}/mcp"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.user_tz)

    def validate(self) -> "Settings":
        secrets = {
            "PHONE_INGEST_SECRET": self.phone_ingest_secret,
            "MCP_AUTH_SECRET": self.mcp_auth_secret,
            "OAUTH_CLIENT_SECRET": self.oauth_client_secret,
        }
        for name, value in secrets.items():
            if len(value) < MIN_SECRET_LEN:
                raise ConfigError(f"{name} must be at least {MIN_SECRET_LEN} characters")
        if len(set(secrets.values())) != len(secrets):
            raise ConfigError("PHONE_INGEST_SECRET, MCP_AUTH_SECRET and OAUTH_CLIENT_SECRET must all differ")
        if not self.oauth_client_id:
            raise ConfigError("OAUTH_CLIENT_ID is required")
        parsed = urlparse(self.public_base_url)
        local = parsed.hostname in ("localhost", "127.0.0.1")
        if parsed.scheme != "https" and not local:
            raise ConfigError("PUBLIC_BASE_URL must be https")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ConfigError("PUBLIC_BASE_URL must be a bare origin")
        try:
            ZoneInfo(self.user_tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigError("USER_TZ must be an IANA timezone name") from None
        if not self.database_url.startswith(("postgresql://", "postgresql+psycopg://", "postgres://")):
            raise ConfigError("DATABASE_URL must be a PostgreSQL URL")
        return self


def _csv(name: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in os.environ.get(name, "").split(",") if v.strip())


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def load_settings() -> Settings:
    redirects = _csv("ALLOWED_REDIRECT_URIS") or (CLAUDE_CALLBACK,)
    return Settings(
        database_url=_required("DATABASE_URL"),
        phone_ingest_secret=_required("PHONE_INGEST_SECRET"),
        mcp_auth_secret=_required("MCP_AUTH_SECRET"),
        oauth_client_id=_required("OAUTH_CLIENT_ID"),
        oauth_client_secret=_required("OAUTH_CLIENT_SECRET"),
        public_base_url=_required("PUBLIC_BASE_URL").rstrip("/"),
        user_tz=os.environ.get("USER_TZ", "UTC"),
        active_source=os.environ.get("ACTIVE_SOURCE", "health_connect"),
        allowed_redirect_uris=redirects,
        extra_allowed_hosts=_csv("EXTRA_ALLOWED_HOSTS"),
        trust_proxy=os.environ.get("TRUST_PROXY", "") == "1",
    ).validate()
