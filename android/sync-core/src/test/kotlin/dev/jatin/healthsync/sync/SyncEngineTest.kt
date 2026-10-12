package dev.jatin.healthsync.sync

import kotlinx.coroutines.test.runTest
import java.time.Duration
import java.time.Instant
import java.time.LocalDate
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertIs
import kotlin.test.assertNull
import kotlin.test.assertTrue

class SyncEngineTest {
    private val now = Instant.parse("2026-10-08T03:30:00Z")

    private fun world(): Triple<FakeHealth, FakeServer, InMemoryStateStore> {
        val hc = FakeHealth()
        repeat(20) { i -> hc.put(sleepSession("sleep-$i", T0.plus(Duration.ofHours(24L * i + 17)))) }
        repeat(6) { i -> hc.put(exerciseSession("exercise-$i", T0.plus(Duration.ofHours(24L * i * 5 + 12)), 5000.0)) }
        hc.daily = { d -> DailyIn(d.toString(), steps = 1000L + d.dayOfMonth) }
        return Triple(hc, FakeServer(), InMemoryStateStore(confirmed()))
    }

    private fun expectedSleep(hc: FakeHealth) = hc.records.values.filter { it.type == SessionType.SLEEP }.map { it.id }.sorted()
    private fun expectedExercise(hc: FakeHealth) = hc.records.values.filter { it.type == SessionType.EXERCISE }.map { it.id }.sorted()

    @Test
    fun `not configured until origins are confirmed`() = runTest {
        val (hc, server, _) = world()
        val outcome = engine(hc, server, InMemoryStateStore(), FixedClock(now)).run()
        assertEquals(SyncEngine.Outcome.NotConfigured, outcome)
        assertTrue(server.payloads.isEmpty())
    }

    @Test
    fun `first run is a full resync and uploads everything`() = runTest {
        val (hc, server, store) = world()
        val out = engine(hc, server, store, FixedClock(now)).run()
        assertIs<SyncEngine.Outcome.Ok>(out)
        assertTrue(out.fullResync)
        assertTrue(server.payloads.first().fullResyncBegin)
        assertEquals(true, server.payloads.last().runComplete?.fullResync)
        assertEquals(expectedSleep(hc), server.ids("sleep"))
        assertEquals(expectedExercise(hc), server.ids("exercise"))
        val s = store.state
        assertFalse(s.fullResyncPending)
        assertEquals(now.toEpochMilli(), s.lastRunCompletedAtMs)
        SessionType.entries.forEach { assertEquals(Phase.CHANGES, s.type(it).phase) }
        assertEquals(server.serverTime(1), s.resyncStartedAt)
        // Daily metrics cover every readable day up to today (full resync).
        assertEquals(LocalDate.parse("2026-08-01") to LocalDate.parse("2026-10-08"), hc.dailyRequests.first().first to hc.dailyRequests.last().second)
    }

    @Test
    fun `incremental changes upload upserts and deletions, delete wins in order`() = runTest {
        val (hc, server, store) = world()
        engine(hc, server, store, FixedClock(now)).run()
        hc.put(sleepSession("sleep-new", now.minus(Duration.ofHours(10))))
        hc.remove("sleep-3")
        hc.put(sleepSession("sleep-tmp", now.minus(Duration.ofHours(30))))
        hc.remove("sleep-tmp") // upsert then delete -> only the delete is sent
        hc.put(exerciseSession("exercise-9", now.minus(Duration.ofHours(5))))
        val before = server.payloads.size
        val out = engine(hc, server, store, FixedClock(now.plusSeconds(3600))).run()
        assertIs<SyncEngine.Outcome.Ok>(out)
        assertFalse(out.fullResync)
        assertEquals(expectedSleep(hc), server.ids("sleep"))
        assertEquals(expectedExercise(hc), server.ids("exercise"))
        val sent = server.payloads.drop(before)
        assertTrue(sent.none { p -> p.sleep.any { it.sourceRecordId == "sleep-tmp" } })
        assertTrue(sent.any { p -> p.deleted.any { it.sourceRecordId == "sleep-tmp" } })
        assertTrue(sent.none { it.fullResyncBegin })
    }

