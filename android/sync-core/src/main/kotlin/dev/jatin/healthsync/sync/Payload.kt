package dev.jatin.healthsync.sync

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.time.Instant
import java.time.ZoneOffset

/*
 * Wire models for POST /v1/ingest. These mirror backend/app/schemas.py
 * field-for-field (the backend forbids unknown fields); see PayloadContractTest.
 * Timestamps are ISO-8601 strings with an offset; dates are YYYY-MM-DD.
 */

@Serializable
data class StageIn(val stage: String, val start: String, val end: String)

@Serializable
data class SleepIn(
    @SerialName("source_record_id") val sourceRecordId: String,
    @SerialName("start_time") val startTime: String,
    @SerialName("end_time") val endTime: String,
    @SerialName("start_offset") val startOffset: String,
    @SerialName("end_offset") val endOffset: String,
    val stages: List<StageIn> = emptyList(),
    @SerialName("data_origin") val dataOrigin: String? = null,
    @SerialName("source_last_modified") val sourceLastModified: String? = null,
    @SerialName("compute_seq") val computeSeq: Long = 0,
)

@Serializable
data class DailyIn(
    val date: String,
    val steps: Long? = null,
    @SerialName("resting_hr") val restingHr: Double? = null,
    @SerialName("hrv_ms") val hrvMs: Double? = null,
    @SerialName("heart_rate_min") val heartRateMin: Double? = null,
    @SerialName("heart_rate_max") val heartRateMax: Double? = null,
    @SerialName("heart_rate_avg") val heartRateAvg: Double? = null,
    @SerialName("utc_offset") val utcOffset: String? = null,
    @SerialName("compute_seq") val computeSeq: Long = 0,
)

@Serializable
data class ExerciseIn(
    @SerialName("source_record_id") val sourceRecordId: String,
    @SerialName("start_time") val startTime: String,
    @SerialName("end_time") val endTime: String,
    @SerialName("start_offset") val startOffset: String,
    @SerialName("end_offset") val endOffset: String,
    @SerialName("exercise_type") val exerciseType: String,
    @SerialName("distance_m") val distanceM: Double? = null,
    val calories: Double? = null,
    @SerialName("avg_hr") val avgHr: Double? = null,
    @SerialName("max_hr") val maxHr: Double? = null,
    val steps: Long? = null,
    @SerialName("data_origin") val dataOrigin: String? = null,
    @SerialName("source_last_modified") val sourceLastModified: String? = null,
    @SerialName("compute_seq") val computeSeq: Long = 0,
)

@Serializable
data class DeletedIn(val type: String, @SerialName("source_record_id") val sourceRecordId: String)

@Serializable
data class RecoveryIn(
    val state: String = "partial_gap",
    @SerialName("gap_start") val gapStart: String,
    @SerialName("gap_end") val gapEnd: String,
)

@Serializable
data class RunCompleteIn(
    @SerialName("data_through") val dataThrough: String,
    @SerialName("full_resync") val fullResync: Boolean = false,
)

@Serializable
data class RunReportIn(
    val result: String,
    @SerialName("duration_ms") val durationMs: Long,
    @SerialName("error_category") val errorCategory: String? = null,
)

@Serializable
data class IngestPayload(
    @SerialName("run_id") val runId: String,
    @SerialName("full_resync_begin") val fullResyncBegin: Boolean = false,
    val sleep: List<SleepIn> = emptyList(),
    @SerialName("daily_metrics") val dailyMetrics: List<DailyIn> = emptyList(),
    val exercise: List<ExerciseIn> = emptyList(),
    val deleted: List<DeletedIn> = emptyList(),
    val recovery: RecoveryIn? = null,
    @SerialName("run_complete") val runComplete: RunCompleteIn? = null,
    @SerialName("run_report") val runReport: RunReportIn? = null,
) {
    val itemCount: Int get() = sleep.size + dailyMetrics.size + exercise.size + deleted.size
}

@Serializable
data class IngestResponse(
    @SerialName("sleep_upserted") val sleepUpserted: Int = 0,
    @SerialName("daily_metrics_upserted") val dailyMetricsUpserted: Int = 0,
    @SerialName("exercise_upserted") val exerciseUpserted: Int = 0,
    val deleted: Int = 0,
    @SerialName("stale_skipped") val staleSkipped: Int = 0,
    val swept: Int = 0,
    @SerialName("resync_started_at") val resyncStartedAt: String? = null,
)

@Serializable
data class StatusResponse(
    @SerialName("max_compute_seq") val maxComputeSeq: Long = 0,
    /** Server-owned boundary of an in-progress full resync, null when none is active. */
    @SerialName("resync_started_at") val resyncStartedAt: String? = null,
    @SerialName("last_full_sync_at") val lastFullSyncAt: String? = null,
)

/** Omits nulls (the backend treats a missing optional field as null) and ignores new response fields. */
val WireJson = Json {
    encodeDefaults = true
    explicitNulls = false
    ignoreUnknownKeys = true
}

// --- Formatting & sanitising to the backend's validation bounds ---------------

/** "+05:30" style; never "Z" (the backend's offset pattern requires ±HH:MM). */
fun formatOffset(offset: ZoneOffset): String {
    val total = offset.totalSeconds
    val sign = if (total < 0) "-" else "+"
    val abs = kotlin.math.abs(total)
    return "%s%02d:%02d".format(sign, abs / 3600, (abs % 3600) / 60)
}

/** Always includes seconds (OffsetDateTime.toString() drops ":00"). */
fun iso(instant: Instant, offset: ZoneOffset): String =
    java.time.format.DateTimeFormatter.ISO_OFFSET_DATE_TIME.format(instant.atOffset(offset))

/** Out-of-range values become null rather than failing the whole batch with a 422. */
object Bounds {
    fun bpm(v: Double?): Double? = v?.takeIf { it in 20.0..250.0 }
    fun hrv(v: Double?): Double? = v?.takeIf { it in 0.0..500.0 }
    fun steps(v: Long?): Long? = v?.takeIf { it in 0..300_000 }
    fun distance(v: Double?): Double? = v?.takeIf { it in 0.0..1_000_000.0 }
    fun calories(v: Double?): Double? = v?.takeIf { it in 0.0..20_000.0 }

    private val recordId = Regex("^[A-Za-z0-9._:\\-]{1,128}$")
    private val origin = Regex("^[A-Za-z0-9._]{1,255}$")
    fun validRecordId(id: String) = recordId.matches(id)
    fun origin(o: String?): String? = o?.takeIf { origin.matches(it) }

    /** The backend rejects sessions that are not strictly positive or longer than 24h. */
    fun validSession(start: Instant, end: Instant) =
        end.isAfter(start) && java.time.Duration.between(start, end) <= java.time.Duration.ofHours(24)
}
