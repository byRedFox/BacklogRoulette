import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

// Постоянный ключ подписи — только на GitHub: файл ключа из секрета BR_KEYSTORE_BASE64 сборка кладёт во временную
// папку и передаёт путь и пароль через переменные окружения. В репозитории ключа нет. Одна и та же подпись —
// чтобы новая версия ставилась поверх прежней, с данными.
val keystorePath: String? = System.getenv("BR_KEYSTORE_PATH")

android {
    namespace = "io.github.byredfox.backlogroulette"
    compileSdk = 36

    defaultConfig {
        applicationId = "io.github.byredfox.backlogroulette"
        minSdk = 26 // Android 8 — ставится и на старые телефоны
        targetSdk = 36 // Android 16
        versionCode = 6 // растёт с каждой сборкой для телефона: меньший номер Android поверх не поставит
        versionName = "0.4.0"
    }

    signingConfigs {
        create("release") {
            if (keystorePath != null) {
                storeFile = file(keystorePath)
                storePassword = System.getenv("BR_KEYSTORE_PASSWORD")
                keyAlias = "backlogroulette"
                keyPassword = System.getenv("BR_KEYSTORE_PASSWORD") // в ключе PKCS12 пароль один
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = if (keystorePath != null) signingConfigs.getByName("release") else null
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    lint {
        checkReleaseBuilds = false // проверку кода (lint) запустим отдельно, когда дойдём до релизов
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2025.06.00")
    implementation(composeBom)
    implementation("androidx.core:core-ktx:1.16.0")
    implementation("androidx.activity:activity-compose:1.10.1")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-core")
    implementation("androidx.compose.ui:ui-tooling-preview")
    debugImplementation("androidx.compose.ui:ui-tooling")
    // Обложки: загрузка и кэш картинок (только с серверов картинок Steam — адреса программа собирает сама)
    implementation("io.coil-kt.coil3:coil-compose:3.2.0")
    implementation("io.coil-kt.coil3:coil-network-okhttp:3.2.0")
    // Подкачка обложек в фоне: продолжается при погасшем экране, ждёт Wi-Fi, переживает перезапуск телефона
    implementation("androidx.work:work-runtime-ktx:2.10.1")
}
