import datetime as dt

from sqlalchemy import func, insert, select

from app.db import daily_metrics, exercise_sessions, sleep_sessions, sync_state
from tests.conftest import (
    CLIENT_SECRET,
    OWNER,
    exercise_rec,
    daily_rec,
    ingest,
    payload,
    phone_headers,
    sleep_rec,
)

NIGHT = ("2026-10-06T23:00:00+05:30", "2026-10-07T07:00:00+05:30")
STAGES = [
    ("LIGHT", "2026-10-06T23:00:00+05:30", "2026-10-07T01:00:00+05:30"),
    ("DEEP", "2026-10-07T01:00:00+05:30", "2026-10-07T02:30:00+05:30"),
    ("REM", "2026-10-07T02:30:00+05:30", "2026-10-07T04:00:00+05:30"),
    ("AWAKE", "2026-10-07T04:00:00+05:30", "2026-10-07T04:20:00+05:30"),
    ("LIGHT", "2026-10-07T04:20:00+05:30", "2026-10-07T07:00:00+05:30"),
]


def count(engine, table) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def row(engine, table, **where):
    with engine.connect() as conn:
        q = select(table)
        for k, v in where.items():
            q = q.where(table.c[k] == v)
        return conn.execute(q).mappings().one()


def state(engine):
    with engine.connect() as conn:
        return conn.execute(select(sync_state)).mappings().one()


# --- auth & validation ------------------------------------------------------


def test_ingest_auth(client):
    body = payload()
    assert client.post("/v1/ingest", json=body, headers=phone_headers()).status_code == 200
    for header in (None, "Bearer wrong", f"Bearer {OWNER}", f"Bearer {CLIENT_SECRET}", "Basic abc"):
        headers = {"Authorization": header} if header else {}
        assert client.post("/v1/ingest", json=body, headers=headers).status_code == 401
    assert client.get("/v1/status").status_code == 401


def test_malformed_payload_rejected_without_echo(client):
    bad = payload(
        daily_metrics=[daily_rec("2026-10-07", resting_hr=987.25, steps=424242)],
        sleep=[sleep_rec("abc", *NIGHT) | {"title": "my secret nap 31337"}],
    )
    r = client.post("/v1/ingest", json=bad, headers=phone_headers())
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_request"
    for value in ("987.25", "424242", "31337", "secret nap"):
        assert value not in r.text


def test_non_uuid_run_id_rejected(client):
    r = client.post("/v1/ingest", json={"run_id": "pixel-8-serial-123"}, headers=phone_headers())
    assert r.status_code == 422
    assert "pixel" not in r.text


def test_naive_datetimes_and_bad_intervals_rejected(client):
    for rec in (
        sleep_rec("a", "2026-10-06T23:00:00", "2026-10-07T07:00:00"),
        sleep_rec("b", NIGHT[1], NIGHT[0]),
        sleep_rec("c", "2026-10-05T07:00:00+05:30", "2026-10-07T07:00:00+05:30"),
    ):
        r = client.post("/v1/ingest", json=payload(sleep=[rec]), headers=phone_headers())
        assert r.status_code == 422


def test_body_size_limit(client):
    r = client.post(
        "/v1/ingest", content=b"{" + b" " * (2 * 1024 * 1024 + 10) + b"}", headers=phone_headers() | {"Content-Type": "application/json"}
    )
    assert r.status_code == 413


# --- idempotency & versioning ------------------------------------------------


def test_repeated_ingest_is_idempotent(client, engine):
    body = dict(
        sleep=[sleep_rec("s1", *NIGHT, STAGES)],
        daily_metrics=[daily_rec("2026-10-07", steps=8000, resting_hr=57)],
        exercise=[exercise_rec("e1", "2026-10-07T18:00:00+05:30", "2026-10-07T18:45:00+05:30", distance_m=6200)],
    )
    first = ingest(client, **body)
    assert (first["sleep_upserted"], first["daily_metrics_upserted"], first["exercise_upserted"]) == (1, 1, 1)
    for _ in range(4):
        ingest(client, **body)
    assert count(engine, sleep_sessions) == 1
    assert count(engine, daily_metrics) == 1
    assert count(engine, exercise_sessions) == 1


