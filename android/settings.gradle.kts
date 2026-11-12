// Backlog Roulette для Android. Собирается на GitHub (.github/workflows/android.yml) — Android Studio не нужна.
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

rootProject.name = "BacklogRoulette"
include(":app")
