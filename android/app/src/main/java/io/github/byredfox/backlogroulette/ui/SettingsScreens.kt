package io.github.byredfox.backlogroulette.ui

import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import io.github.byredfox.backlogroulette.BuildConfig
import io.github.byredfox.backlogroulette.data.AppLog
import io.github.byredfox.backlogroulette.data.Game
import io.github.byredfox.backlogroulette.data.LibraryStore
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

@Composable
fun SettingsScreen(store: LibraryStore, onImport: () -> Unit) {
    val library = store.library
    var confirmClear by remember { mutableStateOf(false) }
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        SettingsCard("Данные с ПК") {
            if (library == null) {
                Text("Пока пусто.")
            } else {
                val date = SimpleDateFormat("d MMMM yyyy, HH:mm", Locale.forLanguageTag("ru"))
                    .format(Date(library.exported * 1000))
                val from = if (library.appVersion.isNotEmpty()) ", Backlog Roulette ${library.appVersion}" else ""
                Text("Файл от $date$from")
                Text(
                    "Steam: ${library.count(Game.KIND_STEAM)} · порты: ${library.count(Game.KIND_PORT)} · " +
                        "дисковые: ${library.count(Game.KIND_DISC)} · коллекций: ${library.collections.size}",
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Text(
                "На ПК: «Настройки» → «Телефон» → «Экспорт для телефона…». Новый импорт заменяет данные, а игры, " +
                    "убранные из рулетки на телефоне, остаются убранными.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = onImport) { Text("Импорт с ПК") }
                if (library != null) {
                    OutlinedButton(onClick = { confirmClear = true }) { Text("Удалить с телефона") }
                }
            }
        }
        if (library != null) {
            SettingsCard("Что крутит рулетка") {
                val pool = store.activePool
                val inCollection = pool.startsWith(LibraryStore.COLLECTION_POOL)
                val sources = if (inCollection) emptySet() else pool.split("+").toSet()
                Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    LibraryStore.SOURCE_TITLES.forEach { (key, title) ->
                        FilterChip(selected = key in sources, onClick = { store.toggleSource(key) }, label = { Text(title) })
                    }
                }
                if (inCollection) {
                    Text("Сейчас крутится ${store.poolTitle().replaceFirstChar { it.lowercase() }}.")
                }
                if (store.pool != null) {
                    TextButton(onClick = { store.choosePool(null) }) { Text("Как на ПК") }
                }
            }
        }
    }

    if (confirmClear) {
        AlertDialog(
            onDismissRequest = { confirmClear = false },
            title = { Text("Удалить данные с телефона?") },
            text = { Text("Игры и коллекции с ПК и игры, убранные из рулетки на телефоне, удалятся. На ПК ничего не изменится.") },
            confirmButton = { TextButton(onClick = { store.clear(); confirmClear = false }) { Text("Удалить") } },
            dismissButton = { TextButton(onClick = { confirmClear = false }) { Text("Отмена") } },
        )
    }
}

@Composable
private fun SettingsCard(title: String, content: @Composable ColumnScope.() -> Unit) {
    Card(
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainer),
        shape = MaterialTheme.shapes.large,
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(title, style = MaterialTheme.typography.titleMedium)
            content()
        }
    }
}

@Composable
fun LogsScreen() {
    var text by remember { mutableStateOf(AppLog.read()) }
    val clipboard = LocalClipboardManager.current
    Column(Modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            FilledTonalButton(onClick = { clipboard.setText(AnnotatedString(text)) }) { Text("Копировать всё") }
            OutlinedButton(onClick = { text = AppLog.read() }) { Text("Обновить") }
            OutlinedButton(onClick = { AppLog.clear(); text = AppLog.read() }) { Text("Очистить") }
        }
        Box(Modifier.weight(1f).fillMaxWidth().verticalScroll(rememberScrollState())) {
            SelectionContainer {
                Text(
                    // Свежие строки — сверху
                    text.trimEnd().lines().asReversed().joinToString("\n").ifEmpty { "Журнал пуст." },
                    fontFamily = FontFamily.Monospace,
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        }
    }
}

@Composable
fun AboutScreen() {
    val links = LocalUriHandler.current
    Column(
        Modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Backlog Roulette", style = MaterialTheme.typography.headlineSmall)
        Text("Версия для Android ${BuildConfig.VERSION_NAME} — пробная сборка")
        Text("Рандомайзер «Во что сыграть дальше?». Лицензия GNU GPL v3.")
        Text(
            "Приложение берёт из интернета только обложки игр с сервера картинок Steam. Данные с ПК хранятся " +
                "только на этом телефоне.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        TextButton(onClick = { links.openUri(GITHUB_URL) }) { Text("Проект на GitHub") }
    }
}

@Composable
fun Placeholder(text: String) {
    Box(Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.Center) {
        Text(text, style = MaterialTheme.typography.titleMedium, textAlign = TextAlign.Center)
    }
}
