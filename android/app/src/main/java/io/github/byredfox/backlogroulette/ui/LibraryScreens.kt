package io.github.byredfox.backlogroulette.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowRight
import androidx.compose.material.icons.automirrored.filled.List
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import coil3.compose.AsyncImage
import io.github.byredfox.backlogroulette.data.Game
import io.github.byredfox.backlogroulette.data.LibraryStore

@Composable
fun CollectionsScreen(store: LibraryStore, onImport: () -> Unit, onSpin: () -> Unit) {
    val library = store.library ?: run { EmptyLibrary(onImport); return }
    var openId by rememberSaveable { mutableStateOf<String?>(null) }
    val opened = library.collections.firstOrNull { it.id == openId }
    BackHandler(enabled = opened != null) { openId = null }

    if (opened == null) {
        if (library.collections.isEmpty()) {
            Placeholder("Коллекций нет — их создают на ПК, во вкладке «Коллекции»")
            return
        }
        LazyColumn(Modifier.fillMaxSize()) {
            items(library.collections, key = { it.id }) { collection ->
                ListItem(
                    headlineContent = { Text(collection.name) },
                    supportingContent = { Text("игр Steam: ${collection.items.count { library.find(it) != null }}") },
                    leadingContent = { Icon(Icons.AutoMirrored.Filled.List, contentDescription = null) },
                    trailingContent = { Icon(Icons.AutoMirrored.Filled.KeyboardArrowRight, contentDescription = null) },
                    modifier = Modifier.clickable { openId = collection.id },
                )
            }
        }
        return
    }
    val games = opened.items.mapNotNull(library::find).distinct()
    Column(Modifier.fillMaxSize()) {
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text(opened.name, style = MaterialTheme.typography.titleLarge, modifier = Modifier.weight(1f))
            FilledTonalButton(
                onClick = { store.choosePool(LibraryStore.COLLECTION_POOL + opened.id); onSpin() },
                enabled = games.isNotEmpty(),
            ) { Text("Крутить её") }
        }
        LazyColumn(Modifier.fillMaxSize()) {
            items(games) { game ->
                ListItem(headlineContent = { Text(game.name) }, leadingContent = { Thumb(game) })
            }
        }
    }
}

/** Маленькая обложка в списке; нет обложки — первая буква названия. */
@Composable
private fun Thumb(game: Game) {
    Surface(
        color = MaterialTheme.colorScheme.secondaryContainer,
        shape = RoundedCornerShape(6.dp),
        modifier = Modifier.size(width = 40.dp, height = 60.dp),
    ) {
        Box(contentAlignment = Alignment.Center) {
            Text(game.name.take(1), style = MaterialTheme.typography.titleMedium, textAlign = TextAlign.Center)
            game.coverUrl?.let { url ->
                AsyncImage(
                    model = url,
                    contentDescription = null,
                    contentScale = ContentScale.Crop,
                    modifier = Modifier.fillMaxSize(),
                )
            }
        }
    }
}
