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
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/**
 * Где взять обложку игры — как в программе на ПК (download_cover, steam_assets):
 * 1) вертикальная по старому адресу Steam steam/apps/<appid>/library_600x900.jpg;
 * 2) нет её (у игр последних лет адреса с хэшем) — настоящий адрес из магазина (IStoreBrowseService/GetItems,
 *    без ключа);
 * 3) вертикальной нет нигде (сняли с продажи, старая игра) — горизонтальная шапка игры;
 * 4) нет и шапки — название на цветной карточке. Такие игры проверяются снова через неделю.
 * SteamGridDB, которым программа на ПК находит обложки снятых с продажи игр, на телефоне не используется: ему
 * нужен ключ, а ключи с ПК на телефон не переносятся. Что выяснили — в cover_urls.json, второй раз не спрашиваем.
 * Все адреса собираются здесь, только на серверах картинок Steam.
 */
object CoverUrls {
    enum class Kind { PORTRAIT, HEADER, NONE }

    data class Cover(val kind: Kind, val url: String?)

    private const val LEGACY = "https://cdn.cloudflare.steamstatic.com/steam/apps/%d/library_600x900.jpg"
    private const val LEGACY_HEADER = "https://cdn.cloudflare.steamstatic.com/steam/apps/%d/header.jpg"
    private const val ASSETS = "https://shared.akamai.steamstatic.com/store_item_assets/"
    private const val ITEMS_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
    private const val NONE_TTL = 7L * 24 * 3600 * 1000
    private const val BATCH = 100
    private const val PLACEHOLDER = "\${FILENAME}" // в адресе-шаблоне магазина — место для имени файла
    // Без фигурных скобок в выражении: регулярные выражения Android (ICU) не принимают «}» без экранирования
    private val SAFE_PATH = Regex("[A-Za-z0-9_./?=&-]+")
    private val SAFE_NAME = Regex("[A-Za-z0-9_.-]+")

    /** Шаблон адреса из ответа магазина годится: одно место для имени, остальное — обычный путь, без «..». */
    private fun safeFormat(format: String): Boolean {
        val parts = format.split(PLACEHOLDER)
        return parts.size == 2 && parts[0].isNotEmpty() && ".." !in format &&
            parts.all { it.isEmpty() || SAFE_PATH.matches(it) }
    }

    // Ступени: 0 — старый адрес (не хранится), 1 — вертикальная из магазина, 2 — шапка, 3 — ничего нет
    private data class Entry(val stage: Int, val url: String?, val time: Long)

    private val entries = mutableStateMapOf<Int, Entry>()
    private val pending = mutableSetOf<Int>() // старый адрес не сработал — спросить магазин
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var resolveJob: Job? = null
    private var saveJob: Job? = null
    private var file: File? = null

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
                val entry = Entry(o.optInt("s"), o.str("u").ifEmpty { null }, o.optLong("t"))
                if (entry.stage == 3 && now - entry.time > NONE_TTL) return@forEach // неделя прошла — проверим снова
                if (entry.stage in 1..3) entries[appid] = entry
            }
        } catch (e: Exception) {
            AppLog.warning("cover_urls.json не прочитался: ${e.message}")
        }
    }

    fun cover(game: Game): Cover {
        val appid = game.appid ?: return Cover(Kind.NONE, null)
        val entry = entries[appid] ?: return Cover(Kind.PORTRAIT, LEGACY.format(appid))
        return when (entry.stage) {
            1 -> Cover(Kind.PORTRAIT, entry.url)
            2 -> Cover(Kind.HEADER, entry.url)
            else -> Cover(Kind.NONE, null)
        }
    }

    /** 404 и прочие «нет такой картинки» — значит, искать дальше. Нет сети — не значит ничего. */
    fun isMissing(error: Throwable?): Boolean = error is HttpException && error.response.code in 400..499

    /** Картинка по адресу url не нашлась — следующая ступень. Вызывать в главном потоке. */
    fun failed(game: Game, url: String?) {
        val appid = game.appid ?: return
        if (url == null || cover(game).url != url) return // уже перешли дальше
        when (entries[appid]?.stage ?: 0) {
            0 -> {
                pending += appid
                if (resolveJob?.isActive != true) {
                    resolveJob = scope.launch {
                        delay(400) // собираем пачку — видимые на экране обложки ошибаются почти разом
                        resolvePending()
                    }
                }
            }
            1 -> set(appid, Entry(2, LEGACY_HEADER.format(appid), System.currentTimeMillis()))
            else -> set(appid, Entry(3, null, System.currentTimeMillis()))
        }
    }

    /** Спросить у магазина настоящие адреса для всех, у кого не сработал старый. В фоне. */
    suspend fun resolvePending() {
        val ids = withContext(Dispatchers.Main) { pending.toList().also { pending.clear() } }
        if (ids.isEmpty()) return
        val now = System.currentTimeMillis()
        val found = mutableMapOf<Int, Entry>()
        for (part in ids.chunked(BATCH)) {
            val items = try {
                storeItems(part)
            } catch (e: Exception) {
                AppLog.warning("Магазин Steam не ответил, где обложки ${part.size} игр: ${e.message}")
                continue // в следующий раз спросим снова
            }
            for (appid in part) {
                val item = items[appid]
                found[appid] = when {
                    item?.first != null -> Entry(1, item.first, now)
                    item?.second != null -> Entry(2, item.second, now)
                    else -> Entry(2, LEGACY_HEADER.format(appid), now) // магазин игру не знает — снята с продажи
                }
            }
        }
        withContext(Dispatchers.Main) { found.forEach { (appid, entry) -> set(appid, entry) } }
        if (found.isNotEmpty()) {
            AppLog.info("Обложки: магазин Steam подсказал адреса для ${found.size} игр, " +
                "вертикальных из них ${found.values.count { it.stage == 1 }}")
        }
    }

    /** {appid: (вертикальная обложка или null, шапка или null)} — только для игр, которые магазин знает. */
    private fun storeItems(appids: List<Int>): Map<Int, Pair<String?, String?>> {
        val query = JSONObject()
            .put("ids", JSONArray().apply { appids.forEach { put(JSONObject().put("appid", it)) } })
            .put("context", JSONObject().put("language", "russian").put("country_code", "US"))
            .put("data_request", JSONObject().put("include_assets", true))
        val connection = URL(ITEMS_URL + "?input_json=" + URLEncoder.encode(query.toString(), "UTF-8"))
            .openConnection() as HttpURLConnection
        connection.connectTimeout = 10_000
        connection.readTimeout = 15_000
        val text = try {
            if (connection.responseCode != 200) throw java.io.IOException("ответ ${connection.responseCode}")
            connection.inputStream.use { it.readBytes().toString(Charsets.UTF_8) }
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
            result[appid] = asset("library_capsule_2x", "library_capsule") to asset("header")
        }
        return result
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
            root.put(appid.toString(), JSONObject().put("s", e.stage).put("u", e.url ?: "").put("t", e.time))
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
