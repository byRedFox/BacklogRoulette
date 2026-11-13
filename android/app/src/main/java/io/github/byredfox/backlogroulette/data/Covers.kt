package io.github.byredfox.backlogroulette.data

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import coil3.SingletonImageLoader
import coil3.request.CachePolicy
import coil3.request.ErrorResult
import coil3.request.ImageRequest
import coil3.request.SuccessResult
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext

/** Обложки: подкачка в фоне и заранее — для барабана. Где брать обложку — CoverUrls. */
object Covers {
    private const val PAUSE_MS = 150L // между обложками — не забиваем сеть и сервер
    private const val STEPS = 4 // старый адрес → магазин → шапка → ничего

    /** Обложки этих игр — в кэш заранее (барабан крутится несколько секунд, итог успевает скачаться). */
    fun warm(context: Context, games: List<Game>) {
        val loader = SingletonImageLoader.get(context)
        games.mapNotNull { CoverUrls.cover(it).url }.distinct().forEach { url ->
            loader.enqueue(ImageRequest.Builder(context).data(url).build())
        }
    }

    /** Все обложки по одной, только по Wi-Fi (сеть без лимита трафика), пока открыто приложение.
     *  Уже скачанные пропускаются. Прогресс — в store.coversDone (есть картинка), coversMissing (нет нигде). */
    suspend fun prefetchAll(context: Context, store: LibraryStore) = withContext(Dispatchers.IO) {
        val games = store.library?.games?.filter { it.appid != null }?.distinctBy { it.appid } ?: return@withContext
        val loader = SingletonImageLoader.get(context)
        val network = context.getSystemService(ConnectivityManager::class.java)
        var done = 0
        var missing = 0
        var fetched = 0
        fun publish() {
            store.coversTotal = games.size
            store.coversDone = done
            store.coversMissing = missing
        }
        publish()
        for ((index, game) in games.withIndex()) {
            if (!store.prefetchOn) return@withContext
            for (step in 0 until STEPS) {
                val cover = withContext(Dispatchers.Main) { CoverUrls.cover(game) }
                val url = cover.url
                if (cover.kind == CoverUrls.Kind.NONE || url == null) {
                    missing++
                    break
                }
                if (loader.diskCache?.openSnapshot(url)?.use { true } == true) {
                    done++
                    break
                }
                val caps = network?.getNetworkCapabilities(network.activeNetwork)
                if (caps == null || !caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED)) {
                    store.coversWaiting = true
                    publish()
                    return@withContext
                }
                store.coversWaiting = false
                val result = loader.execute(
                    ImageRequest.Builder(context).data(url).memoryCachePolicy(CachePolicy.DISABLED).build()
                )
                fetched++
                delay(PAUSE_MS)
                if (result is SuccessResult) {
                    done++
                    break
                }
                val error = (result as? ErrorResult)?.throwable
                if (!CoverUrls.isMissing(error)) break // нет сети или сервер занят — эту игру в следующий раз
                withContext(Dispatchers.Main) { CoverUrls.failed(game, url) }
                CoverUrls.resolvePending()
            }
            if (index % 10 == 0) publish()
        }
        publish()
        store.coversWaiting = false
        if (fetched > 0) {
            AppLog.info("Обложки: скачано в фоне $fetched, есть у $done из ${games.size}, нет нигде в Steam у $missing")
        }
    }
}
