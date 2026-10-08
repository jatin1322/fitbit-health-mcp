package dev.jatin.healthsync.net

import dev.jatin.healthsync.sync.HttpResult
import dev.jatin.healthsync.sync.Transport
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit

/**
 * HTTPS transport to the one configured backend origin. Redirects are refused
 * (a 3xx is surfaced as an error), so the bearer secret can never be forwarded
 * to another host. Bodies and headers are never logged.
 */
class OkHttpTransport(
    private val baseUrl: suspend () -> String,
    private val secret: suspend () -> String?,
) : Transport {

    private val client = OkHttpClient.Builder()
        .followRedirects(false)
        .followSslRedirects(false)
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        .build()

    private val json = "application/json".toMediaType()

    private suspend fun url(path: String): String {
        val base = baseUrl()
        require(base.startsWith("https://")) { "backend URL must be https" }
        return base + path
    }

    override suspend fun get(path: String) = execute(Request.Builder().url(url(path)).get())

    override suspend fun post(path: String, json: String) =
        execute(Request.Builder().url(url(path)).post(json.toRequestBody(this.json)))

    private suspend fun execute(builder: Request.Builder): HttpResult = withContext(Dispatchers.IO) {
        val token = secret() ?: return@withContext HttpResult(401, "")
        val request = builder.header("Authorization", "Bearer $token").build()
        client.newCall(request).execute().use { response ->
            HttpResult(
                code = response.code,
                body = response.body.string(),
                retryAfterSeconds = response.header("Retry-After")?.trim()?.toLongOrNull(),
            )
        }
    }
}
