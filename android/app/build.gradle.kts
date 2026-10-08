import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.plugin.compose")
}

// Your backend's HTTPS origin, baked into the release build (the phone secret is only ever
// sent there). Set `healthsync.baseUrl=https://...` in local.properties (git-ignored).
val localProps = Properties().apply {
    rootProject.file("local.properties").takeIf { it.exists() }?.inputStream()?.use { load(it) }
}
val productionBaseUrl: String = (localProps.getProperty("healthsync.baseUrl")
    ?: providers.gradleProperty("healthsync.baseUrl").orNull ?: "").trim().trimEnd('/')
if (productionBaseUrl.isEmpty()) {
    logger.warn("healthsync.baseUrl is not set in local.properties; the app will report 'backend URL not configured'.")
} else {
    require(productionBaseUrl.startsWith("https://")) { "healthsync.baseUrl must start with https://" }
}

android {
    namespace = "dev.jatin.healthsync"
    compileSdk = 37

    defaultConfig {
        applicationId = "dev.jatin.healthsync"
        minSdk = 28
        targetSdk = 37
        versionCode = 1
        versionName = "1.0"
        buildConfigField("String", "BASE_URL", "\"$productionBaseUrl\"")
    }

    buildTypes {
        debug {
            applicationIdSuffix = ".debug"
            // Debug may override the URL (HTTPS only) and can generate fake data on an emulator.
            buildConfigField("Boolean", "ALLOW_URL_OVERRIDE", "true")
        }
        release {
            buildConfigField("Boolean", "ALLOW_URL_OVERRIDE", "false")
            isMinifyEnabled = false
            // Sideloaded personal app: signed with the debug key unless a release key is configured.
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    testOptions {
        unitTests.isReturnDefaultValues = true
    }
}

kotlin {
    jvmToolchain(17)
}

dependencies {
    implementation(project(":sync-core"))

    implementation("androidx.health.connect:connect-client:1.1.0")
    implementation("androidx.work:work-runtime-ktx:2.12.0")
    implementation("androidx.datastore:datastore-preferences:1.2.1")
    implementation("com.squareup.okhttp3:okhttp:5.5.0")
    implementation("androidx.core:core-ktx:1.19.1")
    implementation("androidx.activity:activity-compose:1.13.0")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.11.0")
    implementation(platform("androidx.compose:compose-bom:2026.09.00"))
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui")

    testImplementation(kotlin("test-junit"))
    testImplementation("junit:junit:4.13.2")
}