    @Test
    fun `deleted then recreated in one changes page is kept`() = runTest {
        val (hc, server, store) = world()
        engine(hc, server, store, FixedClock(now)).run()
        hc.remove("sleep-5")
        hc.put(sleepSession("sleep-5", T0.plus(Duration.ofHours(24L * 5 + 17)), minutes = 400))
        engine(hc, server, store, FixedClock(now.plusSeconds(60))).run()
        assertTrue("sleep-5" in server.ids("sleep"))
    }

    @Test
    fun `crash before every state write still converges to exactly the right rows`() = runTest {
        val (probeHc, probeServer, probeStore) = world()
        val probe = CrashingStore(probeStore, crashAt = Int.MAX_VALUE)
        engine(probeHc, probeServer, probe, FixedClock(now)).run()
        val totalUpdates = probe.updates
        for (crashAt in 1..totalUpdates) {
            val (hc, server, inner) = world()
            val crashed = engine(hc, server, CrashingStore(inner, crashAt), FixedClock(now)).run()
            assertIs<SyncEngine.Outcome.Failed>(crashed, "crashAt=$crashAt")
            val out = engine(hc, server, inner, FixedClock(now.plusSeconds(60))).run()
            assertIs<SyncEngine.Outcome.Ok>(out, "crashAt=$crashAt")
            assertEquals(expectedSleep(hc), server.ids("sleep"), "crashAt=$crashAt")
            assertEquals(expectedExercise(hc), server.ids("exercise"), "crashAt=$crashAt")
            assertFalse(inner.state.fullResyncPending, "crashAt=$crashAt")
        }
    }

    @Test
    fun `failure at every Health Connect call and lost 2xx responses are recoverable`() = runTest {
        val (probeHc, probeServer, probeStore) = world()
        engine(probeHc, probeServer, probeStore, FixedClock(now)).run()
        val hcCalls = probeHc.calls.size
        val uploads = probeServer.payloads.size
        for (n in 1..hcCalls) {
            val (hc, server, store) = world()
            hc.failAtCall = n
            assertIs<SyncEngine.Outcome.Failed>(engine(hc, server, store, FixedClock(now)).run())
            hc.failAtCall = null
            assertIs<SyncEngine.Outcome.Ok>(engine(hc, server, store, FixedClock(now.plusSeconds(60))).run(), "hc call $n")
            assertEquals(expectedSleep(hc), server.ids("sleep"), "hc call $n")
        }
        for (n in 1..uploads) {
            val (hc, server, store) = world()
            server.loseResponseAt = n
            assertIs<SyncEngine.Outcome.Failed>(engine(hc, server, store, FixedClock(now)).run())
            assertIs<SyncEngine.Outcome.Ok>(engine(hc, server, store, FixedClock(now.plusSeconds(60))).run(), "lost 2xx $n")
            assertEquals(expectedSleep(hc), server.ids("sleep"), "lost 2xx $n")
            assertEquals(expectedExercise(hc), server.ids("exercise"), "lost 2xx $n")
        }
    }

    @Test
    fun `interrupted full resync is resumed without a second begin so earlier slices survive`() = runTest {
        val (hc, server, store) = world()
        server.failBeforeAt = 4 // fail mid-scan, after begin + some slices
        assertIs<SyncEngine.Outcome.Failed>(engine(hc, server, store, FixedClock(now)).run())
        assertTrue(store.state.resyncBeginAcked)
        assertIs<SyncEngine.Outcome.Ok>(engine(hc, server, store, FixedClock(now.plusSeconds(60))).run())
        assertEquals(1, server.payloads.count { it.fullResyncBegin })
        assertEquals(expectedSleep(hc), server.ids("sleep"))
    }

