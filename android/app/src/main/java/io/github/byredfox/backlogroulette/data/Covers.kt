package io.github.byredfox.backlogroulette.data

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import coil3.SingletonImageLoader
import coil3.request.CachePolicy
import coil3.request.ImageRequest
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext

/** Обложки: подкачка в фоне и заранее — для барабана. Только с сервера картинок Steam (адреса — Game.coverUrl). */
object Covers {
    private const val PAUSE_MS = 150L // между обложками — не забиваем сеть и сервер

    /** Обложки этих игр — в кэш заранее (барабан крутится несколько секунд, итог успевает скачаться). */
    fun warm(context: Context, games: List<Game>) {
        val loader = SingletonImageLoader.get(context)
        games.mapNotNull { it.coverUrl }.distinct().forEach { url ->
            loader.enqueue(ImageRequest.Builder(context).data(url).build())
        }
    }

    /** Все обложки по одной, только по Wi-Fi (сеть без лимита трафика), пока открыто приложение.
     *  Уже скачанные пропускаются. Прогресс — в store.coversDone/coversTotal. */
    suspend fun prefetchAll(context: Context, store: LibraryStore) = withContext(Dispatchers.IO) {
        val urls = store.library?.games?.mapNotNull { it.coverUrl }?.distinct() ?: return@withContext
        val loader = SingletonImageLoader.get(context)
        val network = context.getSystemService(ConnectivityManager::class.java)
        store.coversTotal = urls.size
        var done = 0
        var fetched = 0
        for (url in urls) {
            if (!store.prefetchOn) return@withContext
            val cached = loader.diskCache?.openSnapshot(url)?.use { true } ?: false
            if (!cached) {
                val caps = network?.getNetworkCapabilities(network.activeNetwork)
                if (caps == null || !caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED)) {
                    store.coversWaiting = true
                    store.coversDone = done
                    return@withContext
                }
                store.coversWaiting = false
                loader.execute(
                    ImageRequest.Builder(context).data(url).memoryCachePolicy(CachePolicy.DISABLED).build()
                )
                fetched++
                delay(PAUSE_MS)
            }
            done++
            if (done % 10 == 0) store.coversDone = done
        }
        store.coversDone = done
        store.coversWaiting = false
        if (fetched > 0) AppLog.info("Обложки: скачано в фоне $fetched, всего на телефоне $done из ${urls.size}")
    }
}
