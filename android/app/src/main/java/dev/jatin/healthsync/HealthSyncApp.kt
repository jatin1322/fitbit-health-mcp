package dev.jatin.healthsync

import android.app.Application
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

class HealthSyncApp : Application() {
    lateinit var controller: SyncController
        private set
    val appScope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    override fun onCreate() {
        super.onCreate()
        controller = SyncController(this)
        appScope.launch {
            controller.applyRestoreGuard()
            if (controller.sdkAvailable()) runCatching { controller.updateSchedule() }
        }
    }
}
