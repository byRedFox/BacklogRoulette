package io.github.byredfox.backlogroulette.data

import android.content.Context
import android.net.Uri
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.InputStream

/**
 * Данные приложения во внутренней папке (другим приложениям недоступна):
 * library.json — последний импортированный файл с ПК как есть, state.json — что изменили на телефоне
 * (убранные из рулетки, что крутит рулетка). Повторный импорт заменяет library.json, а state.json остаётся.
 */
class LibraryStore(context: Context) {
    private val app = context.applicationContext
    private val libraryFile = File(app.filesDir, "library.json")
    private val stateFile = File(app.filesDir, "state.json")

    var library by mutableStateOf<Library?>(null)
        private set
    var removedHere by mutableStateOf<Set<GameRef>>(emptySet())
        private set
    var pool by mutableStateOf<String?>(null) // null — как на ПК
        private set
    var message by mutableStateOf<String?>(null) // для уведомления внизу экрана
    var picked by mutableStateOf<Game?>(null) // что выпало — остаётся, пока переключаются разделы

    val activePool: String get() = pool ?: library?.pool ?: "steam"

    suspend fun load() = withContext(Dispatchers.IO) {
        val state = readState()
        val loaded = try {
            libraryFile.takeIf { it.exists() }?.let { ExportParser.parse(it.readText()) }
        } catch (e: Exception) {
            AppLog.warning("Сохранённые данные не прочитались: ${e.message}")
            null
        }
        withContext(Dispatchers.Main) {
            removedHere = state.first
            pool = state.second
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
            AppLog.info("Импорт с ПК: Steam ${parsed.count(Game.KIND_STEAM)}, портов ${parsed.count(Game.KIND_PORT)}, " +
                "дисковых ${parsed.count(Game.KIND_DISC)}, коллекций ${parsed.collections.size}")
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
            if (pool?.startsWith(COLLECTION_POOL) == true && result.collections.none { "$COLLECTION_POOL${it.id}" == pool }) {
                pool = null // крутили коллекцию, которой больше нет
            }
            message = "Готово: игр ${result.games.size}, коллекций ${result.collections.size}"
        }
    }

    /** Игры, которые крутит рулетка сейчас. */
    fun poolGames(): List<Game> {
        val lib = library ?: return emptyList()
        val current = activePool
        if (current.startsWith(COLLECTION_POOL)) {
            // В коллекции крутятся и убранные из рулетки — как на ПК
            val collection = lib.collections.firstOrNull { it.id == current.removePrefix(COLLECTION_POOL) }
            return collection?.items?.mapNotNull(lib::find)?.distinct().orEmpty()
        }
        val kinds = current.split("+").mapNotNull { SOURCE_KINDS[it] }.toSet()
        return lib.games.filter { it.kind in kinds && !lib.isRemoved(it, removedHere) }
    }

    fun poolTitle(): String {
        val current = activePool
        if (current.startsWith(COLLECTION_POOL)) {
            val name = library?.collections?.firstOrNull { it.id == current.removePrefix(COLLECTION_POOL) }?.name
            return "Коллекция «${name ?: "?"}»"
        }
        return current.split("+").mapNotNull { SOURCE_TITLES[it] }.joinToString(" + ").ifEmpty { "Ничего не выбрано" }
    }

    fun choosePool(value: String?) {
        pool = value
        saveState()
        AppLog.info("Рулетка крутит: ${value ?: "как на ПК"}")
    }

    fun toggleSource(source: String) {
        val current = activePool.takeUnless { it.startsWith(COLLECTION_POOL) }?.split("+")?.toMutableSet() ?: mutableSetOf()
        if (!current.add(source)) current.remove(source)
        choosePool(SOURCE_TITLES.keys.filter { it in current }.joinToString("+"))
    }

    fun removeFromRoulette(game: Game) {
        removedHere = removedHere + game.ref
        if (picked == game) picked = null
        saveState()
        AppLog.info("Убрана из рулетки на телефоне: «${game.name}»")
    }

    fun clear() {
        libraryFile.delete()
        stateFile.delete()
        library = null
        picked = null
        removedHere = emptySet()
        pool = null
        AppLog.info("Данные с ПК удалены с телефона")
    }

    private fun readState(): Pair<Set<GameRef>, String?> = try {
        if (!stateFile.exists()) {
            emptySet<GameRef>() to null
        } else {
            val root = JSONObject(stateFile.readText())
            root.objects("removed").mapNotNull(ExportParser::parseRef).toSet() to root.str("pool").ifEmpty { null }
        }
    } catch (e: Exception) {
        AppLog.warning("state.json не прочитался: ${e.message}")
        emptySet<GameRef>() to null
    }

    private fun saveState() {
        val root = JSONObject()
        root.put("removed", JSONArray().apply { removedHere.forEach { put(it.toJson()) } })
        pool?.let { root.put("pool", it) }
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
        val SOURCE_KINDS = mapOf("steam" to Game.KIND_STEAM, "ports" to Game.KIND_PORT, "discs" to Game.KIND_DISC)
        val SOURCE_TITLES = linkedMapOf("steam" to "Steam", "ports" to "Порты", "discs" to "Дисковые")

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
