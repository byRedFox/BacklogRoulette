package io.github.byredfox.backlogroulette.data

import androidx.compose.runtime.mutableStateMapOf
import coil3.network.HttpException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URI
import java.net.URL
import java.net.URLEncoder

/**
 * Где взять обложку игры — как в программе на ПК (download_cover, steam_assets):
 * 1) вертикальная по старому адресу Steam steam/apps/<appid>/library_600x900.jpg;
 * 2) нет её (у игр последних лет адреса с хэшем) — настоящий адрес из магазина (IStoreBrowseService/GetItems,
 *    без ключа, по 100 игр за запрос);
 * 3) вертикальной в Steam нет (сняли с продажи, старая игра) и пользователь добавил свой ключ SteamGridDB —
 *    обложка оттуда, по appid игры;
 * 4) иначе — горизонтальная шапка игры; нет и шапки — название на цветной карточке. Такие игры проверяются снова
 *    через неделю.
 * Что выяснили — в cover_urls.json, второй раз не спрашиваем. Адреса картинок — только серверы картинок Steam
 * и SteamGridDB: адрес из ответа другого сервера не берётся.
 */
object CoverUrls {
    enum class Kind { PORTRAIT, HEADER, NONE }

    data class Cover(val kind: Kind, val url: String?)

    private const val LEGACY = "https://cdn.cloudflare.steamstatic.com/steam/apps/%d/library_600x900.jpg"
    private const val LEGACY_HEADER = "https://cdn.cloudflare.steamstatic.com/steam/apps/%d/header.jpg"
    private const val ASSETS = "https://shared.akamai.steamstatic.com/store_item_assets/"
    private const val ITEMS_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
    // Только вертикальные размеры, только картинки (не анимация), без «взрослых» и шуточных
    private const val GRID_URL = "https://www.steamgriddb.com/api/v2/grids/steam/%d" +
        "?dimensions=600x900,342x482,660x930&types=static&nsfw=false&humor=false"
    private const val CHECK_APPID = 220 // Half-Life 2 — проверка ключа: у неё в SteamGridDB обложки точно есть
    private const val RECHECK = 7L * 24 * 3600 * 1000
    private const val BATCH = 100
    private const val GRID_PAUSE_MS = 250L // SteamGridDB ограничивает частоту запросов
    private const val MAX_ANSWER = 2 * 1024 * 1024
    private const val PLACEHOLDER = "\${FILENAME}" // в адресе-шаблоне магазина — место для имени файла
    // Без фигурных скобок в выражении: регулярные выражения Android (ICU) не принимают «}» без экранирования
    private val SAFE_PATH = Regex("[A-Za-z0-9_./?=&-]+")
    private val SAFE_NAME = Regex("[A-Za-z0-9_.-]+")
    // Регион магазина — страна из настроек телефона (две латинские буквы), иначе США
    private val homeCountry: String =
        java.util.Locale.getDefault().country.uppercase().takeIf { it.matches(Regex("[A-Z]{2}")) } ?: "US"

    // Ступени: 0 — старый адрес (не хранится), 1 — вертикальная из магазина, 2 — шапка, 3 — ничего нет,
    // 4 — вертикальная из SteamGridDB. grid — когда спрашивали SteamGridDB (0 — ещё нет).
    private const val STORE = 1
    private const val HEADER = 2
    private const val NONE = 3
    private const val GRID = 4

    private data class Entry(val stage: Int, val url: String?, val time: Long, val grid: Long = 0)

    private class KeyRejected : IOException("ключ не подошёл")

    private val entries = mutableStateMapOf<Int, Entry>()
    private val pending = mutableSetOf<Int>() // старый адрес не сработал — спросить магазин
    private val gridPending = mutableSetOf<Int>() // в Steam вертикальной нет — спросить SteamGridDB
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var resolveJob: Job? = null
    private var gridJob: Job? = null
    private var saveJob: Job? = null
    private var file: File? = null

    /** Шаблон адреса из ответа магазина годится: одно место для имени, остальное — обычный путь, без «..». */
    private fun safeFormat(format: String): Boolean {
        val parts = format.split(PLACEHOLDER)
        return parts.size == 2 && parts[0].isNotEmpty() && ".." !in format &&
            parts.all { it.isEmpty() || SAFE_PATH.matches(it) }
    }

