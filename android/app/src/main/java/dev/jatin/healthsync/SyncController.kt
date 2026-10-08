package dev.jatin.healthsync

import android.content.Context
import android.util.Log
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.HealthConnectFeatures
import androidx.health.connect.client.permission.HealthPermission
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import dev.jatin.healthsync.hc.BackgroundPolicy
import dev.jatin.healthsync.hc.BackgroundStatus
import dev.jatin.healthsync.hc.HealthConnectSource
import dev.jatin.healthsync.hc.RestoreGuard
import dev.jatin.healthsync.hc.UrlPolicy
import dev.jatin.healthsync.net.OkHttpTransport
import dev.jatin.healthsync.store.DataStoreStateStore
import dev.jatin.healthsync.store.SecretStore
import dev.jatin.healthsync.sync.ErrorCategory
import dev.jatin.healthsync.sync.HealthSourceException
import dev.jatin.healthsync.sync.RetryingUploader
import dev.jatin.healthsync.sync.SyncCoordinator
import dev.jatin.healthsync.sync.SyncEngine
import dev.jatin.healthsync.sync.SyncState
import dev.jatin.healthsync.work.SyncWorker
import kotlinx.coroutines.delay
import java.time.Duration
import java.time.Instant
import java.time.ZoneId
import java.util.concurrent.TimeUnit

const val TAG = "HealthSync"

/**
 * Process-wide singleton wiring. All runs (periodic worker, Sync now, Full resync)
 * go through [coordinator], whose mutex guarantees they never overlap.
 * Logs contain only outcomes, counts and error categories — never values or secrets.
 */
class SyncController(private val context: Context) {
    val client: HealthConnectClient by lazy { HealthConnectClient.getOrCreate(context) }
    val stateStore = DataStoreStateStore(context)
    val secrets = SecretStore(context)

    @Volatile private var origins: Map<String, Set<String>> = emptyMap()

    val source by lazy { HealthConnectSource(client, { origins }, { ZoneId.systemDefault() }) }

    val transport = OkHttpTransport(
        baseUrl = { UrlPolicy.resolve(BuildConfig.BASE_URL, secrets.urlOverride(), BuildConfig.ALLOW_URL_OVERRIDE) },
        secret = { secrets.load() },
    )
    val uploader = RetryingUploader(transport, sleep = { delay(it) })

    private val engine by lazy {
        SyncEngine(
            hc = source,
            uploader = uploader,
            store = stateStore,
            clock = { Instant.now() },
            zone = { ZoneId.systemDefault() },
            onError = { Log.w(TAG, "sync error: ${it.javaClass.simpleName}") },
        )
    }
    val coordinator by lazy { SyncCoordinator(engine, stateStore) }

    fun sdkAvailable(): Boolean =
        HealthConnectClient.getSdkStatus(context) == HealthConnectClient.SDK_AVAILABLE

    /** Defence in depth against restored/migrated state (backups are disabled). */
    suspend fun applyRestoreGuard() {
        val present = secrets.isPresent()
        if (RestoreGuard.mustReset(present, present && secrets.load() != null)) {
            Log.w(TAG, "restored install detected; resetting sync state")
            coordinator.resetAll()
            secrets.clear()
        }
    }

    suspend fun state(): SyncState = stateStore.read()

    suspend fun backgroundStatus(): BackgroundStatus {
        if (!sdkAvailable()) return BackgroundPolicy.evaluate(featureAvailable = false, permissionGranted = false)
        val feature = client.features.getFeatureStatus(HealthConnectFeatures.FEATURE_READ_HEALTH_DATA_IN_BACKGROUND) ==
            HealthConnectFeatures.FEATURE_STATUS_AVAILABLE
        val granted = HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND in client.permissionController.getGrantedPermissions()
        return BackgroundPolicy.evaluate(feature, granted)
    }

    /** Periodic work only when background reads are actually possible. */
    suspend fun updateSchedule(): BackgroundStatus {
        val status = backgroundStatus()
        val wm = WorkManager.getInstance(context)
        if (status.enabled && state().originsConfirmed) {
            val request = PeriodicWorkRequestBuilder<SyncWorker>(6, TimeUnit.HOURS)
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, Duration.ofMinutes(15))
                .build()
            wm.enqueueUniquePeriodicWork(PERIODIC_WORK, ExistingPeriodicWorkPolicy.KEEP, request)
        } else {
            wm.cancelUniqueWork(PERIODIC_WORK)
        }
        return status
    }

    suspend fun setOrigins(map: Map<String, Set<String>>) {
        coordinator.setOrigins(map)
        updateSchedule()
    }

    suspend fun sync(full: Boolean = false): SyncEngine.Outcome {
        origins = state().allowedOrigins
        try {
            source.refreshPermissions()
        } catch (e: HealthSourceException) {
            Log.w(TAG, "sync skipped: ${e.category.wire}")
            return SyncEngine.Outcome.Failed(e.category)
        } catch (e: Exception) {
            Log.w(TAG, "sync skipped: ${e.javaClass.simpleName}")
            return SyncEngine.Outcome.Failed(ErrorCategory.HC_UNAVAILABLE)
        }
        val outcome = if (full) coordinator.fullResync() else coordinator.sync()
        when (outcome) {
            is SyncEngine.Outcome.Ok -> Log.i(TAG, "sync ok uploaded=${outcome.uploaded} full=${outcome.fullResync}")
            is SyncEngine.Outcome.Failed -> Log.w(TAG, "sync failed category=${outcome.category.wire}")
            SyncEngine.Outcome.NotConfigured -> Log.i(TAG, "sync not configured")
        }
        return outcome
    }

    companion object {
        const val PERIODIC_WORK = "healthsync-periodic"
    }
}
