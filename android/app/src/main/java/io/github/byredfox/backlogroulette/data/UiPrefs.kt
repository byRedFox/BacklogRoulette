package io.github.byredfox.backlogroulette.data

import android.content.Context
import android.content.SharedPreferences
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue

/** Оформление: тема. Отдельно от данных с ПК — «Удалить с телефона» её не сбрасывает. */
object UiPrefs {
    enum class Theme(val title: String) { SYSTEM("Системная"), LIGHT("Светлая"), DARK("Тёмная") }

    private var prefs: SharedPreferences? = null
    var theme by mutableStateOf(Theme.SYSTEM)
        private set

    fun init(context: Context) {
        if (prefs != null) return
        val saved = context.getSharedPreferences("ui", Context.MODE_PRIVATE)
        prefs = saved
        theme = Theme.entries.firstOrNull { it.name == saved.getString("theme", null) } ?: Theme.SYSTEM
    }

    fun chooseTheme(value: Theme) {
        theme = value
        prefs?.edit()?.putString("theme", value.name)?.apply()
    }
}
