package io.github.byredfox.backlogroulette.ui

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import io.github.byredfox.backlogroulette.BuildConfig
import io.github.byredfox.backlogroulette.data.AppLog
import io.github.byredfox.backlogroulette.data.CoverUrls
import io.github.byredfox.backlogroulette.data.Covers
import io.github.byredfox.backlogroulette.data.LibraryStore
import io.github.byredfox.backlogroulette.data.SteamGridKey
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

@Composable
fun SettingsScreen(store: LibraryStore, onImport: () -> Unit, onOpenUrl: (String) -> Unit) {
    val library = store.library
    var confirmClear by remember { mutableStateOf(false) }
    var showMissing by remember { mutableStateOf(false) }
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
                    "Игр Steam: ${library.games.size} · коллекций: ${library.collections.size}",
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Text(
                "На ПК: «Настройки» → «Телефон» → «Экспорт для телефона…». Новый импорт заменяет данные, а игры, " +
                    "убранные из рулетки на телефоне, остаются убранными. Порты и дисковые игры на телефоне пока " +
                    "не показываются.",
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
            // Кому не нашлось вертикальной обложки — по тому, что уже выяснили (обновляется на ходу)
            val steamGames = library.games.filter { it.appid != null }.distinctBy { it.appid }
            val headerOnly = steamGames.count { CoverUrls.cover(it).kind == CoverUrls.Kind.HEADER }
            val noCover = steamGames.filter { CoverUrls.cover(it).kind == CoverUrls.Kind.NONE }
            SettingsCard("Обложки") {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("Подкачивать по Wi-Fi", modifier = Modifier.weight(1f))
                    Switch(checked = store.prefetchOn, onCheckedChange = store::choosePrefetch)
                }
                val total = store.coversTotal
                val have = store.coversDone
                val none = store.coversMissing
                val status = when {
                    total == 0 -> ""
                    have + none >= total -> "Обложки на телефоне: $have из $total"
                    store.coversWaiting -> "На телефоне $have из $total — остальные ждут Wi-Fi"
                    store.prefetchOn -> "Скачиваю: $have из $total"
                    else -> "На телефоне $have из $total"
                }
                if (status.isNotEmpty()) Text(status, color = MaterialTheme.colorScheme.onSurfaceVariant)
                if (headerOnly > 0) {
                    Text("Без вертикальной обложки, видна шапка игры: $headerOnly",
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                if (noCover.isNotEmpty()) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text("Нет ни обложки, ни шапки: ${noCover.size} — видно название",
                            color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.weight(1f))
                        TextButton(onClick = { showMissing = true }) { Text("Какие") }
                    }
                }
                NotificationPermission()
                Text(
                    "Обложки игр — с серверов картинок Steam, около 80 КБ каждая, по 6 разом. Подкачка идёт только " +
                        "по Wi-Fi, но и со свёрнутым приложением, и при погасшем экране — с уведомлением о ходе " +
                        "загрузки. Без подкачки обложка скачивается, когда игра попадается на глаза.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            SteamGridCard(store, onOpenUrl)
        }
    }

    if (showMissing && library != null) {
        val noCover = library.games.filter { it.appid != null && CoverUrls.cover(it).kind == CoverUrls.Kind.NONE }
            .distinctBy { it.appid }.sortedBy { it.name.lowercase() }
        AlertDialog(
            onDismissRequest = { showMissing = false },
            title = { Text("Нет ни обложки, ни шапки") },
            text = {
                Column(Modifier.heightIn(max = 420.dp).verticalScroll(rememberScrollState())) {
                    Text(
                        if (SteamGridKey.saved) "Ни в Steam, ни в SteamGridDB. Нажмите — откроется страница игры."
                        else "В Steam картинок нет — обычно игру сняли с продажи. Их часто находит SteamGridDB " +
                            "(ключ — ниже в настройках). Нажмите на игру — откроется её страница.",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    noCover.forEach { game ->
                        TextButton(
                            onClick = { game.storeUrl?.let { showMissing = false; onOpenUrl(it) } },
                            modifier = Modifier.fillMaxWidth(),
                        ) {
                            Text("${game.name} · ${game.appid}", modifier = Modifier.fillMaxWidth())
                        }
                    }
                }
            },
            confirmButton = { TextButton(onClick = { showMissing = false }) { Text("Закрыть") } },
        )
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

private const val STEAMGRIDDB_KEY_PAGE = "https://www.steamgriddb.com/profile/preferences/api"

/** Свой ключ SteamGridDB: обложки сообщества для игр, у которых в Steam вертикальной нет. */
@Composable
private fun SteamGridCard(store: LibraryStore, onOpenUrl: (String) -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var input by remember { mutableStateOf("") }
    var checking by remember { mutableStateOf(false) }
    var confirmDelete by remember { mutableStateOf(false) }
    SettingsCard("SteamGridDB") {
        Text(
            "Красивые обложки от сообщества — для игр, у которых в Steam нет вертикальной (снятые с продажи, " +
                "старые). Нужен свой бесплатный ключ: войдите на steamgriddb.com через Steam, откройте Preferences → " +
                "API и скопируйте ключ.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        if (SteamGridKey.saved) {
            Text("Ключ сохранён: …${SteamGridKey.tail}")
            OutlinedButton(onClick = { confirmDelete = true }) { Text("Удалить ключ") }
        } else {
            OutlinedTextField(
                value = input,
                onValueChange = { input = it.trim() },
                singleLine = true,
                label = { Text("Ключ API") },
                visualTransformation = PasswordVisualTransformation(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                isError = input.isNotEmpty() && !SteamGridKey.VALID.matches(input),
                modifier = Modifier.fillMaxWidth(),
            )
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(
                    enabled = !checking && SteamGridKey.VALID.matches(input),
                    onClick = {
                        checking = true
                        val key = input
                        scope.launch {
                            when (CoverUrls.checkKey(key)) {
                                true -> try {
                                    SteamGridKey.save(key)
                                    input = ""
                                    CoverUrls.keyAdded()
                                    if (store.prefetchOn) Covers.schedule(context, store, replace = true)
                                    store.message = "Ключ работает — ищу обложки в SteamGridDB"
                                } catch (e: Exception) {
                                    store.message = "Ключ не сохранился: ${e.message}"
                                }
                                false -> store.message = "SteamGridDB не принял ключ — проверьте, что скопирован целиком"
                                null -> store.message = "Нет связи с SteamGridDB — попробуйте позже"
                            }
                            checking = false
                        }
                    },
                ) { Text(if (checking) "Проверяю…" else "Проверить и сохранить") }
                TextButton(onClick = { onOpenUrl(STEAMGRIDDB_KEY_PAGE) }) { Text("Где взять") }
            }
        }
        Text(
            "Ключ хранится только на этом телефоне, зашифрованным ключом Android, и уходит только на сервер " +
                "SteamGridDB. В журнал и экспорт он не попадает.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }

    if (confirmDelete) {
        AlertDialog(
            onDismissRequest = { confirmDelete = false },
            title = { Text("Удалить ключ SteamGridDB?") },
            text = { Text("Уже скачанные обложки останутся, новые искать в SteamGridDB приложение не будет.") },
            confirmButton = {
                TextButton(onClick = { SteamGridKey.clear(); confirmDelete = false }) { Text("Удалить") }
            },
            dismissButton = { TextButton(onClick = { confirmDelete = false }) { Text("Отмена") } },
        )
    }
}

/** Android 13+: уведомление о ходе подкачки показывается только с разрешения. Без него подкачка тоже идёт. */
@Composable
private fun NotificationPermission() {
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
    val context = LocalContext.current
    fun granted() = ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) ==
        PackageManager.PERMISSION_GRANTED
    var allowed by remember { mutableStateOf(granted()) }
    val ask = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { allowed = it }
    if (allowed) return
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(
            "Показывать ход загрузки в шторке",
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.weight(1f),
        )
        TextButton(onClick = { ask.launch(Manifest.permission.POST_NOTIFICATIONS) }) { Text("Разрешить") }
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
            "Из интернета приложение берёт обложки игр с серверов картинок Steam (и SteamGridDB, если вы добавили " +
                "свой ключ), а встроенный браузер открывает только те сайты, на которые вы перешли сами. Данные с ПК " +
                "хранятся только на этом телефоне.",
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
