import datetime as dt
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.db import create_schema, metadata, normalize_url  # noqa: E402
from app.main import create_app  # noqa: E402

PHONE = "phone-secret-" + "p" * 40
OWNER = "owner-secret-" + "o" * 40
CLIENT_ID = "claude-connector"
CLIENT_SECRET = "client-secret-" + "c" * 40
BASE = "http://localhost"
TZ = "Asia/Kolkata"
# Fixed "now": 2026-10-08 09:00 IST
NOW = dt.datetime(2026, 10, 8, 3, 30, tzinfo=dt.UTC)


class Clock:
    def __init__(self, now: dt.datetime):
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


@pytest.fixture(scope="session")
def pg_url():
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return
    import pgserver

    data_dir = Path(tempfile.gettempdir()) / "fitbit-mcp-test-pg"
    server = pgserver.get_server(data_dir, cleanup_mode="stop")
    yield server.get_uri()


@pytest.fixture(scope="session")
def engine(pg_url):
    eng = create_engine(normalize_url(pg_url), pool_size=10, max_overflow=5)
    with eng.begin() as conn:
        metadata.drop_all(conn)
    create_schema(eng)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def clean_db(engine):
    with engine.begin() as conn:
        names = ", ".join(t.name for t in metadata.sorted_tables)
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY"))
    yield


@pytest.fixture
def settings():
    return Settings(
        database_url="postgresql://unused",
        phone_ingest_secret=PHONE,
        mcp_auth_secret=OWNER,
        oauth_client_id=CLIENT_ID,
        oauth_client_secret=CLIENT_SECRET,
        public_base_url=BASE,
        user_tz=TZ,
        refresh_reuse_grace_seconds=30,
    ).validate()


@pytest.fixture
def clock():
    return Clock(NOW)


@pytest.fixture
def app(settings, engine, clock):
    return create_app(settings, engine=engine, clock=clock)


@pytest.fixture
def client(app):
    with TestClient(app, base_url=BASE) as c:
        yield c


def phone_headers() -> dict:
    return {"Authorization": f"Bearer {PHONE}"}


def payload(**parts) -> dict:
    return {"run_id": str(uuid.uuid4()), **parts}


def ingest(client, expect: int = 200, **parts) -> dict:
    r = client.post("/v1/ingest", json=payload(**parts), headers=phone_headers())
    assert r.status_code == expect, r.text
    return r.json()


def sleep_rec(rid, start, end, stages=(), offset="+05:30", seq=1, slm=None, end_offset=None):
    return {
        "source_record_id": rid,
        "start_time": start,
        "end_time": end,
        "start_offset": offset,
        "end_offset": end_offset or offset,
        "stages": [{"stage": s, "start": a, "end": b} for s, a, b in stages],
        "data_origin": "com.google.android.apps.fitness",
        "source_last_modified": slm,
        "compute_seq": seq,
    }


def daily_rec(date, seq=1, **values):
    return {"date": date, "compute_seq": seq, "utc_offset": "+05:30", **values}


def exercise_rec(rid, start, end, etype="running", seq=1, slm=None, offset="+05:30", **values):
    return {
        "source_record_id": rid,
        "start_time": start,
        "end_time": end,
        "start_offset": offset,
        "end_offset": offset,
        "exercise_type": etype,
        "data_origin": "com.google.android.apps.fitness",
        "source_last_modified": slm,
        "compute_seq": seq,
        **values,
    }
