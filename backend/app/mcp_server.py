"""Read-only MCP tools over the stored daily aggregates."""

import datetime as dt
import json
from collections.abc import Callable
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app import metrics
from app.auth import DbTokenVerifier
from app.config import Settings

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

INSTRUCTIONS = """Personal, read-only health data synced from Health Connect (Fitbit).
All values are daily aggregates or per-session summaries; there are no raw samples.
Compare against the owner's own normal using the provided baselines: each value's
*_30d field is the mean of the previous 30 days (excluding that day), and each
tool's `baseline` block gives the 30-day mean and typical range (10th-90th
percentile) ending today. Baselines are null when fewer than 7 days have data.
Dates are calendar dates in the owner's home timezone (see get_data_status.user_tz).
Missing data is omitted or null, never zero. If data looks missing or stale,
call get_data_status before drawing conclusions."""


def compact(model: BaseModel) -> CallToolResult:
    """Structured content plus the same data as compact JSON text (the SDK default is indented)."""
    data = model.model_dump(mode="json")
    text = json.dumps(data, separators=(",", ":"))
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=data)


# --- Output models (published as outputSchema) -----------------------------


class Baseline(BaseModel):
    """The owner's normal over the 30 days ending today."""

    mean: int | float
    typical_low: int | float  # 10th percentile
    typical_high: int | float  # 90th percentile
    days_with_data: int
    window: str  # "YYYY-MM-DD..YYYY-MM-DD"


class SleepStages(BaseModel):
    deep_min: int | None
    rem_min: int | None
    light_min: int | None
    awake_min: int | None
    unknown_min: int | None


class SleepSession(BaseModel):
    date: str
    start: str
    end: str
    duration_min: int
    asleep_min: int
    nap: bool
    stages: SleepStages | None
    asleep_30d: int | None  # main nights only: mean asleep minutes over the previous 30 nights


class SleepResult(BaseModel):
    sleep: list[SleepSession]


class SleepSummary(BaseModel):
    days: int
    nights: int
    avg_sleep_min: int | None
    avg_deep_min: int | None
    avg_rem_min: int | None
    avg_awake_min: int | None
    min_sleep_min: int | None
    max_sleep_min: int | None
    baseline: dict[str, Baseline | None]  # asleep_min, deep_min, rem_min (main nights)


class HeartRateDay(BaseModel):
    date: str
    avg: int | None
    min: int | None
    max: int | None
    resting: int | None
    resting_30d: int | None


class HeartRateTrend(BaseModel):
    days: int
    data: list[HeartRateDay]
    baseline: dict[str, Baseline | None]  # resting, avg


class HrvDay(BaseModel):
    date: str
    hrv_ms: float
    hrv_30d: float | None


class HrvTrend(BaseModel):
    days: int
    data: list[HrvDay]
    baseline: dict[str, Baseline | None]  # hrv_ms


class StepsDay(BaseModel):
    date: str
    steps: int
    steps_30d: int | None


class ExerciseSession(BaseModel):
    start: str
    end: str
    type: str
    duration_min: int
    distance_m: int | None
    calories: int | None
    avg_hr: int | None
    max_hr: int | None
    steps: int | None


class ActivityResult(BaseModel):
    daily: list[StepsDay]
    exercise: list[ExerciseSession]
    baseline: dict[str, Baseline | None]  # steps


class Coverage(BaseModel):
    first: str | None
    last: str | None
    count: int


class Gap(BaseModel):
    start: str
    end: str
    reason: str


class LastRun(BaseModel):
    result: str
    duration_ms: int | None
    error_category: str | None


class DataStatus(BaseModel):
    today: str
    user_tz: str
    last_sync_completed_at: str | None
    hours_since_sync: float | None
    data_through: str | None
    coverage: dict[str, Coverage]
    current: bool
    history_complete: bool
    recovery_state: str
    known_gaps: list[Gap]
    last_full_sync_at: str | None
    historical_deletions: str
    last_run: LastRun | None


