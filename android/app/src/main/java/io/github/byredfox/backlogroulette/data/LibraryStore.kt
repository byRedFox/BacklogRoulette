package io.github.byredfox.backlogroulette.data

import android.content.Context
import android.net.Uri
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.InputStream

/**
 * Данные приложения во внутренней папке (другим приложениям недоступна):
 * library.json — последний импортированный файл с ПК как есть, state.json — что изменили на телефоне
 * (убранные из рулетки, какую коллекцию крутить, фильтры, подкачка обложек). Повторный импорт заменяет
 * library.json, а state.json остаётся.
 */
class LibraryStore(context: Context) {
    private val app = context.applicationContext
    private val libraryFile = File(app.filesDir, "library.json")
    private val stateFile = File(app.filesDir, "state.json")

    var library by mutableStateOf<Library?>(null)
        private set
    var removedHere by mutableStateOf<Set<GameRef>>(emptySet())
        private set
    var pool by mutableStateOf<String?>(null) // null — все игры Steam, иначе "col:<id>" — коллекция
        private set
    var filters by mutableStateOf(RouletteFilters())
        private set
    private var filtersSaved = false // фильтры уже меняли на телефоне — импорт их не трогает
    var prefetchOn by mutableStateOf(true)
        private set
    var message by mutableStateOf<String?>(null) // для уведомления внизу экрана
    var picked by mutableStateOf<Game?>(null) // что выпало — остаётся, пока переключаются разделы
    var coversDone by mutableIntStateOf(0) // подкачка обложек: у скольких картинка уже на телефоне
    var coversMissing by mutableIntStateOf(0) // у скольких её нет нигде в Steam
    var coversTotal by mutableIntStateOf(0)
    var coversWaiting by mutableStateOf(false) // ждёт Wi-Fi

    val inCollection: Boolean get() = pool != null

    private val loadLock = Mutex()
    private var loadDone = false

    /** Прочитать сохранённое. Один раз: зовут и экран, и подкачка обложек в фоне — кто первый. */
    suspend fun load() = loadLock.withLock {
        if (!loadDone) {
            readSaved()
            loadDone = true
        }
    }

    private suspend fun readSaved() = withContext(Dispatchers.IO) {
        val state = try {
            stateFile.takeIf { it.exists() }?.let { JSONObject(it.readText()) }
        } catch (e: Exception) {
            AppLog.warning("state.json не прочитался: ${e.message}")
            null
        }
        val loaded = try {
            libraryFile.takeIf { it.exists() }?.let { ExportParser.parse(it.readText()) }
        } catch (e: Exception) {
            AppLog.warning("Сохранённые данные не прочитались: ${e.message}")
            null
        }
        withContext(Dispatchers.Main) {
            if (state != null) {
                removedHere = state.objects("removed").mapNotNull(ExportParser::parseRef).toSet()
                pool = state.str("pool").takeIf { it.startsWith(COLLECTION_POOL) }
                filtersSaved = state.has("filters")
                filters = if (filtersSaved) RouletteFilters.fromJson(state.optJSONObject("filters"))
                else loaded?.filters ?: RouletteFilters()
                prefetchOn = state.optBoolean("prefetch", true)
            } else {
                filters = loaded?.filters ?: RouletteFilters()
            }
            library = loaded
        }
        loaded?.let { AppLog.info("Данные с ПК: игр ${it.games.size}, коллекций ${it.collections.size}") }
    }

    suspend fun import(uri: Uri) = withContext(Dispatchers.IO) {
        val result = try {
            val bytes = app.contentResolver.openInputStream(uri)?.use(::readLimited)
                ?: throw ImportError("файл не открылся")
            val text = bytes.toString(Charsets.UTF_8)
            val parsed = ExportParser.parse(text)
            writeAtomic(libraryFile, text)
            AppLog.info("Импорт с ПК: игр Steam ${parsed.games.size}, коллекций ${parsed.collections.size}, " +
                "сведений для фильтров ${parsed.meta.size}")
            parsed
        } catch (e: ImportError) {
            AppLog.warning("Импорт с ПК не получился: ${e.message}")
            withContext(Dispatchers.Main) { message = "Не получилось: ${e.message}" }
            return@withContext
        } catch (e: Exception) {
            AppLog.warning("Импорт с ПК не получился: $e")
            withContext(Dispatchers.Main) { message = "Файл не прочитался: ${e.message ?: e.javaClass.simpleName}" }
            return@withContext
        }
        withContext(Dispatchers.Main) {
            library = result
            if (!filtersSaved) filters = result.filters
            if (pool != null && result.collections.none { "$COLLECTION_POOL${it.id}" == pool }) {
                pool = null // крутили коллекцию, которой больше нет
            }
            message = "Готово: игр ${result.games.size}, коллекций ${result.collections.size}"
            if (prefetchOn) Covers.schedule(app, this@LibraryStore, replace = true) // новые игры — сразу в подкачку
        }
    }

