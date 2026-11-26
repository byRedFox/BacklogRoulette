package io.github.byredfox.backlogroulette.data

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.content.FileProvider
import androidx.core.content.pm.PackageInfoCompat
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

/**
 * Порт в каталоге «Нативные ИИ порты». Каталог ведём сами — программа не ищет порты где попало: только эти
 * репозитории GitHub и только APK, у которого совпали контрольная сумма GitHub и имя пакета.
 */
data class PortEntry(
    val id: String,
    val title: String,
    val platform: String,
    val repo: String, // владелец/репозиторий на GitHub
    val packageName: String, // какое приложение должно оказаться в APK — другое не ставим
    val apkPattern: Regex, // какой файл релиза — APK для Android (в релизе бывают и сборки для iOS)
    val about: String,
    val needs: String, // что нужно от пользователя, "" — ничего
    val signer: String = "", // SHA-256 сертификата автора: пусто — пока не сверяем, только пишем в журнал
)

object PortCatalog {
    val entries = listOf(
        PortEntry(
            id = "soh",
            title = "Ship of Harkinian",
            platform = "Nintendo 64 · The Legend of Zelda: Ocarina of Time",
            repo = "linkzenic/Shipwright-Ports",
            packageName = "com.linkzenic.soh",
            apkPattern = Regex("""^soh_release_.*\.apk$""", RegexOption.IGNORE_CASE),
            about = "Нативный порт «Ocarina of Time» на основе разобранного кода игры: высокое разрешение и частота " +
                "кадров, широкий экран, сенсорное управление с редактором раскладки, поддержка геймпадов. Сборка " +
                "для Android от linkzenic на основе Ship of Harkinian.",
            needs = "Свой образ «Ocarina of Time» для N64 (файл .z64, .n64 или .v64). Порт сам попросит выбрать его " +
                "при первом запуске и соберёт из него ресурсы. Самой игры в порте нет.",
        ),
    )
}

data class PortAsset(val name: String, val size: Long, val sha256: String, val url: String)

data class PortRelease(
    val tag: String,
    val title: String,
    val notes: String,
    val published: String,
    val url: String,
    val asset: PortAsset?,
)

data class InstalledPort(val versionName: String, val versionCode: Long)

data class PortArchive(val versionName: String, val versionCode: Long, val signer: String)

class PortException(message: String) : Exception(message)

/** Релизы портов на GitHub, скачивание со сверкой и установка — только через системное окно Android. */
object Ports {
    private const val API = "https://api.github.com/repos/"
    private const val JSON_MAX = 2 * 1024 * 1024
    private const val APK_MAX = 400L * 1024 * 1024
    private const val NOTES_MAX = 4000
    private const val PREFS = "ports"

    /** Последний релиз порта. В фоновом потоке. */
    fun latest(entry: PortEntry): PortRelease {
        val json = JSONObject(get(API + entry.repo + "/releases/latest"))
        val assets = json.optJSONArray("assets") ?: JSONArray()
        var asset: PortAsset? = null
        for (i in 0 until assets.length()) {
            val item = assets.getJSONObject(i)
            val name = item.optString("name")
            val url = item.optString("browser_download_url")
            // Файл — только из релизов этого же репозитория
            if (!entry.apkPattern.matches(name) || !url.startsWith("https://github.com/${entry.repo}/releases/download/")) continue
            val digest = item.optString("digest")
            val sha = if (digest.startsWith("sha256:")) digest.removePrefix("sha256:").lowercase() else ""
            asset = PortAsset(name, item.optLong("size"), if (sha.length == 64) sha else "", url)
            break
        }
        return PortRelease(
            tag = json.optString("tag_name"),
            title = json.optString("name"),
            notes = json.optString("body").take(NOTES_MAX),
            published = json.optString("published_at").take(10),
            url = json.optString("html_url"),
            asset = asset,
        )
    }

    private fun get(url: String): String {
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.connectTimeout = 15_000
        conn.readTimeout = 20_000
        conn.setRequestProperty("Accept", "application/vnd.github+json")
        conn.setRequestProperty("User-Agent", "BacklogRoulette-Android")
        try {
            val code = conn.responseCode
            if (code == 403 || code == 429) throw PortException("GitHub временно ограничил запросы — попробуйте через час")
            if (code == 404) throw PortException("У порта на GitHub нет релизов")
            if (code != 200) throw PortException("GitHub ответил: $code")
            val out = ByteArrayOutputStream()
            conn.inputStream.use { input ->
                val buffer = ByteArray(16 * 1024)
                while (true) {
                    val n = input.read(buffer)
                    if (n < 0) break
                    out.write(buffer, 0, n)
                    if (out.size() > JSON_MAX) throw PortException("Ответ GitHub слишком большой")
                }
            }
            return out.toString(Charsets.UTF_8.name())
        } finally {
            conn.disconnect()
        }
    }

    fun apkFile(context: Context, entry: PortEntry) = File(context.cacheDir, "ports/${entry.id}.apk")

    /** Уже скачанный и сверенный APK этого релиза — заново не качаем. */
    fun cached(context: Context, entry: PortEntry, asset: PortAsset): File? {
        val file = apkFile(context, entry)
        return file.takeIf { asset.sha256.isNotEmpty() && it.length() == asset.size && sha256(it) == asset.sha256 }
    }

