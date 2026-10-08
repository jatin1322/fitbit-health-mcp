"""All aggregation semantics live here (see README "Semantics").

The pure functions at the top are used at ingest time; the query functions
below them back the MCP tools and never return more than daily aggregates.
"""

import datetime as dt
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import and_, func, select
from sqlalchemy.engine import Connection

from app.db import daily_metrics, exercise_sessions, sleep_sessions, sync_state

MAX_DAYS = 365

# Health Connect stage name -> stored stage.
STAGE_MAP = {
    "LIGHT": "light",
    "DEEP": "deep",
    "REM": "rem",
    "AWAKE": "awake",
    "AWAKE_IN_BED": "awake",
    "OUT_OF_BED": "awake",
    "SLEEPING": "asleep_unstaged",
    "UNKNOWN": "unknown",
}
ASLEEP_STAGES = ("light", "deep", "rem", "asleep_unstaged")


# ---------------------------------------------------------------------------
# Pure semantics
# ---------------------------------------------------------------------------


def parse_offset(offset: str) -> dt.timezone:
    sign = -1 if offset[0] == "-" else 1
    hours, minutes = int(offset[1:3]), int(offset[4:6])
    return dt.timezone(sign * dt.timedelta(hours=hours, minutes=minutes))


def local_date(instant: dt.datetime, offset: str) -> dt.date:
    return instant.astimezone(parse_offset(offset)).date()


def round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


@dataclass(frozen=True)
class Stage:
    stage: str  # stored stage name
    start: dt.datetime
    end: dt.datetime


def normalize_stages(
    session_start: dt.datetime, session_end: dt.datetime, raw: Iterable[tuple[str, dt.datetime, dt.datetime]]
) -> list[Stage]:
    """Map, clip to the session, sort by start, and trim overlaps.

    Where stages overlap, the earlier-starting stage keeps the overlap and the
    later one is trimmed to start where the earlier one ends. Ties on start keep
    input order. Stages left empty after clipping/trimming are dropped.
    """
    mapped = [
        (max(start, session_start), min(end, session_end), i, STAGE_MAP[name])
        for i, (name, start, end) in enumerate(raw)
    ]
    mapped.sort(key=lambda m: (m[0], m[2]))
    out: list[Stage] = []
    cursor = session_start
    for start, end, _, stage in mapped:
        start = max(start, cursor)
        if end <= start:
            continue
        out.append(Stage(stage, start, end))
        cursor = end
    return out


@dataclass(frozen=True)
class StageTotals:
    asleep: int
    light: int | None
    deep: int | None
    rem: int | None
    awake: int | None
    unknown: int | None


def stage_totals(session_start: dt.datetime, session_end: dt.datetime, stages: Sequence[Stage]) -> StageTotals:
    """Seconds per stage. Uncovered session time counts as unknown.

    A session with no stages at all is treated as entirely asleep, with the
    per-stage fields null.
    """
    duration = int((session_end - session_start).total_seconds())
    if not stages:
        return StageTotals(asleep=duration, light=None, deep=None, rem=None, awake=None, unknown=None)
    sums = {"light": 0, "deep": 0, "rem": 0, "awake": 0, "asleep_unstaged": 0, "unknown": 0}
    covered = 0
    for s in stages:
        secs = int((s.end - s.start).total_seconds())
        sums[s.stage] += secs
        covered += secs
    sums["unknown"] += max(duration - covered, 0)
    return StageTotals(
        asleep=sum(sums[k] for k in ASLEEP_STAGES),
        light=sums["light"],
        deep=sums["deep"],
        rem=sums["rem"],
        awake=sums["awake"],
        unknown=sums["unknown"],
    )


# ---------------------------------------------------------------------------
# Query-side validation
# ---------------------------------------------------------------------------


class QueryError(ValueError):
    """Safe-to-show validation error for tool arguments."""


def parse_date(value: str, name: str) -> dt.date:
    try:
        if len(value) != 10:
            raise ValueError
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        raise QueryError(f"{name} must be a date in YYYY-MM-DD format") from None


def date_range(start_date: str, end_date: str) -> tuple[dt.date, dt.date]:
    start, end = parse_date(start_date, "start_date"), parse_date(end_date, "end_date")
    if end < start:
        raise QueryError("end_date must not be before start_date")
    if (end - start).days + 1 > MAX_DAYS:
        raise QueryError(f"date range may cover at most {MAX_DAYS} days")
    return start, end


