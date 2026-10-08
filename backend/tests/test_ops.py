import asyncio
import dataclasses
import json
import logging
import os
import re
import socket
import stat
import threading
import time

import pytest
import uvicorn

from tests.conftest import CLIENT_SECRET, OWNER, PHONE, daily_rec, exercise_rec, ingest, phone_headers, sleep_rec
from tests.test_oauth import full_flow
from tests.test_tools import MCP_HEADERS, call, make_token

from app.main import create_app


def test_health_reveals_nothing(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_security_headers(client):
    r = client.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"
    auth_page = client.get("/authorize?client_id=x")
    assert "frame-ancestors 'none'" in auth_page.headers["content-security-policy"]


def test_hsts_only_on_https(settings, engine, clock):
    from fastapi.testclient import TestClient

    https = create_app(dataclasses.replace(settings, public_base_url="https://health.example.com"), engine=engine, clock=clock)
    with TestClient(https, base_url="https://health.example.com") as c:
        assert "max-age" in c.get("/health").headers["strict-transport-security"]


def test_no_docs_endpoints(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code in (401, 404, 405)


def test_no_health_values_tokens_or_secrets_in_logs(client, engine, caplog):
    caplog.set_level(logging.DEBUG)
    marker_values = ["8241", "48.7", "57.25", "6199"]
    ingest(
        client,
        sleep=[sleep_rec("s-log", "2026-10-06T23:00:00+05:30", "2026-10-07T07:00:00+05:30")],
        daily_metrics=[daily_rec("2026-10-07", steps=8241, hrv_ms=48.7, resting_hr=57.25)],
        exercise=[exercise_rec("e-log", "2026-10-07T18:00:00+05:30", "2026-10-07T18:45:00+05:30", distance_m=6199)],
    )
    client.post("/v1/ingest", json={"run_id": "nope", "daily_metrics": [{"date": "x", "steps": 8241}]}, headers=phone_headers())
    client.post("/v1/ingest", json={}, headers={"Authorization": f"Bearer {OWNER}"})
    tokens = full_flow(client)
    client.post("/token", data={"grant_type": "refresh_token", "refresh_token": "bogus", "client_id": "x", "client_secret": "y"})
    token = make_token(engine)
    call(client, token, "get_activity", start_date="2026-10-01", end_date="2026-10-07")
    call(client, token, "get_hrv_trend", days=7)
    call(client, token, "get_sleep", start_date="bad", end_date="2026-10-07")
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "ingest ok" in logs  # sanity: logging is captured
    for secret in (PHONE, OWNER, CLIENT_SECRET, tokens["access_token"], tokens["refresh_token"], token, *marker_values):
        assert secret not in logs, f"leaked into logs: {secret[:6]}..."


def test_export_permissions_and_wipe_confirmation(client, pg_url, tmp_path, capsys):
    from scripts import export, wipe

    ingest(client, daily_metrics=[daily_rec("2026-10-07", steps=5)])
    out = tmp_path / "backup.json"
    os.environ["DATABASE_URL"] = pg_url
    try:
        assert export.main([str(out)]) == 0
        assert stat.S_IMODE(out.stat().st_mode) == 0o600
        assert len(json.loads(out.read_text())["daily_metrics"]) == 1
        assert wipe.main([]) == 2
        assert wipe.main(["--yes"]) == 2
        assert len(json.loads(out.read_text())["daily_metrics"]) == 1
        assert wipe.main(["--yes-delete-all-health-data"]) == 0
        assert export.main([str(out)]) == 0
        assert json.loads(out.read_text())["daily_metrics"] == []
    finally:
        del os.environ["DATABASE_URL"]


# --- protocol compatibility over a real HTTP server ---------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(settings, engine, clock):
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    app = create_app(dataclasses.replace(settings, public_base_url=base), engine=engine, clock=clock)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield base
    server.should_exit = True
    thread.join(timeout=5)


def test_2025_initialize_handshake(live_server, engine):
    import httpx

    token = make_token(engine, base=live_server)
    headers = MCP_HEADERS | {"Authorization": f"Bearer {token}"}
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    }
    with httpx.Client(base_url=live_server) as c:
        r = c.post("/mcp", json=init, headers=headers)
        assert r.status_code == 200
        assert r.json()["result"]["protocolVersion"] == "2025-06-18"
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=headers | {"mcp-protocol-version": "2025-06-18"})
        assert len(r.json()["result"]["tools"]) == 6
        assert c.post("/mcp", json=init, headers=MCP_HEADERS).status_code == 401


def test_2026_client(live_server, engine):
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    token = make_token(engine, base=live_server)

    async def run():
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})
        async with http, Client(streamable_http_client(f"{live_server}/mcp", http_client=http)) as client:
            tools = await client.list_tools()
            result = await client.call_tool("get_data_status", {})
            return client.protocol_version, tools, result

    version, tools, result = asyncio.run(run())
    assert version == "2026-07-28"
    assert len(tools.tools) == 6
    assert result.structured_content["user_tz"] == "Asia/Kolkata"
