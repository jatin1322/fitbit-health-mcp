package dev.jatin.healthsync.work

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import dev.jatin.healthsync.HealthSyncApp
import dev.jatin.healthsync.sync.ErrorCategory
import dev.jatin.healthsync.sync.SyncEngine

/** Periodic background sync (scheduled only when background reads are enabled). */
class SyncWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val controller = (applicationContext as HealthSyncApp).controller
        return when (val outcome = controller.sync()) {
            is SyncEngine.Outcome.Ok, SyncEngine.Outcome.NotConfigured -> Result.success()
            is SyncEngine.Outcome.Failed -> when (outcome.category) {
                // Transient: let WorkManager back off and retry.
                ErrorCategory.NETWORK, ErrorCategory.SERVER_5XX, ErrorCategory.HC_UNAVAILABLE, ErrorCategory.UNKNOWN -> Result.retry()
                // Needs the user (secret, permissions) or a fix: don't hammer; next period tries again.
                else -> Result.failure()
            }
        }
    }
}