    @Test
    fun `duplicate start timestamps on slice boundaries are uploaded exactly once despite interruptions`() = runTest {
        // Slices start at hc.earliest; put 12 sessions exactly on a boundary and 12 just before it.
        val boundary = Instant.parse("2026-08-01T00:00:00Z").plus(Duration.ofDays(14))
        fun seed(hc: FakeHealth) {
            hc.records.clear()
            repeat(12) { i -> hc.put(sleepSession("sleep-b$i", boundary, minutes = 60)) }
            repeat(12) { i -> hc.put(sleepSession("sleep-p$i", boundary.minusMillis(1), minutes = 60)) }
            hc.pageSize = 3
        }
        val probeHc = FakeHealth().also(::seed)
        val probeServer = FakeServer()
        engine(probeHc, probeServer, InMemoryStateStore(confirmed()), FixedClock(now)).run()
        // Clean run: every session appears in exactly one scan upload.
        val perId = probeServer.payloads.flatMap { it.sleep }.groupingBy { it.sourceRecordId }.eachCount()
        assertEquals(24, perId.size)
        assertTrue(perId.values.all { it == 1 }, "uploaded more than once: $perId")
        // Interrupt at every Health Connect call (every page and slice), then resume.
        for (n in 1..probeHc.calls.size) {
            val hc = FakeHealth().also(::seed)
            val server = FakeServer()
            val store = InMemoryStateStore(confirmed())
            hc.failAtCall = n
            engine(hc, server, store, FixedClock(now)).run()
            hc.failAtCall = null
            assertIs<SyncEngine.Outcome.Ok>(engine(hc, server, store, FixedClock(now.plusSeconds(60))).run())
            assertEquals(24, server.ids("sleep").size, "interrupted at call $n")
        }
    }

    @Test
    fun `records written during the scan are picked up by changes`() = runTest {
        val (hc, server, store) = world()
        var injected = false
        hc.onRead = {
            if (!injected) {
                injected = true
                hc.put(sleepSession("sleep-race", T0.plus(Duration.ofHours(2)))) // lands in an early slice
            }
        }
        engine(hc, server, store, FixedClock(now)).run()
        assertTrue("sleep-race" in server.ids("sleep"))
    }

    @Test
    fun `expired token triggers a recovery scan and reports a gap beyond readable history`() = runTest {
        val (hc, server, store) = world()
        engine(hc, server, store, FixedClock(now)).run()
        // 40 days later; history readable only for 30 days.
        val later = now.plus(Duration.ofDays(40))
        hc.earliest = later.minus(Duration.ofDays(30))
        hc.expiredTokens += store.state.type(SessionType.SLEEP).token!!
        hc.put(sleepSession("sleep-late", later.minus(Duration.ofDays(2))))
        val out = engine(hc, server, store, FixedClock(later)).run()
        assertIs<SyncEngine.Outcome.Ok>(out)
        assertTrue("sleep-late" in server.ids("sleep"))
        assertEquals(1, server.gaps.size)
        assertEquals(now.minus(Duration.ofDays(3)).toString(), server.gaps.single().gapStart)
        assertEquals(hc.earliest.toString(), server.gaps.single().gapEnd)
        assertTrue(store.state.pendingGaps.isEmpty())
        assertEquals(Phase.CHANGES, store.state.type(SessionType.SLEEP).phase)
    }

    @Test
    fun `daily window widens after a 10 day gap and is a week otherwise`() = runTest {
        val (hc, server, store) = world()
        engine(hc, server, store, FixedClock(now)).run()
        hc.dailyRequests.clear()
        engine(hc, server, store, FixedClock(now.plus(Duration.ofHours(6)))).run()
        assertEquals(LocalDate.parse("2026-10-01"), hc.dailyRequests.first().first) // today - 7
        hc.dailyRequests.clear()
        val tenDaysLater = now.plus(Duration.ofDays(10))
        engine(hc, server, store, FixedClock(tenDaysLater)).run()
        // last run 2026-10-08 (+6h) -> minus 3 days = 2026-10-05, earlier than today-7 (2026-10-11)
        assertEquals(LocalDate.parse("2026-10-05"), hc.dailyRequests.first().first)
    }

