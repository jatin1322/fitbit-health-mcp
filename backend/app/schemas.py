"""Strict ingest payload models.

Every model forbids unknown fields, and no field carries free text: values are
numbers, timestamps, enums or tightly-patterned identifiers.
"""

import datetime as dt
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

MAX_ITEMS = 2000
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_SESSION = dt.timedelta(hours=24)

Offset = Annotated[str, Field(pattern=r"^[+-](0\d|1[0-4]):[0-5]\d$")]
RecordId = Annotated[str, Field(pattern=r"^[A-Za-z0-9._:\-]{1,128}$")]
Origin = Annotated[str, Field(pattern=r"^[A-Za-z0-9._]{1,255}$")]
Seq = Annotated[int, Field(ge=0, le=2**62)]
Bpm = Annotated[float, Field(ge=20, le=250)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HcStage(str, Enum):
    """Health Connect SleepSessionRecord stage names, as sent by the phone."""

    AWAKE = "AWAKE"
    SLEEPING = "SLEEPING"
    OUT_OF_BED = "OUT_OF_BED"
    LIGHT = "LIGHT"
    DEEP = "DEEP"
    REM = "REM"
    AWAKE_IN_BED = "AWAKE_IN_BED"
    UNKNOWN = "UNKNOWN"


class ExerciseType(str, Enum):
    running = "running"
    walking = "walking"
    cycling = "cycling"
    swimming = "swimming"
    hiking = "hiking"
    strength_training = "strength_training"
    yoga = "yoga"
    pilates = "pilates"
    hiit = "hiit"
    elliptical = "elliptical"
    rowing = "rowing"
    stair_climbing = "stair_climbing"
    dancing = "dancing"
    sports = "sports"
    martial_arts = "martial_arts"
    other = "other"


class ErrorCategory(str, Enum):
    network = "network"
    auth = "auth"
    hc_permission = "hc_permission"
    hc_unavailable = "hc_unavailable"
    hc_token_expired = "hc_token_expired"
    server_4xx = "server_4xx"
    server_5xx = "server_5xx"
    unknown = "unknown"


class StageIn(Strict):
    stage: HcStage
    start: AwareDatetime
    end: AwareDatetime


def _check_interval(start: dt.datetime, end: dt.datetime) -> None:
    if end <= start:
        raise ValueError("end must be after start")
    if end - start > MAX_SESSION:
        raise ValueError("session longer than 24h")


class SleepIn(Strict):
    source_record_id: RecordId
    start_time: AwareDatetime
    end_time: AwareDatetime
    start_offset: Offset
    end_offset: Offset
    stages: list[StageIn] = Field(default_factory=list, max_length=500)
    data_origin: Origin | None = None
    source_last_modified: AwareDatetime | None = None
    compute_seq: Seq

    @model_validator(mode="after")
    def _interval(self) -> "SleepIn":
        _check_interval(self.start_time, self.end_time)
        for s in self.stages:
            if s.end <= s.start:
                raise ValueError("stage end must be after start")
        return self


class DailyIn(Strict):
    date: dt.date
    steps: Annotated[int, Field(ge=0, le=300_000)] | None = None
    resting_hr: Bpm | None = None
    hrv_ms: Annotated[float, Field(ge=0, le=500)] | None = None
    heart_rate_min: Bpm | None = None
    heart_rate_max: Bpm | None = None
    heart_rate_avg: Bpm | None = None
    utc_offset: Offset | None = None
    compute_seq: Seq


class ExerciseIn(Strict):
    source_record_id: RecordId
    start_time: AwareDatetime
    end_time: AwareDatetime
    start_offset: Offset
    end_offset: Offset
    exercise_type: ExerciseType
    distance_m: Annotated[float, Field(ge=0, le=1_000_000)] | None = None
    calories: Annotated[float, Field(ge=0, le=20_000)] | None = None
    avg_hr: Bpm | None = None
    max_hr: Bpm | None = None
    steps: Annotated[int, Field(ge=0, le=300_000)] | None = None
    data_origin: Origin | None = None
    source_last_modified: AwareDatetime | None = None
    compute_seq: Seq

    @model_validator(mode="after")
    def _interval(self) -> "ExerciseIn":
        _check_interval(self.start_time, self.end_time)
        return self


class DeletedIn(Strict):
    type: Literal["sleep", "exercise"]
    source_record_id: RecordId


class RecoveryIn(Strict):
    state: Literal["partial_gap"]
    gap_start: AwareDatetime
    gap_end: AwareDatetime

    @model_validator(mode="after")
    def _order(self) -> "RecoveryIn":
        if self.gap_end <= self.gap_start:
            raise ValueError("gap_end must be after gap_start")
        return self


class RunCompleteIn(Strict):
    data_through: AwareDatetime
    full_resync: bool = False


class RunReportIn(Strict):
    result: Literal["ok", "partial", "error"]
    duration_ms: Annotated[int, Field(ge=0, le=86_400_000)]
    error_category: ErrorCategory | None = None


class IngestPayload(Strict):
    run_id: UUID
    full_resync_begin: bool = False
    sleep: list[SleepIn] = Field(default_factory=list, max_length=MAX_ITEMS)
    daily_metrics: list[DailyIn] = Field(default_factory=list, max_length=MAX_ITEMS)
    exercise: list[ExerciseIn] = Field(default_factory=list, max_length=MAX_ITEMS)
    deleted: list[DeletedIn] = Field(default_factory=list, max_length=MAX_ITEMS)
    recovery: RecoveryIn | None = None
    run_complete: RunCompleteIn | None = None
    run_report: RunReportIn | None = None

    @model_validator(mode="after")
    def _total(self) -> "IngestPayload":
        total = len(self.sleep) + len(self.daily_metrics) + len(self.exercise) + len(self.deleted)
        if total > MAX_ITEMS:
            raise ValueError(f"at most {MAX_ITEMS} items per batch")
        return self
