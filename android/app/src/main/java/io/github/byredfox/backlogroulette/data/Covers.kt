package io.github.byredfox.backlogroulette.data

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import androidx.work.Constraints
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import coil3.SingletonImageLoader
import coil3.request.CachePolicy
import coil3.request.ErrorResult
import coil3.request.ImageRequest
import coil3.request.SuccessResult
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.joinAll
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/** Обложки: подкачка в фоне и заранее — для барабана. Где брать обложку — CoverUrls. */
object Covers {
    private const val WORK = "covers"
    private const val PARALLEL = 6 // столько обложек качается разом
    private const val STEPS = 6 // старый адрес → магазин → SteamGridDB → шапка → ничего (с запасом)

    /** Обложки этих игр — в кэш заранее (барабан крутится несколько секунд, итог успевает скачаться). */
    fun warm(context: Context, games: List<Game>) {
        val loader = SingletonImageLoader.get(context)
        games.mapNotNull { CoverUrls.cover(it).url }.distinct().forEach { url ->
            loader.enqueue(ImageRequest.Builder(context).data(url).build())
        }
    }

    /** Подкачка всех обложек — фоновой задачей Android: идёт и при погасшем экране, ждёт Wi-Fi (сеть без лимита
     *  трафика), продолжается после перезапуска. replace — начать заново (новый импорт, новый ключ). */
    fun schedule(context: Context, store: LibraryStore, replace: Boolean = false) {
        store.coversWaiting = !unmetered(context)
        val request = OneTimeWorkRequestBuilder<CoverWorker>()
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.UNMETERED).build())
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(
            WORK, if (replace) ExistingWorkPolicy.REPLACE else ExistingWorkPolicy.KEEP, request,
        )
    }

    fun cancel(context: Context) {
        WorkManager.getInstance(context).cancelUniqueWork(WORK)
    }

    private fun unmetered(context: Context): Boolean {
        val network = context.getSystemService(ConnectivityManager::class.java) ?: return false
        val caps = network.getNetworkCapabilities(network.activeNetwork) ?: return false
        return caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED)
    }

    /** Все обложки, по PARALLEL разом. Уже скачанные пропускаются. Прогресс — в store.coversDone (есть картинка),
     *  coversMissing (нет нигде) и раз в секунду в progress — только если есть что качать. */
    suspend fun prefetchAll(
        context: Context,
        store: LibraryStore,
        progress: suspend (done: Int, total: Int) -> Unit = { _, _ -> },
    ) = withContext(Dispatchers.IO) {
        val games = store.library?.games?.filter { it.appid != null }?.distinctBy { it.appid } ?: return@withContext
        val loader = SingletonImageLoader.get(context)
        val done = AtomicInteger()
        val missing = AtomicInteger()
        val fetched = AtomicInteger()
        fun publish() {
            store.coversTotal = games.size
            store.coversDone = done.get()
            store.coversMissing = missing.get()
        }
        fun cached(url: String) = loader.diskCache?.openSnapshot(url)?.use { true } == true

        // Что уже на телефоне — без сети. Скачанную шапку стоит заменить, если SteamGridDB ещё не спрашивали
        val need = mutableListOf<Game>()
        for (game in games) {
            val (cover, better) = withContext(Dispatchers.Main) { CoverUrls.cover(game) to CoverUrls.gridMayHelp(game) }
            val url = cover.url
            when {
                better -> need += game
                url == null -> missing.incrementAndGet()
                cached(url) -> done.incrementAndGet()
                else -> need += game
            }
        }
        publish()
        if (need.isEmpty() || !store.prefetchOn) return@withContext
        if (!unmetered(context)) {
            store.coversWaiting = true
            return@withContext
        }
        store.coversWaiting = false
        progress(done.get(), games.size)

        // Адреса — сразу для всех: магазин Steam по 100 игр за запрос, потом SteamGridDB
        CoverUrls.lookup(need.mapNotNull { it.appid })

        val next = AtomicInteger()
        val stop = AtomicBoolean(false)
        suspend fun fetch(game: Game) {
            repeat(STEPS) {
                val cover = withContext(Dispatchers.Main) { CoverUrls.cover(game) }
                val url = cover.url
                if (cover.kind == CoverUrls.Kind.NONE || url == null) {
                    missing.incrementAndGet()
                    return
                }
                if (cached(url)) {
                    done.incrementAndGet()
                    return
                }
                if (!unmetered(context)) {
                    stop.set(true)
                    store.coversWaiting = true
                    return
                }
                // Маленький размер — картинка ложится в кэш на диске целиком, а расшифровывается уменьшенной: быстрее
                val result = loader.execute(
                    ImageRequest.Builder(context).data(url).size(64, 96)
                        .memoryCachePolicy(CachePolicy.DISABLED).build()
                )
                fetched.incrementAndGet()
                if (result is SuccessResult) {
                    done.incrementAndGet()
                    return
                }
                val error = (result as? ErrorResult)?.throwable
                if (!CoverUrls.isMissing(error)) return // нет сети или сервер занят — эту игру в следующий раз
                withContext(Dispatchers.Main) { CoverUrls.failed(game, url) }
                CoverUrls.lookup(listOf(game.appid ?: return))
            }
        }
        coroutineScope {
            val ticker = launch {
                while (true) {
                    publish()
                    progress(done.get(), games.size)
                    delay(1000)
                }
            }
            List(PARALLEL) {
                launch {
                    while (store.prefetchOn && !stop.get()) {
                        fetch(need.getOrNull(next.getAndIncrement()) ?: break)
                    }
                }
            }.joinAll()
            ticker.cancel()
        }
        publish()
        if (!stop.get()) store.coversWaiting = false
        if (fetched.get() > 0) {
            AppLog.info("Обложки: скачано в фоне ${fetched.get()}, есть у ${done.get()} из ${games.size}, " +
                "нет нигде у ${missing.get()}")
        }
    }
}