    @Test
    fun `incremental runs re-send exercise aggregates in the window`() = runTest {
        val (hc, server, store) = world()
        hc.put(exerciseSession("exercise-recent", now.minus(Duration.ofDays(2)), distance = 1000.0))
        engine(hc, server, store, FixedClock(now)).run()
        // Distance arrives late: no change event for the session itself.
        hc.records["exercise-recent"] = exerciseSession("exercise-recent", now.minus(Duration.ofDays(2)), distance = 6200.0)
        engine(hc, server, store, FixedClock(now.plusSeconds(3600))).run()
        val body = server.rows["exercise:exercise-recent"]!!.body as ExerciseIn
        assertEquals(6200.0, body.distanceM)
    }

    @Test
    fun `sleep rewritten without an upsert change still syncs via the rolling re-read`() = runTest {
        val (hc, server, store) = world()
        engine(hc, server, store, FixedClock(now)).run()
        // Fitbit-style rewrite: old record deleted (reported), replacement written (not reported).
        hc.put(sleepSession("sleep-orig", now.minus(Duration.ofHours(30))))
        engine(hc, server, store, FixedClock(now.plusSeconds(600))).run()
        hc.remove("sleep-orig")
        hc.putSilently(sleepSession("sleep-rewritten", now.minus(Duration.ofHours(30))))
        val out = engine(hc, server, store, FixedClock(now.plusSeconds(1200))).run()
        assertIs<SyncEngine.Outcome.Ok>(out)
        assertFalse("sleep-orig" in server.ids("sleep"))
        assertTrue("sleep-rewritten" in server.ids("sleep"))
    }

    @Test
    fun `full resync removes rows that no longer exist in Health Connect`() = runTest {
        val (hc, server, store) = world()
        val coordinator = SyncCoordinator(engine(hc, server, store, FixedClock(now)), store)
        coordinator.sync()
        hc.removeSilently("sleep-2") // deletion we never saw as a change
        val out = coordinator.fullResync()
        assertIs<SyncEngine.Outcome.Ok>(out)
        assertTrue(out.fullResync)
        assertFalse("sleep-2" in server.ids("sleep"))
        assertEquals(expectedSleep(hc), server.ids("sleep"))
    }

    @Test
    fun `server 409 resets to a full resync`() = runTest {
        val (hc, server, store) = world()
        engine(hc, server, store, FixedClock(now)).run()
        store.update { it.resetForFullResync() }
        server.forget409 = true
        val out = engine(hc, server, store, FixedClock(now.plusSeconds(60))).run()
        assertEquals(SyncEngine.Outcome.Failed(ErrorCategory.SERVER_4XX), out)
        assertTrue(store.state.fullResyncPending)
        assertFalse(store.state.resyncBeginAcked)
        SessionType.entries.forEach { assertEquals(Phase.NEW, store.state.type(it).phase) }
    }

    @Test
    fun `lost ack of a completed resync is reconciled without a second resync`() = runTest {
        val (hc, server, store) = world()
        val probe = CrashingStore(store, crashAt = Int.MAX_VALUE)
        engine(hc, server, probe, FixedClock(now)).run()
        val lastUpdate = probe.updates
        val (hc2, server2, store2) = world()
        // Crash on the very last state write (after run_complete full_resync was applied).
        assertIs<SyncEngine.Outcome.Failed>(engine(hc2, server2, CrashingStore(store2, lastUpdate), FixedClock(now)).run())
        assertTrue(store2.state.fullResyncPending)
        val before = server2.payloads.size
        val out = engine(hc2, server2, store2, FixedClock(now.plusSeconds(60))).run()
        assertIs<SyncEngine.Outcome.Ok>(out)
        assertFalse(out.fullResync) // reconciled: no begin, no second full upload
        assertTrue(server2.payloads.drop(before).none { it.fullResyncBegin })
    }

