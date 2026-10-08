package dev.jatin.healthsync

import androidx.health.connect.client.records.ExerciseSessionRecord
import androidx.health.connect.client.records.SleepSessionRecord
import dev.jatin.healthsync.hc.BackgroundPolicy
import dev.jatin.healthsync.hc.Mappings
import dev.jatin.healthsync.hc.RestoreGuard
import dev.jatin.healthsync.hc.UrlPolicy
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class AppPoliciesTest {
    private val backendStages = setOf("AWAKE", "SLEEPING", "OUT_OF_BED", "LIGHT", "DEEP", "REM", "AWAKE_IN_BED", "UNKNOWN")
    private val backendExerciseTypes = setOf(
        "running", "walking", "cycling", "swimming", "hiking", "strength_training", "yoga", "pilates",
        "hiit", "elliptical", "rowing", "stair_climbing", "dancing", "sports", "martial_arts", "other",
    )

    @Test
    fun `stage ints map to the backend's stage names`() {
        assertEquals("DEEP", Mappings.stageName(SleepSessionRecord.STAGE_TYPE_DEEP))
        assertEquals("REM", Mappings.stageName(SleepSessionRecord.STAGE_TYPE_REM))
        assertEquals("AWAKE_IN_BED", Mappings.stageName(SleepSessionRecord.STAGE_TYPE_AWAKE_IN_BED))
        assertEquals("UNKNOWN", Mappings.stageName(999))
        assertTrue((0..10).map(Mappings::stageName).all { it in backendStages })
    }

    @Test
    fun `every exercise type maps into the backend enum`() {
        assertEquals("running", Mappings.exerciseType(ExerciseSessionRecord.EXERCISE_TYPE_RUNNING_TREADMILL))
        assertEquals("cycling", Mappings.exerciseType(ExerciseSessionRecord.EXERCISE_TYPE_BIKING))
        assertEquals("sports", Mappings.exerciseType(ExerciseSessionRecord.EXERCISE_TYPE_TENNIS))
        assertEquals("other", Mappings.exerciseType(ExerciseSessionRecord.EXERCISE_TYPE_SAILING))
        assertTrue((0..120).map(Mappings::exerciseType).all { it in backendExerciseTypes })
    }

    @Test
    fun `background status distinguishes supported from enabled`() {
        val granted = BackgroundPolicy.evaluate(featureAvailable = true, permissionGranted = true)
        assertTrue(granted.supported && granted.enabled)
        assertNull(granted.message)
        val denied = BackgroundPolicy.evaluate(featureAvailable = true, permissionGranted = false)
        assertTrue(denied.supported)
        assertFalse(denied.enabled)
        assertEquals(BackgroundPolicy.UNAVAILABLE_MESSAGE, denied.message)
        val unsupported = BackgroundPolicy.evaluate(featureAvailable = false, permissionGranted = true)
        assertFalse(unsupported.supported || unsupported.enabled)
        assertEquals("Background sync unavailable — open the app to sync.", unsupported.message)
    }

    @Test
    fun `release ignores URL overrides and everything must be https`() {
        val prod = "https://my-backend.example.run.app"
        assertEquals(prod, UrlPolicy.resolve(prod, "https://evil.example", allowOverride = false))
        assertEquals("https://dev.example", UrlPolicy.resolve(prod, "https://dev.example/", allowOverride = true))
        assertEquals(prod, UrlPolicy.resolve(prod, "  ", allowOverride = true))
        assertFailsWith<IllegalArgumentException> { UrlPolicy.resolve(prod, "http://10.0.2.2:8000", allowOverride = true) }
    }

    @Test
    fun `restore guard resets only when a stored secret cannot be decrypted`() {
        assertTrue(RestoreGuard.mustReset(secretPresent = true, secretDecryptable = false))
        assertFalse(RestoreGuard.mustReset(secretPresent = true, secretDecryptable = true))
        assertFalse(RestoreGuard.mustReset(secretPresent = false, secretDecryptable = false)) // fresh setup
    }

    @Test
    fun `configured base URL is https or unset`() {
        assertTrue(BuildConfig.BASE_URL.isEmpty() || BuildConfig.BASE_URL.startsWith("https://"))
    }
}
