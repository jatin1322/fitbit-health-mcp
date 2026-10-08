"""POST /v1/ingest and GET /v1/status for the phone.

One request = one transaction. Rows are versioned by
(source_last_modified, compute_seq); a row is replaced only when the incoming
pair is >= the stored one (nulls lowest), so stale retries never regress data.
All operational timestamps (seen_at, resync_started_at, ...) use the database
clock, never the phone's.
"""

import datetime as dt
import hmac
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from pydantic import ValidationError
from sqlalchemy import case, delete, func, literal, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from app import metrics
from app.db import HEALTH_TABLES, daily_metrics, exercise_sessions, sleep_sessions, sync_state
from app.schemas import MAX_BODY_BYTES, DailyIn, ExerciseIn, IngestPayload, SleepIn

log = logging.getLogger("app.ingest")
router = APIRouter()

NEG_INF = dt.datetime(1, 1, 1, tzinfo=dt.UTC)


def validation_error_response(exc: ValidationError | list) -> JSONResponse:
    """422 listing field paths and error types only, never the submitted values."""
    errors = exc.errors() if isinstance(exc, ValidationError) else exc
    fields = [{"loc": [str(p) for p in e.get("loc", ())], "type": e.get("type", "invalid")} for e in errors[:50]]
    return JSONResponse(status_code=422, content={"error": "invalid_request", "fields": fields})


def require_phone_secret(request: Request) -> None:
    settings = request.app.state.settings
    header = request.headers.get("authorization", "")
    token = header[7:] if header[:7].lower() == "bearer " else ""
    if not token or not hmac.compare_digest(token.encode(), settings.phone_ingest_secret.encode()):
        raise HTTPException(status_code=401, detail="unauthorized")


async def read_limited_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="payload too large")
        chunks.append(chunk)
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


def _sleep_row(source: str, s: SleepIn) -> dict:
    stages = metrics.normalize_stages(s.start_time, s.end_time, [(st.stage.value, st.start, st.end) for st in s.stages])
    totals = metrics.stage_totals(s.start_time, s.end_time, stages)
    return {
        "source": source,
        "source_record_id": s.source_record_id,
        "start_time": s.start_time,
        "end_time": s.end_time,
        "start_offset": s.start_offset,
        "end_offset": s.end_offset,
        "wake_date": metrics.local_date(s.end_time, s.end_offset),
        "duration_seconds": int((s.end_time - s.start_time).total_seconds()),
        "stages_json": [
            {"stage": st.stage, "start": st.start.astimezone(dt.UTC).isoformat(), "end": st.end.astimezone(dt.UTC).isoformat()}
            for st in stages
        ],
        "asleep_seconds": totals.asleep,
        "deep_seconds": totals.deep,
        "light_seconds": totals.light,
        "rem_seconds": totals.rem,
        "awake_seconds": totals.awake,
        "unknown_seconds": totals.unknown,
        "data_origin": s.data_origin,
        "source_last_modified": s.source_last_modified,
        "compute_seq": s.compute_seq,
    }


def _exercise_row(source: str, e: ExerciseIn) -> dict:
    return {
        "source": source,
        "source_record_id": e.source_record_id,
        "start_time": e.start_time,
        "end_time": e.end_time,
        "start_offset": e.start_offset,
        "end_offset": e.end_offset,
        "local_date": metrics.local_date(e.start_time, e.start_offset),
        "exercise_type": e.exercise_type.value,
        "duration_seconds": int((e.end_time - e.start_time).total_seconds()),
        "distance_m": e.distance_m,
        "calories": e.calories,
        "avg_hr": e.avg_hr,
        "max_hr": e.max_hr,
        "steps": e.steps,
        "data_origin": e.data_origin,
        "source_last_modified": e.source_last_modified,
        "compute_seq": e.compute_seq,
    }


def _daily_row(source: str, d: DailyIn) -> dict:
    return {"source": source, **d.model_dump()}


def _version(row: dict) -> tuple[dt.datetime, int]:
    return (row.get("source_last_modified") or NEG_INF, row["compute_seq"])


def _dedupe(rows: list[dict], key: tuple[str, ...]) -> tuple[list[dict], int]:
    """Keep the highest-versioned row per key (Postgres can't upsert one key twice per statement)."""
    best: dict[tuple, dict] = {}
    for row in rows:
        k = tuple(row[c] for c in key)
        if k not in best or _version(row) >= _version(best[k]):
            best[k] = row
    return list(best.values()), len(rows) - len(best)


