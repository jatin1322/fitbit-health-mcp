package dev.jatin.healthsync.hc

import androidx.health.connect.client.records.ExerciseSessionRecord as E
import androidx.health.connect.client.records.SleepSessionRecord as S

/** Pure mappings from Health Connect constants to the backend's enums. */
object Mappings {
    /** Health Connect stage int -> backend HcStage name. Unknown ints map to UNKNOWN. */
    fun stageName(type: Int): String = when (type) {
        S.STAGE_TYPE_AWAKE -> "AWAKE"
        S.STAGE_TYPE_SLEEPING -> "SLEEPING"
        S.STAGE_TYPE_OUT_OF_BED -> "OUT_OF_BED"
        S.STAGE_TYPE_LIGHT -> "LIGHT"
        S.STAGE_TYPE_DEEP -> "DEEP"
        S.STAGE_TYPE_REM -> "REM"
        S.STAGE_TYPE_AWAKE_IN_BED -> "AWAKE_IN_BED"
        else -> "UNKNOWN"
    }

    /** Health Connect exercise type -> backend ExerciseType value. Anything unlisted is "other". */
    fun exerciseType(type: Int): String = when (type) {
        E.EXERCISE_TYPE_RUNNING, E.EXERCISE_TYPE_RUNNING_TREADMILL -> "running"
        E.EXERCISE_TYPE_WALKING -> "walking"
        E.EXERCISE_TYPE_BIKING, E.EXERCISE_TYPE_BIKING_STATIONARY -> "cycling"
        E.EXERCISE_TYPE_SWIMMING_POOL, E.EXERCISE_TYPE_SWIMMING_OPEN_WATER -> "swimming"
        E.EXERCISE_TYPE_HIKING -> "hiking"
        E.EXERCISE_TYPE_STRENGTH_TRAINING, E.EXERCISE_TYPE_WEIGHTLIFTING, E.EXERCISE_TYPE_CALISTHENICS -> "strength_training"
        E.EXERCISE_TYPE_YOGA -> "yoga"
        E.EXERCISE_TYPE_PILATES -> "pilates"
        E.EXERCISE_TYPE_HIGH_INTENSITY_INTERVAL_TRAINING, E.EXERCISE_TYPE_BOOT_CAMP -> "hiit"
        E.EXERCISE_TYPE_ELLIPTICAL -> "elliptical"
        E.EXERCISE_TYPE_ROWING, E.EXERCISE_TYPE_ROWING_MACHINE -> "rowing"
        E.EXERCISE_TYPE_STAIR_CLIMBING, E.EXERCISE_TYPE_STAIR_CLIMBING_MACHINE -> "stair_climbing"
        E.EXERCISE_TYPE_DANCING -> "dancing"
        E.EXERCISE_TYPE_MARTIAL_ARTS, E.EXERCISE_TYPE_BOXING -> "martial_arts"
        E.EXERCISE_TYPE_BADMINTON, E.EXERCISE_TYPE_BASEBALL, E.EXERCISE_TYPE_BASKETBALL, E.EXERCISE_TYPE_CRICKET,
        E.EXERCISE_TYPE_FOOTBALL_AMERICAN, E.EXERCISE_TYPE_FOOTBALL_AUSTRALIAN, E.EXERCISE_TYPE_FRISBEE_DISC,
        E.EXERCISE_TYPE_GOLF, E.EXERCISE_TYPE_HANDBALL, E.EXERCISE_TYPE_ICE_HOCKEY, E.EXERCISE_TYPE_RACQUETBALL,
        E.EXERCISE_TYPE_ROLLER_HOCKEY, E.EXERCISE_TYPE_RUGBY, E.EXERCISE_TYPE_SOCCER, E.EXERCISE_TYPE_SOFTBALL,
        E.EXERCISE_TYPE_SQUASH, E.EXERCISE_TYPE_TABLE_TENNIS, E.EXERCISE_TYPE_TENNIS, E.EXERCISE_TYPE_VOLLEYBALL,
        E.EXERCISE_TYPE_WATER_POLO, E.EXERCISE_TYPE_FENCING -> "sports"
        else -> "other"
    }
}

/** Data types whose source app (data origin) the user picks, one set per type. */
enum class DataKind(val key: String, val label: String) {
    SLEEP("sleep", "Sleep"),
    EXERCISE("exercise", "Exercise sessions"),
    STEPS("steps", "Steps"),
    HEART_RATE("heart_rate", "Heart rate"),
    RESTING_HEART_RATE("resting_heart_rate", "Resting heart rate"),
    HRV("hrv", "Heart rate variability"),
    DISTANCE("distance", "Distance"),
    CALORIES("calories", "Calories burned"),
}

/** Whether background sync can run, and what to tell the user. */
data class BackgroundStatus(val supported: Boolean, val enabled: Boolean, val message: String?)

object BackgroundPolicy {
    const val UNAVAILABLE_MESSAGE = "Background sync unavailable — open the app to sync."

    fun evaluate(featureAvailable: Boolean, permissionGranted: Boolean): BackgroundStatus {
        val enabled = featureAvailable && permissionGranted
        return BackgroundStatus(supported = featureAvailable, enabled = enabled, message = if (enabled) null else UNAVAILABLE_MESSAGE)
    }
}

/** Where the phone secret may be sent. Release: the built-in host only. Debug: HTTPS override allowed. */
object UrlPolicy {
    fun resolve(builtIn: String, override: String?, allowOverride: Boolean): String {
        val candidate = override?.trim()?.trimEnd('/')?.takeIf { allowOverride && it.isNotEmpty() } ?: builtIn
        require(candidate.startsWith("https://")) { "backend URL must be https" }
        return candidate
    }
}

/**
 * Backups are disabled, but as defence in depth: an install whose encrypted secret
 * exists yet cannot be decrypted (Keystore keys are never backed up or transferred)
 * is a restored/migrated copy. Its sync state, tokens included, must be discarded.
 */
object RestoreGuard {
    fun mustReset(secretPresent: Boolean, secretDecryptable: Boolean): Boolean = secretPresent && !secretDecryptable
}
