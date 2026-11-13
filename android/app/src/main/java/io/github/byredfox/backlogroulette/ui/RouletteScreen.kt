package io.github.byredfox.backlogroulette.ui

import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.CubicBezierEasing
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
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
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.blur
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.zIndex
import coil3.compose.AsyncImage
import coil3.compose.AsyncImagePainter
import io.github.byredfox.backlogroulette.data.AppLog
import io.github.byredfox.backlogroulette.data.CoverUrls
import io.github.byredfox.backlogroulette.data.Covers
import io.github.byredfox.backlogroulette.data.Game
import io.github.byredfox.backlogroulette.data.LibraryStore
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.launch
import kotlin.math.abs
import kotlin.math.ceil
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

private const val SPIN_CARDS = 40 // столько карточек пролетает за прокрут
private const val SPIN_MS = 4200
private const val SIDE = 3 // по столько карточек слева и справа от выпавшей
private val SPIN_EASING = CubicBezierEasing(0.12f, 0.65f, 0.08f, 1f) // быстрый разгон, долгое торможение

@Composable
fun RouletteScreen(store: LibraryStore, onImport: () -> Unit, onOpenUrl: (String) -> Unit) {
    val library = store.library
    if (library == null) {
        EmptyLibrary(onImport)
        return
    }
    val context = LocalContext.current
    val haptics = LocalHapticFeedback.current
    val scope = rememberCoroutineScope()
    val pool = remember(library, store.pool, store.removedHere, store.filters) { store.poolGames() }
    var strip by remember { mutableStateOf<List<Game>>(emptyList()) }
    val position = remember { Animatable(0f) } // какая карточка ленты сейчас в центре (дробная — между ними)
    var spinning by remember { mutableStateOf(false) }
    var confirmRemove by remember { mutableStateOf(false) }
    var showFilters by remember { mutableStateOf(false) }
    val current = store.picked

    // Лента в покое: выпавшая игра в центре, по бокам — случайные
    LaunchedEffect(pool) {
        if (spinning) return@LaunchedEffect
        if (pool.isEmpty()) {
            strip = emptyList()
            return@LaunchedEffect
        }
        val center = current?.takeIf { it in pool } ?: pool.random()
        strip = List(SIDE) { pool.random() } + center + List(SIDE) { pool.random() }
        position.snapTo(SIDE.toFloat())
        Covers.warm(context, strip)
    }
    // Щелчок вибрацией на каждой карточке, что проходит через центр
    LaunchedEffect(Unit) {
        snapshotFlow { position.value.roundToInt() }.distinctUntilChanged().collect {
            if (spinning) haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
        }
    }

    fun spin() = scope.launch {
        if (pool.isEmpty()) return@launch
        spinning = true
        val start = position.value.roundToInt().coerceIn(0, max(0, strip.lastIndex))
        val keep = strip.subList(max(0, start - SIDE), min(strip.size, start + SIDE + 1)) // видимое не прыгает
        val result = pool.filter { it != store.picked }.randomOrNull() ?: pool.random() // не два раза подряд
        val target = keep.size + SPIN_CARDS
        strip = keep + List(SPIN_CARDS) { pool.random() } + result + List(SIDE) { pool.random() }
        position.snapTo((start - max(0, start - SIDE)).toFloat())
        Covers.warm(context, strip.subList(target - 8, strip.size)) // конец ленты и итог — скачиваются, пока она летит
        position.animateTo(target.toFloat(), tween(SPIN_MS, easing = SPIN_EASING))
        store.picked = result
        spinning = false
        AppLog.info("Рулетка: «${result.name}»")
    }

    val centerGame = strip.getOrNull(position.value.roundToInt())
    Column(Modifier.fillMaxSize().padding(vertical = 12.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 16.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text(
                "${store.poolTitle()} · игр: ${pool.size}",
                style = MaterialTheme.typography.labelLarge,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.weight(1f),
            )
            if (!store.inCollection) {
                val count = store.filters.count
                FilledTonalButton(onClick = { showFilters = true }, enabled = !spinning) {
                    Text(if (count > 0) "Фильтры · $count" else "Фильтры")
                }
            } else {
                TextButton(onClick = { store.choosePool(null) }, enabled = !spinning) { Text("Все игры") }
            }
        }
        Spacer(Modifier.height(12.dp))
        Reel(strip, position.value, Modifier.weight(1f).fillMaxWidth())
        Spacer(Modifier.height(16.dp))
        Text(
            (if (spinning) centerGame?.name else current?.name)
                ?: if (pool.isEmpty()) "Крутить нечего" else "Во что сыграть дальше?",
            style = MaterialTheme.typography.headlineSmall,
            textAlign = TextAlign.Center,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(horizontal = 24.dp),
        )
        val details = current?.takeIf { !spinning }?.let { gameDetails(library.meta(it)) }.orEmpty()
        Text(
            when {
                pool.isEmpty() && store.filters.active -> "Под фильтры ничего не подходит — ослабьте их"
                pool.isEmpty() -> "Все игры убраны из рулетки"
                else -> details.ifEmpty { " " }
            },
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
            maxLines = 2,
            modifier = Modifier.padding(horizontal = 24.dp),
        )
        Spacer(Modifier.height(16.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically) {
            Button(
                onClick = { spin() },
                enabled = !spinning && pool.isNotEmpty(),
                modifier = Modifier.height(56.dp),
                contentPadding = ButtonDefaults.ButtonWithIconContentPadding,
            ) {
                Icon(Icons.Filled.Refresh, contentDescription = null)
                Spacer(Modifier.width(8.dp))
                Text("Крутить", style = MaterialTheme.typography.titleMedium)
            }
            if (current != null && !spinning) {
                current.storeUrl?.let { url ->
                    OutlinedButton(onClick = { onOpenUrl(url) }, modifier = Modifier.height(56.dp)) { Text("В Steam") }
                }
                // В коллекции убирать нельзя — как на ПК: коллекция крутит и убранные из рулетки
                if (!store.inCollection) {
                    OutlinedButton(onClick = { confirmRemove = true }, modifier = Modifier.height(56.dp)) {
                        Icon(Icons.Filled.Delete, contentDescription = "Убрать из рулетки")
                    }
                }
            }
        }
    }

    if (showFilters) FiltersSheet(store, onDismiss = { showFilters = false })
    if (confirmRemove && current != null) {
        AlertDialog(
            onDismissRequest = { confirmRemove = false },
            title = { Text("Убрать из рулетки?") },
            text = {
                Text("«${current.name}» больше не выпадет на телефоне. В «Играх Steam» и коллекциях игра останется. " +
                    "Вернуть её в рулетку нельзя — как и на ПК.")
            },
            confirmButton = {
                TextButton(onClick = { store.removeFromRoulette(current); confirmRemove = false }) { Text("Убрать") }
            },
            dismissButton = { TextButton(onClick = { confirmRemove = false }) { Text("Отмена") } },
        )
    }
}

/** Лента обложек: карточка position — в центре, крупнее; соседние меньше и бледнее. Рисуются только видимые. */
@Composable
private fun Reel(strip: List<Game>, position: Float, modifier: Modifier) {
    BoxWithConstraints(modifier, contentAlignment = Alignment.Center) {
        val cardHeight = minOf(maxHeight, maxWidth * 0.62f * 1.5f)
        val cardWidth = cardHeight * 2 / 3
        val gap = 14.dp
        if (strip.isEmpty()) {
            Cover(null, Modifier.size(cardWidth, cardHeight))
        } else {
            val step = with(LocalDensity.current) { (cardWidth + gap).toPx() }
            val half = constraints.maxWidth / step / 2
            val first = floor(position - half - 1).toInt().coerceAtLeast(0)
            val last = ceil(position + half + 1).toInt().coerceAtMost(strip.lastIndex)
            for (index in first..last) {
                key(index) {
                    val delta = index - position
                    val near = min(abs(delta), 2f)
                    Cover(
                        strip[index],
                        Modifier.size(cardWidth, cardHeight).zIndex(-near).graphicsLayer {
                            translationX = delta * step
                            val scale = 1f - min(near, 1f) * 0.16f
                            scaleX = scale
                            scaleY = scale
                            alpha = 1f - near * 0.3f
                        },
                    )
                }
            }
        }
    }
}

/** Год, оценка и жанры — одной строкой. */
fun gameDetails(meta: io.github.byredfox.backlogroulette.data.GameMeta?): String {
    if (meta == null) return ""
    return listOfNotNull(
        meta.year?.toString(),
        meta.percent?.let { "$it % положительных" },
        meta.genres.take(3).joinToString(", ").ifEmpty { null },
    ).joinToString(" · ")
}

/** Обложка игры 2:3. Вертикальной нет — шапка игры на размытой себе же; нет ничего (или ещё качается) —
 *  название на цветной карточке. Где брать картинку — CoverUrls. */
@Composable
fun Cover(game: Game?, modifier: Modifier = Modifier, small: Boolean = false) {
    Card(modifier, shape = if (small) MaterialTheme.shapes.medium else MaterialTheme.shapes.extraLarge) {
        Box(
            Modifier.fillMaxSize().background(MaterialTheme.colorScheme.secondaryContainer),
            contentAlignment = Alignment.Center,
        ) {
            val style: TextStyle = when {
                game == null -> MaterialTheme.typography.displayLarge
                small -> MaterialTheme.typography.labelMedium
                else -> MaterialTheme.typography.titleLarge
            }
            Text(
                game?.name ?: "?",
                style = style,
                color = MaterialTheme.colorScheme.onSecondaryContainer,
                textAlign = TextAlign.Center,
                modifier = Modifier.padding(if (small) 6.dp else 20.dp),
            )
            if (game != null) {
                val cover = CoverUrls.cover(game)
                val url = cover.url
                val onError = { state: AsyncImagePainter.State.Error ->
                    if (CoverUrls.isMissing(state.result.throwable)) CoverUrls.failed(game, url)
                }
                when {
                    url == null -> Unit
                    cover.kind == CoverUrls.Kind.PORTRAIT -> AsyncImage(
                        model = url,
                        contentDescription = game.name,
                        contentScale = ContentScale.Crop,
                        onError = onError,
                        modifier = Modifier.fillMaxSize(),
                    )
                    else -> {
                        // Шапка 460×215 — по центру, а вокруг она же, растянутая и размытая
                        AsyncImage(
                            model = url,
                            contentDescription = null,
                            contentScale = ContentScale.Crop,
                            modifier = Modifier.fillMaxSize().blur(if (small) 10.dp else 28.dp),
                        )
                        AsyncImage(
                            model = url,
                            contentDescription = game.name,
                            contentScale = ContentScale.FillWidth,
                            onError = onError,
                            modifier = Modifier.fillMaxWidth(),
                        )
                    }
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
