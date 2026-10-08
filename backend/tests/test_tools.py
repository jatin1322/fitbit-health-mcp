import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import func, insert

from app.db import oauth_tokens
from tests.conftest import BASE, CLIENT_ID, daily_rec, exercise_rec, ingest, sleep_rec

SAMPLE = Path(__file__).resolve().parents[1] / "sample_data"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def make_token(engine, token="access-" + "t" * 40, base=BASE) -> str:
    with engine.begin() as conn:
        conn.execute(
            insert(oauth_tokens).values(
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                kind="access",
                client_id=CLIENT_ID,
                scopes="health.read",
                resource=f"{base}/mcp",
                issuer=base,
                family_id="fam",
                expires_at=func.now() + dt.timedelta(hours=1),
            )
        )
    return token


def rpc(client, token, method, params=None, id_=1):
    r = client.post(
        "/mcp",
        headers=MCP_HEADERS | {"Authorization": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    return r.json()


def call(client, token, name, **args):
    result = rpc(client, token, "tools/call", {"name": name, "arguments": args})["result"]
    if result.get("isError"):
        return None, result["content"][0]["text"]
    text = result["content"][0]["text"]
    assert json.loads(text) == result["structuredContent"]
    assert "\n" not in text  # compact
    return result["structuredContent"], None


@pytest.fixture
def token(engine):
    return make_token(engine)


def load_sample(client):
    for name in ("sleep", "daily_metrics", "exercise"):
        ingest(client, **{name: json.loads((SAMPLE / f"{name}.json").read_text())})


def test_exactly_six_read_only_tools_with_output_schema(client, token):
    tools = rpc(client, token, "tools/list")["result"]["tools"]
    assert sorted(t["name"] for t in tools) == sorted(
        ["get_sleep", "get_sleep_summary", "get_heart_rate_trend", "get_hrv_trend", "get_activity", "get_data_status"]
    )
    for t in tools:
        assert t["annotations"]["readOnlyHint"] is True
        assert t["annotations"]["destructiveHint"] is False
        assert t["outputSchema"]["type"] == "object"


def test_sleep_summary_math_excludes_naps(client, token):
    nights = [
        sleep_rec("n1", "2026-10-04T23:00:00+05:30", "2026-10-05T05:40:00+05:30"),  # 400
        sleep_rec("n2", "2026-10-05T23:00:00+05:30", "2026-10-06T06:00:00+05:30"),  # 420
        sleep_rec("n3", "2026-10-06T23:00:00+05:30", "2026-10-07T06:11:00+05:30"),  # 431
        sleep_rec("nap", "2026-10-07T14:00:00+05:30", "2026-10-07T14:30:00+05:30"),
    ]
    ingest(client, sleep=nights)
    out, err = call(client, token, "get_sleep_summary", days=7)
    assert err is None
    assert out == {
        "days": 7,
        "nights": 3,
        "avg_sleep_min": 417,
        "avg_deep_min": None,
        "avg_rem_min": None,
        "avg_awake_min": None,
        "min_sleep_min": 400,
        "max_sleep_min": 431,
    }


def test_get_sleep_range_and_fields(client, token):
    ingest(
        client,
        sleep=[
            sleep_rec(
                "s1",
                "2026-10-06T23:00:00+05:30",
                "2026-10-07T07:00:00+05:30",
                [("DEEP", "2026-10-06T23:00:00+05:30", "2026-10-07T00:30:00+05:30")],
            ),
            sleep_rec("s0", "2026-09-29T23:00:00+05:30", "2026-09-30T07:00:00+05:30"),
        ],
    )
    out, _ = call(client, token, "get_sleep", start_date="2026-10-01", end_date="2026-10-07")
    assert out["sleep"] == [
        {
            "date": "2026-10-07",
            "start": "2026-10-06T23:00:00+05:30",
            "end": "2026-10-07T07:00:00+05:30",
            "duration_min": 480,
            "asleep_min": 90,
            "nap": False,
            "stages": {"deep_min": 90, "rem_min": 0, "light_min": 0, "awake_min": 0, "unknown_min": 390},
        }
    ]


def test_hr_and_hrv_trends_omit_missing_days(client, token):
    ingest(
        client,
        daily_metrics=[
            daily_rec("2026-10-06", heart_rate_avg=64.4, heart_rate_min=49, heart_rate_max=132.5, resting_hr=57, hrv_ms=48.26),
            daily_rec("2026-10-07", steps=9000),  # no HR, no HRV
        ],
    )
    hr, _ = call(client, token, "get_heart_rate_trend", days=30)
    assert hr == {"days": 30, "data": [{"date": "2026-10-06", "avg": 64, "min": 49, "max": 133, "resting": 57}]}
    hrv, _ = call(client, token, "get_hrv_trend", days=30)
    assert hrv == {"days": 30, "data": [{"date": "2026-10-06", "hrv_ms": 48.3}]}


def test_activity(client, token):
    ingest(
        client,
        daily_metrics=[daily_rec("2026-10-07", steps=8241)],
        exercise=[
            exercise_rec("e1", "2026-10-07T18:00:00+05:30", "2026-10-07T18:45:00+05:30", distance_m=6200, avg_hr=151),
            exercise_rec("e2", "2026-09-01T18:00:00+05:30", "2026-09-01T18:30:00+05:30"),
        ],
    )
    out, _ = call(client, token, "get_activity", start_date="2026-10-01", end_date="2026-10-07")
    assert out == {
        "daily": [{"date": "2026-10-07", "steps": 8241}],
        "exercise": [
            {
                "start": "2026-10-07T18:00:00+05:30",
                "end": "2026-10-07T18:45:00+05:30",
                "type": "running",
                "duration_min": 45,
                "distance_m": 6200,
                "calories": None,
                "avg_hr": 151,
                "max_hr": None,
                "steps": None,
            }
        ],
    }


@pytest.mark.parametrize(
    "name,args",
    [
        ("get_sleep", {"start_date": "2026-10-07", "end_date": "2026-10-01"}),
        ("get_sleep", {"start_date": "07/10/2026", "end_date": "2026-10-07"}),
        ("get_activity", {"start_date": "2024-01-01", "end_date": "2026-10-07"}),
        ("get_sleep_summary", {"days": 0}),
        ("get_hrv_trend", {"days": -3}),
        ("get_heart_rate_trend", {"days": 400}),
    ],
)
def test_invalid_arguments_return_tool_errors(client, token, name, args):
    out, err = call(client, token, name, **args)
    assert out is None and err


def test_today_comes_from_user_tz_not_record_offsets(client, token, clock):
    # 2026-10-07 20:00 UTC is already 2026-10-08 in Asia/Kolkata.
    clock.now = dt.datetime(2026, 10, 7, 20, 0, tzinfo=dt.UTC)
    ingest(
        client,
        daily_metrics=[daily_rec("2026-10-08", hrv_ms=50), daily_rec("2026-10-07", hrv_ms=40)],
        sleep=[sleep_rec("trip", "2026-10-07T00:00:00+01:00", "2026-10-07T07:00:00+01:00", offset="+01:00")],
    )
    out, _ = call(client, token, "get_hrv_trend", days=1)
    assert [d["date"] for d in out["data"]] == ["2026-10-08"]
    status, _ = call(client, token, "get_data_status")
    assert status["today"] == "2026-10-08"
    summary, _ = call(client, token, "get_sleep_summary", days=2)  # 10-07 and 10-08
    assert summary["nights"] == 1


def test_data_status_gaps_and_freshness(client, token):
    ingest(
        client,
        full_resync_begin=True,
        daily_metrics=[daily_rec("2026-10-07", steps=1)],
        exercise=[exercise_rec("e1", "2026-10-07T18:00:00+05:30", "2026-10-07T18:45:00+05:30")],
    )
    ingest(client, run_complete={"data_through": "2026-10-08T08:00:00+05:30", "full_resync": True}, run_report={"result": "ok", "duration_ms": 5})
    status, _ = call(client, token, "get_data_status")
    assert status["current"] is True and status["history_complete"] is True
    assert status["coverage"]["exercise"] == {"first": "2026-10-07", "last": "2026-10-07", "count": 1}
    assert status["historical_deletions"] == "best_effort_before:2026-10-01"
    # A gap, followed by a successful incremental sync: current, but history stays incomplete.
    ingest(client, recovery={"state": "partial_gap", "gap_start": "2026-08-01T00:00:00Z", "gap_end": "2026-08-20T00:00:00Z"})
    ingest(client, run_complete={"data_through": "2026-10-08T08:30:00+05:30"})
    status, _ = call(client, token, "get_data_status")
    assert status["current"] is True
    assert status["history_complete"] is False
    assert status["recovery_state"] == "partial_gap" and len(status["known_gaps"]) == 1


def test_sample_data_end_to_end(client, token):
    load_sample(client)
    summary, _ = call(client, token, "get_sleep_summary", days=30)
    assert 25 <= summary["nights"] <= 30  # one missing night, naps excluded
    assert 300 <= summary["min_sleep_min"] <= summary["avg_sleep_min"] <= summary["max_sleep_min"] <= 500
    week, _ = call(client, token, "get_sleep", start_date="2026-10-01", end_date="2026-10-07")
    assert any(s["nap"] for s in week["sleep"])
    assert any(s["stages"] is None for s in week["sleep"])  # unstaged night (and naps)
    hrv, _ = call(client, token, "get_hrv_trend", days=60)
    assert len(hrv["data"]) == 60 - 3 - 1  # 3 days without HRV; window ends 10-08 (no data yet)
    activity, _ = call(client, token, "get_activity", start_date="2026-08-09", end_date="2026-10-07")
    assert len(activity["daily"]) == 60 and len(activity["exercise"]) == 35