def test_sleep_derived_fields(client, engine):
    ingest(client, sleep=[sleep_rec("s1", *NIGHT, STAGES)])
    r = row(engine, sleep_sessions, source_record_id="s1")
    assert r["wake_date"] == dt.date(2026, 10, 7)
    assert r["deep_seconds"] == 90 * 60 and r["rem_seconds"] == 90 * 60 and r["awake_seconds"] == 20 * 60
    assert r["light_seconds"] == (120 + 160) * 60
    assert r["asleep_seconds"] == r["duration_seconds"] - 20 * 60
    assert r["is_main"] is True


def test_newer_compute_seq_updates_and_stale_retry_ignored(client, engine):
    rec = lambda seq, dist: exercise_rec("e1", "2026-10-07T18:00:00+05:30", "2026-10-07T18:45:00+05:30", seq=seq, distance_m=dist)  # noqa: E731
    ingest(client, exercise=[rec(5, 5000)])
    assert ingest(client, exercise=[rec(6, 6200)])["exercise_upserted"] == 1
    assert row(engine, exercise_sessions, source_record_id="e1")["distance_m"] == 6200
    stale = ingest(client, exercise=[rec(4, 1000)])
    assert stale["stale_skipped"] == 1 and stale["exercise_upserted"] == 0
    assert row(engine, exercise_sessions, source_record_id="e1")["distance_m"] == 6200


def test_newer_session_edit_wins_over_older_seq(client, engine):
    old_slm, new_slm = "2026-10-07T08:00:00Z", "2026-10-07T09:00:00Z"
    ingest(client, sleep=[sleep_rec("s1", *NIGHT, seq=9, slm=old_slm)])
    edited = sleep_rec("s1", "2026-10-06T23:30:00+05:30", NIGHT[1], seq=3, slm=new_slm)
    assert ingest(client, sleep=[edited])["sleep_upserted"] == 1
    assert row(engine, sleep_sessions, source_record_id="s1")["duration_seconds"] == int(7.5 * 3600)


def test_newer_computation_with_lower_values_is_accepted(client, engine):
    ingest(client, daily_metrics=[daily_rec("2026-10-07", seq=1, steps=9000, heart_rate_max=150)])
    ingest(client, daily_metrics=[daily_rec("2026-10-07", seq=2, steps=7000, heart_rate_max=120)])
    r = row(engine, daily_metrics, date=dt.date(2026, 10, 7))
    assert (r["steps"], r["heart_rate_max"]) == (7000, 120)
    out = ingest(client, daily_metrics=[daily_rec("2026-10-07", seq=1, steps=9000)])
    assert out["stale_skipped"] == 1
    assert row(engine, daily_metrics, date=dt.date(2026, 10, 7))["steps"] == 7000


def test_duplicate_keys_in_one_batch(client, engine):
    out = ingest(client, daily_metrics=[daily_rec("2026-10-07", seq=1, steps=1), daily_rec("2026-10-07", seq=2, steps=2)])
    assert out["daily_metrics_upserted"] == 1 and out["stale_skipped"] == 1
    assert row(engine, daily_metrics, date=dt.date(2026, 10, 7))["steps"] == 2


def test_daily_rows_separate_per_source(client, engine):
    with engine.begin() as conn:
        conn.execute(insert(daily_metrics).values(source="google_health_api", date=dt.date(2026, 10, 7), steps=1, compute_seq=1))
    ingest(client, daily_metrics=[daily_rec("2026-10-07", steps=8000)])
    assert count(engine, daily_metrics) == 2


# --- deletions & main sleep ------------------------------------------------------


def test_nap_and_main_sleep_and_deletion_recomputes_main(client, engine):
    nap = sleep_rec("nap", "2026-10-07T14:00:00+05:30", "2026-10-07T14:40:00+05:30")
    night = sleep_rec("night", *NIGHT, STAGES)
    ingest(client, sleep=[nap, night])
    assert row(engine, sleep_sessions, source_record_id="night")["is_main"] is True
    assert row(engine, sleep_sessions, source_record_id="nap")["is_main"] is False
    out = ingest(client, deleted=[{"type": "sleep", "source_record_id": "night"}])
    assert out["deleted"] == 1
    assert row(engine, sleep_sessions, source_record_id="nap")["is_main"] is True


