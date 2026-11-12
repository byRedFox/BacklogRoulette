package io.github.byredfox.backlogroulette.ui

import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.List
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import io.github.byredfox.backlogroulette.data.Covers
import io.github.byredfox.backlogroulette.data.LibraryStore
import kotlinx.coroutines.launch

/** Разделы в нижней панели. Порты и дисковые игры на телефоне пока не показываются. */
enum class Section(val title: String, val icon: ImageVector) {
    ROULETTE("Рулетка", Icons.Filled.Refresh),
    GAMES("Игры Steam", Icons.Filled.PlayArrow),
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
fun App(store: LibraryStore) {
    var section by rememberSaveable { mutableStateOf(Section.ROULETTE) }
    var extra by rememberSaveable { mutableStateOf<Extra?>(null) }
    val opened = extra
    BackHandler(enabled = opened != null) { extra = null } // «назад» со служебного экрана — к разделу

    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    // Файл с ПК — через системный выбор файла: приложение получает только этот файл, без доступа к остальным
    val importer = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) scope.launch { store.import(uri) }
    }
    val startImport = { importer.launch(arrayOf("application/json", "application/octet-stream", "text/*")) }
    // Браузер живёт, пока открыто приложение: переключились на другой раздел и обратно — та же страница
    val browser = remember { BrowserHolder(context) }
    DisposableEffect(Unit) { onDispose { browser.destroy() } }
    val openInBrowser = { url: String ->
        browser.open(url)
        extra = null
        section = Section.BROWSER
    }

    LaunchedEffect(Unit) { store.load() }
    LaunchedEffect(store.library, store.prefetchOn) {
        if (store.library != null && store.prefetchOn) Covers.prefetchAll(context, store)
    }
    val snackbar = remember { SnackbarHostState() }
    val message = store.message
    LaunchedEffect(message) {
        if (message != null) {
            snackbar.showSnackbar(message)
            store.message = null
        }
    }

    Scaffold(
        snackbarHost = { SnackbarHost(snackbar) },
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
                opened == Extra.SETTINGS -> SettingsScreen(store, startImport)
                opened == Extra.LOGS -> LogsScreen()
                section == Section.ROULETTE -> RouletteScreen(store, startImport, openInBrowser)
                section == Section.GAMES -> GamesScreen(store, startImport, openInBrowser)
                section == Section.COLLECTIONS ->
                    CollectionsScreen(store, startImport, onSpin = { section = Section.ROULETTE })
                else -> BrowserScreen(browser)
            }
        }
    }
}
