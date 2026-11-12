package io.github.byredfox.backlogroulette.data

import org.json.JSONArray
import org.json.JSONObject

/** Ссылка на игру — как в коллекциях и списке рулетки на ПК: Steam — по номеру или названию, порт и дисковая — по id. */
data class GameRef(val kind: String, val appid: Int? = null, val name: String? = null, val id: String? = null) {
    fun toJson(): JSONObject = JSONObject().apply {
        put("kind", kind)
        appid?.let { put("appid", it) }
        name?.let { put("name", it) }
        id?.let { put("id", it) }
    }
}

/** Игра Steam. Порты и дисковые игры с ПК на телефоне пока не показываются. */
data class Game(val kind: String, val name: String, val appid: Int? = null, val id: String? = null) {
    val ref: GameRef
        get() = when {
            kind != KIND_STEAM -> GameRef(kind, id = id)
            appid != null -> GameRef(KIND_STEAM, appid = appid)
            else -> GameRef(KIND_STEAM, name = name)
        }

    /** Ссылки, по которым игра может быть убрана: игру Steam убирали и по названию, пока у неё не было номера. */
    val refs: List<GameRef>
        get() = if (kind == KIND_STEAM && appid != null) listOf(ref, GameRef(KIND_STEAM, name = name)) else listOf(ref)

    /** Вертикальная обложка Steam — только по номеру игры, адрес собирается здесь. */
    val coverUrl: String?
        get() = appid?.let { "https://cdn.cloudflare.steamstatic.com/steam/apps/$it/library_600x900.jpg" }

    val storeUrl: String?
        get() = appid?.let { "https://store.steampowered.com/app/$it/" }

    companion object {
        const val KIND_STEAM = "steam"
        const val KIND_PORT = "port"
        const val KIND_DISC = "disc"
    }
}

data class PortInfo(
    val id: String,
    val name: String,
    val kind: String,
    val repo: String,
    val installedTag: String,
    val latestTag: String,
)

data class GameCollection(val id: String, val name: String, val items: List<GameRef>)

data class GameMeta(val genres: List<String>, val year: Int?, val percent: Int?)

/** Фильтры рулетки — как на ПК: жанры (любой из), годы выхода «с» и «по», отзывы — от скольких % положительных.
 *  Нужных фильтру сведений об игре нет — она не подходит (не крутим вслепую). */
data class RouletteFilters(
    val genres: Set<String> = emptySet(),
    val yearFrom: Int? = null,
    val yearTo: Int? = null,
    val reviews: Int = 0,
) {
    val active: Boolean get() = genres.isNotEmpty() || yearFrom != null || yearTo != null || reviews > 0

    val count: Int get() = (if (genres.isNotEmpty()) 1 else 0) + (if (yearFrom != null || yearTo != null) 1 else 0) +
        (if (reviews > 0) 1 else 0)

    fun matches(meta: GameMeta?): Boolean {
        if (!active) return true
        val entry = meta ?: return false
        if (genres.isNotEmpty() && entry.genres.none { it in genres }) return false
        if (yearFrom != null || yearTo != null) {
            val year = entry.year ?: return false
            if (yearFrom != null && year < yearFrom) return false
            if (yearTo != null && year > yearTo) return false
        }
        if (reviews > 0 && (entry.percent ?: return false) < reviews) return false
        return true
    }

    fun toJson(): JSONObject = JSONObject().apply {
        put("genres", JSONArray(genres.toList()))
        yearFrom?.let { put("year_from", it) }
        yearTo?.let { put("year_to", it) }
        put("reviews", reviews)
    }

    companion object {
        fun fromJson(o: JSONObject?): RouletteFilters {
            if (o == null) return RouletteFilters()
            val genres = o.optJSONArray("genres")?.let { a -> List(a.length()) { a.optString(it) } }.orEmpty()
            return RouletteFilters(genres.filter { it.isNotEmpty() }.toSet(), o.positiveInt("year_from"),
                o.positiveInt("year_to"), o.positiveInt("reviews") ?: 0)
        }
    }
}

