package dev.jatin.healthsync.sync

import java.time.Duration
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneOffset

val T0: Instant = Instant.parse("2026-09-01T00:00:00Z")
val IST: ZoneOffset = ZoneOffset.ofHoursMinutes(5, 30)

fun sleepSession(id: String, start: Instant, minutes: Long = 420, modified: Instant = start): Session {
    val end = start.plus(Duration.ofMinutes(minutes))
    return Session(
        SessionType.SLEEP, id, start,
        sleep = SleepIn(id, iso(start, IST), iso(end, IST), "+05:30", "+05:30", sourceLastModified = modified.toString()),
    )
}

fun exerciseSession(id: String, start: Instant, distance: Double? = null): Session {
    val end = start.plus(Duration.ofMinutes(45))
    return Session(
        SessionType.EXERCISE, id, start,
        exercise = ExerciseIn(id, iso(start, IST), iso(end, IST), "+05:30", "+05:30", "running", distanceM = distance),
    )
}

class InjectedFailure(msg: String) : RuntimeException(msg)

/** In-memory Health Connect with a change log, paging and failure injection. */
class FakeHealth(
    var pageSize: Int = 3,
    var earliest: Instant = Instant.parse("2026-08-01T00:00:00Z"),
) : HealthSource {
    val records = mutableMapOf<String, Session>()
    private val log = mutableListOf<Pair<String, Boolean>>() // (id, deleted)
    val expiredTokens = mutableSetOf<String>()
    var daily: (LocalDate) -> DailyIn? = { null }
    var emptyStringLastToken = false
    val calls = mutableListOf<String>()
    var failAtCall: Int? = null
    var onRead: (() -> Unit)? = null
    val dailyRequests = mutableListOf<Pair<LocalDate, LocalDate>>()

    private fun tick(name: String) {
        calls += name
        if (failAtCall == calls.size) throw InjectedFailure("hc call ${calls.size} $name")
    }

    fun put(s: Session) {
        records[s.id] = s
        log += s.id to false
    }

    fun remove(id: String) {
        records.remove(id)
        log += id to true
    }

    /** Remove without a change-log entry (e.g. data that aged out / older deletions). */
    fun removeSilently(id: String) { records.remove(id) }

    override suspend fun changesToken(type: SessionType): String {
        tick("token")
        return "${type.wire}:${log.size}"
    }

    override suspend fun readSessions(type: SessionType, from: Instant, to: Instant, pageToken: String?): SessionPage {
        tick("read")
        onRead?.invoke()
        val all = records.values.filter { it.type == type }
            .filter { s -> s.start.isBefore(to) && !end(s).isBefore(from) }
            .sortedWith(compareBy({ it.start }, { it.id }))
        val offset = pageToken?.toInt() ?: 0
        val page = all.drop(offset).take(pageSize)
        val next = offset + page.size
        val token = if (next < all.size) next.toString() else if (emptyStringLastToken) "" else null
        return SessionPage(page, token)
    }

    private fun end(s: Session): Instant = java.time.OffsetDateTime.parse(s.sleep?.endTime ?: s.exercise!!.endTime).toInstant()

    override suspend fun changes(type: SessionType, token: String): ChangesPage {
        tick("changes")
        if (token in expiredTokens) throw ChangesTokenExpiredException()
        val (wire, idx) = token.split(":")
        require(wire == type.wire)
        val start = idx.toInt()
        val mine = log.withIndex().filter { it.index >= start && it.value.first.startsWith(type.wire) }
        val page = mine.take(pageSize)
        val nextIdx = if (page.size < mine.size) page.last().index + 1 else log.size
        val changes = page.mapNotNull { (_, ev) ->
            val (id, isDelete) = ev
            if (isDelete) Change.Delete(id) else records[id]?.let { Change.Upsert(it) }
        }
        return ChangesPage(changes, "${type.wire}:$nextIdx", hasMore = page.size < mine.size)
    }

    override suspend fun dailyMetrics(from: LocalDate, to: LocalDate): List<DailyIn> {
        tick("daily")
        dailyRequests += from to to
        return generateSequence(from) { it.plusDays(1) }.takeWhile { !it.isAfter(to) }.mapNotNull(daily).toList()
    }

    override fun earliestReadable(now: Instant): Instant = earliest
}

/** Emulates the backend's ingest semantics closely enough to check end states. */
class FakeServer : Uploader {
    data class Row(val version: Pair<String, Long>, val seenTick: Long, val body: Any)

