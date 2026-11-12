package io.github.byredfox.backlogroulette.ui

import androidx.activity.compose.BackHandler
import androidx.compose.animation.AnimatedContent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.List
import androidx.compose.material.icons.filled.Build
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.Button
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import io.github.byredfox.backlogroulette.BuildConfig

/** Разделы в нижней панели — как в меню программы на ПК. */
enum class Section(val title: String, val icon: ImageVector) {
    ROULETTE("Рулетка", Icons.Filled.Refresh),
    PORTS("Порты", Icons.Filled.Build),
    COLLECTIONS("Коллекции", Icons.AutoMirrored.Filled.List),
    BROWSER("Браузер", Icons.Filled.Search),
}

/** Служебные экраны — в меню «⋮» сверху. */
enum class Extra(val title: String, val icon: ImageVector) {
    LOGS("Логи", Icons.Filled.Warning),
    SETTINGS("Настройки", Icons.Filled.Settings),
    ABOUT("О программе", Icons.Filled.Info),
}

const val GITHUB_URL = "https://github.com/byRedFox/BacklogRoulette"

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun App() {
    var section by rememberSaveable { mutableStateOf(Section.ROULETTE) }
    var extra by rememberSaveable { mutableStateOf<Extra?>(null) }
    val opened = extra
    BackHandler(enabled = opened != null) { extra = null } // «назад» со служебного экрана — к разделу

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(opened?.title ?: section.title) },
                navigationIcon = {
                    if (opened != null) {
                        IconButton(onClick = { extra = null }) {
                            Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Назад")
                        }
                    }
                },
                actions = {
                    if (opened == null) {
                        var menu by remember { mutableStateOf(false) }
                        IconButton(onClick = { menu = true }) {
                            Icon(Icons.Filled.MoreVert, contentDescription = "Ещё")
                        }
                        DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                            Extra.entries.forEach { item ->
                                DropdownMenuItem(
                                    text = { Text(item.title) },
                                    leadingIcon = { Icon(item.icon, contentDescription = null) },
                                    onClick = { menu = false; extra = item },
                                )
                            }
                        }
                    }
                },
            )
        },
        bottomBar = {
            if (opened == null) {
                NavigationBar {
                    Section.entries.forEach { item ->
                        NavigationBarItem(
                            selected = item == section,
                            onClick = { section = item },
                            icon = { Icon(item.icon, contentDescription = null) },
                            label = { Text(item.title) },
                        )
                    }
                }
            }
        },
    ) { padding ->
        Box(Modifier.padding(padding).fillMaxSize()) {
            when {
                opened == Extra.ABOUT -> AboutScreen()
                opened != null -> Placeholder(opened.title)
                section == Section.ROULETTE -> RouletteScreen()
                else -> Placeholder(section.title)
            }
        }
    }
}

// Проба рулетки на нескольких названиях — настоящий список игр появится вместе с импортом с ПК
private val DEMO_GAMES = listOf("Hollow Knight", "Celeste", "Alan Wake 2", "Hades", "Outer Wilds", "Disco Elysium")

@Composable
fun RouletteScreen() {
    var game by rememberSaveable { mutableStateOf<String?>(null) }
    Column(
        Modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        AnimatedContent(targetState = game, label = "game") { name ->
            Text(
                name ?: "Во что сыграть дальше?",
                style = MaterialTheme.typography.headlineMedium,
                textAlign = TextAlign.Center,
            )
        }
        Spacer(Modifier.height(32.dp))
        Button(onClick = { game = (DEMO_GAMES - game).random() }) { // одна игра не выпадает два раза подряд
            Text("Крутить")
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
        TextButton(onClick = { links.openUri(GITHUB_URL) }) { Text("Проект на GitHub") }
    }
}

@Composable
fun Placeholder(title: String) {
    Box(Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.Center) {
        Text("«$title» — скоро здесь", style = MaterialTheme.typography.titleMedium, textAlign = TextAlign.Center)
    }
}
