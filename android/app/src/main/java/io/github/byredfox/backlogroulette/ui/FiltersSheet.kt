package io.github.byredfox.backlogroulette.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.RangeSlider
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import io.github.byredfox.backlogroulette.data.LibraryStore
import io.github.byredfox.backlogroulette.data.RouletteFilters
import kotlin.math.roundToInt

// Пороги «Отзывов» — по правилам Steam, как на ПК
private val REVIEW_LEVELS = listOf(0 to "Любые", 40 to "от 40 %", 70 to "от 70 %", 80 to "от 80 %", 95 to "от 95 %")

/** Фильтры рулетки снизу экрана. Применяются, когда шторку закрывают (кнопкой или жестом). */
@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
fun FiltersSheet(store: LibraryStore, onDismiss: () -> Unit) {
    val library = store.library ?: return
    var filters by remember { mutableStateOf(store.filters) }
    // Жанры — по числу игр: частые сверху
    val genres = remember(library) {
        library.meta.values.flatMap { it.genres }.groupingBy { it }.eachCount()
            .entries.sortedByDescending { it.value }.map { it.key }
    }
    val years = remember(library) { library.meta.values.mapNotNull { it.year } }
    val minYear = years.minOrNull() ?: 1990
    val maxYear = max(years.maxOrNull() ?: 2026, minYear + 1)
    val matching = remember(filters, library, store.removedHere) { store.poolGames(filters).size }
    val close = {
        store.chooseFilters(filters)
        onDismiss()
    }

    ModalBottomSheet(onDismissRequest = close, sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)) {
        Column(
            Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(horizontal = 24.dp)
                .padding(bottom = 24.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text("Фильтры рулетки", style = MaterialTheme.typography.titleLarge)
            Text("Подходит игр: $matching", color = MaterialTheme.colorScheme.primary)
            if (library.meta.isEmpty()) {
                Text(
                    "В файле с ПК нет жанров, годов и оценок: программа собирает их в фоне после импорта из Steam. " +
                        "Подождите, пока она соберёт, и сделайте экспорт ещё раз.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

            Text("Отзывы", style = MaterialTheme.typography.titleSmall)
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                REVIEW_LEVELS.forEach { (value, title) ->
                    FilterChip(
                        selected = filters.reviews == value,
                        onClick = { filters = filters.copy(reviews = value) },
                        label = { Text(title) },
                    )
                }
            }

            val from = filters.yearFrom ?: minYear
            val to = filters.yearTo ?: maxYear
            Text("Год выхода: $from — $to", style = MaterialTheme.typography.titleSmall)
            RangeSlider(
                value = from.toFloat()..to.toFloat(),
                onValueChange = { range ->
                    val start = range.start.roundToInt()
                    val end = range.endInclusive.roundToInt()
                    filters = filters.copy(yearFrom = start.takeIf { it > minYear }, yearTo = end.takeIf { it < maxYear })
                },
                valueRange = minYear.toFloat()..maxYear.toFloat(),
                steps = max(0, maxYear - minYear - 1),
            )

            if (genres.isNotEmpty()) {
                Text("Жанры — любой из выбранных", style = MaterialTheme.typography.titleSmall)
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    genres.forEach { genre ->
                        val selected = genre in filters.genres
                        FilterChip(
                            selected = selected,
                            onClick = {
                                filters = filters.copy(genres = if (selected) filters.genres - genre else filters.genres + genre)
                            },
                            label = { Text(genre) },
                        )
                    }
                }
            }

            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                TextButton(onClick = { filters = RouletteFilters() }, enabled = filters.active) { Text("Сбросить") }
                Spacer(Modifier.weight(1f))
                Button(onClick = close) { Text("Готово") }
            }
        }
    }
}

private fun max(a: Int, b: Int) = if (a > b) a else b
