package io.github.byredfox.backlogroulette.ui

import android.os.Build
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext

// Бирюзовый — как акцент программы на ПК. На Android 12+ цвета берутся из обоев телефона (Material You)
private val Teal40 = Color(0xFF0F8C82)
private val Teal80 = Color(0xFF6FD8CC)

@Composable
fun BacklogTheme(content: @Composable () -> Unit) {
    val dark = isSystemInDarkTheme()
    val context = LocalContext.current
    val colors = when {
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.S ->
            if (dark) dynamicDarkColorScheme(context) else dynamicLightColorScheme(context)
        dark -> darkColorScheme(primary = Teal80)
        else -> lightColorScheme(primary = Teal40)
    }
    MaterialTheme(colorScheme = colors, content = content)
}