    /** Адрес картинки из ответа SteamGridDB годится: https, сервер SteamGridDB, без логина и чужого порта. */
    private fun safeGridUrl(url: String): Boolean = try {
        val uri = URI(url)
        val host = uri.host?.lowercase() ?: ""
        uri.scheme == "https" && uri.userInfo == null && uri.port in listOf(-1, 443) &&
            (host == "steamgriddb.com" || host.endsWith(".steamgriddb.com"))
    } catch (e: Exception) {
        false
    }

    fun init(filesDir: File) {
        if (file != null) return
        val target = File(filesDir, "cover_urls.json")
        file = target
        try {
            if (!target.exists()) return
            val root = JSONObject(target.readText())
            val now = System.currentTimeMillis()
            root.keys().forEach { key ->
                val appid = key.toIntOrNull() ?: return@forEach
                val o = root.optJSONObject(key) ?: return@forEach
                var entry = Entry(o.optInt("s"), o.str("u").ifEmpty { null }, o.optLong("t"), o.optLong("g"))
                if (entry.stage == NONE && now - entry.time > RECHECK) return@forEach // неделя прошла — проверим снова
                if (entry.stage != GRID && entry.grid != 0L && now - entry.grid > RECHECK) entry = entry.copy(grid = 0)
                if (entry.stage in STORE..GRID) entries[appid] = entry
            }
        } catch (e: Exception) {
            AppLog.warning("cover_urls.json не прочитался: ${e.message}")
        }
    }

    fun cover(game: Game): Cover {
        val appid = game.appid ?: return Cover(Kind.NONE, null)
        val entry = entries[appid] ?: return Cover(Kind.PORTRAIT, LEGACY.format(appid))
        return when (entry.stage) {
            STORE, GRID -> Cover(Kind.PORTRAIT, entry.url)
            HEADER -> Cover(Kind.HEADER, entry.url)
            else -> Cover(Kind.NONE, null)
        }
    }

    /** Вертикальной обложки в Steam нет, а SteamGridDB об этой игре ещё не спрашивали (и ключ есть). */
    fun gridMayHelp(game: Game): Boolean = game.appid?.let(::needsGrid) == true

    private fun needsGrid(appid: Int): Boolean {
        val entry = entries[appid] ?: return false
        return entry.stage in HEADER..NONE && entry.grid == 0L && SteamGridKey.get() != null
    }

    /** 404 и прочие «нет такой картинки» — значит, искать дальше. Нет сети — не значит ничего. */
    fun isMissing(error: Throwable?): Boolean = error is HttpException && error.response.code in 400..499

    /** Картинка по адресу url не нашлась — следующая ступень. Вызывать в главном потоке. */
    fun failed(game: Game, url: String?) {
        val appid = game.appid ?: return
        if (url == null || cover(game).url != url) return // уже перешли дальше
        val now = System.currentTimeMillis()
        val entry = entries[appid]
        when (entry?.stage ?: 0) {
            0 -> {
                pending += appid
                if (resolveJob?.isActive != true) {
                    resolveJob = scope.launch {
                        delay(400) // собираем пачку — видимые на экране обложки ошибаются почти разом
                        resolvePending()
                    }
                }
            }
            STORE -> set(appid, Entry(HEADER, LEGACY_HEADER.format(appid), now))
            GRID -> set(appid, Entry(HEADER, LEGACY_HEADER.format(appid), now, grid = now)) // эту не спрашиваем неделю
            else -> set(appid, Entry(NONE, null, now, entry?.grid ?: 0))
        }
        queueGrid(appid)
    }

    /** Узнать всё, что можно, об обложках этих игр — сразу, а не по одной: магазин Steam пачками,
     *  потом SteamGridDB для тех, у кого в Steam вертикальной нет. Для подкачки в фоне. */
    suspend fun lookup(appids: Collection<Int>) {
        withContext(Dispatchers.Main) { appids.filterTo(pending) { entries[it] == null } }
        resolvePending()
        withContext(Dispatchers.Main) { appids.filterTo(gridPending) { needsGrid(it) } }
        resolveGrid()
    }

