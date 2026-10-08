package dev.jatin.healthsync

import android.os.Build
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.Text
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.runtime.Composable
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.health.connect.client.HealthConnectClient
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
import androidx.health.connect.client.records.metadata.Metadata
import androidx.health.connect.client.units.Energy
import androidx.health.connect.client.units.Length
import kotlinx.coroutines.launch
import java.time.Duration
import java.time.LocalDate
import java.time.LocalTime
import java.time.ZoneId
import java.time.ZoneOffset
import kotlin.random.Random

/**
 * Debug-only: writes obviously fake data into Health Connect so the full pipeline
 * can be tested without real health data. Refuses to run on a physical device.
 */
object DebugTools {
    val writePermissions: Set<String> = setOf(
        HealthPermission.getWritePermission(SleepSessionRecord::class),
        HealthPermission.getWritePermission(HeartRateRecord::class),
        HealthPermission.getWritePermission(RestingHeartRateRecord::class),
        HealthPermission.getWritePermission(HeartRateVariabilityRmssdRecord::class),
        HealthPermission.getWritePermission(StepsRecord::class),
        HealthPermission.getWritePermission(ExerciseSessionRecord::class),
        HealthPermission.getWritePermission(DistanceRecord::class),
        HealthPermission.getWritePermission(TotalCaloriesBurnedRecord::class),
    )

    fun isEmulator(): Boolean =
        Build.FINGERPRINT.contains("generic") || Build.FINGERPRINT.contains("emulator") ||
            Build.HARDWARE.contains("ranchu") || Build.HARDWARE.contains("goldfish") ||
            Build.PRODUCT.contains("sdk")

    @Composable
    fun Section(client: HealthConnectClient, onMessage: (String) -> Unit) {
        val scope = rememberCoroutineScope()
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp)) {
                Text("Debug: fake data (emulator only)")
                Button(onClick = {
                    if (!isEmulator()) {
                        onMessage("Refused: fake data can only be generated on an emulator.")
                        return@Button
                    }
                    scope.launch {
                        onMessage(
                            runCatching { "Wrote ${generate(client)} fake records" }
                                .getOrElse { "Fake data failed (${it.javaClass.simpleName}: ${it.message})" }, // debug + fake data only
                        )
                    }
                }) { Text("Generate 21 days of fake data") }
            }
        }
    }

    /**
     * Mirrors backend/scripts/generate_sample.py edge cases, except overlapping
     * stages: Health Connect rejects those on write, so they cannot occur here.
     * Returns the number of records written.
     */
    suspend fun generate(client: HealthConnectClient, days: Int = 21): Int {
        val rng = Random(20261007)
        val zone = ZoneId.systemDefault()
        val today = LocalDate.now(zone)
        val trip = (8..10).map { today.minusDays(it.toLong()) }.toSet()
        val missingNight = today.minusDays(6)
        val unstagedNight = today.minusDays(4)
        val noHrv = setOf(today.minusDays(3), today.minusDays(12))
        val records = mutableListOf<Record>()
        fun meta() = Metadata.manualEntry()

        for (n in days downTo 1) {
            val wake = today.minusDays(n.toLong() - 1)
            val off: ZoneOffset = if (wake in trip) ZoneOffset.ofHours(1) else zone.rules.getOffset(wake.atStartOfDay(zone).toInstant())
            if (wake != missingNight) {
                val bed = wake.minusDays(1).atTime(LocalTime.of(22, 30)).plusMinutes(rng.nextLong(0, 90)).toInstant(off)
                val end = bed.plus(Duration.ofMinutes(rng.nextLong(380, 500)))
                val stages = mutableListOf<SleepSessionRecord.Stage>()
                if (wake != unstagedNight) {
                    var t = bed
                    var k = 0
                    val cycle = intArrayOf(SleepSessionRecord.STAGE_TYPE_LIGHT, SleepSessionRecord.STAGE_TYPE_DEEP, SleepSessionRecord.STAGE_TYPE_LIGHT, SleepSessionRecord.STAGE_TYPE_REM)
                    while (t.isBefore(end)) {
                        val type = if (k % 7 == 6) SleepSessionRecord.STAGE_TYPE_AWAKE else cycle[k % 4]
                        val next = minOf(t.plus(Duration.ofMinutes(rng.nextLong(10, 45))), end)
                        stages += SleepSessionRecord.Stage(t, next, type)
                        t = next
                        k++
                    }
                }
                records += SleepSessionRecord(bed, off, end, off, meta(), stages = stages)
                if (wake.dayOfWeek.value == 6) {
                    val napStart = wake.atTime(14, 0).toInstant(off)
                    records += SleepSessionRecord(napStart, off, napStart.plus(Duration.ofMinutes(35)), off, meta())
                }
            }
            // Daily records.
            val noon = wake.atTime(12, 0).toInstant(off)
            val resting = rng.nextLong(54, 62)
            records += RestingHeartRateRecord(wake.atTime(8, 0).toInstant(off), off, resting, meta())
            if (wake !in noHrv) records += HeartRateVariabilityRmssdRecord(wake.atTime(4, 0).toInstant(off), off, rng.nextDouble(32.0, 62.0), meta())
            records += StepsRecord(
                startTime = wake.atTime(9, 0).toInstant(off), startZoneOffset = off,
                endTime = wake.atTime(21, 0).toInstant(off), endZoneOffset = off,
                count = rng.nextLong(3500, 14000), metadata = meta(),
            )
            records += HeartRateRecord(
                noon, off, noon.plus(Duration.ofHours(1)), off,
                (0 until 6).map { HeartRateRecord.Sample(noon.plus(Duration.ofMinutes(10L * it)), resting + rng.nextLong(5, 40)) },
                meta(),
            )
            // Exercise every other day with distance, calories, HR and steps inside the session.
            if (n % 2 == 0) {
                val start = wake.atTime(18, 0).toInstant(off)
                val end = start.plus(Duration.ofMinutes(45))
                records += ExerciseSessionRecord(
                    startTime = start, startZoneOffset = off, endTime = end, endZoneOffset = off,
                    metadata = meta(), exerciseType = ExerciseSessionRecord.EXERCISE_TYPE_RUNNING,
                )
                records += DistanceRecord(start, off, end, off, Length.meters(rng.nextDouble(4000.0, 8000.0)), meta())
                records += TotalCaloriesBurnedRecord(start, off, end, off, Energy.kilocalories(rng.nextDouble(300.0, 600.0)), meta())
                records += HeartRateRecord(start, off, end, off, (0 until 9).map { HeartRateRecord.Sample(start.plus(Duration.ofMinutes(5L * it)), rng.nextLong(120, 175)) }, meta())
                records += StepsRecord(startTime = start, startZoneOffset = off, endTime = end, endZoneOffset = off, count = rng.nextLong(5000, 8000), metadata = meta())
            }
        }
        // Health Connect rejects records in the future (e.g. today's evening workout).
        val now = java.time.Instant.now()
        val past = records.filter { r ->
            val end = when (r) {
                is SleepSessionRecord -> r.endTime
                is ExerciseSessionRecord -> r.endTime
                is HeartRateRecord -> r.endTime
                is StepsRecord -> r.endTime
                is DistanceRecord -> r.endTime
                is TotalCaloriesBurnedRecord -> r.endTime
                is RestingHeartRateRecord -> r.time
                is HeartRateVariabilityRmssdRecord -> r.time
                else -> now
            }
            !end.isAfter(now)
        }
        past.chunked(500).forEach { client.insertRecords(it) }
        return past.size
    }
}