    @Test
    fun `server that lost the begin marker restarts the resync cleanly`() = runTest {
        val (hc, server, store) = world()
        server.failBeforeAt = 3
        engine(hc, server, store, FixedClock(now)).run() // begin acked, then failure
        assertTrue(store.state.resyncBeginAcked)
        server.resyncBoundary = null // backend wiped / boundary gone
        val out = engine(hc, server, store, FixedClock(now.plusSeconds(60))).run()
        assertIs<SyncEngine.Outcome.Ok>(out)
        assertTrue(out.fullResync)
        assertEquals(2, server.payloads.count { it.fullResyncBegin })
        assertEquals(expectedSleep(hc), server.ids("sleep"))
    }

    @Test
    fun `compute_seq is one value per run from max(local, server) + 1`() = runTest {
        val (hc, server, store) = world()
        server.maxSeq = 41
        store.update { it.copy(localSeq = 5) }
        engine(hc, server, store, FixedClock(now), batcher = Batcher(maxItems = 2)).run()
        val seqs = server.payloads.flatMap { p -> p.sleep.map { it.computeSeq } + p.exercise.map { it.computeSeq } + p.dailyMetrics.map { it.computeSeq } }.toSet()
        assertEquals(setOf(42L), seqs)
        assertTrue(server.payloads.size > 10) // many batches, same seq
        assertEquals(42, store.state.localSeq)
        // Reinstall: empty local state continues above the server max.
        val fresh = InMemoryStateStore(confirmed())
        engine(hc, server, fresh, FixedClock(now.plusSeconds(60))).run()
        assertEquals(43, fresh.state.localSeq)
    }

    @Test
    fun `status failure uploads nothing`() = runTest {
        val (hc, server, store) = world()
        server.statusFails = true
        val out = engine(hc, server, store, FixedClock(now)).run()
        assertEquals(SyncEngine.Outcome.Failed(ErrorCategory.NETWORK), out)
        assertTrue(server.payloads.isEmpty())
        assertEquals("network", store.state.pendingReport?.errorCategory)
        // The failure report rides along with the next run and is then cleared.
        server.statusFails = false
        engine(hc, server, store, FixedClock(now.plusSeconds(60))).run()
        assertEquals("error", server.payloads.first().runReport?.result)
        assertNull(store.state.pendingReport)
        assertEquals("ok", server.lastReport?.result)
    }

    @Test
    fun `empty page token ends paging without an extra request`() = runTest {
        for (emptyString in listOf(false, true)) {
            val hc = FakeHealth(pageSize = 3).apply { emptyStringLastToken = emptyString }
            repeat(7) { i -> hc.put(sleepSession("sleep-$i", Instant.parse("2026-08-01T10:00:00Z").plusSeconds(i * 60L))) }
            val server = FakeServer()
            engine(hc, server, InMemoryStateStore(confirmed()), FixedClock(now)).run()
            assertEquals(7, server.ids("sleep").size)
            // First slice holds all 7 records: pages of 3,3,1 -> exactly 3 reads for that slice.
            val reads = hc.calls.indexOfFirst { it == "read" }
            val firstSliceReads = hc.calls.drop(reads).takeWhile { it == "read" }.size
            assertTrue(firstSliceReads >= 3)
            // Total reads = 3 for the populated slice + 1 per other (empty) slice + exercise slices.
            val sleepSlices = Duration.between(Instant.parse("2026-08-01T00:00:00Z"), now).toDays() / 7 + 1
            assertEquals(3 + (sleepSlices - 1) + sleepSlices, hc.calls.count { it == "read" }.toLong(), "emptyString=$emptyString")
        }
    }
}