    /** Ключ SteamGridDB добавили — спросить его обо всех играх без вертикальной обложки. Главный поток. */
    fun keyAdded() {
        entries.keys.toList().forEach(::queueGrid)
    }

    /** Спросить у магазина настоящие адреса для всех, у кого не сработал старый. В фоне. */
    suspend fun resolvePending() {
        val ids = withContext(Dispatchers.Main) { pending.toList().also { pending.clear() } }
        if (ids.isEmpty()) return
        val now = System.currentTimeMillis()
        val found = mutableMapOf<Int, Entry>()
        for (part in ids.chunked(BATCH)) {
            val items = try {
                // Сначала магазин своего региона, а чего там нет — магазин США: игра, которой нет в одном
                // регионе, не снята с продажи, и у неё есть обложка
                val local = storeItems(part, homeCountry)
                val rest = part.filter { it !in local }
                if (rest.isNotEmpty() && homeCountry != "US") local + storeItems(rest, "US") else local
            } catch (e: Exception) {
                AppLog.warning("Магазин Steam не ответил, где обложки ${part.size} игр: ${e.message}")
                continue // в следующий раз спросим снова
            }
            for (appid in part) {
                val item = items[appid]
                found[appid] = when {
                    item?.first != null -> Entry(STORE, item.first, now)
                    item?.second != null -> Entry(HEADER, item.second, now)
                    else -> Entry(HEADER, LEGACY_HEADER.format(appid), now) // магазин игру не знает — снята с продажи
                }
            }
        }
        withContext(Dispatchers.Main) {
            found.forEach { (appid, entry) ->
                set(appid, entry)
                queueGrid(appid)
            }
        }
        if (found.isNotEmpty()) {
            AppLog.info("Обложки: магазин Steam подсказал адреса для ${found.size} игр, " +
                "вертикальных из них ${found.values.count { it.stage == STORE }}")
        }
    }

    /** В очередь к SteamGridDB, если есть ключ и эту игру там ещё не искали. Главный поток. */
    private fun queueGrid(appid: Int) {
        if (!needsGrid(appid)) return
        gridPending += appid
        if (gridJob?.isActive != true) {
            gridJob = scope.launch {
                delay(400)
                resolveGrid()
            }
        }
    }

    /** Спросить SteamGridDB об играх из очереди — по одной, с паузой. В фоне. */
    suspend fun resolveGrid() {
        val key = SteamGridKey.get() ?: return
        val ids = withContext(Dispatchers.Main) { gridPending.toList().also { gridPending.clear() } }
        if (ids.isEmpty()) return
        var found = 0
        var asked = 0
        for ((index, appid) in ids.withIndex()) {
            if (index > 0) delay(GRID_PAUSE_MS)
            val url = try {
                gridCover(appid, key)
            } catch (e: KeyRejected) {
                AppLog.warning("SteamGridDB: ключ не подошёл — проверьте его в настройках")
                return
            } catch (e: Exception) {
                AppLog.warning("SteamGridDB не ответил: ${e.message}")
                continue // в следующий раз спросим снова
            }
            asked++
            if (url != null) found++
            val now = System.currentTimeMillis()
            withContext(Dispatchers.Main) {
                val entry = entries[appid] ?: return@withContext
                set(appid, if (url != null) Entry(GRID, url, now, grid = now) else entry.copy(grid = now))
            }
        }
        if (asked > 0) AppLog.info("Обложки: SteamGridDB нашёл $found из $asked игр без вертикальной обложки в Steam")
    }

    /** Ключ работает? true — да, false — SteamGridDB его не принял, null — нет связи. */
    suspend fun checkKey(key: String): Boolean? = withContext(Dispatchers.IO) {
        try {
            gridCover(CHECK_APPID, key)
            true
        } catch (e: KeyRejected) {
            false
        } catch (e: Exception) {
            AppLog.warning("SteamGridDB не ответил при проверке ключа: ${e.message}")
            null
        }
    }

