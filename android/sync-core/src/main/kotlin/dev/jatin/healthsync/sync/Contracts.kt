package dev.jatin.healthsync.sync

import kotlinx.serialization.Serializable
import java.time.Instant
import java.time.LocalDate

/** Session record types synced through the Health Connect Changes API. */
enum class SessionType(val wire: String) { SLEEP("sleep"), EXERCISE("exercise") }

/** One session as read from Health Connect, already mapped to the wire model (compute_seq is stamped later). */
data class Session(
    val type: SessionType,
    val id: String,
    val start: Instant,
    val sleep: SleepIn? = null,
    val exercise: ExerciseIn? = null,
)

data class SessionPage(val sessions: List<Session>, val nextPageToken: String?)

/** One Health Connect change, in the order Health Connect reported it. */
sealed interface Change {
    /** An upsert already mapped and filtered to the allowed origins. */
    data class Upsert(val session: Session) : Change
    data class Delete(val id: String) : Change
}

data class ChangesPage(val changes: List<Change>, val nextToken: String, val hasMore: Boolean)

class ChangesTokenExpiredException : Exception("changes token expired")

/** Errors the engine reports in run_report.error_category (mirrors backend ErrorCategory). */
enum class ErrorCategory(val wire: String) {
    NETWORK("network"),
    AUTH("auth"),
    HC_PERMISSION("hc_permission"),
    HC_UNAVAILABLE("hc_unavailable"),
    HC_TOKEN_EXPIRED("hc_token_expired"),
    SERVER_4XX("server_4xx"),
    SERVER_5XX("server_5xx"),
    UNKNOWN("unknown"),
}

/** Thrown by HealthSource implementations for permission/availability problems. */
class HealthSourceException(val category: ErrorCategory, message: String) : Exception(message)

/** Health Connect, seen through the narrow interface the engine needs. */
interface HealthSource {
    suspend fun changesToken(type: SessionType): String

    /**
     * Sessions overlapping [from, to), one page at a time. Implementations pass
     * [pageToken] straight to Health Connect; a null or empty returned token means done.
     */
    suspend fun readSessions(type: SessionType, from: Instant, to: Instant, pageToken: String?): SessionPage

    /** @throws ChangesTokenExpiredException when Health Connect rejects the token as expired. */
    suspend fun changes(type: SessionType, token: String): ChangesPage

    /** Daily aggregates for local dates [from, to] inclusive (compute_seq stamped later). */
    suspend fun dailyMetrics(from: LocalDate, to: LocalDate): List<DailyIn>

    /** Earliest instant Health Connect will let us read (depends on the history permission). */
    fun earliestReadable(now: Instant): Instant
}

/** The backend, through the narrow interface the engine needs. Retries live in [RetryingUploader]. */
interface Uploader {
    suspend fun status(): StatusResponse
    suspend fun ingest(payload: IngestPayload): IngestResponse
}

class UploadException(
    val category: ErrorCategory,
    /** 409: the server has no record of our full-resync begin; restart the resync. */
    val resyncRequired: Boolean = false,
    message: String = category.wire,
) : Exception(message)

@Serializable
data class TypeState(
    val phase: Phase = Phase.NEW,
    val token: String? = null,
    val scanFromMs: Long? = null,
    val scanToMs: Long? = null,
    val scanSliceDoneUntilMs: Long? = null,
)

@Serializable
enum class Phase { NEW, SCAN, CHANGES }

@Serializable
data class PendingReport(val result: String, val durationMs: Long, val errorCategory: String?)

@Serializable
data class PendingGap(val startMs: Long, val endMs: Long)

/** Everything the engine persists. Stored as one value so every transition is atomic. */
@Serializable
data class SyncState(
    val types: Map<SessionType, TypeState> = SessionType.entries.associateWith { TypeState() },
    val lastRunCompletedAtMs: Long? = null,
    val localSeq: Long = 0,
    /** True from a Full Resync request (or first install) until its run_complete is acknowledged. */
    val fullResyncPending: Boolean = true,
    /** True once the server acknowledged full_resync_begin for the pending resync. */
    val resyncBeginAcked: Boolean = false,
    /** Informational: the server-owned boundary returned for the current resync. */
    val resyncStartedAt: String? = null,
    /** Record type name -> allowed data-origin package names. */
    val allowedOrigins: Map<String, Set<String>> = emptyMap(),
    val originsConfirmed: Boolean = false,
    val pendingReport: PendingReport? = null,
    val pendingGaps: List<PendingGap> = emptyList(),
) {
    fun type(t: SessionType) = types[t] ?: TypeState()
    fun withType(t: SessionType, s: TypeState) = copy(types = types + (t to s))

    /** The state a Full Resync starts from; keeps configuration and the seq counter. */
    fun resetForFullResync() = copy(
        types = SessionType.entries.associateWith { TypeState() },
        fullResyncPending = true,
        resyncBeginAcked = false,
        resyncStartedAt = null,
        pendingGaps = emptyList(),
    )
}

/** Persistent state with atomic read-modify-write. */
interface StateStore {
    suspend fun read(): SyncState
    suspend fun update(transform: (SyncState) -> SyncState): SyncState
}

fun interface Clock { fun now(): Instant }
