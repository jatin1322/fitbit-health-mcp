package dev.jatin.healthsync.sync

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.yield
import java.io.File
import java.io.IOException
import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneId
import java.time.ZoneOffset
import kotlin.random.Random
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class DayWindowsTest {
    private val ny = ZoneId.of("America/New_York")

    @Test
    fun `normal day and midnight boundary`() {
        val d = LocalDate.parse("2026-10-07")
        assertEquals(LocalDateTime.parse("2026-10-07T00:00") to LocalDateTime.parse("2026-10-08T00:00"), DayWindows.range(d, d))
        assertEquals(24, DayWindows.dayLengthHours(d, ZoneId.of("Asia/Kolkata")))
    }

    @Test
    fun `multi day range and chunks`() {
        val r = DayWindows.range(LocalDate.parse("2026-01-30"), LocalDate.parse("2026-02-02"))
        assertEquals(LocalDateTime.parse("2026-01-30T00:00") to LocalDateTime.parse("2026-02-03T00:00"), r)
        val chunks = DayWindows.chunks(LocalDate.parse("2026-01-01"), LocalDate.parse("2026-12-31"), 90)
        assertEquals(LocalDate.parse("2026-01-01"), chunks.first().first)
        assertEquals(LocalDate.parse("2026-12-31"), chunks.last().second)
        chunks.zipWithNext().forEach { (a, b) -> assertEquals(a.second.plusDays(1), b.first) }
        assertTrue(chunks.all { (a, b) -> java.time.temporal.ChronoUnit.DAYS.between(a, b) < 90 })
    }

    @Test
    fun `DST transition days keep local boundaries and change length`() {
        val spring = LocalDate.parse("2026-03-08")
        val fall = LocalDate.parse("2026-11-01")
        assertEquals(LocalDateTime.parse("2026-03-08T00:00") to LocalDateTime.parse("2026-03-09T00:00"), DayWindows.range(spring, spring))
        assertEquals(23, DayWindows.dayLengthHours(spring, ny))
        assertEquals(25, DayWindows.dayLengthHours(fall, ny))
        assertEquals(ZoneOffset.ofHours(-5), DayWindows.offsetAt(spring, ny))
        assertEquals(ZoneOffset.ofHours(-4), DayWindows.offsetAt(spring.plusDays(1), ny))
    }

    @Test
    fun `offset change between zones`() {
        val d = LocalDate.parse("2026-09-15")
        assertEquals(ZoneOffset.ofHoursMinutes(5, 30), DayWindows.offsetAt(d, ZoneId.of("Asia/Kolkata")))
        assertEquals(ZoneOffset.ofHours(1), DayWindows.offsetAt(d, ZoneId.of("Europe/London")))
    }

    @Test
    fun `rolling start is a week or widened by the gap`() {
        val z = ZoneId.of("Asia/Kolkata")
        val today = LocalDate.parse("2026-10-18")
        assertEquals(LocalDate.parse("2026-10-11"), DayWindows.rollingStart(today, null, z))
        assertEquals(LocalDate.parse("2026-10-11"), DayWindows.rollingStart(today, Instant.parse("2026-10-18T00:00:00Z"), z))
        assertEquals(LocalDate.parse("2026-10-05"), DayWindows.rollingStart(today, Instant.parse("2026-10-08T03:30:00Z"), z))
    }
}

class PayloadTest {
    @Test
    fun `offsets never use Z`() {
        assertEquals("+00:00", formatOffset(ZoneOffset.UTC))
        assertEquals("+05:30", formatOffset(ZoneOffset.ofHoursMinutes(5, 30)))
        assertEquals("-03:30", formatOffset(ZoneOffset.ofHoursMinutes(-3, -30)))
        assertEquals("+14:00", formatOffset(ZoneOffset.ofHours(14)))
    }

    @Test
    fun `iso timestamps always include seconds`() {
        assertEquals("2026-09-02T05:30:00+05:30", iso(Instant.parse("2026-09-02T00:00:00Z"), ZoneOffset.ofHoursMinutes(5, 30)))
        assertEquals("2026-09-02T00:00:00.5Z", iso(Instant.parse("2026-09-02T00:00:00.5Z"), ZoneOffset.UTC))
    }

    @Test
    fun `out of range values become null instead of failing the batch`() {
        assertNull(Bounds.bpm(260.0))
        assertNull(Bounds.bpm(10.0))
        assertEquals(57.0, Bounds.bpm(57.0))
        assertNull(Bounds.steps(400_000))
        assertNull(Bounds.distance(-1.0))
        assertNull(Bounds.origin("bad origin!"))
        assertFalse(Bounds.validRecordId("has space"))
        assertFalse(Bounds.validSession(Instant.parse("2026-10-01T00:00:00Z"), Instant.parse("2026-10-02T00:00:01Z")))
        assertTrue(Bounds.validSession(Instant.parse("2026-10-01T00:00:00Z"), Instant.parse("2026-10-01T08:00:00Z")))
    }

