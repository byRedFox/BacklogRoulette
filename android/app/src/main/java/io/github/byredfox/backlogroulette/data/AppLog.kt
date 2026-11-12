package io.github.byredfox.backlogroulette.data

import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Журнал приложения: logs/app.log во внутренней папке приложения (другим приложениям недоступна). */
object AppLog {
    private const val MAX_BYTES = 512 * 1024
    private var file: File? = null
    private val format = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.ROOT)

    fun init(filesDir: File) {
        file = File(filesDir, "logs/app.log").also { it.parentFile?.mkdirs() }
    }

    fun info(message: String) = write("INFO   ", message)

    fun warning(message: String) = write("WARNING", message)

    @Synchronized
    private fun write(level: String, message: String) {
        val target = file ?: return
        try {
            if (target.length() > MAX_BYTES) { // разросся — оставляем вторую половину
                val text = target.readText()
                target.writeText(text.substring(text.length / 2).substringAfter('\n'))
            }
            target.appendText("${format.format(Date())}  $level  $message\n")
        } catch (_: Exception) {
            // нет места или прав — без журнала
        }
    }

    @Synchronized
    fun read(): String = try {
        file?.takeIf { it.exists() }?.readText().orEmpty()
    } catch (_: Exception) {
        ""
    }

    @Synchronized
    fun clear() {
        file?.writeText("")
    }
}
