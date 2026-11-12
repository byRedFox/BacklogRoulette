package io.github.byredfox.backlogroulette.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
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
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import coil3.compose.AsyncImage
import io.github.byredfox.backlogroulette.data.AppLog
import io.github.byredfox.backlogroulette.data.Game
import io.github.byredfox.backlogroulette.data.LibraryStore
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

private const val SPIN_STEPS = 26

@Composable
fun RouletteScreen(store: LibraryStore, onImport: () -> Unit) {
    val library = store.library
    if (library == null) {
        EmptyLibrary(onImport)
        return
    }
    val pool = remember(library, store.activePool, store.removedHere) { store.poolGames() }
    var spinning by remember { mutableStateOf(false) }
    var confirmRemove by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val haptics = LocalHapticFeedback.current
    val current = store.picked
    val inCollection = store.activePool.startsWith(LibraryStore.COLLECTION_POOL)

    Column(
        Modifier.fillMaxSize().padding(horizontal = 24.dp, vertical = 12.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(
            "${store.poolTitle()} · игр: ${pool.size}",
            style = MaterialTheme.typography.labelLarge,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(12.dp))
        Cover(current, Modifier.weight(1f).aspectRatio(2f / 3f, matchHeightConstraintsFirst = true))
        Spacer(Modifier.height(16.dp))
        Text(
            current?.name ?: if (pool.isEmpty()) "Крутить нечего" else "Во что сыграть дальше?",
            style = MaterialTheme.typography.headlineSmall,
            textAlign = TextAlign.Center,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
        )
        Text(
            current?.let(::kindTitle) ?: if (pool.isEmpty()) "Выберите другое в «Настройках» → «Что крутит рулетка»" else " ",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
        )
        Spacer(Modifier.height(16.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically) {
            Button(
                onClick = {
                    scope.launch {
                        spinning = true
                        // Одна игра не выпадает два раза подряд
                        val result = pool.filter { it != store.picked }.randomOrNull() ?: pool.random()
                        for (step in 0 until SPIN_STEPS) {
                            store.picked = if (step == SPIN_STEPS - 1) result else pool.random()
                            haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                            delay(30L + step * step / 2) // барабан замедляется
                        }
                        AppLog.info("Рулетка: «${result.name}»")
                        spinning = false
                    }
                },
                enabled = !spinning && pool.isNotEmpty(),
                modifier = Modifier.height(56.dp),
                contentPadding = ButtonDefaults.ButtonWithIconContentPadding,
            ) {
                Icon(Icons.Filled.Refresh, contentDescription = null)
                Spacer(Modifier.width(8.dp))
                Text("Крутить", style = MaterialTheme.typography.titleMedium)
            }
            // В коллекции убирать нельзя — как на ПК: коллекция крутит и убранные из рулетки
            if (current != null && !spinning && !inCollection) {
                OutlinedButton(onClick = { confirmRemove = true }, modifier = Modifier.height(56.dp)) {
                    Icon(Icons.Filled.Delete, contentDescription = null)
                    Spacer(Modifier.width(8.dp))
                    Text("Убрать")
                }
            }
        }
    }

    if (confirmRemove && current != null) {
        AlertDialog(
            onDismissRequest = { confirmRemove = false },
            title = { Text("Убрать из рулетки?") },
            text = {
                Text("«${current.name}» больше не выпадет на телефоне. В коллекциях игра останется. " +
                    "Вернуть её в рулетку нельзя — как и на ПК.")
            },
            confirmButton = {
                TextButton(onClick = { store.removeFromRoulette(current); confirmRemove = false }) { Text("Убрать") }
            },
            dismissButton = { TextButton(onClick = { confirmRemove = false }) { Text("Отмена") } },
        )
    }
}

/** Обложка игры 2:3; нет обложки (порт, дисковая, Steam её не отдал) — название на цветной карточке. */
@Composable
fun Cover(game: Game?, modifier: Modifier = Modifier) {
    Card(modifier, shape = MaterialTheme.shapes.extraLarge) {
        Box(
            Modifier.fillMaxSize().background(MaterialTheme.colorScheme.secondaryContainer),
            contentAlignment = Alignment.Center,
        ) {
            Text(
                game?.name ?: "?",
                style = if (game == null) MaterialTheme.typography.displayLarge else MaterialTheme.typography.titleLarge,
                color = MaterialTheme.colorScheme.onSecondaryContainer,
                textAlign = TextAlign.Center,
                modifier = Modifier.padding(20.dp),
            )
            game?.let { g ->
                g.coverUrl?.let { url ->
                    AsyncImage(
                        model = url,
                        contentDescription = g.name,
                        contentScale = ContentScale.Crop,
                        modifier = Modifier.fillMaxSize(),
                    )
                }
            }
        }
    }
}

/** Данных с ПК ещё нет. */
@Composable
fun EmptyLibrary(onImport: () -> Unit) {
    Column(
        Modifier.fillMaxSize().padding(32.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text("Игр пока нет", style = MaterialTheme.typography.headlineSmall)
        Spacer(Modifier.height(12.dp))
        Text(
            "На ПК: «Настройки» → «Телефон» → «Экспорт для телефона…». Перенесите файл на телефон " +
                "(Telegram, облако или кабель) и выберите его здесь.",
            textAlign = TextAlign.Center,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(24.dp))
        Button(onClick = onImport, contentPadding = ButtonDefaults.ButtonWithIconContentPadding) {
            Icon(Icons.Filled.Add, contentDescription = null, modifier = Modifier.size(ButtonDefaults.IconSize))
            Spacer(Modifier.width(8.dp))
            Text("Импорт с ПК")
        }
    }
}

fun kindTitle(game: Game): String = when (game.kind) {
    Game.KIND_PORT -> "Native Port"
    Game.KIND_DISC -> "Дисковая игра"
    else -> "Steam"
}