    @Test
    fun `batcher respects item and byte limits`() {
        val daily = (1..25).map { DailyIn("2026-09-%02d".format(it), steps = it.toLong()) }
        val byItems = Batcher(maxItems = 10).split("r", daily = daily)
        assertEquals(listOf(10, 10, 5), byItems.map { it.itemCount })
        val bySize = Batcher(maxBytes = 300).split("r", daily = daily)
        assertTrue(bySize.size > 3)
        assertEquals(25, bySize.sumOf { it.itemCount })
    }

    /**
     * Writes the canonical payload the backend contract test validates
     * (backend/tests/test_contract.py reads this exact file).
     */
    @Test
    fun `contract fixture matches the committed file`() {
        val payload = IngestPayload(
            runId = "1b7c4c6e-6f5e-4f5e-9d1e-2a9e0e4a8b11",
            fullResyncBegin = true,
            sleep = listOf(
                SleepIn(
                    "hc-sleep-1", "2026-10-06T23:00:00+05:30", "2026-10-07T07:00:00.123456789+05:30", "+05:30", "+05:30",
                    stages = listOf(StageIn("LIGHT", "2026-10-06T23:00:00+05:30", "2026-10-07T01:00:00+05:30"), StageIn("SLEEPING", "2026-10-07T01:00:00+05:30", "2026-10-07T07:00:00+05:30")),
                    dataOrigin = "com.google.android.apps.fitness", sourceLastModified = "2026-10-07T01:40:00Z", computeSeq = 7,
                ),
            ),
            dailyMetrics = listOf(DailyIn("2026-10-07", steps = 8241, restingHr = 57.0, hrvMs = 48.3, heartRateMin = 49.0, heartRateMax = 132.0, heartRateAvg = 64.0, utcOffset = "+05:30", computeSeq = 7)),
            exercise = listOf(ExerciseIn("hc-ex-1", "2026-10-07T18:00:00+05:30", "2026-10-07T18:45:00+05:30", "+05:30", "+05:30", "running", 6200.0, 410.0, 151.0, 178.0, 7400, "com.google.android.apps.fitness", "2026-10-07T13:20:00Z", 7)),
            deleted = listOf(DeletedIn("sleep", "hc-sleep-0")),
            recovery = RecoveryIn(gapStart = "2026-08-01T00:00:00Z", gapEnd = "2026-08-20T00:00:00Z"),
            runComplete = RunCompleteIn("2026-10-08T03:30:00Z", fullResync = true),
            runReport = RunReportIn("ok", 1234, null),
        )
        val json = WireJson.encodeToString(IngestPayload.serializer(), payload)
        val fixture = File("../../backend/tests/fixtures/android_ingest_payload.json")
        if (System.getenv("UPDATE_CONTRACT") == "1") {
            fixture.parentFile.mkdirs()
            fixture.writeText(json + "\n")
        }
        assertEquals(fixture.readText().trim(), json, "run with UPDATE_CONTRACT=1 and re-run the backend contract test")
    }
}

class RetryingUploaderTest {
    private class ScriptedTransport(vararg steps: Any) : Transport {
        val script = steps.toMutableList()
        var calls = 0
        private suspend fun next(): HttpResult {
            calls++
            return when (val s = script.removeAt(0)) {
                is IOException -> throw s
                is HttpResult -> s
                else -> error("bad step")
            }
        }
        override suspend fun get(path: String) = next()
        override suspend fun post(path: String, json: String) = next()
    }

    private val ok = HttpResult(200, """{"sleep_upserted":1,"new_field":true}""")
    private val sleeps = mutableListOf<Long>()
    private fun uploader(t: Transport) = RetryingUploader(t, { sleeps += it }, random = Random(1))

    @Test
    fun `5xx and IO errors are retried with backoff then succeed`() = runTest {
        val t = ScriptedTransport(HttpResult(503, ""), IOException("reset"), ok)
        assertEquals(1, uploader(t).ingest(IngestPayload("r")).sleepUpserted)
        assertEquals(3, t.calls)
        assertEquals(2, sleeps.size)
        assertTrue(sleeps[1] >= 2000)
    }

    @Test
    fun `429 honours Retry-After`() = runTest {
        val t = ScriptedTransport(HttpResult(429, "", retryAfterSeconds = 7), ok)
        uploader(t).ingest(IngestPayload("r"))
        assertEquals(listOf(7000L), sleeps)
    }

