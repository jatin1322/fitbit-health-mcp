package dev.jatin.healthsync.sync

import java.io.IOException
import kotlin.random.Random

/** Minimal HTTP seam so retry policy is testable without a network. */
interface Transport {
    /** @throws IOException on transport failure (timeout, DNS, connection reset...). */
    suspend fun get(path: String): HttpResult
    suspend fun post(path: String, json: String): HttpResult
}

data class HttpResult(val code: Int, val body: String, val retryAfterSeconds: Long? = null)

/**
 * Retry policy (all retries are safe because ingest is idempotent):
 *  - retried with exponential backoff + jitter: IOExceptions, 429 (honouring
 *    Retry-After), 5xx, up to [maxRetries] times within a run;
 *  - never retried: 401/403 (auth), 409 (resync required), other 4xx (bug).
 */
class RetryingUploader(
    private val transport: Transport,
    private val sleep: suspend (Long) -> Unit,
    private val maxRetries: Int = 3,
    private val random: Random = Random.Default,
) : Uploader {

    override suspend fun status(): StatusResponse =
        WireJson.decodeFromString(StatusResponse.serializer(), call { transport.get("/v1/status") })

    override suspend fun ingest(payload: IngestPayload): IngestResponse {
        val body = WireJson.encodeToString(IngestPayload.serializer(), payload)
        return WireJson.decodeFromString(IngestResponse.serializer(), call { transport.post("/v1/ingest", body) })
    }

    private suspend fun call(op: suspend () -> HttpResult): String {
        var attempt = 0
        while (true) {
            val result = try {
                op()
            } catch (e: IOException) {
                if (attempt >= maxRetries) throw UploadException(ErrorCategory.NETWORK)
                sleep(backoffMs(attempt, null))
                attempt++
                continue
            }
            when (result.code) {
                in 200..299 -> return result.body
                401, 403 -> throw UploadException(ErrorCategory.AUTH)
                409 -> throw UploadException(ErrorCategory.SERVER_4XX, resyncRequired = true)
                429 -> {
                    if (attempt >= maxRetries) throw UploadException(ErrorCategory.SERVER_4XX)
                    sleep(backoffMs(attempt, result.retryAfterSeconds))
                }
                in 500..599 -> {
                    if (attempt >= maxRetries) throw UploadException(ErrorCategory.SERVER_5XX)
                    sleep(backoffMs(attempt, result.retryAfterSeconds))
                }
                else -> throw UploadException(ErrorCategory.SERVER_4XX)
            }
            attempt++
        }
    }

    internal fun backoffMs(attempt: Int, retryAfterSeconds: Long?): Long {
        if (retryAfterSeconds != null && retryAfterSeconds >= 0) return minOf(retryAfterSeconds, 60) * 1000
        val base = minOf(30_000L, 1_000L shl attempt)
        return base + random.nextLong(0, 500)
    }
}
