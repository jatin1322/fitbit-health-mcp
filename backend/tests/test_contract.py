"""Contract between the Android app and the backend.

android/sync-core's PayloadTest serialises a canonical IngestPayload to
tests/fixtures/android_ingest_payload.json; this test proves the backend
accepts exactly that JSON, so field names/types can't drift apart.
"""

import json
from pathlib import Path

from app.schemas import IngestPayload
from tests.conftest import phone_headers

FIXTURE = Path(__file__).parent / "fixtures" / "android_ingest_payload.json"


def test_android_payload_fixture_validates():
    payload = IngestPayload.model_validate_json(FIXTURE.read_text())
    assert payload.full_resync_begin and payload.run_complete.full_resync
    assert payload.sleep[0].stages[1].stage.value == "SLEEPING"
    assert payload.exercise[0].exercise_type.value == "running"


def test_android_payload_fixture_ingests(client):
    body = json.loads(FIXTURE.read_text())
    begin = {k: v for k, v in body.items() if k != "run_complete"}
    r = client.post("/v1/ingest", json=begin, headers=phone_headers())
    assert r.status_code == 200, r.text
    assert r.json()["resync_started_at"]
    status = client.get("/v1/status", headers=phone_headers()).json()
    assert status["resync_started_at"] == r.json()["resync_started_at"]
    done = client.post("/v1/ingest", json={"run_id": body["run_id"], "run_complete": body["run_complete"]}, headers=phone_headers())
    assert done.status_code == 200
    status = client.get("/v1/status", headers=phone_headers()).json()
    assert status["resync_started_at"] is None and status["last_full_sync_at"]