    @Test
    fun `retries are bounded`() = runTest {
        val t = ScriptedTransport(HttpResult(500, ""), HttpResult(500, ""), HttpResult(500, ""), HttpResult(500, ""))
        val e = assertFailsWith<UploadException> { uploader(t).ingest(IngestPayload("r")) }
        assertEquals(ErrorCategory.SERVER_5XX, e.category)
        assertEquals(4, t.calls)
        val io = ScriptedTransport(IOException(), IOException(), IOException(), IOException())
        assertEquals(ErrorCategory.NETWORK, assertFailsWith<UploadException> { uploader(io).status() }.category)
    }

    @Test
    fun `auth, validation and conflict errors are not retried`() = runTest {
        for ((code, category) in listOf(401 to ErrorCategory.AUTH, 403 to ErrorCategory.AUTH, 422 to ErrorCategory.SERVER_4XX, 400 to ErrorCategory.SERVER_4XX)) {
            val t = ScriptedTransport(HttpResult(code, ""), ok)
            val e = assertFailsWith<UploadException> { uploader(t).ingest(IngestPayload("r")) }
            assertEquals(category, e.category)
            assertFalse(e.resyncRequired)
            assertEquals(1, t.calls)
        }
        val conflict = assertFailsWith<UploadException> { uploader(ScriptedTransport(HttpResult(409, ""))).ingest(IngestPayload("r")) }
        assertTrue(conflict.resyncRequired)
    }
}

class CoordinatorTest {
    @Test
    fun `full resync waits for a running sync and never interleaves uploads`() = runTest {
        val hc = FakeHealth()
        repeat(5) { i -> hc.put(sleepSession("sleep-$i", T0.plusSeconds(86_400L * i))) }
        val store = InMemoryStateStore(confirmed())
        val server = FakeServer()
        val release = CompletableDeferred<Unit>()
        val log = mutableListOf<String>()
        var blockFirst = true
        server.gate = {
            if (blockFirst) {
                blockFirst = false
                release.await()
            }
        }
        val clock = FixedClock(Instant.parse("2026-10-08T03:30:00Z"))
        var runCounter = 0
        val engine = SyncEngine(hc, server, store, clock, { ZoneOffset.UTC }, newRunId = { "run-${++runCounter}" })
        val coordinator = SyncCoordinator(engine, store)
        // First full sync completes so the next one is incremental.
        blockFirst = false
        coordinator.sync()
        val before = server.payloads.size
        blockFirst = true
        val first = async { coordinator.sync() }
        yield()
        assertTrue(coordinator.isRunning)
        val second = async {
            log += "resync-requested"
            coordinator.fullResync()
        }
        yield()
        // While the first run is blocked, the resync must not have reset state.
        assertFalse(store.state.fullResyncPending)
        log += "release"
        release.complete(Unit)
        first.await()
        second.await()
        val runs = server.payloads.drop(before).map { it.runId }
        // All of run 2's uploads precede all of run 3's (no interleaving).
        assertEquals(runs.sorted(), runs.sortedBy { it.removePrefix("run-").toInt() })
        assertEquals(runs, runs.sortedBy { it.removePrefix("run-").toInt() })
        assertTrue(server.payloads.last().runComplete?.fullResync == true)
    }

    @Test
    fun `changing origins forces a full resync, same origins do not`() = runTest {
        val hc = FakeHealth()
        hc.put(sleepSession("sleep-1", T0))
        val store = InMemoryStateStore(confirmed())
        val server = FakeServer()
        val coordinator = SyncCoordinator(engine(hc, server, store, FixedClock(Instant.parse("2026-10-08T03:30:00Z"))), store)
        coordinator.sync()
        val tokens = store.state.types.values.map { it.token }
        coordinator.setOrigins(store.state.allowedOrigins) // unchanged
        assertFalse(store.state.fullResyncPending)
        coordinator.setOrigins(mapOf("sleep" to setOf("com.example.fake"), "steps" to setOf("com.example.steps")))
        val s = store.state
        assertTrue(s.fullResyncPending)
        assertFalse(s.resyncBeginAcked)
        SessionType.entries.forEach { assertEquals(Phase.NEW, s.type(it).phase); assertNull(s.type(it).token) }
        assertTrue(tokens.none { it == null })
    }

    @Test
    fun `reset after restore drops tokens and requires confirmation again`() = runTest {
        val store = InMemoryStateStore(confirmed().copy(localSeq = 9))
        val coordinator = SyncCoordinator(engine(FakeHealth(), FakeServer(), store, FixedClock(Instant.now())), store)
        coordinator.resetAll()
        assertFalse(store.state.originsConfirmed)
        assertTrue(store.state.fullResyncPending)
        assertEquals(SyncEngine.Outcome.NotConfigured, coordinator.sync())
    }
}