def day_window(today: dt.date, days: int) -> tuple[dt.date, dt.date]:
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DAYS:
        raise QueryError(f"days must be an integer between 1 and {MAX_DAYS}")
    return today - dt.timedelta(days=days - 1), today


def _minutes(seconds: int | None) -> int | None:
    return None if seconds is None else round_half_up(seconds / 60)


def _iso(instant: dt.datetime, offset: str) -> str:
    return instant.astimezone(parse_offset(offset)).isoformat()


def _num(x: float | None, ndigits: int = 0) -> float | int | None:
    if x is None:
        return None
    return round_half_up(x) if ndigits == 0 else round(x, ndigits)


def _mean(values: list[int]) -> int | None:
    return round_half_up(sum(values) / len(values)) if values else None


# ---------------------------------------------------------------------------
# Queries (read-only)
# ---------------------------------------------------------------------------


def get_sleep(conn: Connection, source: str, start: dt.date, end: dt.date) -> list[dict]:
    t = sleep_sessions
    rows = conn.execute(
        select(t)
        .where(and_(t.c.source == source, t.c.wake_date >= start, t.c.wake_date <= end))
        .order_by(t.c.start_time, t.c.id)
    ).mappings()
    out = []
    for r in rows:
        stages = None
        if r["deep_seconds"] is not None:
            stages = {
                "deep_min": _minutes(r["deep_seconds"]),
                "rem_min": _minutes(r["rem_seconds"]),
                "light_min": _minutes(r["light_seconds"]),
                "awake_min": _minutes(r["awake_seconds"]),
                "unknown_min": _minutes(r["unknown_seconds"]),
            }
        out.append(
            {
                "date": r["wake_date"].isoformat(),
                "start": _iso(r["start_time"], r["start_offset"]),
                "end": _iso(r["end_time"], r["end_offset"]),
                "duration_min": _minutes(r["duration_seconds"]),
                "asleep_min": _minutes(r["asleep_seconds"]),
                "nap": not r["is_main"],
                "stages": stages,
            }
        )
    return out


def get_sleep_summary(conn: Connection, source: str, start: dt.date, end: dt.date, days: int) -> dict:
    t = sleep_sessions
    rows = list(
        conn.execute(
            select(t.c.asleep_seconds, t.c.deep_seconds, t.c.rem_seconds, t.c.awake_seconds).where(
                and_(t.c.source == source, t.c.is_main, t.c.wake_date >= start, t.c.wake_date <= end)
            )
        ).mappings()
    )
    asleep = [_minutes(r["asleep_seconds"]) for r in rows]

    def stage_avg(col: str) -> int | None:
        return _mean([_minutes(r[col]) for r in rows if r[col] is not None])

    return {
        "days": days,
        "nights": len(rows),
        "avg_sleep_min": _mean(asleep),
        "avg_deep_min": stage_avg("deep_seconds"),
        "avg_rem_min": stage_avg("rem_seconds"),
        "avg_awake_min": stage_avg("awake_seconds"),
        "min_sleep_min": min(asleep) if asleep else None,
        "max_sleep_min": max(asleep) if asleep else None,
    }


def _daily(conn: Connection, source: str, start: dt.date, end: dt.date, *cols):
    t = daily_metrics
    return conn.execute(
        select(t.c.date, *[t.c[c] for c in cols])
        .where(and_(t.c.source == source, t.c.date >= start, t.c.date <= end))
        .order_by(t.c.date)
    ).mappings()


def get_heart_rate_trend(conn: Connection, source: str, start: dt.date, end: dt.date, days: int) -> dict:
    data = []
    for r in _daily(conn, source, start, end, "heart_rate_avg", "heart_rate_min", "heart_rate_max", "resting_hr"):
        item = {
            "date": r["date"].isoformat(),
            "avg": _num(r["heart_rate_avg"]),
            "min": _num(r["heart_rate_min"]),
            "max": _num(r["heart_rate_max"]),
            "resting": _num(r["resting_hr"]),
        }
        if any(v is not None for k, v in item.items() if k != "date"):
            data.append(item)
    return {"days": days, "data": data}


def get_hrv_trend(conn: Connection, source: str, start: dt.date, end: dt.date, days: int) -> dict:
    data = [
        {"date": r["date"].isoformat(), "hrv_ms": _num(r["hrv_ms"], 1)}
        for r in _daily(conn, source, start, end, "hrv_ms")
        if r["hrv_ms"] is not None
    ]
    return {"days": days, "data": data}


