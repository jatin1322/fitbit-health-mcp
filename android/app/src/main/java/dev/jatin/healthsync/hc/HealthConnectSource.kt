package dev.jatin.healthsync.hc

import android.os.RemoteException
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.HealthConnectFeatures
import androidx.health.connect.client.changes.DeletionChange
import androidx.health.connect.client.changes.UpsertionChange
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.DistanceRecord
import androidx.health.connect.client.records.ExerciseSessionRecord
import androidx.health.connect.client.records.HeartRateRecord
import androidx.health.connect.client.records.HeartRateVariabilityRmssdRecord
import androidx.health.connect.client.records.Record
import androidx.health.connect.client.records.RestingHeartRateRecord
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.records.TotalCaloriesBurnedRecord
import androidx.health.connect.client.records.metadata.DataOrigin
import androidx.health.connect.client.request.AggregateGroupByPeriodRequest
import androidx.health.connect.client.request.AggregateRequest
import androidx.health.connect.client.request.ChangesTokenRequest
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import dev.jatin.healthsync.sync.Bounds
import dev.jatin.healthsync.sync.Change
import dev.jatin.healthsync.sync.ChangesPage
import dev.jatin.healthsync.sync.ChangesTokenExpiredException
import dev.jatin.healthsync.sync.DailyIn
import dev.jatin.healthsync.sync.DayWindows
import dev.jatin.healthsync.sync.ErrorCategory
import dev.jatin.healthsync.sync.ExerciseIn
import dev.jatin.healthsync.sync.HealthSource
import dev.jatin.healthsync.sync.HealthSourceException
import dev.jatin.healthsync.sync.HrvAttribution
import dev.jatin.healthsync.sync.HrvReading
import dev.jatin.healthsync.sync.SleepWindow
import dev.jatin.healthsync.sync.Session
import dev.jatin.healthsync.sync.SessionPage
import dev.jatin.healthsync.sync.SessionType
import dev.jatin.healthsync.sync.SleepIn
import dev.jatin.healthsync.sync.StageIn
import dev.jatin.healthsync.sync.formatOffset
import dev.jatin.healthsync.sync.iso
import java.io.IOException
import java.time.Duration
import java.time.Instant
import java.time.LocalDate
import java.time.Period
import java.time.ZoneId
import java.time.ZoneOffset
import kotlin.reflect.KClass

/** Read permissions the release app requests (no write permissions). */
val READ_PERMISSIONS: Set<String> = setOf(
    HealthPermission.getReadPermission(SleepSessionRecord::class),
    HealthPermission.getReadPermission(HeartRateRecord::class),
    HealthPermission.getReadPermission(RestingHeartRateRecord::class),
    HealthPermission.getReadPermission(HeartRateVariabilityRmssdRecord::class),
    HealthPermission.getReadPermission(StepsRecord::class),
    HealthPermission.getReadPermission(ExerciseSessionRecord::class),
    HealthPermission.getReadPermission(DistanceRecord::class),
    HealthPermission.getReadPermission(TotalCaloriesBurnedRecord::class),
)
val EXTRA_PERMISSIONS: Set<String> = setOf(
    HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND,
    HealthPermission.PERMISSION_READ_HEALTH_DATA_HISTORY,
)

fun recordClass(kind: DataKind): KClass<out Record> = when (kind) {
    DataKind.SLEEP -> SleepSessionRecord::class
    DataKind.EXERCISE -> ExerciseSessionRecord::class
    DataKind.STEPS -> StepsRecord::class
    DataKind.HEART_RATE -> HeartRateRecord::class
    DataKind.RESTING_HEART_RATE -> RestingHeartRateRecord::class
    DataKind.HRV -> HeartRateVariabilityRmssdRecord::class
    DataKind.DISTANCE -> DistanceRecord::class
    DataKind.CALORIES -> TotalCaloriesBurnedRecord::class
}

/**
 * HealthSource backed by Health Connect. Every read and aggregate is filtered to
 * the user-confirmed data origins for that data type. Titles/notes are never read.
 */