    val rows = mutableMapOf<String, Row>() // "sleep:<id>" / "exercise:<id>" / "daily:<date>"
    val payloads = mutableListOf<IngestPayload>()
    var maxSeq = 0L
    var tick = 0L
    var resyncBoundary: Long? = null
    val gaps = mutableListOf<RecoveryIn>()
    var lastReport: RunReportIn? = null
    var dataThrough: String? = null
    var statusFails = false
    /** Apply the payload, then fail as if the 2xx response was lost. */
    var loseResponseAt: Int? = null
    var failBeforeAt: Int? = null
    var forget409 = false
    var gate: (suspend () -> Unit)? = null

    var lastFullSyncTick: Long? = null
    /** The fake server's own clock (deliberately unrelated to the phone clock). */
    fun serverTime(t: Long): String = Instant.parse("2031-01-01T00:00:00Z").plusSeconds(t).toString()

    override suspend fun status(): StatusResponse {
        if (statusFails) throw UploadException(ErrorCategory.NETWORK)
        return StatusResponse(maxSeq, resyncBoundary?.let(::serverTime), lastFullSyncTick?.let(::serverTime))
    }

    override suspend fun ingest(payload: IngestPayload): IngestResponse {
        gate?.invoke()
        val n = payloads.size + 1
        if (failBeforeAt == n) { failBeforeAt = null; throw UploadException(ErrorCategory.NETWORK) }
        payloads += payload
        tick++
        if (payload.fullResyncBegin) resyncBoundary = tick
        fun put(key: String, slm: String?, seq: Long, body: Any) {
            maxSeq = maxOf(maxSeq, seq)
            val v = (slm ?: "") to seq
            val old = rows[key]
            val fresh = old == null || compareValuesBy(v, old.version, { it.first }, { it.second }) >= 0
            rows[key] = Row(if (fresh) v else old!!.version, tick, if (fresh) body else old!!.body)
        }
        payload.sleep.forEach { put("sleep:${it.sourceRecordId}", it.sourceLastModified, it.computeSeq, it) }
        payload.exercise.forEach { put("exercise:${it.sourceRecordId}", it.sourceLastModified, it.computeSeq, it) }
        payload.dailyMetrics.forEach { put("daily:${it.date}", null, it.computeSeq, it) }
        payload.deleted.forEach { rows.remove("${it.type}:${it.sourceRecordId}") }
        payload.recovery?.let { gaps += it }
        payload.runReport?.let { lastReport = it }
        var swept = 0
        payload.runComplete?.let { rc ->
            dataThrough = rc.dataThrough
            if (rc.fullResync) {
                val boundary = resyncBoundary
                if (boundary == null || forget409) throw UploadException(ErrorCategory.SERVER_4XX, resyncRequired = true)
                val stale = rows.filterValues { it.seenTick < boundary }.keys
                swept = stale.size
                stale.forEach(rows::remove)
                gaps.clear()
                resyncBoundary = null
                lastFullSyncTick = tick
            }
        }
        if (loseResponseAt == n) { loseResponseAt = null; throw UploadException(ErrorCategory.NETWORK) }
        return IngestResponse(swept = swept, resyncStartedAt = if (payload.fullResyncBegin) serverTime(tick) else null)
    }

    fun ids(prefix: String) = rows.keys.filter { it.startsWith("$prefix:") }.map { it.substringAfter(":") }.sorted()
}

/** Wraps a store and throws on the Nth update (a crash between a 2xx and the state write). */
class CrashingStore(private val inner: InMemoryStateStore, private val crashAt: Int) : StateStore {
    var updates = 0
    override suspend fun read() = inner.read()
    override suspend fun update(transform: (SyncState) -> SyncState): SyncState {
        updates++
        if (updates == crashAt) throw InjectedFailure("crash before state write $updates")
        return inner.update(transform)
    }
}

class FixedClock(var now: Instant) : Clock { override fun now() = now }

fun confirmed() = SyncState(allowedOrigins = mapOf("sleep" to setOf("com.example.fake")), originsConfirmed = true)

fun engine(hc: HealthSource, server: Uploader, store: StateStore, clock: Clock, batcher: Batcher = Batcher(), slice: Long = 7) =
    SyncEngine(hc, server, store, clock, { IST }, sliceDays = slice, batcher = batcher, onError = { if (it !is InjectedFailure && it !is UploadException) println("ENGINE ERROR: " + it) })
