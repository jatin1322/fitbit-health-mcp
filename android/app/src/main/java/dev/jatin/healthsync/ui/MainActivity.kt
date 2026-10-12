package dev.jatin.healthsync.ui

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.Checkbox
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.health.connect.client.PermissionController
import dev.jatin.healthsync.BuildConfig
import dev.jatin.healthsync.DebugTools
import dev.jatin.healthsync.HealthSyncApp
import dev.jatin.healthsync.hc.BackgroundStatus
import dev.jatin.healthsync.hc.DataKind
import dev.jatin.healthsync.hc.EXTRA_PERMISSIONS
import dev.jatin.healthsync.hc.READ_PERMISSIONS
import dev.jatin.healthsync.sync.SyncEngine
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.time.Instant

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val controller = (application as HealthSyncApp).controller
        setContent {
            MaterialTheme {
                Surface(Modifier.fillMaxSize()) { Screen(controller) }
            }
        }
    }
}

@Composable
private fun Section(title: String, content: @Composable () -> Unit) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(title, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            content()
        }
    }
}

@Composable
private fun Screen(controller: dev.jatin.healthsync.SyncController) {
    val scope = rememberCoroutineScope()
    var message by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    var refreshKey by remember { mutableStateOf(0) }
    var background by remember { mutableStateOf<BackgroundStatus?>(null) }
    var granted by remember { mutableStateOf<Set<String>>(emptySet()) }
    var serverStatus by remember { mutableStateOf<JsonObject?>(null) }
    var secretSaved by remember { mutableStateOf(false) }
    var confirmedOrigins by remember { mutableStateOf<Map<String, Set<String>>>(emptyMap()) }
    var originsConfirmed by remember { mutableStateOf(false) }
    val discovered = remember { mutableStateMapOf<DataKind, Map<String, Int>>() }
    val selected = remember { mutableStateMapOf<DataKind, Set<String>>() }
    var confirmFull by remember { mutableStateOf(false) }
    val sdkOk = controller.sdkAvailable()

    val permissionLauncher = rememberLauncherForActivityResult(PermissionController.createRequestPermissionResultContract()) {
        refreshKey++
    }

    LaunchedEffect(refreshKey) {
        if (sdkOk) {
            granted = runCatching { controller.client.permissionController.getGrantedPermissions() }.getOrDefault(emptySet())
            background = runCatching { controller.updateSchedule() }.getOrNull()
        }
        secretSaved = controller.secrets.load() != null
        val s = controller.state()
        confirmedOrigins = s.allowedOrigins
        originsConfirmed = s.originsConfirmed
        serverStatus = if (secretSaved) {
            runCatching {
                val r = controller.transport.get("/v1/status")
                if (r.code == 200) Json.parseToJsonElement(r.body).jsonObject else null
            }.getOrNull()
        } else null
    }

    fun run(label: String, block: suspend () -> String) {
        busy = true
        message = "$label…"
        scope.launch {
            message = runCatching { block() }.getOrElse { "$label failed (${it.javaClass.simpleName})" }
            busy = false
            refreshKey++
        }
    }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Health Sync", style = MaterialTheme.typography.headlineSmall)
        if (!sdkOk) Text("Health Connect is not available on this device.", color = MaterialTheme.colorScheme.error)
        if (BuildConfig.BASE_URL.isEmpty() && !BuildConfig.ALLOW_URL_OVERRIDE) {
            Text("Backend URL not configured: set healthsync.baseUrl in local.properties and rebuild.", color = MaterialTheme.colorScheme.error)
        }
        message?.let { Text(it) }

        Section("Status") {
            background?.let {
                Text(if (it.enabled) "Background sync: on (every ~6 hours)" else it.message ?: "")
                Text("background_sync_supported=${it.supported}  background_sync_enabled=${it.enabled}", style = MaterialTheme.typography.bodySmall)
            }
            serverStatus?.let { s ->
                fun str(k: String) = s[k]?.let { runCatching { it.jsonPrimitive.content }.getOrNull() }
                Text("Server: last sync ${str("last_sync_completed_at") ?: "never"}")
                Text("current=${str("current")}  history_complete=${str("history_complete")}  recovery=${str("recovery_state")}")
                s["last_run"]?.toString()?.takeIf { it != "null" }?.let { Text("Last run: $it", style = MaterialTheme.typography.bodySmall) }
            } ?: Text("Server status unavailable (save the secret first).")
        }

        Section("1. Phone secret") {
            var secret by remember { mutableStateOf("") }
            var editing by remember { mutableStateOf(false) }
            val testButton = @Composable {
                OutlinedButton(enabled = !busy && secretSaved, onClick = {
                    run("Testing connection") {
                        val r = controller.transport.get("/v1/status")
                        when (r.code) {
                            200 -> "Connection OK"
                            401 -> "Secret rejected (401)"
                            else -> "Server returned ${r.code}"
                        }
                    }
                }) { Text("Test connection") }
            }
            if (secretSaved && !editing) {
                // Saved: no input box, so it never looks like the app is asking again.
                Text("✓ Secret saved (encrypted on this device)", fontWeight = FontWeight.Medium)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedButton(enabled = !busy, onClick = { editing = true }) { Text("Change secret") }
                    testButton()
                }
            } else {
                Text(if (secretSaved) "Enter the new secret." else "Paste or type the PHONE_INGEST_SECRET.")
                OutlinedTextField(
                    value = secret,
                    onValueChange = { secret = it.trim() },
                    label = { Text("Ingest secret") },
                    singleLine = true,
                    visualTransformation = PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                    modifier = Modifier.fillMaxWidth(),
                )
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(enabled = !busy && secret.length >= 32, onClick = {
                        run("Saving secret") {
                            controller.secrets.save(secret)
                            secret = ""
                            editing = false
                            "Secret saved"
                        }
                    }) { Text("Save") }
                    if (secretSaved) OutlinedButton(enabled = !busy, onClick = { secret = ""; editing = false }) { Text("Cancel") }
                }
            }
            if (BuildConfig.ALLOW_URL_OVERRIDE) {
                var url by remember { mutableStateOf("") }
                OutlinedTextField(url, { url = it.trim() }, label = { Text("Debug: backend URL override (https only)") }, singleLine = true, modifier = Modifier.fillMaxWidth())
                OutlinedButton(enabled = !busy, onClick = {
                    run("Saving URL") {
                        if (url.isNotEmpty() && !url.startsWith("https://")) "URL must start with https://"
                        else { controller.secrets.setUrlOverride(url.ifEmpty { null }); "URL saved" }
                    }
                }) { Text("Save URL") }
            }
        }

        Section("2. Health Connect permissions") {
            val missing = READ_PERMISSIONS - granted
            Text(if (missing.isEmpty()) "All read permissions granted." else "${missing.size} of ${READ_PERMISSIONS.size} read permissions missing.")
            EXTRA_PERMISSIONS.forEach { p -> Text("${p.substringAfterLast('.')}: ${if (p in granted) "granted" else "not granted"}", style = MaterialTheme.typography.bodySmall) }
            Button(enabled = sdkOk, onClick = { permissionLauncher.launch(READ_PERMISSIONS + EXTRA_PERMISSIONS + DebugTools.writePermissions) }) {
                Text("Grant permissions")
            }
        }

        Section("3. Data sources") {
            Text("Pick which app's data to trust for each type. Changing this later triggers a full resync.")
            if (originsConfirmed) {
                confirmedOrigins.forEach { (k, v) -> Text("$k: ${v.joinToString()}", style = MaterialTheme.typography.bodySmall) }
            }
            OutlinedButton(enabled = !busy && sdkOk && (READ_PERMISSIONS - granted).isEmpty(), onClick = {
                run("Discovering sources") {
                    val now = Instant.now()
                    DataKind.entries.forEach { kind ->
                        val counts = controller.source.discoverOrigins(kind, now)
                        discovered[kind] = counts
                        selected[kind] = confirmedOrigins[kind.key]?.takeIf { originsConfirmed }
                            ?: counts.maxByOrNull { it.value }?.let { setOf(it.key) } ?: emptySet()
                    }
                    "Found sources for ${discovered.count { it.value.isNotEmpty() }} data types — review and confirm"
                }
            }) { Text("Discover sources (last 30 days)") }
            DataKind.entries.forEach { kind ->
                val counts = discovered[kind] ?: return@forEach
                Spacer(Modifier.height(4.dp))
                Text(
                    if (kind == DataKind.STEPS) "${kind.label} — pick the source that matches your Fitbit numbers" else kind.label,
                    fontWeight = FontWeight.Medium,
                )
                if (counts.isEmpty()) Text("No data found", style = MaterialTheme.typography.bodySmall)
                counts.entries.sortedByDescending { it.value }.forEach { (origin, n) ->
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        val on = origin in (selected[kind] ?: emptySet())
                        Checkbox(on, onCheckedChange = { c ->
                            val cur = selected[kind] ?: emptySet()
                            selected[kind] = if (c) cur + origin else cur - origin
                        })
                        Text("$origin ($n records)", style = MaterialTheme.typography.bodySmall)
                    }
                }
            }
            if (discovered.isNotEmpty()) {
                Button(enabled = !busy && selected[DataKind.SLEEP].orEmpty().isNotEmpty(), onClick = {
                    run("Saving sources") {
                        controller.setOrigins(selected.mapKeys { it.key.key }.filterValues { it.isNotEmpty() })
                        "Sources confirmed"
                    }
                }) { Text("Confirm sources") }
            }
        }

        Section("4. Sync") {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(enabled = !busy && originsConfirmed && secretSaved, onClick = {
                    run("Syncing") { describe(controller.sync()) }
                }) { Text("Sync now") }
                OutlinedButton(enabled = !busy && originsConfirmed && secretSaved, onClick = { confirmFull = true }) { Text("Full resync") }
            }
        }

        DebugTools.Section(controller.client) { msg -> message = msg; refreshKey++ }
    }

    if (confirmFull) {
        AlertDialog(
            onDismissRequest = { confirmFull = false },
            title = { Text("Full resync?") },
            text = { Text("Re-reads all of Health Connect and rebuilds the server copy. Rows no longer in Health Connect are removed when it finishes.") },
            confirmButton = { TextButton(onClick = { confirmFull = false; run("Full resync") { describe(controller.sync(full = true)) } }) { Text("Resync") } },
            dismissButton = { TextButton(onClick = { confirmFull = false }) { Text("Cancel") } },
        )
    }
}

private fun describe(o: SyncEngine.Outcome): String = when (o) {
    is SyncEngine.Outcome.Ok -> "Sync complete: ${o.uploaded} items uploaded${if (o.fullResync) " (full resync)" else ""}"
    is SyncEngine.Outcome.Failed -> when (o.category.wire) {
        "auth" -> "Sync failed: the server rejected the secret. Re-enter it."
        "hc_permission" -> "Sync failed: Health Connect permissions are missing."
        else -> "Sync failed (${o.category.wire}). It will be retried."
    }
    SyncEngine.Outcome.NotConfigured -> "Confirm data sources first."
}