def test_main_sleep_tie_goes_to_earlier_start(client, engine):
    a = sleep_rec("a", "2026-10-07T01:00:00+05:30", "2026-10-07T03:00:00+05:30")
    b = sleep_rec("b", "2026-10-07T04:00:00+05:30", "2026-10-07T06:00:00+05:30")
    ingest(client, sleep=[b, a])
    assert row(engine, sleep_sessions, source_record_id="a")["is_main"] is True
    assert row(engine, sleep_sessions, source_record_id="b")["is_main"] is False


def test_editing_wake_date_recomputes_old_date(client, engine):
    ingest(client, sleep=[sleep_rec("x", *NIGHT, seq=1), sleep_rec("y", "2026-10-07T13:00:00+05:30", "2026-10-07T13:30:00+05:30", seq=1)])
    moved = sleep_rec("x", "2026-10-07T23:00:00+05:30", "2026-10-08T07:00:00+05:30", seq=2)
    ingest(client, sleep=[moved])
    assert row(engine, sleep_sessions, source_record_id="y")["is_main"] is True


def test_exercise_deletion(client, engine):
    ingest(client, exercise=[exercise_rec("e1", "2026-10-07T18:00:00+05:30", "2026-10-07T18:45:00+05:30")])
    assert ingest(client, deleted=[{"type": "exercise", "source_record_id": "e1"}])["deleted"] == 1
    assert count(engine, exercise_sessions) == 0


# --- sync state ---------------------------------------------------------------


def test_data_through_moves_only_on_run_complete(client, engine):
    ingest(client, daily_metrics=[daily_rec("2026-10-07", steps=1)])
    assert state(engine)["data_through"] is None
    ingest(client, run_complete={"data_through": "2026-10-08T08:00:00+05:30"})
    s = state(engine)
    assert s["data_through"] == dt.datetime(2026, 10, 8, 2, 30, tzinfo=dt.UTC)
    assert s["client_last_success_at"] is not None


def test_partial_gap_recorded_and_data_through_still_advances(client, engine):
    gap = {"state": "partial_gap", "gap_start": "2026-08-01T00:00:00Z", "gap_end": "2026-08-20T00:00:00Z"}
    ingest(client, recovery=gap, run_complete={"data_through": "2026-10-08T00:00:00Z"})
    ingest(client, recovery=gap)  # same gap twice is stored once
    s = state(engine)
    assert len(s["known_gaps"]) == 1 and s["data_through"] is not None


def test_full_resync_sweeps_only_unseen_rows_and_clears_gaps(client, engine):
    ingest(
        client,
        sleep=[sleep_rec("keep", *NIGHT), sleep_rec("gone", "2026-10-05T23:00:00+05:30", "2026-10-06T07:00:00+05:30")],
        daily_metrics=[daily_rec("2026-10-07", steps=1), daily_rec("2026-10-01", steps=2)],
        recovery={"state": "partial_gap", "gap_start": "2026-08-01T00:00:00Z", "gap_end": "2026-08-20T00:00:00Z"},
    )
    ingest(client, full_resync_begin=True, sleep=[sleep_rec("keep", *NIGHT)])
    # Stale re-send of a daily row still marks it as seen during the resync.
    ingest(client, daily_metrics=[daily_rec("2026-10-07", seq=0, steps=1)])
    out = ingest(client, run_complete={"data_through": "2026-10-08T00:00:00Z", "full_resync": True})
    assert out["swept"] == 2
    with engine.connect() as conn:
        assert [r[0] for r in conn.execute(select(sleep_sessions.c.source_record_id))] == ["keep"]
        assert [r[0] for r in conn.execute(select(daily_metrics.c.date))] == [dt.date(2026, 10, 7)]
    s = state(engine)
    assert s["known_gaps"] == [] and s["last_full_sync_at"] is not None and s["resync_started_at"] is None


def test_incomplete_resync_deletes_nothing_and_sweep_requires_begin(client, engine):
    ingest(client, sleep=[sleep_rec("old", *NIGHT)])
    ingest(client, full_resync_begin=True)
    assert count(engine, sleep_sessions) == 1  # no run_complete yet
    with engine.begin() as conn:
        from sqlalchemy import update

        conn.execute(update(sync_state).values(resync_started_at=None))
    r = client.post(
        "/v1/ingest",
        json=payload(run_complete={"data_through": "2026-10-08T00:00:00Z", "full_resync": True}),
        headers=phone_headers(),
    )
    assert r.status_code == 409
    assert count(engine, sleep_sessions) == 1


