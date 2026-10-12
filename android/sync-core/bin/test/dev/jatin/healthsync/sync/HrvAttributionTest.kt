package dev.jatin.healthsync.sync

import java.time.Instant
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.ZoneOffset
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class HrvAttributionTest {
    private val ist = ZoneOffset.ofHoursMinutes(5, 30)
    private fun t(s: String): Instant = OffsetDateTime.parse(s).toInstant()
    private fun r(s: String, ms: Double) = HrvReading(t(s), ist, ms)
    private val oct5 = LocalDate.parse("2026-10-05")
    private val oct6 = LocalDate.parse("2026-10-06")
    private val oct7 = LocalDate.parse("2026-10-07")

    // Night of Oct 5 -> 6: 22:30 to 06:30 IST, crossing midnight.
    private val night = SleepWindow(t("2026-10-05T22:30:00+05:30"), t("2026-10-06T06:30:00+05:30"), oct6)

    @Test
    fun `readings across midnight all count toward the wake-up date`() {
        val readings = listOf(
            r("2026-10-05T23:00:00+05:30", 100.0), // before midnight, same night
            r("2026-10-06T02:00:00+05:30", 110.0),
            r("2026-10-06T05:00:00+05:30", 120.0),
        )
        val means = HrvAttribution.dailyMeans(readings, listOf(night), oct5, oct7)
        assertEquals(mapOf(oct6 to 110.0), means)
    }

    @Test
    fun `old calendar-date bucketing would have split the night`() {
        // Without the session, the 23:00 reading lands on Oct 5 — the bug this fixes.
        val readings = listOf(r("2026-10-05T23:00:00+05:30", 100.0), r("2026-10-06T02:00:00+05:30", 110.0))
        val means = HrvAttribution.dailyMeans(readings, emptyList(), oct5, oct7)
        assertEquals(mapOf(oct5 to 100.0, oct6 to 110.0), means)
    }

    @Test
    fun `readings outside sleep keep their own date and session boundaries are inclusive`() {
        val readings = listOf(
            r("2026-10-05T22:30:00+05:30", 90.0), // exactly at sleep start
            r("2026-10-06T06:30:00+05:30", 130.0), // exactly at sleep end
            r("2026-10-06T15:00:00+05:30", 50.0), // daytime reading
        )
        val means = HrvAttribution.dailyMeans(readings, listOf(night), oct5, oct7)
        assertEquals(90.0, means[oct6]!!, 1e-9) // (90 + 130 + 50) / 3
        assertTrue(oct5 !in means)
    }

    @Test
    fun `dates outside the requested range are dropped`() {
        val readings = listOf(r("2026-10-05T23:00:00+05:30", 100.0))
        assertTrue(HrvAttribution.dailyMeans(readings, listOf(night), oct7, oct7).isEmpty())
        assertEquals(mapOf(oct6 to 100.0), HrvAttribution.dailyMeans(readings, listOf(night), oct6, oct6))
    }
}
