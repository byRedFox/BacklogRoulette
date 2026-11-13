package io.github.byredfox.backlogroulette.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import io.github.byredfox.backlogroulette.data.Game
import io.github.byredfox.backlogroulette.data.LibraryStore

/** «Игры Steam»: все игры коллажем из обложек во весь экран, поиск по названию. */
@Composable
fun GamesScreen(store: LibraryStore, onImport: () -> Unit, onOpenUrl: (String) -> Unit) {
    val library = store.library ?: run { EmptyLibrary(onImport); return }
    var query by rememberSaveable { mutableStateOf("") }
    var opened by remember { mutableStateOf<Game?>(null) }
    val all = remember(library) { library.games.sortedBy { it.name.lowercase() } }
    val shown = remember(all, query) {
        val text = query.trim()
        if (text.isEmpty()) all else all.filter { it.name.contains(text, ignoreCase = true) }
    }

    Column(Modifier.fillMaxSize()) {
        OutlinedTextField(
            value = query,
            onValueChange = { query = it },
            singleLine = true,
            shape = CircleShape,
            placeholder = { Text("Поиск · игр: ${all.size}") },
            leadingIcon = { Icon(Icons.Filled.Search, contentDescription = null) },
            trailingIcon = {
                if (query.isNotEmpty()) {
                    IconButton(onClick = { query = "" }) { Icon(Icons.Filled.Close, contentDescription = "Очистить") }
                }
            },
            modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp),
        )
        LazyVerticalGrid(
            columns = GridCells.Adaptive(minSize = 104.dp),
            contentPadding = PaddingValues(6.dp),
            horizontalArrangement = Arrangement.spacedBy(6.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp),
            modifier = Modifier.fillMaxSize(),
        ) {
            items(shown) { game ->
                val removed = library.isRemoved(game, store.removedHere)
                Cover(
                    game,
                    Modifier.aspectRatio(2f / 3f).alpha(if (removed) 0.4f else 1f).clickable { opened = game },
                    small = true,
                )
            }
        }
    }

    opened?.let { game ->
        val removed = library.isRemoved(game, store.removedHere)
        AlertDialog(
            onDismissRequest = { opened = null },
            title = { Text(game.name) },
            text = {
                // Обложка — по центру окна, во всю его ширину (но не выше 360 dp), подпись — под ней по центру
                Column(
                    Modifier.fillMaxWidth(),
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.spacedBy(12.dp),
                ) {
                    Cover(game, Modifier.heightIn(max = 360.dp).aspectRatio(2f / 3f))
                    val details = gameDetails(library.meta(game))
                    if (details.isNotEmpty()) {
                        Text(details, style = MaterialTheme.typography.bodyMedium, textAlign = TextAlign.Center)
                    }
                    if (removed) {
                        Text("Убрана из рулетки", color = MaterialTheme.colorScheme.error,
                            style = MaterialTheme.typography.bodySmall)
                    }
                }
            },
            confirmButton = {
                game.storeUrl?.let { url ->
                    TextButton(onClick = { opened = null; onOpenUrl(url) }) { Text("Страница в Steam") }
                }
            },
            dismissButton = { TextButton(onClick = { opened = null }) { Text("Закрыть") } },
        )
    }
}
