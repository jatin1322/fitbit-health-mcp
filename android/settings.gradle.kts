pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "health-sync"
include(":sync-core")
// The Android app module is included once the Android SDK is available.
if (file("app/build.gradle.kts").exists() && (System.getenv("ANDROID_HOME") != null || file("local.properties").exists())) {
    include(":app")
}
