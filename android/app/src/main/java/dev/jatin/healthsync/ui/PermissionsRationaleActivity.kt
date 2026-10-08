package dev.jatin.healthsync.ui

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

/** Shown by Health Connect's "privacy policy" link (rationale intent / ViewPermissionUsageActivity). */
class PermissionsRationaleActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                Surface(Modifier.fillMaxSize()) {
                    Column(Modifier.padding(24.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                        Text("How Health Sync uses your data", style = MaterialTheme.typography.headlineSmall)
                        Text("This personal app reads sleep (with stages), heart rate, resting heart rate, heart rate variability, steps, exercise sessions, distance and calories from Health Connect.")
                        Text("It uploads daily aggregates and per-session summaries over HTTPS to your own private server, which Claude can read through your connector. Raw heart-rate samples, exercise titles and notes never leave the phone.")
                        Text("Background access lets it sync about every 6 hours; past-data access lets the first sync include your history. Nothing is shared with anyone else, and the app never writes to Health Connect.")
                        Button(onClick = { finish() }) { Text("Close") }
                    }
                }
            }
        }
    }
}
