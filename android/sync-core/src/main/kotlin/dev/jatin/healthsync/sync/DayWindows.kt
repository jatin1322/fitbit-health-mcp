package dev.jatin.healthsync.sync

import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneId
import java.time.ZoneOffset
import java.time.temporal.ChronoUnit

/**
 * Local-calendar day windows for Health Connect period aggregation.
 *
 * Daily aggregates always use LocalDateTime boundaries ([start of day, start of
 * next day)), never Instants, so Health Connect buckets each record by its own
 * zone offset and DST/offset changes are handled.
 */
object DayWindows {
    /** [from 00:00, (to + 1 day) 00:00) as local date-times. */
    fun range(from: LocalDate, to: LocalDate): Pair<LocalDateTime, LocalDateTime> {
        require(!to.isBefore(from)) { "to before from" }
        return from.atStartOfDay() to to.plusDays(1).atStartOfDay()
    }

    /** Inclusive date chunks of at most [maxDays] days, in order. */
    fun chunks(from: LocalDate, to: LocalDate, maxDays: Long = 90): List<Pair<LocalDate, LocalDate>> {
        require(maxDays >= 1)
        val out = mutableListOf<Pair<LocalDate, LocalDate>>()
        var start = from
        while (!start.isAfter(to)) {
            val end = minOf(start.plusDays(maxDays - 1), to)
            out += start to end
            start = end.plusDays(1)
        }
        return out
    }

    /** The zone's offset at the (first instant of the) given local day. Informational utc_offset. */
    fun offsetAt(date: LocalDate, zone: ZoneId): ZoneOffset = date.atStartOfDay(zone).offset

    /** Instant length of a local day in a zone (23h/25h on DST transition days). */
    fun dayLengthHours(date: LocalDate, zone: ZoneId): Long =
        ChronoUnit.HOURS.between(date.atStartOfDay(zone), date.plusDays(1).atStartOfDay(zone))

    /**
     * First date of the rolling recompute window: at least 7 days back, widened
     * to cover any gap since the last completed run (minus a 3-day overlap).
     */
    fun rollingStart(today: LocalDate, lastRunCompletedAt: Instant?, zone: ZoneId): LocalDate {
        val week = today.minusDays(7)
        if (lastRunCompletedAt == null) return week
        val sinceLast = lastRunCompletedAt.atZone(zone).toLocalDate().minusDays(3)
        return minOf(week, sinceLast)
    }
}