class HealthConnectSource(
    private val client: HealthConnectClient,
    private val origins: () -> Map<String, Set<String>>,
    private val zone: () -> ZoneId,
) : HealthSource {
    @Volatile private var historyGranted = false

    /** Call before each run: refreshes whether full history may be read. */
    suspend fun refreshPermissions(): Set<String> {
        val granted = guard { client.permissionController.getGrantedPermissions() }
        historyGranted = HealthPermission.PERMISSION_READ_HEALTH_DATA_HISTORY in granted &&
            client.features.getFeatureStatus(HealthConnectFeatures.FEATURE_READ_HEALTH_DATA_HISTORY) ==
            HealthConnectFeatures.FEATURE_STATUS_AVAILABLE
        if (!granted.containsAll(READ_PERMISSIONS)) {
            throw HealthSourceException(ErrorCategory.HC_PERMISSION, "missing read permissions")
        }
        return granted
    }

    private fun filter(kind: DataKind): Set<DataOrigin> = (origins()[kind.key] ?: emptySet()).map(::DataOrigin).toSet()

    private fun kind(type: SessionType) = if (type == SessionType.SLEEP) DataKind.SLEEP else DataKind.EXERCISE

    override fun earliestReadable(now: Instant): Instant =
        if (historyGranted) HISTORY_FLOOR else now.minus(Duration.ofDays(30))

    override suspend fun changesToken(type: SessionType): String = guard {
        client.getChangesToken(ChangesTokenRequest(setOf(recordClass(kind(type))), filter(kind(type))))
    }

    override suspend fun readSessions(type: SessionType, from: Instant, to: Instant, pageToken: String?): SessionPage = guard {
        val k = kind(type)
        val response = client.readRecords(
            ReadRecordsRequest(recordClass(k), TimeRangeFilter.between(from, to), filter(k), pageSize = PAGE_SIZE, pageToken = pageToken),
        )
        SessionPage(response.records.mapNotNull { toSession(it) }, response.pageToken)
    }

    override suspend fun changes(type: SessionType, token: String): ChangesPage = guard {
        val response = client.getChanges(token)
        if (response.changesTokenExpired) throw ChangesTokenExpiredException()
        val allowed = origins()[kind(type).key] ?: emptySet()
        val changes = response.changes.mapNotNull { change ->
            when (change) {
                is UpsertionChange -> {
                    val record = change.record
                    if (record.metadata.dataOrigin.packageName !in allowed) null
                    else toSession(record)?.let { Change.Upsert(it) }
                }
                is DeletionChange -> Change.Delete(change.recordId)
                else -> null
            }
        }
        ChangesPage(changes, response.nextChangesToken, response.hasMore)
    }

    override suspend fun dailyMetrics(from: LocalDate, to: LocalDate): List<DailyIn> = guard {
        val z = zone()
        val (start, end) = DayWindows.range(from, to)
        val range = TimeRangeFilter.between(start, end) // LocalDateTime boundaries, never Instants
        val day = Period.ofDays(1)
        val steps = client.aggregateGroupByPeriod(
            AggregateGroupByPeriodRequest(setOf(StepsRecord.COUNT_TOTAL), range, day, filter(DataKind.STEPS)),
        ).associate { it.startTime.toLocalDate() to it.result[StepsRecord.COUNT_TOTAL] }
        val hr = client.aggregateGroupByPeriod(
            AggregateGroupByPeriodRequest(
                setOf(HeartRateRecord.BPM_AVG, HeartRateRecord.BPM_MIN, HeartRateRecord.BPM_MAX), range, day, filter(DataKind.HEART_RATE),
            ),
        ).associate { it.startTime.toLocalDate() to it.result }
        val resting = client.aggregateGroupByPeriod(
            AggregateGroupByPeriodRequest(setOf(RestingHeartRateRecord.BPM_AVG), range, day, filter(DataKind.RESTING_HEART_RATE)),
        ).associate { it.startTime.toLocalDate() to it.result[RestingHeartRateRecord.BPM_AVG] }
        val hrv = hrvDailyMeans(from, to, z)

        generateSequence(from) { it.plusDays(1) }.takeWhile { !it.isAfter(to) }.mapNotNull { date ->
            val h = hr[date]
            val row = DailyIn(
                date = date.toString(),
                steps = Bounds.steps(steps[date]),
                restingHr = Bounds.bpm(resting[date]?.toDouble()),
                hrvMs = Bounds.hrv(hrv[date]),
                heartRateMin = Bounds.bpm(h?.get(HeartRateRecord.BPM_MIN)?.toDouble()),
                heartRateMax = Bounds.bpm(h?.get(HeartRateRecord.BPM_MAX)?.toDouble()),
                heartRateAvg = Bounds.bpm(h?.get(HeartRateRecord.BPM_AVG)?.toDouble()),
                utcOffset = formatOffset(DayWindows.offsetAt(date, z)),
            )
            row.takeIf { listOf(it.steps, it.restingHr, it.hrvMs, it.heartRateMin, it.heartRateMax, it.heartRateAvg).any { v -> v != null } }
        }.toList()
    }

    /**
     * Mean RMSSD per date. Readings during a sleep session count toward that
     * session's wake-up date (see [HrvAttribution]); the read window is padded by
     * a day on both sides so nights that cross a range boundary are complete.
     */
    private suspend fun hrvDailyMeans(from: LocalDate, to: LocalDate, z: ZoneId): Map<LocalDate, Double> {
        val start = from.minusDays(1).atStartOfDay(z).toInstant()
        val end = to.plusDays(2).atStartOfDay(z).toInstant()
        val readings = mutableListOf<HrvReading>()
        val sleeps = mutableListOf<SleepWindow>()
        var pageToken: String? = null
        do {
            val response = client.readRecords(
                ReadRecordsRequest(HeartRateVariabilityRmssdRecord::class, TimeRangeFilter.between(start, end), filter(DataKind.HRV), pageSize = PAGE_SIZE, pageToken = pageToken),
            )
            response.records.mapTo(readings) { HrvReading(it.time, it.zoneOffset ?: z.rules.getOffset(it.time), it.heartRateVariabilityMillis) }
            pageToken = response.pageToken
        } while (!pageToken.isNullOrEmpty())
        pageToken = null
        do {
            val response = client.readRecords(
                ReadRecordsRequest(SleepSessionRecord::class, TimeRangeFilter.between(start, end), filter(DataKind.SLEEP), pageSize = PAGE_SIZE, pageToken = pageToken),
            )
            response.records.mapTo(sleeps) {
                SleepWindow(it.startTime, it.endTime, it.endTime.atOffset(offsetOr(it.endZoneOffset, it.endTime)).toLocalDate())
            }
            pageToken = response.pageToken
        } while (!pageToken.isNullOrEmpty())
        return HrvAttribution.dailyMeans(readings, sleeps, from, to)
    }

    // --- mapping ---------------------------------------------------------------------

    private fun offsetOr(offset: ZoneOffset?, at: Instant): ZoneOffset = offset ?: zone().rules.getOffset(at)

    private suspend fun toSession(record: Record): Session? = when (record) {
        is SleepSessionRecord -> sleep(record)
        is ExerciseSessionRecord -> exercise(record)
        else -> null
    }

    private fun sleep(r: SleepSessionRecord): Session? {
        val id = r.metadata.id
        if (!Bounds.validRecordId(id) || !Bounds.validSession(r.startTime, r.endTime)) return null
        val so = offsetOr(r.startZoneOffset, r.startTime)
        val eo = offsetOr(r.endZoneOffset, r.endTime)
        val stages = r.stages.filter { it.endTime.isAfter(it.startTime) }.map {
            StageIn(Mappings.stageName(it.stage), iso(it.startTime, so), iso(it.endTime, so))
        }
        return Session(
            SessionType.SLEEP, id, r.startTime,
            sleep = SleepIn(
                sourceRecordId = id,
                startTime = iso(r.startTime, so),
                endTime = iso(r.endTime, eo),
                startOffset = formatOffset(so),
                endOffset = formatOffset(eo),
                stages = stages,
                dataOrigin = Bounds.origin(r.metadata.dataOrigin.packageName),
                sourceLastModified = r.metadata.lastModifiedTime.toString(),
            ),
        )
    }

    private suspend fun exercise(r: ExerciseSessionRecord): Session? {
        val id = r.metadata.id
        if (!Bounds.validRecordId(id) || !Bounds.validSession(r.startTime, r.endTime)) return null
        val so = offsetOr(r.startZoneOffset, r.startTime)
        val eo = offsetOr(r.endZoneOffset, r.endTime)
        val window = TimeRangeFilter.between(r.startTime, r.endTime)
        suspend fun <T : Any> agg(metric: androidx.health.connect.client.aggregate.AggregateMetric<T>, kind: DataKind): T? =
            client.aggregate(AggregateRequest(setOf(metric), window, filter(kind)))[metric]
        return Session(
            SessionType.EXERCISE, id, r.startTime,
            exercise = ExerciseIn(
                sourceRecordId = id,
                startTime = iso(r.startTime, so),
                endTime = iso(r.endTime, eo),
                startOffset = formatOffset(so),
                endOffset = formatOffset(eo),
                exerciseType = Mappings.exerciseType(r.exerciseType),
                distanceM = Bounds.distance(agg(DistanceRecord.DISTANCE_TOTAL, DataKind.DISTANCE)?.inMeters),
                calories = Bounds.calories(agg(TotalCaloriesBurnedRecord.ENERGY_TOTAL, DataKind.CALORIES)?.inKilocalories),
                avgHr = Bounds.bpm(agg(HeartRateRecord.BPM_AVG, DataKind.HEART_RATE)?.toDouble()),
                maxHr = Bounds.bpm(agg(HeartRateRecord.BPM_MAX, DataKind.HEART_RATE)?.toDouble()),
                steps = Bounds.steps(agg(StepsRecord.COUNT_TOTAL, DataKind.STEPS)),
                dataOrigin = Bounds.origin(r.metadata.dataOrigin.packageName),
                sourceLastModified = r.metadata.lastModifiedTime.toString(),
            ),
        )
    }

    /** Record counts per data origin for the last [days] days (origin picker). */
    suspend fun discoverOrigins(kind: DataKind, now: Instant, days: Long = 30, maxPages: Int = 5): Map<String, Int> = guard {
        val counts = mutableMapOf<String, Int>()
        var pageToken: String? = null
        var pages = 0
        do {
            val response = client.readRecords(
                ReadRecordsRequest(recordClass(kind), TimeRangeFilter.between(now.minus(Duration.ofDays(days)), now), pageSize = PAGE_SIZE, pageToken = pageToken),
            )
            response.records.forEach { counts.merge(it.metadata.dataOrigin.packageName, 1, Int::plus) }
            pageToken = response.pageToken
        } while (!pageToken.isNullOrEmpty() && ++pages < maxPages)
        counts
    }

    private suspend fun <T> guard(block: suspend () -> T): T = try {
        block()
    } catch (e: SecurityException) {
        throw HealthSourceException(ErrorCategory.HC_PERMISSION, "permission denied")
    } catch (e: RemoteException) {
        throw HealthSourceException(ErrorCategory.HC_UNAVAILABLE, "health connect unavailable")
    } catch (e: IOException) {
        throw HealthSourceException(ErrorCategory.HC_UNAVAILABLE, "health connect io")
    } catch (e: IllegalStateException) {
        throw HealthSourceException(ErrorCategory.HC_UNAVAILABLE, "health connect state")
    }

    companion object {
        const val PAGE_SIZE = 500
        /** Oldest data a full-history sync asks for. */
        val HISTORY_FLOOR: Instant = Instant.parse("2020-01-01T00:00:00Z")
    }
}