def get_activity(conn: Connection, source: str, start: dt.date, end: dt.date) -> dict:
    daily = [
        {"date": r["date"].isoformat(), "steps": r["steps"]}
        for r in _daily(conn, source, start, end, "steps")
        if r["steps"] is not None
    ]
    t = exercise_sessions
    rows = conn.execute(
        select(t)
        .where(and_(t.c.source == source, t.c.local_date >= start, t.c.local_date <= end))
        .order_by(t.c.start_time, t.c.id)
    ).mappings()
    exercise = [
        {
            "start": _iso(r["start_time"], r["start_offset"]),
            "end": _iso(r["end_time"], r["end_offset"]),
            "type": r["exercise_type"],
            "duration_min": _minutes(r["duration_seconds"]),
            "distance_m": _num(r["distance_m"]),
            "calories": _num(r["calories"]),
            "avg_hr": _num(r["avg_hr"]),
            "max_hr": _num(r["max_hr"]),
            "steps": r["steps"],
        }
        for r in rows
    ]
    return {"daily": daily, "exercise": exercise}


# A run is "current" if the backend holds complete data through a recent point.
CURRENT_MAX_AGE = dt.timedelta(hours=12)
# Every sync recomputes at least this many days of daily aggregates.
ROLLING_WINDOW_DAYS = 7


def _coverage(conn: Connection, table, date_col, source: str) -> dict:
    first, last, count = conn.execute(
        select(func.min(table.c[date_col]), func.max(table.c[date_col]), func.count()).where(
            table.c.source == source
        )
    ).one()
    return {
        "first": first.isoformat() if first else None,
        "last": last.isoformat() if last else None,
        "count": count,
    }


def get_data_status(conn: Connection, source: str, now: dt.datetime, tz: dt.tzinfo, user_tz: str) -> dict:
    state = conn.execute(select(sync_state).where(sync_state.c.source == source)).mappings().first()
    coverage = {
        "sleep": _coverage(conn, sleep_sessions, "wake_date", source),
        "daily_metrics": _coverage(conn, daily_metrics, "date", source),
        "exercise": _coverage(conn, exercise_sessions, "local_date", source),
    }
    base = {
        "today": now.astimezone(tz).date().isoformat(),
        "user_tz": user_tz,
        "coverage": coverage,
    }
    if state is None:
        return base | {
            "last_sync_completed_at": None,
            "hours_since_sync": None,
            "data_through": None,
            "current": False,
            "history_complete": False,
            "recovery_state": "ok",
            "known_gaps": [],
            "last_full_sync_at": None,
            "historical_deletions": "best_effort",
            "last_run": None,
        }
    gaps = state["known_gaps"] or []
    last_ok = state["client_last_success_at"]
    data_through = state["data_through"]
    hours = round((now - last_ok).total_seconds() / 3600, 1) if last_ok else None
    if data_through:
        boundary = data_through.astimezone(tz).date() - dt.timedelta(days=ROLLING_WINDOW_DAYS)
        deletions = f"best_effort_before:{boundary.isoformat()}"
    else:
        deletions = "best_effort"
    last_run = None
    if state["last_run_result"]:
        last_run = {
            "result": state["last_run_result"],
            "duration_ms": state["last_run_duration_ms"],
            "error_category": state["last_run_error_category"],
        }
    return base | {
        "last_sync_completed_at": last_ok.isoformat() if last_ok else None,
        "hours_since_sync": hours,
        "data_through": data_through.isoformat() if data_through else None,
        "current": bool(data_through and now - data_through <= CURRENT_MAX_AGE),
        "history_complete": bool(state["last_full_sync_at"]) and not gaps,
        "recovery_state": "partial_gap" if gaps else "ok",
        "known_gaps": gaps,
        "last_full_sync_at": state["last_full_sync_at"].isoformat() if state["last_full_sync_at"] else None,
        "historical_deletions": deletions,
        "last_run": last_run,
    }


def max_compute_seq(conn: Connection, source: str) -> int:
    values = [
        conn.execute(select(func.max(t.c.compute_seq)).where(t.c.source == source)).scalar()
        for t in (sleep_sessions, daily_metrics, exercise_sessions)
    ]
    return max((v for v in values if v is not None), default=0)