def _versioned_upsert(conn: Connection, table, rows: list[dict], key: tuple[str, ...]) -> tuple[int, int]:
    """Returns (applied, stale_skipped)."""
    if not rows:
        return 0, 0
    rows, dupes = _dedupe(rows, key)
    stmt = pg_insert(table).values(rows)
    ex = stmt.excluded
    has_slm = "source_last_modified" in table.c
    if has_slm:
        incoming = tuple_(func.coalesce(ex.source_last_modified, literal(NEG_INF)), ex.compute_seq)
        stored = tuple_(func.coalesce(table.c.source_last_modified, literal(NEG_INF)), table.c.compute_seq)
    else:
        incoming, stored = ex.compute_seq, table.c.compute_seq
    fresh = incoming >= stored
    data_cols = [c for c in rows[0] if c not in key]
    set_ = {c: case((fresh, ex[c]), else_=table.c[c]) for c in data_cols}
    set_["updated_at"] = case((fresh, func.now()), else_=table.c.updated_at)
    set_["seen_at"] = func.now()
    returning = [table.c[c] for c in key] + [table.c.compute_seq]
    if has_slm:
        returning.append(table.c.source_last_modified)
    stmt = stmt.on_conflict_do_update(index_elements=list(key), set_=set_).returning(*returning)
    sent = {tuple(r[c] for c in key): _version(r) for r in rows}
    applied = stale = 0
    for result in conn.execute(stmt).mappings():
        k = tuple(result[c] for c in key)
        stored_version = (result.get("source_last_modified") or NEG_INF, result["compute_seq"])
        if stored_version == sent[k]:
            applied += 1
        else:
            stale += 1
    return applied, stale + dupes


def _existing_wake_dates(conn: Connection, source: str, record_ids: list[str]) -> set[dt.date]:
    if not record_ids:
        return set()
    t = sleep_sessions
    rows = conn.execute(select(t.c.wake_date).where(t.c.source == source, t.c.source_record_id.in_(record_ids)))
    return {r[0] for r in rows}


RECOMPUTE_MAIN = text(
    """
    UPDATE sleep_sessions s SET is_main = (s.id = m.main_id)
    FROM (
        SELECT DISTINCT ON (wake_date) wake_date, id AS main_id
        FROM sleep_sessions
        WHERE source = :source AND wake_date = ANY(:dates)
        ORDER BY wake_date, asleep_seconds DESC, start_time ASC, id ASC
    ) m
    WHERE s.source = :source AND s.wake_date = m.wake_date
    """
)


def recompute_main_sleep(conn: Connection, source: str, dates: set[dt.date]) -> None:
    if dates:
        conn.execute(RECOMPUTE_MAIN, {"source": source, "dates": sorted(dates)})


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


class ResyncNotStarted(Exception):
    pass