def test_phone_clock_skew_does_not_affect_sweep_or_versioning(client, engine):
    # Phone clock 1 day in the future / past: only compute_seq and server time matter.
    ingest(client, daily_metrics=[daily_rec("2026-10-07", seq=1, steps=1)])
    ingest(client, full_resync_begin=True, daily_metrics=[daily_rec("2026-10-07", seq=2, steps=2)])
    out = ingest(
        client,
        run_complete={"data_through": "2026-10-09T09:00:00Z", "full_resync": True},
        run_report={"result": "ok", "duration_ms": 1200},
    )
    assert out["swept"] == 0
    assert row(engine, daily_metrics, date=dt.date(2026, 10, 7))["steps"] == 2


def test_run_report_and_status(client, engine):
    ingest(client, run_report={"result": "error", "duration_ms": 900, "error_category": "network"})
    r = client.get("/v1/status", headers=phone_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["last_run"] == {"result": "error", "duration_ms": 900, "error_category": "network"}
    assert body["max_compute_seq"] == 0
    bad = client.post(
        "/v1/ingest",
        json=payload(run_report={"result": "error", "duration_ms": 1, "error_category": "stack trace here"}),
        headers=phone_headers(),
    )
    assert bad.status_code == 422


def test_max_compute_seq_for_reinstall(client):
    ingest(client, daily_metrics=[daily_rec("2026-10-07", seq=41, steps=1)], exercise=[exercise_rec("e", "2026-10-07T18:00:00+05:30", "2026-10-07T18:30:00+05:30", seq=42)])
    assert client.get("/v1/status", headers=phone_headers()).json()["max_compute_seq"] == 42
    # A reinstalled phone continues from server max + 1, so its writes apply.
    assert ingest(client, daily_metrics=[daily_rec("2026-10-07", seq=43, steps=5)])["daily_metrics_upserted"] == 1


def test_nanosecond_timestamps_from_kotlin_accepted(client, engine):
    # Kotlin Instant.toString() can emit 9 fractional digits.
    rec = sleep_rec("ns", "2026-10-06T17:30:00.123456789Z", "2026-10-07T01:30:00.987654321Z", offset="+05:30")
    assert ingest(client, sleep=[rec])["sleep_upserted"] == 1
    assert row(engine, sleep_sessions, source_record_id="ns")["wake_date"] == dt.date(2026, 10, 7)


# --- server-owned full-resync boundary (phone clock never involved) ---------------

import pytest  # noqa: E402


def test_begin_returns_server_owned_boundary(client, engine):
    out = ingest(client, full_resync_begin=True)
    boundary = dt.datetime.fromisoformat(out["resync_started_at"])
    assert state(engine)["resync_started_at"] == boundary
    # Server clock, not anything the phone sent: within a minute of the DB's now().
    with engine.connect() as conn:
        db_now = conn.execute(select(func.now())).scalar_one()
    assert abs((db_now - boundary).total_seconds()) < 60
    assert "resync_started_at" not in ingest(client, daily_metrics=[daily_rec("2026-10-07", steps=1)])


@pytest.mark.parametrize("skew_days", [1, -1], ids=["phone_clock_ahead", "phone_clock_behind"])
def test_full_resync_unaffected_by_phone_clock_skew(client, engine, skew_days):
    phone_now = dt.datetime(2026, 10, 8, 9, 0, tzinfo=dt.UTC) + dt.timedelta(days=skew_days)
    slm = phone_now.isoformat()  # phone-derived timestamps are skewed
    # Before the resync: two rows; only one will be re-sent.
    ingest(client, sleep=[sleep_rec("kept", *NIGHT, slm=slm), sleep_rec("dropped", "2026-10-04T23:00:00+05:30", "2026-10-05T07:00:00+05:30", slm=slm)])
    ingest(client, full_resync_begin=True, sleep=[sleep_rec("kept", *NIGHT, slm=slm, seq=2)])
    # Written during the resync (new record) must survive.
    ingest(client, sleep=[sleep_rec("new", "2026-10-05T23:00:00+05:30", "2026-10-06T07:00:00+05:30", slm=slm, seq=2)])
    out = ingest(client, run_complete={"data_through": phone_now.isoformat(), "full_resync": True})
    assert out["swept"] == 1
    with engine.connect() as conn:
        ids = sorted(r[0] for r in conn.execute(select(sleep_sessions.c.source_record_id)))
    assert ids == ["kept", "new"]