data class Library(
    val games: List<Game>,
    val ports: List<PortInfo>, // порты и дисковые игры — на будущее, пока не показываются
    val collections: List<GameCollection>,
    val removed: Set<GameRef>,
    val filters: RouletteFilters,
    val meta: Map<Int, GameMeta>,
    val exported: Long,
    val appVersion: String,
) {
    fun isRemoved(game: Game, extra: Set<GameRef> = emptySet()) = game.refs.any { it in removed || it in extra }

    fun meta(game: Game): GameMeta? = game.appid?.let { meta[it] }

    /** Игра Steam по ссылке из коллекции; её нет в списке (или это порт) — null. */
    fun find(ref: GameRef): Game? {
        if (ref.kind != Game.KIND_STEAM) return null
        return games.firstOrNull { game ->
            if (ref.appid != null) game.appid == ref.appid else game.name.equals(ref.name, ignoreCase = true)
        }
    }
}

class ImportError(message: String) : Exception(message)

/** Файл «Экспорт для телефона» программы на ПК → Library. Чего-то не хватает или испорчено — пропускается. */
object ExportParser {
    const val FORMAT = "backlog-roulette-export"
    const val VERSION = 1
    private const val BOM = 0xFEFF // метка, которую Блокнот Windows ставит в начало файла

    fun parse(text: String): Library {
        val root = try {
            JSONObject(text.trimStart { it.code == BOM })
        } catch (_: Exception) {
            throw ImportError("это не файл экспорта Backlog Roulette (не JSON)")
        }
        if (root.str("format") != FORMAT) throw ImportError("это не файл «Экспорт для телефона» из Backlog Roulette")
        if (root.optInt("version") > VERSION) {
            throw ImportError("файл сделан более новой программой — обновите приложение на телефоне")
        }
        val games = root.objects("games").mapNotNull { g ->
            g.str("name").takeIf { it.isNotBlank() }?.let { Game(Game.KIND_STEAM, it, appid = g.positiveInt("appid")) }
        }
        val ports = root.objects("ports").mapNotNull { p ->
            val id = p.str("id")
            val name = p.str("name")
            if (id.isEmpty() || name.isBlank()) null
            else PortInfo(id, name, if (p.str("kind") == Game.KIND_DISC) Game.KIND_DISC else Game.KIND_PORT,
                p.str("repo"), p.str("installed_tag"), p.optJSONObject("latest")?.str("tag").orEmpty())
        }
        val collections = root.objects("collections").mapNotNull { c ->
            val id = c.str("id")
            val name = c.str("name")
            if (id.isEmpty() || name.isBlank()) null
            else GameCollection(id, name, c.objects("items").mapNotNull(::parseRef))
        }
        val roulette = root.optJSONObject("roulette") ?: JSONObject()
        val meta = mutableMapOf<Int, GameMeta>()
        root.optJSONObject("meta")?.let { all ->
            all.keys().forEach { key ->
                val appid = key.toIntOrNull() ?: return@forEach
                val entry = all.optJSONObject(key) ?: return@forEach
                val genres = entry.optJSONArray("genres")?.let { a -> List(a.length()) { a.optString(it) } }.orEmpty()
                meta[appid] = GameMeta(genres.filter { it.isNotEmpty() }, entry.positiveInt("year"),
                    if (entry.isNull("percent")) null else entry.optInt("percent"))
            }
        }
        return Library(
            games = games,
            ports = ports,
            collections = collections,
            removed = roulette.objects("removed").mapNotNull(::parseRef).toSet(),
            filters = RouletteFilters.fromJson(roulette.optJSONObject("filters")),
            meta = meta,
            exported = root.optLong("exported"),
            appVersion = root.str("app"),
        )
    }

    fun parseRef(o: JSONObject): GameRef? = when (val kind = o.str("kind")) {
        Game.KIND_STEAM -> o.positiveInt("appid")?.let { GameRef(kind, appid = it) }
            ?: o.str("name").takeIf { it.isNotEmpty() }?.let { GameRef(kind, name = it) }
        Game.KIND_PORT, Game.KIND_DISC -> o.str("id").takeIf { it.isNotEmpty() }?.let { GameRef(kind, id = it) }
        else -> null
    }
}

// JSON null у org.json превращается в строку "null" — поэтому свои помощники
internal fun JSONObject.str(key: String): String = if (isNull(key)) "" else optString(key)

internal fun JSONObject.positiveInt(key: String): Int? = if (isNull(key)) null else optInt(key).takeIf { it > 0 }

internal fun JSONObject.objects(key: String): List<JSONObject> {
    val array: JSONArray = optJSONArray(key) ?: return emptyList()
    return List(array.length()) { array.optJSONObject(it) }.filterNotNull()
}
