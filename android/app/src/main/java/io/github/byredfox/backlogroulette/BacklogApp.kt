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
import okio.Path.Companion.toOkioPath

/** Общее на всё приложение: данные с ПК нужны и экрану, и подкачке обложек в фоне (она идёт и без экрана). */
class BacklogApp : Application(), SingletonImageLoader.Factory {
    val store by lazy { LibraryStore(this) }

    override fun onCreate() {
        super.onCreate()
        AppLog.init(filesDir)
        CoverUrls.init(filesDir)
        SteamGridKey.init(filesDir)
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
