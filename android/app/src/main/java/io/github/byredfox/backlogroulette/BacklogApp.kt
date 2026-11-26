package io.github.byredfox.backlogroulette

import android.app.Application
import coil3.ImageLoader
import coil3.PlatformContext
import coil3.SingletonImageLoader
import coil3.disk.DiskCache
import io.github.byredfox.backlogroulette.data.AppLog
import io.github.byredfox.backlogroulette.data.CoverUrls
import io.github.byredfox.backlogroulette.data.LibraryStore
import io.github.byredfox.backlogroulette.data.SteamGridKey
import io.github.byredfox.backlogroulette.data.UiPrefs
import okio.Path.Companion.toOkioPath

/** Общее на всё приложение: данные с ПК нужны и экрану, и подкачке обложек в фоне (она идёт и без экрана). */
class BacklogApp : Application(), SingletonImageLoader.Factory {
    val store by lazy { LibraryStore(this) }

    override fun onCreate() {
        super.onCreate()
        AppLog.init(filesDir)
        CoverUrls.init(filesDir)
        SteamGridKey.init(filesDir)
        UiPrefs.init(this)
        removePortLeftovers()
    }

    // Раздел «Нативные ИИ порты» (0.6.0–0.6.1) убран: скачанный APK порта (десятки МБ) и метки релиза не должны
    // оставаться на телефоне. Сами порты, если стоят, — отдельные приложения: их удаляет пользователь, как обычно.
    private fun removePortLeftovers() {
        val apks = cacheDir.resolve("ports")
        if (apks.exists() && apks.deleteRecursively()) AppLog.info("Убраны файлы прежнего раздела портов")
        deleteSharedPreferences("ports")
    }

    // Кэш обложек на диске — 512 МБ: по умолчанию он не больше 250 МБ, а библиотека в 800+ игр туда не влезает
    // целиком — старые обложки вытеснялись бы новыми и качались заново. Папка та же, что была: скачанное остаётся.
    override fun newImageLoader(context: PlatformContext): ImageLoader =
        ImageLoader.Builder(context)
            .diskCache {
                DiskCache.Builder()
                    .directory(cacheDir.resolve("image_cache").toOkioPath())
                    .maxSizeBytes(512L * 1024 * 1024)
                    .build()
            }
            .build()
}