    /** Игры, которые крутит рулетка сейчас. */
    fun poolGames(): List<Game> = poolGames(filters)

    fun poolGames(using: RouletteFilters): List<Game> {
        val lib = library ?: return emptyList()
        collection()?.let { collection ->
            // В коллекции крутятся и убранные из рулетки — как на ПК
            return collection.items.mapNotNull(lib::find).distinct()
        }
        return lib.games.filter { !lib.isRemoved(it, removedHere) && using.matches(lib.meta(it)) }
    }

    fun collection(): GameCollection? =
        pool?.let { current -> library?.collections?.firstOrNull { "$COLLECTION_POOL${it.id}" == current } }

    fun poolTitle(): String = collection()?.let { "Коллекция «${it.name}»" } ?: "Steam"

    fun choosePool(value: String?) {
        pool = value
        saveState()
        AppLog.info("Рулетка крутит: ${value ?: "все игры Steam"}")
    }

    fun chooseFilters(value: RouletteFilters) {
        if (value == filters && filtersSaved) return
        filters = value
        filtersSaved = true
        saveState()
        AppLog.info("Фильтры рулетки: жанры ${value.genres.size}, годы ${value.yearFrom ?: "…"}–${value.yearTo ?: "…"}, " +
            "отзывы от ${value.reviews} %")
    }

    fun choosePrefetch(on: Boolean) {
        prefetchOn = on
        saveState()
        if (on) {
            if (library != null) Covers.schedule(app, this)
        } else {
            Covers.cancel(app)
        }
    }

    fun removeFromRoulette(game: Game) {
        removedHere = removedHere + game.ref
        if (picked == game) picked = null
        saveState()
        AppLog.info("Убрана из рулетки на телефоне: «${game.name}»")
    }

    fun clear() {
        Covers.cancel(app)
        libraryFile.delete()
        stateFile.delete()
        library = null
        coversTotal = 0
        coversDone = 0
        coversMissing = 0
        picked = null
        removedHere = emptySet()
        pool = null
        filters = RouletteFilters()
        filtersSaved = false
        AppLog.info("Данные с ПК удалены с телефона")
    }

    private fun saveState() {
        val root = JSONObject()
        root.put("removed", JSONArray().apply { removedHere.forEach { put(it.toJson()) } })
        pool?.let { root.put("pool", it) }
        if (filtersSaved) root.put("filters", filters.toJson())
        root.put("prefetch", prefetchOn)
        try {
            writeAtomic(stateFile, root.toString(2))
        } catch (e: Exception) {
            AppLog.warning("state.json не сохранился: ${e.message}")
            message = "Изменение не сохранилось: ${e.message}"
        }
    }

    companion object {
        const val COLLECTION_POOL = "col:"
        private const val MAX_IMPORT_BYTES = 20 * 1024 * 1024

        private fun readLimited(input: InputStream): ByteArray {
            val out = ByteArrayOutputStream()
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val read = input.read(buffer)
                if (read < 0) break
                out.write(buffer, 0, read)
                if (out.size() > MAX_IMPORT_BYTES) throw ImportError("файл слишком большой — это не экспорт программы")
            }
            return out.toByteArray()
        }

        /** Сначала во временный файл, потом подмена — сбой при записи не испортит прежние данные. */
        private fun writeAtomic(file: File, text: String) {
            val tmp = File(file.parentFile, file.name + ".tmp")
            tmp.writeText(text)
            if (!tmp.renameTo(file)) {
                file.delete()
                if (!tmp.renameTo(file)) throw java.io.IOException("не удалось записать ${file.name}")
            }
        }
    }
}