def apply_ingest(conn: Connection, source: str, p: IngestPayload) -> dict:
    conn.execute(pg_insert(sync_state).values(source=source).on_conflict_do_nothing())
    state = conn.execute(select(sync_state).where(sync_state.c.source == source).with_for_update()).mappings().one()
    state_update: dict = {"last_ingest_at": func.now(), "updated_at": func.now()}
    resync_started_at = state["resync_started_at"]

    if p.full_resync_begin:
        # Recorded before any row of this batch is written; rows in this same
        # transaction get seen_at = now() == this value, so they survive the sweep.
        resync_started_at = conn.execute(select(func.now())).scalar_one()
        state_update["resync_started_at"] = resync_started_at

    affected_dates: set[dt.date] = set()
    sleep_rows = [_sleep_row(source, s) for s in p.sleep]
    affected_dates |= _existing_wake_dates(conn, source, [r["source_record_id"] for r in sleep_rows])
    affected_dates |= {r["wake_date"] for r in sleep_rows}
    key = ("source", "source_record_id")
    sleep_applied, s1 = _versioned_upsert(conn, sleep_sessions, sleep_rows, key)
    ex_applied, s2 = _versioned_upsert(conn, exercise_sessions, [_exercise_row(source, e) for e in p.exercise], key)
    daily_applied, s3 = _versioned_upsert(conn, daily_metrics, [_daily_row(source, d) for d in p.daily_metrics], ("source", "date"))

    deleted = 0
    for kind, table, date_col in (("sleep", sleep_sessions, "wake_date"), ("exercise", exercise_sessions, "local_date")):
        ids = [d.source_record_id for d in p.deleted if d.type == kind]
        if ids:
            gone = conn.execute(
                delete(table)
                .where(table.c.source == source, table.c.source_record_id.in_(ids))
                .returning(table.c[date_col])
            ).all()
            deleted += len(gone)
            if kind == "sleep":
                affected_dates |= {r[0] for r in gone}

    if p.recovery:
        gap = {
            "start": p.recovery.gap_start.astimezone(dt.UTC).isoformat(),
            "end": p.recovery.gap_end.astimezone(dt.UTC).isoformat(),
            "reason": "changes_token_expired_beyond_history",
        }
        gaps = list(state["known_gaps"] or [])
        if gap not in gaps:
            gaps.append(gap)
        state_update["known_gaps"] = gaps

    if p.run_report:
        state_update |= {
            "last_run_result": p.run_report.result,
            "last_run_duration_ms": p.run_report.duration_ms,
            "last_run_error_category": p.run_report.error_category.value if p.run_report.error_category else None,
        }

    swept = 0
    if p.run_complete:
        state_update["data_through"] = p.run_complete.data_through
        state_update["client_last_success_at"] = func.now()
        if p.run_complete.full_resync:
            if resync_started_at is None:
                raise ResyncNotStarted()
            for table in HEALTH_TABLES:
                stmt = delete(table).where(table.c.source == source, table.c.seen_at < resync_started_at)
                if table is sleep_sessions:
                    gone = conn.execute(stmt.returning(table.c.wake_date)).all()
                    affected_dates |= {r[0] for r in gone}
                    swept += len(gone)
                else:
                    swept += conn.execute(stmt).rowcount
            state_update |= {"known_gaps": [], "last_full_sync_at": func.now(), "resync_started_at": None}

    recompute_main_sleep(conn, source, affected_dates)
    conn.execute(update(sync_state).where(sync_state.c.source == source).values(**state_update))
    result = {
        "sleep_upserted": sleep_applied,
        "daily_metrics_upserted": daily_applied,
        "exercise_upserted": ex_applied,
        "deleted": deleted,
        "stale_skipped": s1 + s2 + s3,
        "swept": swept,
    }
    if p.full_resync_begin:
        # Informational only: the server-owned boundary the sweep will use.
        result["resync_started_at"] = resync_started_at.isoformat()
    return result


@router.post("/v1/ingest")
async def ingest(request: Request):
    require_phone_secret(request)
    body = await read_limited_body(request)
    try:
        payload = IngestPayload.model_validate_json(body)
    except ValidationError as exc:
        return validation_error_response(exc)
    settings, engine = request.app.state.settings, request.app.state.engine
    started = time.monotonic()

    def run() -> dict:
        with engine.begin() as conn:
            return apply_ingest(conn, settings.active_source, payload)

    try:
        counts = await run_in_threadpool(run)
    except ResyncNotStarted:
        return JSONResponse(status_code=409, content={"error": "full_resync_not_started"})
    log.info(
        "ingest ok run=%s sleep=%d daily=%d exercise=%d deleted=%d stale=%d swept=%d ms=%d",
        str(payload.run_id)[:8],
        counts["sleep_upserted"],
        counts["daily_metrics_upserted"],
        counts["exercise_upserted"],
        counts["deleted"],
        counts["stale_skipped"],
        counts["swept"],
        int((time.monotonic() - started) * 1000),
    )
    return counts


@router.get("/v1/status")
async def status(request: Request):
    require_phone_secret(request)
    settings, engine = request.app.state.settings, request.app.state.engine
    now = request.app.state.clock()

    def run() -> dict:
        with engine.connect() as conn:
            result = metrics.get_data_status(conn, settings.active_source, now, settings.tz, settings.user_tz)
            result["max_compute_seq"] = metrics.max_compute_seq(conn, settings.active_source)
            # Lets the phone tell "my resync already completed" from "server lost my begin".
            boundary = conn.execute(
                select(sync_state.c.resync_started_at).where(sync_state.c.source == settings.active_source)
            ).scalar()
            result["resync_started_at"] = boundary.isoformat() if boundary else None
            return result

    return await run_in_threadpool(run)