    /**
     * Скачать APK и сверить с контрольной суммой, которую посчитал GitHub. Нет суммы, не тот размер или не та сумма —
     * файла не остаётся. В фоновом потоке; progress — доля от 0 до 1, cancelled — пора бросить.
     */
    fun download(context: Context, entry: PortEntry, asset: PortAsset, progress: (Float) -> Unit, cancelled: () -> Boolean): File {
        if (asset.sha256.isEmpty()) throw PortException("У файла в релизе нет контрольной суммы GitHub — без проверки не ставлю")
        if (asset.size <= 0 || asset.size > APK_MAX) throw PortException("Странный размер файла: ${asset.size} байт")
        val target = apkFile(context, entry)
        target.parentFile?.mkdirs()
        val part = File(target.path + ".part")
        val digest = MessageDigest.getInstance("SHA-256")
        val conn = URL(asset.url).openConnection() as HttpURLConnection
        conn.connectTimeout = 15_000
        conn.readTimeout = 30_000
        conn.instanceFollowRedirects = true // GitHub отдаёт файл со своего хранилища — тоже по https
        conn.setRequestProperty("User-Agent", "BacklogRoulette-Android")
        try {
            if (conn.responseCode != 200) throw PortException("Скачивание: сервер ответил ${conn.responseCode}")
            if (conn.url.protocol != "https") throw PortException("Скачивание ушло не по https — бросаю")
            var done = 0L
            conn.inputStream.use { input ->
                part.outputStream().use { output ->
                    val buffer = ByteArray(64 * 1024)
                    while (true) {
                        if (cancelled()) throw PortException("Скачивание отменено")
                        val n = input.read(buffer)
                        if (n < 0) break
                        done += n
                        if (done > asset.size) throw PortException("Файл больше, чем заявлено в релизе")
                        digest.update(buffer, 0, n)
                        output.write(buffer, 0, n)
                        progress(done.toFloat() / asset.size)
                    }
                }
            }
            if (done != asset.size) throw PortException("Файл скачался не целиком")
            if (hex(digest.digest()) != asset.sha256) {
                throw PortException("Контрольная сумма не совпала с GitHub — файл повреждён или подменён, не ставлю")
            }
            target.delete()
            if (!part.renameTo(target)) throw PortException("Не удалось сохранить файл")
            return target
        } catch (e: Exception) {
            part.delete()
            throw e
        } finally {
            conn.disconnect()
        }
    }

    /** Что внутри APK: то ли приложение, версия и чем подписано. Не то приложение или чужая подпись — исключение. */
    fun inspect(context: Context, entry: PortEntry, apk: File): PortArchive {
        val flags = if (Build.VERSION.SDK_INT >= 28) PackageManager.GET_SIGNING_CERTIFICATES
        else @Suppress("DEPRECATION") PackageManager.GET_SIGNATURES
        val info = context.packageManager.getPackageArchiveInfo(apk.path, flags)
            ?: throw PortException("Файл не похож на приложение Android")
        if (info.packageName != entry.packageName) {
            throw PortException("В файле другое приложение (${info.packageName}), а ждали ${entry.packageName} — не ставлю")
        }
        val signers = if (Build.VERSION.SDK_INT >= 28) info.signingInfo?.apkContentsSigners
        else @Suppress("DEPRECATION") info.signatures
        val signer = signers?.firstOrNull()?.let { hex(MessageDigest.getInstance("SHA-256").digest(it.toByteArray())) }.orEmpty()
        if (entry.signer.isNotEmpty() && signer != entry.signer) {
            throw PortException("APK подписан не тем ключом, что раньше у автора порта — не ставлю")
        }
        return PortArchive(info.versionName.orEmpty(), PackageInfoCompat.getLongVersionCode(info), signer)
    }

    fun installed(context: Context, entry: PortEntry): InstalledPort? = try {
        val info = context.packageManager.getPackageInfo(entry.packageName, 0)
        InstalledPort(info.versionName.orEmpty(), PackageInfoCompat.getLongVersionCode(info))
    } catch (_: PackageManager.NameNotFoundException) {
        null
    }

    /**
     * Открыть системное окно установки — ставит Android, и только после «Установить» самого пользователя.
     * false — у программы ещё нет разрешения «Установка неизвестных приложений»: открыта его настройка.
     */
    fun install(context: Context, apk: File): Boolean {
        if (!context.packageManager.canRequestPackageInstalls()) {
            context.startActivity(
                Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${context.packageName}"))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            )
            return false
        }
        val uri = FileProvider.getUriForFile(context, "${context.packageName}.ports", apk)
        context.startActivity(
            Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/vnd.android.package-archive")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK),
        )
        return true
    }

    /** Запустить установленный порт. false — Android не дал его найти. */
    fun launch(context: Context, entry: PortEntry): Boolean {
        val intent = context.packageManager.getLaunchIntentForPackage(entry.packageName) ?: return false
        context.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        return true
    }

    /** Системное окно удаления — удаляет Android, после подтверждения пользователя. */
    fun uninstall(context: Context, entry: PortEntry) {
        @Suppress("DEPRECATION")
        context.startActivity(
            Intent(Intent.ACTION_UNINSTALL_PACKAGE, Uri.parse("package:${entry.packageName}"))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
        )
    }

    /** Какой релиз мы скачивали для установки — по номеру сборки потом узнаем, тот ли стоит. */
    fun remember(context: Context, entry: PortEntry, tag: String, versionCode: Long) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString("${entry.id}.tag", tag).putLong("${entry.id}.code", versionCode).apply()
    }

    /** Релиз, из которого стоит установленный порт, или null — порт ставили не через программу (или другую сборку). */
    fun installedTag(context: Context, entry: PortEntry, installed: InstalledPort): String? {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        return prefs.getString("${entry.id}.tag", null)?.takeIf { prefs.getLong("${entry.id}.code", -1) == installed.versionCode }
    }

    fun sha256(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val n = input.read(buffer)
                if (n < 0) break
                digest.update(buffer, 0, n)
            }
        }
        return hex(digest.digest())
    }

    private fun hex(bytes: ByteArray) = bytes.joinToString("") { "%02x".format(it) }
}
