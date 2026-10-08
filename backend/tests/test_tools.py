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
        "baseline": {"asleep_min": None, "deep_min": None, "rem_min": None},  # < 7 nights of history
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
            "asleep_30d": None,
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
    assert hr == {
        "days": 30,
        "data": [{"date": "2026-10-06", "avg": 64, "min": 49, "max": 133, "resting": 57, "resting_30d": None}],
        "baseline": {"resting": None, "avg": None},
    }
    hrv, _ = call(client, token, "get_hrv_trend", days=30)
    assert hrv == {"days": 30, "data": [{"date": "2026-10-06", "hrv_ms": 48.3, "hrv_30d": None}], "baseline": {"hrv_ms": None}}


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
        "daily": [{"date": "2026-10-07", "steps": 8241, "steps_30d": None}],
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
        "baseline": {"steps": None},
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


# --- personal baselines -------------------------------------------------------------


def test_baseline_helpers_are_exclusive_and_need_seven_days():
    from app.metrics import baseline_block, trailing_mean

    today = dt.date(2026, 10, 8)
    series = {today - dt.timedelta(days=i): float(i + 1) for i in range(10)}  # today=1, yesterday=2, ...
    # Previous 30 days exclude today: days 2..10 -> mean 6.
    assert trailing_mean(series, today) == 6
    assert trailing_mean({today - dt.timedelta(days=1): 5.0}, today) is None  # 1 day < 7
    block = baseline_block(series, today, ndigits=1)
    assert block["mean"] == 5.5 and block["days_with_data"] == 10
    assert (block["typical_low"], block["typical_high"]) == (1.9, 9.1)  # 10th / 90th percentile
    assert block["window"] == "2026-09-09..2026-10-08"
    assert baseline_block({today: 1.0}, today) is None


def test_tools_return_values_next_to_personal_baselines(client, token):
    # 40 days ending yesterday (10-07): resting HR 60, HRV 50.0, steps 8000, nights of 420 min asleep;
    # then yesterday is an outlier day: resting 70, HRV 30.0, steps 2000, 300 min asleep, plus a nap.
    last = dt.date(2026, 10, 7)
    daily, nights = [], []
    for i in range(40):
        d = last - dt.timedelta(days=i)
        outlier = i == 0
        daily.append(daily_rec(d.isoformat(), steps=2000 if outlier else 8000, resting_hr=70 if outlier else 60,
                               heart_rate_avg=75, hrv_ms=30.0 if outlier else 50.0))
        start = dt.datetime.combine(d - dt.timedelta(days=1), dt.time(23, 0))
        minutes = 300 if outlier else 420
        nights.append(sleep_rec(f"n{i}", start.isoformat() + "+05:30",
                                (start + dt.timedelta(minutes=minutes)).isoformat() + "+05:30"))
    nights.append(sleep_rec("nap", "2026-10-07T14:00:00+05:30", "2026-10-07T14:30:00+05:30"))
    ingest(client, daily_metrics=daily, sleep=nights)

    hr, _ = call(client, token, "get_heart_rate_trend", days=7)
    y = next(d for d in hr["data"] if d["date"] == "2026-10-07")
    assert (y["resting"], y["resting_30d"]) == (70, 60)  # compared with the 30 days before it
    assert hr["baseline"]["resting"]["mean"] == 60  # (28*60 + 70)/29 -> 60
    assert hr["baseline"]["resting"]["typical_low"] == 60
    assert hr["baseline"]["resting"]["days_with_data"] == 29  # window ends today (10-08), data ends 10-07

    hrv, _ = call(client, token, "get_hrv_trend", days=7)
    y = next(d for d in hrv["data"] if d["date"] == "2026-10-07")
    assert (y["hrv_ms"], y["hrv_30d"]) == (30.0, 50.0)
    assert hrv["baseline"]["hrv_ms"]["mean"] == 49.3  # (28*50 + 30)/29

    act, _ = call(client, token, "get_activity", start_date="2026-10-07", end_date="2026-10-07")
    assert act["daily"] == [{"date": "2026-10-07", "steps": 2000, "steps_30d": 8000}]
    assert act["baseline"]["steps"]["typical_high"] == 8000

    sleep, _ = call(client, token, "get_sleep", start_date="2026-10-07", end_date="2026-10-07")
    main = next(s for s in sleep["sleep"] if not s["nap"])
    nap = next(s for s in sleep["sleep"] if s["nap"])
    assert (main["asleep_min"], main["asleep_30d"]) == (300, 420)
    assert nap["asleep_30d"] is None  # naps are not compared with nights
    summary, _ = call(client, token, "get_sleep_summary", days=7)
    assert summary["baseline"]["asleep_min"]["mean"] == 416  # (28*420 + 300)/29, naps excluded
    assert summary["baseline"]["deep_min"] is None  # no stage data recorded