    /** Адрес лучшей вертикальной обложки игры в SteamGridDB или null, если там её нет. */
    private fun gridCover(appid: Int, key: String): String? {
        require(SteamGridKey.VALID.matches(key))
        val connection = URL(GRID_URL.format(appid)).openConnection() as HttpURLConnection
        connection.connectTimeout = 10_000
        connection.readTimeout = 15_000
        connection.instanceFollowRedirects = false // ключ — только этому серверу
        connection.setRequestProperty("Authorization", "Bearer $key")
        connection.setRequestProperty("User-Agent", "BacklogRoulette-Android")
        val text = try {
            when (connection.responseCode) {
                200 -> connection.inputStream.use { readLimited(it) }
                401, 403 -> throw KeyRejected()
                404 -> return null // такой игры в SteamGridDB нет
                else -> throw IOException("ответ ${connection.responseCode}")
            }
        } finally {
            connection.disconnect()
        }
        val root = JSONObject(text)
        if (!root.optBoolean("success")) return null
        // Ответ уже отсортирован по оценкам сообщества — берём первую подходящую
        return root.objects("data").firstNotNullOfOrNull { item ->
            item.str("url").takeIf { safeGridUrl(it) && !item.optBoolean("nsfw") && !item.optBoolean("humor") }
        }
    }

    /** {appid: (вертикальная обложка или null, шапка или null)} — только для игр, которые магазин знает. */
    private fun storeItems(appids: List<Int>, country: String): Map<Int, Pair<String?, String?>> {
        val query = JSONObject()
            .put("ids", JSONArray().apply { appids.forEach { put(JSONObject().put("appid", it)) } })
            .put("context", JSONObject().put("language", "russian").put("country_code", country))
            .put("data_request", JSONObject().put("include_assets", true))
        val connection = URL(ITEMS_URL + "?input_json=" + URLEncoder.encode(query.toString(), "UTF-8"))
            .openConnection() as HttpURLConnection
        connection.connectTimeout = 10_000
        connection.readTimeout = 15_000
        val text = try {
            if (connection.responseCode != 200) throw IOException("ответ ${connection.responseCode}")
            connection.inputStream.use { readLimited(it) }
        } finally {
            connection.disconnect()
        }
        val result = mutableMapOf<Int, Pair<String?, String?>>()
        JSONObject(text).optJSONObject("response")?.objects("store_items")?.forEach { item ->
            val appid = item.positiveInt("appid") ?: item.positiveInt("id") ?: return@forEach
            if (item.optInt("success") != 1) return@forEach
            val assets = item.optJSONObject("assets") ?: JSONObject()
            val format = assets.str("asset_url_format").takeIf(::safeFormat)
            fun asset(vararg names: String): String? {
                format ?: return null
                val name = names.map { assets.str(it) }.firstOrNull { SAFE_NAME.matches(it) } ?: return null
                return ASSETS + format.replace(PLACEHOLDER, name)
            }
            // Обычная 600×900 — для экрана телефона её хватает, а двойная весит в несколько раз больше
            result[appid] = asset("library_capsule", "library_capsule_2x") to asset("header")
        }
        return result
    }

    private fun readLimited(input: java.io.InputStream): String {
        val out = java.io.ByteArrayOutputStream()
        val buffer = ByteArray(16 * 1024)
        while (true) {
            val read = input.read(buffer)
            if (read < 0) break
            out.write(buffer, 0, read)
            if (out.size() > MAX_ANSWER) throw IOException("слишком длинный ответ")
        }
        return out.toByteArray().toString(Charsets.UTF_8)
    }

    private fun set(appid: Int, entry: Entry) {
        entries[appid] = entry
        if (saveJob?.isActive == true) return
        val snapshot = { entries.toMap() }
        saveJob = scope.launch {
            delay(2000)
            val data = withContext(Dispatchers.Main) { snapshot() }
            save(data)
        }
    }

    private fun save(data: Map<Int, Entry>) {
        val target = file ?: return
        val root = JSONObject()
        data.forEach { (appid, e) ->
            root.put(appid.toString(), JSONObject().put("s", e.stage).put("u", e.url ?: "").put("t", e.time).put("g", e.grid))
        }
        try {
            val tmp = File(target.parentFile, target.name + ".tmp")
            tmp.writeText(root.toString())
            if (!tmp.renameTo(target)) {
                target.delete()
                tmp.renameTo(target)
            }
        } catch (e: Exception) {
            AppLog.warning("cover_urls.json не сохранился: ${e.message}")
        }
    }
}
