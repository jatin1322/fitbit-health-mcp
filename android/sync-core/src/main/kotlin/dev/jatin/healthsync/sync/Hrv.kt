package dev.jatin.healthsync.sync

import java.time.Instant
import java.time.LocalDate
import java.time.ZoneOffset

/** One RMSSD reading as stored in Health Connect. */
data class HrvReading(val time: Instant, val offset: ZoneOffset, val ms: Double)

/** A sleep session's span and the local date of its end (its wake-up date). */
data class SleepWindow(val start: Instant, val end: Instant, val wakeDate: LocalDate)

/**
 * HRV is a sleep metric, so (like Fitbit) a reading taken during a sleep session
 * counts toward that session's wake-up date, even when the night crosses midnight.
 * Readings outside any sleep session keep their own local date. Returns the
 * arithmetic mean per date, limited to [from, to].
 */
object HrvAttribution {
    fun dailyMeans(readings: List<HrvReading>, sleeps: List<SleepWindow>, from: LocalDate, to: LocalDate): Map<LocalDate, Double> {
        val sorted = sleeps.sortedBy { it.start }
        val byDate = mutableMapOf<LocalDate, MutableList<Double>>()
        for (r in readings) {
            val session = sorted.firstOrNull { !r.time.isBefore(it.start) && !r.time.isAfter(it.end) }
            val date = session?.wakeDate ?: r.time.atOffset(r.offset).toLocalDate()
            if (!date.isBefore(from) && !date.isAfter(to)) byDate.getOrPut(date) { mutableListOf() } += r.ms
        }
        return byDate.mapValues { (_, v) -> v.average() }
    }
}
