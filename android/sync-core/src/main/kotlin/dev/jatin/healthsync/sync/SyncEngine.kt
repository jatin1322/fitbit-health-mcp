package dev.jatin.healthsync.sync

import kotlinx.coroutines.CancellationException
import java.time.Duration
import java.time.Instant
import java.time.ZoneId
import java.util.UUID

/**
 * One sync run. Per session type: NEW -> SCAN -> CHANGES.
 *
 * Invariants (each persisted step is one atomic StateStore.update, made only
 * after the upload it depends on returned 2xx):
 *  - the changes token is captured BEFORE the historical scan;
 *  - SCAN walks fixed slices of [scanFrom, scanTo); a session belongs to the
 *    slice where slice_start <= start < slice_end; a slice is committed only after
 *    all its pages were read (page token null/empty = done) and uploaded;
 *  - CHANGES advances the token only to the successor returned with the page
 *    that was just uploaded;
 *  - an expired token becomes a recovery SCAN (+ a partial_gap if history is
 *    no longer readable);
 *  - every row of a run carries the same compute_seq = max(local, server) + 1,
 *    persisted before the first upload;
 *  - run_complete is sent only after everything else succeeded.
 *
 * Callers must serialise runs; see [SyncCoordinator].
 */
class SyncEngine(
    private val hc: HealthSource,
    private val uploader: Uploader,
    private val store: StateStore,
    private val clock: Clock,
    private val zone: () -> ZoneId,
    private val sliceDays: Long = 7,
    private val batcher: Batcher = Batcher(),
    private val newRunId: () -> String = { UUID.randomUUID().toString() },
    /** Diagnostics hook. Implementations must log only the class name, never messages/values. */
    private val onError: (Throwable) -> Unit = {},
) {
    sealed interface Outcome {
        data class Ok(val uploaded: Int, val fullResync: Boolean) : Outcome
        data class Failed(val category: ErrorCategory) : Outcome
        data object NotConfigured : Outcome
    }

    private class Run(val id: String, val seq: Long, val start: Instant, val fullResync: Boolean, val lastRun: Instant?) {
        var uploaded = 0
    }

    suspend fun run(): Outcome {
        val started = clock.now()
        val initial = store.read()
        if (!initial.originsConfirmed) return Outcome.NotConfigured
        return try {
            val status = uploader.status()
            val seq = maxOf(initial.localSeq, status.maxComputeSeq) + 1
            val state = store.update { reconcileResync(it, status).copy(localSeq = seq) }
            val run = Run(
                id = newRunId(),
                seq = seq,
                start = started,
                fullResync = state.fullResyncPending,
                lastRun = state.lastRunCompletedAtMs?.let(Instant::ofEpochMilli),
            )
            if (run.fullResync && !state.resyncBeginAcked) {
                val resp = upload(run, IngestPayload(run.id, fullResyncBegin = true))
                store.update { it.copy(resyncBeginAcked = true, resyncStartedAt = resp.resyncStartedAt) }
            }
            for (type in SessionType.entries) syncType(run, type)
            recompute(run)
            val duration = Duration.between(started, clock.now()).toMillis().coerceAtLeast(0)
            upload(
                run,
                IngestPayload(
                    run.id,
                    runComplete = RunCompleteIn(started.toString(), run.fullResync),
                    runReport = RunReportIn("ok", duration),
                ),
            )
            store.update {
                it.copy(
                    lastRunCompletedAtMs = started.toEpochMilli(),
                    pendingReport = null,
                    fullResyncPending = false,
                    resyncBeginAcked = false,
                )
            }
            Outcome.Ok(run.uploaded, run.fullResync)
        } catch (e: CancellationException) {
            throw e
        } catch (e: UploadException) {
            onError(e)
            if (e.resyncRequired) store.update { it.resetForFullResync() }
            fail(started, e.category)
        } catch (e: HealthSourceException) {
            onError(e)
            fail(started, e.category)
        } catch (e: Exception) {
            onError(e)
            fail(started, ErrorCategory.UNKNOWN)
        }
    }

    /**
     * A pending resync whose begin the server acknowledged, but which the server no
     * longer has open: either its run_complete was applied and only our ack was lost
     * (server last_full_sync_at >= our boundary) -> mark it done; or the server lost
     * the begin (e.g. backend wipe) -> restart the resync from scratch.
     */
    private fun reconcileResync(s: SyncState, status: StatusResponse): SyncState {
        if (!s.fullResyncPending || !s.resyncBeginAcked || status.resyncStartedAt != null) return s
        val mine = s.resyncStartedAt?.let(::parseInstant)
        val done = status.lastFullSyncAt?.let(::parseInstant)
        return if (mine != null && done != null && !done.isBefore(mine)) {
            s.copy(fullResyncPending = false, resyncBeginAcked = false, lastRunCompletedAtMs = done.toEpochMilli())
        } else {
            s.resetForFullResync()
        }
    }

    private fun parseInstant(v: String): Instant? =
        runCatching { java.time.OffsetDateTime.parse(v).toInstant() }.getOrNull()

    private suspend fun fail(started: Instant, category: ErrorCategory): Outcome {
        val duration = Duration.between(started, clock.now()).toMillis().coerceAtLeast(0)
        runCatching { store.update { it.copy(pendingReport = PendingReport("error", duration, category.wire)) } }
        return Outcome.Failed(category)
    }

    // --- per type ----------------------------------------------------------------

    private suspend fun syncType(run: Run, type: SessionType) {
        var ts = store.read().type(type)
        if (ts.phase == Phase.NEW) {
            val token = hc.changesToken(type) // captured before the scan
            val from = hc.earliestReadable(run.start)
            ts = TypeState(Phase.SCAN, token, from.toEpochMilli(), run.start.toEpochMilli(), from.toEpochMilli())
            store.update { it.withType(type, ts) }
        }
        if (ts.phase == Phase.SCAN) ts = scan(run, type, ts)
        changes(run, type, ts)
    }

    private suspend fun scan(run: Run, type: SessionType, start: TypeState): TypeState {
        var ts = start
        val to = Instant.ofEpochMilli(ts.scanToMs!!)
        var done = Instant.ofEpochMilli(ts.scanSliceDoneUntilMs ?: ts.scanFromMs!!)
        val margin = Duration.ofDays(1)
        while (done.isBefore(to)) {
            val sliceEnd = minOf(done.plus(Duration.ofDays(sliceDays)), to)
            val owned = LinkedHashMap<String, Session>()
            var pageToken: String? = null
            do {
                val page = hc.readSessions(type, done.minus(margin), sliceEnd.plus(margin), pageToken)
                for (s in page.sessions) {
                    if (!s.start.isBefore(done) && s.start.isBefore(sliceEnd)) owned[s.id] = s
                }
                pageToken = page.nextPageToken
            } while (!pageToken.isNullOrEmpty())
            uploadSessions(run, owned.values.toList(), emptyList())
            ts = ts.copy(scanSliceDoneUntilMs = sliceEnd.toEpochMilli())
            val committed = ts
            store.update { it.withType(type, committed) }
            done = sliceEnd
        }
        ts = ts.copy(phase = Phase.CHANGES)
        val finished = ts
        store.update { it.withType(type, finished) }
        return ts
    }

    private suspend fun changes(run: Run, type: SessionType, start: TypeState) {
        var ts = start
        while (true) {
            val page = try {
                hc.changes(type, ts.token!!)
            } catch (e: ChangesTokenExpiredException) {
                ts = recover(run, type)
                ts = scan(run, type, ts)
                continue
            }
            // Apply in order: a later delete wins over an earlier upsert and vice versa.
            val upserts = LinkedHashMap<String, Session>()
            val deletes = LinkedHashSet<String>()
            for (change in page.changes) {
                when (change) {
                    is Change.Upsert -> {
                        deletes.remove(change.session.id)
                        upserts[change.session.id] = change.session
                    }
                    is Change.Delete -> {
                        upserts.remove(change.id)
                        deletes.add(change.id)
                    }
                }
            }
            uploadSessions(run, upserts.values.toList(), deletes.map { DeletedIn(type.wire, it) }.filter { Bounds.validRecordId(it.sourceRecordId) })
            ts = ts.copy(token = page.nextToken)
            val advanced = ts
            store.update { it.withType(type, advanced) }
            if (!page.hasMore) return
        }
    }

    private suspend fun recover(run: Run, type: SessionType): TypeState {
        val token = hc.changesToken(type)
        val limit = hc.earliestReadable(run.start)
        val wanted = run.lastRun?.minus(Duration.ofDays(3)) ?: limit
        val from = maxOf(wanted, limit)
        val ts = TypeState(Phase.SCAN, token, from.toEpochMilli(), run.start.toEpochMilli(), from.toEpochMilli())
        store.update { s ->
            val gaps = if (wanted.isBefore(limit)) s.pendingGaps + PendingGap(wanted.toEpochMilli(), limit.toEpochMilli()) else s.pendingGaps
            s.withType(type, ts).copy(pendingGaps = gaps.distinct())
        }
        return ts
    }

    // --- rolling recompute -----------------------------------------------------------

    private suspend fun recompute(run: Run) {
        val z = zone()
        val today = run.start.atZone(z).toLocalDate()
        val from = if (run.fullResync) {
            hc.earliestReadable(run.start).atZone(z).toLocalDate()
        } else {
            DayWindows.rollingStart(today, run.lastRun, z)
        }
        for ((a, b) in DayWindows.chunks(from, today)) {
            val rows = hc.dailyMetrics(a, b).map { it.copy(computeSeq = run.seq) }
            for (p in batcher.split(run.id, daily = rows)) upload(run, p)
        }
        if (!run.fullResync) {
            // Exercise aggregates (HR, steps, distance, calories) often land after the
            // session record and trigger no change for it, so re-read and resend them.
            val sessions = LinkedHashMap<String, Session>()
            var pageToken: String? = null
            do {
                val page = hc.readSessions(SessionType.EXERCISE, from.atStartOfDay(z).toInstant(), run.start, pageToken)
                page.sessions.forEach { sessions[it.id] = it }
                pageToken = page.nextPageToken
            } while (!pageToken.isNullOrEmpty())
            uploadSessions(run, sessions.values.toList(), emptyList())
        }
    }

    // --- uploads -------------------------------------------------------------------

    private suspend fun uploadSessions(run: Run, sessions: List<Session>, deleted: List<DeletedIn>) {
        if (sessions.isEmpty() && deleted.isEmpty()) return
        val sleep = sessions.mapNotNull { it.sleep?.copy(computeSeq = run.seq) }
        val exercise = sessions.mapNotNull { it.exercise?.copy(computeSeq = run.seq) }
        for (p in batcher.split(run.id, sleep = sleep, exercise = exercise, deleted = deleted)) upload(run, p)
    }

    /** Sends one payload, piggy-backing a pending failure report / gap; clears them only after 2xx. */
    private suspend fun upload(run: Run, payload: IngestPayload): IngestResponse {
        val state = store.read()
        val report = state.pendingReport?.takeIf { payload.runReport == null }
        val gap = state.pendingGaps.firstOrNull()?.takeIf { payload.recovery == null }
        val outgoing = payload.copy(
            runReport = payload.runReport ?: report?.let { RunReportIn(it.result, it.durationMs, it.errorCategory) },
            recovery = payload.recovery ?: gap?.let {
                RecoveryIn(gapStart = Instant.ofEpochMilli(it.startMs).toString(), gapEnd = Instant.ofEpochMilli(it.endMs).toString())
            },
        )
        val resp = uploader.ingest(outgoing)
        run.uploaded += payload.itemCount
        if (report != null || gap != null) {
            store.update { s ->
                s.copy(
                    pendingReport = if (report != null) null else s.pendingReport,
                    pendingGaps = if (gap != null) s.pendingGaps - gap else s.pendingGaps,
                )
            }
        }
        return resp
    }
}
