package dev.jatin.healthsync

import androidx.compose.runtime.Composable
import androidx.health.connect.client.HealthConnectClient

/** Release build: no fake-data tools and no write permissions. */
object DebugTools {
    val writePermissions: Set<String> = emptySet()

    @Composable
    @Suppress("UNUSED_PARAMETER")
    fun Section(client: HealthConnectClient, onMessage: (String) -> Unit) = Unit
}
