package io.github.byredfox.backlogroulette.ui

import android.app.Activity
import android.os.Build
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.core.view.WindowCompat
import io.github.byredfox.backlogroulette.data.UiPrefs

// Бирюзовый — как акцент программы на ПК. На Android 12+ цвета берутся из обоев телефона (Material You)
private val Teal40 = Color(0xFF0F8C82)
private val Teal80 = Color(0xFF6FD8CC)

@Composable
fun BacklogTheme(content: @Composable () -> Unit) {
    val dark = when (UiPrefs.theme) {
        UiPrefs.Theme.SYSTEM -> isSystemInDarkTheme()
        UiPrefs.Theme.LIGHT -> false
        UiPrefs.Theme.DARK -> true
    }
    val context = LocalContext.current
    val colors = when {
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.S ->
            if (dark) dynamicDarkColorScheme(context) else dynamicLightColorScheme(context)
        dark -> darkColorScheme(primary = Teal80)
        else -> lightColorScheme(primary = Teal40)
    }
    // Значки в строке состояния и панели жестов — под тему приложения, а не системы: иначе при светлой теме
    // в тёмной системе они белые на белом
    val view = LocalView.current
    if (!view.isInEditMode) {
        SideEffect {
            val window = (view.context as? Activity)?.window ?: return@SideEffect
            WindowCompat.getInsetsController(window, view).apply {
                isAppearanceLightStatusBars = !dark
                isAppearanceLightNavigationBars = !dark
            }
        }
    }
    MaterialTheme(colorScheme = colors, content = content)
}
