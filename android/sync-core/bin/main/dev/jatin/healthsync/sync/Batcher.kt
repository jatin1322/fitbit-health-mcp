package dev.jatin.healthsync.sync

/**
 * Splits rows into ingest payloads within the backend limits
 * (2000 items, 2 MB body), keeping a safety margin on size.
 */
class Batcher(
    private val maxItems: Int = 2000,
    private val maxBytes: Int = 1_500_000,
) {
    fun split(
        runId: String,
        sleep: List<SleepIn> = emptyList(),
        daily: List<DailyIn> = emptyList(),
        exercise: List<ExerciseIn> = emptyList(),
        deleted: List<DeletedIn> = emptyList(),
    ): List<IngestPayload> {
        val out = mutableListOf<IngestPayload>()
        var cur = IngestPayload(runId)
        var bytes = 256 // envelope allowance

        fun flush() {
            if (cur.itemCount > 0) out += cur
            cur = IngestPayload(runId)
            bytes = 256
        }

        fun <T> add(item: T, size: Int, append: (IngestPayload, T) -> IngestPayload) {
            if (cur.itemCount >= maxItems || (cur.itemCount > 0 && bytes + size > maxBytes)) flush()
            cur = append(cur, item)
            bytes += size + 1
        }

        sleep.forEach { s -> add(s, WireJson.encodeToString(SleepIn.serializer(), s).length) { p, v -> p.copy(sleep = p.sleep + v) } }
        daily.forEach { d -> add(d, WireJson.encodeToString(DailyIn.serializer(), d).length) { p, v -> p.copy(dailyMetrics = p.dailyMetrics + v) } }
        exercise.forEach { e -> add(e, WireJson.encodeToString(ExerciseIn.serializer(), e).length) { p, v -> p.copy(exercise = p.exercise + v) } }
        deleted.forEach { d -> add(d, WireJson.encodeToString(DeletedIn.serializer(), d).length) { p, v -> p.copy(deleted = p.deleted + v) } }
        flush()
        return out
    }
}
