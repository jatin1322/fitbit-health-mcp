"""One test per rule in the Semantics section (pure functions, no DB)."""

import datetime as dt

import pytest

from app import metrics
from app.metrics import QueryError, Stage, normalize_stages, stage_totals

T0 = dt.datetime(2026, 10, 7, 17, 30, tzinfo=dt.UTC)  # 23:00 IST


def at(minutes: int) -> dt.datetime:
    return T0 + dt.timedelta(minutes=minutes)


def test_stage_mapping():
    raw = [(name, at(i * 10), at(i * 10 + 10)) for i, name in enumerate(metrics.STAGE_MAP)]
    out = normalize_stages(at(0), at(80), raw)
    assert [s.stage for s in out] == [metrics.STAGE_MAP[n] for n in metrics.STAGE_MAP]
    assert {s.stage for s in out} == {"light", "deep", "rem", "awake", "asleep_unstaged", "unknown"}


def test_stages_clipped_to_session():
    out = normalize_stages(at(10), at(50), [("LIGHT", at(0), at(30)), ("DEEP", at(30), at(90))])
    assert out == [Stage("light", at(10), at(30)), Stage("deep", at(30), at(50))]


def test_overlap_earlier_start_wins():
    out = normalize_stages(at(0), at(60), [("DEEP", at(20), at(40)), ("LIGHT", at(0), at(30))])
    assert out == [Stage("light", at(0), at(30)), Stage("deep", at(30), at(40))]


def test_fully_contained_stage_dropped_and_tie_keeps_input_order():
    out = normalize_stages(
        at(0), at(60), [("REM", at(0), at(20)), ("DEEP", at(0), at(10)), ("LIGHT", at(5), at(15))]
    )
    assert out == [Stage("rem", at(0), at(20))]


def test_uncovered_time_is_unknown():
    stages = normalize_stages(at(0), at(60), [("LIGHT", at(0), at(20)), ("REM", at(40), at(50))])
    totals = stage_totals(at(0), at(60), stages)
    assert totals.light == 20 * 60 and totals.rem == 10 * 60
    assert totals.unknown == 30 * 60
    assert totals.asleep == 30 * 60


def test_unstaged_counts_as_asleep():
    stages = normalize_stages(at(0), at(60), [("SLEEPING", at(0), at(40)), ("AWAKE", at(40), at(60))])
    totals = stage_totals(at(0), at(60), stages)
    assert totals.asleep == 40 * 60
    assert totals.awake == 20 * 60
    assert totals.light == totals.deep == totals.rem == 0


def test_no_stages_means_all_asleep_and_null_stage_fields():
    totals = stage_totals(at(0), at(480), [])
    assert totals.asleep == 480 * 60
    assert totals.deep is None and totals.light is None and totals.awake is None and totals.unknown is None


def test_wake_date_uses_record_end_offset():
    end = dt.datetime(2026, 10, 7, 23, 30, tzinfo=dt.UTC)
    assert metrics.local_date(end, "+05:30") == dt.date(2026, 10, 8)
    assert metrics.local_date(end, "-04:00") == dt.date(2026, 10, 7)


def test_round_half_up():
    assert metrics.round_half_up(2.5) == 3
    assert metrics.round_half_up(3.5) == 4
    assert metrics.round_half_up(2.49) == 2


def test_day_window_ends_today_inclusive():
    today = dt.date(2026, 10, 8)
    assert metrics.day_window(today, 7) == (dt.date(2026, 10, 2), today)
    assert metrics.day_window(today, 1) == (today, today)


@pytest.mark.parametrize("bad", [0, -1, 366, True, 1.5, "7"])
def test_day_window_rejects_bad_days(bad):
    with pytest.raises(QueryError):
        metrics.day_window(dt.date(2026, 10, 8), bad)


def test_date_range_validation():
    assert metrics.date_range("2026-01-01", "2026-12-31") == (dt.date(2026, 1, 1), dt.date(2026, 12, 31))
    for start, end in [
        ("2026-13-01", "2026-12-31"),
        ("2026-1-1", "2026-01-02"),
        ("yesterday", "2026-01-02"),
        ("2026-02-01", "2026-01-01"),
        ("2025-01-01", "2026-01-01"),  # 366 days
    ]:
        with pytest.raises(QueryError):
            metrics.date_range(start, end)
