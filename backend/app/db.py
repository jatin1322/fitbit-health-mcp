"""PostgreSQL schema (SQLAlchemy Core) and engine construction."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine

metadata = MetaData()

TS = DateTime(timezone=True)


def _bookkeeping() -> list[Column]:
    return [
        Column("compute_seq", BigInteger, nullable=False),
        Column("created_at", TS, nullable=False, server_default=func.now()),
        Column("updated_at", TS, nullable=False, server_default=func.now()),
        Column("seen_at", TS, nullable=False, server_default=func.now()),
    ]


sleep_sessions = Table(
    "sleep_sessions",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("source", String(32), nullable=False),
    Column("source_record_id", String(128), nullable=False),
    Column("start_time", TS, nullable=False),
    Column("end_time", TS, nullable=False),
    Column("start_offset", String(6), nullable=False),
    Column("end_offset", String(6), nullable=False),
    Column("wake_date", Date, nullable=False),
    Column("duration_seconds", Integer, nullable=False),
    Column("stages_json", JSONB, nullable=False),
    Column("asleep_seconds", Integer, nullable=False),
    Column("deep_seconds", Integer),
    Column("light_seconds", Integer),
    Column("rem_seconds", Integer),
    Column("awake_seconds", Integer),
    Column("unknown_seconds", Integer),
    Column("is_main", Boolean, nullable=False, server_default="false"),
    Column("data_origin", String(255)),
    Column("source_last_modified", TS),
    *_bookkeeping(),
    UniqueConstraint("source", "source_record_id", name="uq_sleep_source_record"),
    Index("ix_sleep_wake_date", "source", "wake_date"),
)

daily_metrics = Table(
    "daily_metrics",
    metadata,
    Column("source", String(32), primary_key=True),
    Column("date", Date, primary_key=True),
    Column("steps", Integer),
    Column("resting_hr", Float),
    Column("hrv_ms", Float),
    Column("heart_rate_min", Float),
    Column("heart_rate_max", Float),
    Column("heart_rate_avg", Float),
    Column("utc_offset", String(6)),
    *_bookkeeping(),
)

exercise_sessions = Table(
    "exercise_sessions",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("source", String(32), nullable=False),
    Column("source_record_id", String(128), nullable=False),
    Column("start_time", TS, nullable=False),
    Column("end_time", TS, nullable=False),
    Column("start_offset", String(6), nullable=False),
    Column("end_offset", String(6), nullable=False),
    Column("local_date", Date, nullable=False),
    Column("exercise_type", String(32), nullable=False),
    Column("duration_seconds", Integer, nullable=False),
    Column("distance_m", Float),
    Column("calories", Float),
    Column("avg_hr", Float),
    Column("max_hr", Float),
    Column("steps", Integer),
    Column("data_origin", String(255)),
    Column("source_last_modified", TS),
    *_bookkeeping(),
    UniqueConstraint("source", "source_record_id", name="uq_exercise_source_record"),
    Index("ix_exercise_local_date", "source", "local_date"),
)

sync_state = Table(
    "sync_state",
    metadata,
    Column("source", String(32), primary_key=True),
    Column("last_ingest_at", TS),
    Column("client_last_success_at", TS),
    Column("data_through", TS),
    Column("known_gaps", JSONB, nullable=False, server_default="[]"),
    Column("resync_started_at", TS),
    Column("last_full_sync_at", TS),
    Column("last_run_result", String(16)),
    Column("last_run_duration_ms", Integer),
    Column("last_run_error_category", String(32)),
    Column("updated_at", TS, nullable=False, server_default=func.now()),
)

oauth_codes = Table(
    "oauth_codes",
    metadata,
    Column("code_hash", String(64), primary_key=True),
    Column("client_id", String(255), nullable=False),
    Column("redirect_uri", String(1024), nullable=False),
    Column("code_challenge", String(128), nullable=False),
    Column("scopes", String(255), nullable=False),
    Column("resource", String(1024), nullable=False),
    Column("family_id", String(64), nullable=False),
    Column("expires_at", TS, nullable=False),
    Column("used_at", TS),
)

oauth_tokens = Table(
    "oauth_tokens",
    metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("kind", String(8), nullable=False),  # access | refresh
    Column("client_id", String(255), nullable=False),
    Column("scopes", String(255), nullable=False),
    Column("resource", String(1024), nullable=False),
    Column("issuer", String(1024), nullable=False),
    Column("family_id", String(64), nullable=False),
    Column("parent_hash", String(64)),
    Column("expires_at", TS, nullable=False),
    Column("revoked_at", TS),
    Column("used_at", TS),
    Index("ix_tokens_family", "family_id"),
)

login_attempts = Table(
    "login_attempts",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("ip_hash", String(64), nullable=False),
    Column("ts", TS, nullable=False, server_default=func.now()),
    Index("ix_login_attempts_ip_ts", "ip_hash", "ts"),
)

HEALTH_TABLES = (sleep_sessions, daily_metrics, exercise_sessions)


def normalize_url(url: str) -> str:
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def make_engine(url: str) -> Engine:
    return create_engine(
        normalize_url(url),
        pool_size=5,
        max_overflow=0,
        pool_pre_ping=True,
        pool_recycle=300,
        echo=False,
        # No server-side prepared statements: they break behind PgBouncer-style poolers.
        connect_args={"prepare_threshold": None},
    )


def create_schema(engine: Engine) -> None:
    metadata.create_all(engine)