def build_mcp(settings: Settings, engine, clock: Callable[[], dt.datetime]) -> MCPServer:
    mcp = MCPServer(
        name="fitbit-health",
        instructions=INSTRUCTIONS,
        token_verifier=DbTokenVerifier(engine, settings),
        auth=AuthSettings(
            issuer_url=settings.issuer,
            resource_server_url=settings.resource_url,
            required_scopes=list(settings.scopes),
            validate_token_resource=True,
        ),
        log_level="WARNING",
    )
    source = settings.active_source

    def today() -> dt.date:
        return clock().astimezone(settings.tz).date()

    async def query(fn, *args):
        def run():
            with engine.connect() as conn:
                return fn(conn, source, *args)

        return await run_in_threadpool(run)

    def checked(fn, *args):
        try:
            return fn(*args)
        except metrics.QueryError as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool(annotations=READ_ONLY)
    async def get_sleep(start_date: str, end_date: str) -> Annotated[CallToolResult, SleepResult]:
        """Sleep sessions whose wake-up date falls in [start_date, end_date] (YYYY-MM-DD, max 365 days).

        Each session: wake-up date, start/end with the session's own UTC offset, duration and
        time asleep in minutes, nap flag (only the longest sleep per wake date is the main
        night), stage minutes (null when the device recorded no stages), and asleep_30d: the
        mean minutes asleep over the previous 30 main nights (null for naps / thin history)."""
        start, end = checked(metrics.date_range, start_date, end_date)
        return compact(SleepResult(sleep=await query(metrics.get_sleep, start, end, today())))

    @mcp.tool(annotations=READ_ONLY)
    async def get_sleep_summary(days: int = 7) -> Annotated[CallToolResult, SleepSummary]:
        """Averages over main nights (naps excluded) for the last `days` dates ending today (1-365).

        Averages are arithmetic means in minutes over nights that have data; `nights` says how
        many. Stage averages only use nights with stage data. `baseline` gives the 30-day normal
        (mean and typical 10th-90th percentile range) for asleep, deep and REM minutes."""
        start, end = checked(metrics.day_window, today(), days)
        return compact(SleepSummary(**await query(metrics.get_sleep_summary, start, end, days, today())))

    @mcp.tool(annotations=READ_ONLY)
    async def get_heart_rate_trend(days: int = 30) -> Annotated[CallToolResult, HeartRateTrend]:
        """Daily heart rate in bpm for the last `days` dates ending today (1-365).

        avg/min/max are over all samples that day; resting is the day's resting heart rate;
        resting_30d is the mean resting HR of the previous 30 days. `baseline` gives the 30-day
        normal (mean and typical range) for resting and average HR. Days without data are omitted."""
        start, end = checked(metrics.day_window, today(), days)
        return compact(HeartRateTrend(**await query(metrics.get_heart_rate_trend, start, end, days, today())))

    @mcp.tool(annotations=READ_ONLY)
    async def get_hrv_trend(days: int = 30) -> Annotated[CallToolResult, HrvTrend]:
        """Daily heart rate variability (RMSSD, milliseconds, usually one nightly value) for the
        last `days` dates ending today (1-365), each with hrv_30d (mean of the previous 30 days).
        `baseline` gives the 30-day normal (mean and typical range). Days without HRV are omitted."""
        start, end = checked(metrics.day_window, today(), days)
        return compact(HrvTrend(**await query(metrics.get_hrv_trend, start, end, days, today())))

    @mcp.tool(annotations=READ_ONLY)
    async def get_activity(start_date: str, end_date: str) -> Annotated[CallToolResult, ActivityResult]:
        """Daily step totals and exercise sessions for [start_date, end_date] (YYYY-MM-DD, max 365 days).

        Exercise sessions include type, duration in minutes, and where recorded: distance in
        metres, calories (kcal), average/max heart rate (bpm) and steps. Daily rows include
        steps_30d (mean of the previous 30 days); `baseline` gives the 30-day steps normal."""
        start, end = checked(metrics.date_range, start_date, end_date)
        return compact(ActivityResult(**await query(metrics.get_activity, start, end, today())))

    @mcp.tool(annotations=READ_ONLY)
    async def get_data_status() -> Annotated[CallToolResult, DataStatus]:
        """Data freshness and completeness. Call this when data looks missing or stale.

        current: the latest sync completed recently. history_complete: false while any known
        gap exists (only a full resync on the phone clears gaps). historical_deletions: deletions
        of daily data older than the given date may not be reflected until a full resync."""
        now = clock()

        def run():
            with engine.connect() as conn:
                return metrics.get_data_status(conn, source, now, settings.tz, settings.user_tz)

        return compact(DataStatus(**await run_in_threadpool(run)))

    return mcp
