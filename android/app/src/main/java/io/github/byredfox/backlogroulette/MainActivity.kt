package io.github.byredfox.backlogroulette

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import io.github.byredfox.backlogroulette.data.AppLog
import io.github.byredfox.backlogroulette.data.LibraryStore
import io.github.byredfox.backlogroulette.ui.App
import io.github.byredfox.backlogroulette.ui.BacklogTheme

class MainActivity : ComponentActivity() {
    private val store by lazy { LibraryStore(this) }

    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge() // интерфейс — на весь экран, под системными панелями, как требует Android 16
        super.onCreate(savedInstanceState)
        AppLog.init(filesDir)
        if (savedInstanceState == null) AppLog.info("Запуск, версия ${BuildConfig.VERSION_NAME}")
        setContent {
            BacklogTheme {
                App(store)
            }
        }
    }
}
