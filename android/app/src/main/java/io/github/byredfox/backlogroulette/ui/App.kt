package io.github.byredfox.backlogroulette.ui

import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.annotation.DrawableRes
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.Menu
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.DrawerValue
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalDrawerSheet
import androidx.compose.material3.ModalNavigationDrawer
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationDrawerItem
import androidx.compose.material3.NavigationDrawerItemDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.rememberDrawerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.coerceIn
import androidx.compose.ui.unit.dp
import io.github.byredfox.backlogroulette.BuildConfig
import io.github.byredfox.backlogroulette.R
import io.github.byredfox.backlogroulette.data.Covers
import io.github.byredfox.backlogroulette.data.LibraryStore
import kotlinx.coroutines.launch

/** Главные разделы — в нижней панели. Порты и дисковые игры на телефоне пока не показываются. */
enum class Section(val title: String, @DrawableRes val icon: Int) {
    ROULETTE("Рулетка", R.drawable.ic_die),
    GAMES("Игры Steam", R.drawable.ic_grid),
}

/** Остальное — в боковом меню, как в Google Диске. */
enum class Page(val title: String, @DrawableRes val icon: Int = 0, val vector: ImageVector? = null) {
    COLLECTIONS("Коллекции", R.drawable.ic_folder),
    SETTINGS("Настройки", vector = Icons.Filled.Settings),
    LOGS("Логи", R.drawable.ic_logs),
    ABOUT("О программе", vector = Icons.Filled.Info),
}

@Composable
private fun PageIcon(page: Page) {
    val vector = page.vector
    if (vector != null) Icon(vector, contentDescription = null) else Icon(painterResource(page.icon), contentDescription = null)
}

@Composable
fun App(store: LibraryStore) {
    var section by rememberSaveable { mutableStateOf(Section.ROULETTE) }
    var page by rememberSaveable { mutableStateOf<Page?>(null) }
    var query by rememberSaveable { mutableStateOf("") }
    var spinPending by remember { mutableStateOf(false) }
    val drawer = rememberDrawerState(DrawerValue.Closed)

    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    // Файл с ПК — через системный выбор файла: приложение получает только этот файл, без доступа к остальным
    val importer = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) scope.launch { store.import(uri) }
    }
    val startImport = { importer.launch(arrayOf("application/json", "application/octet-stream", "text/*")) }
    val open = { url: String -> openLink(context, url) }
    // Логотип справа сверху — с любого экрана сразу на рулетку, и она крутится сама
    val spinNow: () -> Unit = {
        scope.launch { drawer.close() }
        page = null
        query = ""
        section = Section.ROULETTE
        if (store.library != null) spinPending = true
    }

    // «Назад»: закрыть меню → вернуться со страницы меню → с «Игр Steam» на рулетку → выйти
    BackHandler(enabled = drawer.isOpen) { scope.launch { drawer.close() } }
    BackHandler(enabled = !drawer.isOpen && page != null) { page = null }
    BackHandler(enabled = !drawer.isOpen && page == null && section == Section.GAMES) {
        query = ""
        section = Section.ROULETTE
    }

    LaunchedEffect(Unit) { store.load() }
    // Подкачка обложек — фоновой задачей: идёт и со свёрнутым приложением, и при погасшем экране
    LaunchedEffect(store.library, store.prefetchOn) {
        if (store.library != null && store.prefetchOn) Covers.schedule(context, store)
    }
    val snackbar = remember { SnackbarHostState() }
    val message = store.message
    LaunchedEffect(message) {
        if (message != null) {
            snackbar.showSnackbar(message)
            store.message = null
        }
    }

    ModalNavigationDrawer(
        drawerState = drawer,
        drawerContent = {
            AppDrawer(page) { picked ->
                page = picked
                scope.launch { drawer.close() }
            }
        },
    ) {
        Scaffold(
            snackbarHost = { SnackbarHost(snackbar) },
            topBar = {
                val current = page
                if (current == null) {
                    SearchTopBar(
                        query = query,
                        onQuery = {
                            query = it
                            if (it.isNotEmpty()) section = Section.GAMES // ищем — показываем игры
                        },
                        onMenu = { scope.launch { drawer.open() } },
                        onLogo = spinNow,
                    )
                } else {
                    PageTopBar(current.title, onBack = { page = null }, onLogo = spinNow)
                }
            },
            bottomBar = {
                if (page == null) {
                    NavigationBar {
                        Section.entries.forEach { item ->
                            NavigationBarItem(
                                selected = item == section,
                                onClick = {
                                    if (item == Section.ROULETTE) query = ""
                                    section = item
                                },
                                icon = { Icon(painterResource(item.icon), contentDescription = null) },
                                label = { Text(item.title) },
                            )
                        }
                    }
                }
            },
        ) { padding ->
            AnimatedContent(
                targetState = page?.name ?: section.name,
                transitionSpec = {
                    (fadeIn(tween(220)) + scaleIn(tween(320), initialScale = 0.92f)) togetherWith fadeOut(tween(120))
                },
                label = "screen",
                modifier = Modifier.padding(padding).fillMaxSize(),
            ) { screen ->
                when (screen) {
                    Page.COLLECTIONS.name -> CollectionsScreen(store, startImport, onSpin = spinNow)
                    Page.SETTINGS.name -> SettingsScreen(store, startImport, open)
                    Page.LOGS.name -> LogsScreen()
                    Page.ABOUT.name -> AboutScreen(open)
                    Section.GAMES.name -> GamesScreen(store, query, startImport, open)
                    else -> RouletteScreen(
                        store, startImport, open,
                        spinPending = spinPending,
                        onSpinStarted = { spinPending = false },
                    )
                }
            }
        }
    }
}

/** Боковое меню: шапка с логотипом, «Коллекции», затем служебное. */
@Composable
private fun AppDrawer(current: Page?, onPick: (Page) -> Unit) {
    // Около 60 % ширины: на половине экрана подписи пунктов не помещаются
    val width = (LocalConfiguration.current.screenWidthDp * 0.6f).dp.coerceIn(260.dp, 320.dp)
    ModalDrawerSheet(Modifier.width(width)) {
        Row(
            Modifier.padding(horizontal = 28.dp, vertical = 24.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            AppLogo(Modifier.size(40.dp))
            Column {
                Text("Backlog Roulette", style = MaterialTheme.typography.titleMedium)
                Text(
                    "версия ${BuildConfig.VERSION_NAME}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
        DrawerItem(Page.COLLECTIONS, current, onPick)
        HorizontalDivider(Modifier.padding(horizontal = 28.dp, vertical = 8.dp))
        DrawerItem(Page.SETTINGS, current, onPick)
        DrawerItem(Page.LOGS, current, onPick)
        DrawerItem(Page.ABOUT, current, onPick)
    }
}

@Composable
private fun DrawerItem(page: Page, current: Page?, onPick: (Page) -> Unit) {
    NavigationDrawerItem(
        label = { Text(page.title) },
        selected = page == current,
        onClick = { onPick(page) },
        icon = { PageIcon(page) },
        modifier = Modifier.padding(NavigationDrawerItemDefaults.ItemPadding),
    )
}

/** Строка поиска-«таблетка», как в Google Диске: меню слева, логотип справа. */
@Composable
private fun SearchTopBar(query: String, onQuery: (String) -> Unit, onMenu: () -> Unit, onLogo: () -> Unit) {
    val focus = LocalFocusManager.current
    Box(Modifier.fillMaxWidth().statusBarsPadding().padding(horizontal = 16.dp, vertical = 8.dp)) {
        Surface(
            shape = CircleShape,
            color = MaterialTheme.colorScheme.surfaceContainerHigh,
            modifier = Modifier.fillMaxWidth().height(56.dp),
        ) {
            Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(start = 4.dp, end = 4.dp)) {
                IconButton(onClick = onMenu) { Icon(Icons.Filled.Menu, contentDescription = "Меню") }
                Box(Modifier.weight(1f), contentAlignment = Alignment.CenterStart) {
                    if (query.isEmpty()) {
                        Text(
                            "Поиск игр",
                            style = MaterialTheme.typography.bodyLarge,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                    }
                    BasicTextField(
                        value = query,
                        onValueChange = onQuery,
                        singleLine = true,
                        textStyle = MaterialTheme.typography.bodyLarge.copy(color = MaterialTheme.colorScheme.onSurface),
                        cursorBrush = SolidColor(MaterialTheme.colorScheme.primary),
                        keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
                        keyboardActions = KeyboardActions(onSearch = { focus.clearFocus() }),
                        modifier = Modifier.fillMaxWidth(),
                    )
                }
                if (query.isNotEmpty()) {
                    IconButton(onClick = { onQuery(""); focus.clearFocus() }) {
                        Icon(Icons.Filled.Close, contentDescription = "Очистить")
                    }
                }
                LogoButton { focus.clearFocus(); onLogo() }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun PageTopBar(title: String, onBack: () -> Unit, onLogo: () -> Unit) {
    TopAppBar(
        title = { Text(title) },
        navigationIcon = {
            IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Назад") }
        },
        actions = { LogoButton(onLogo) },
    )
}

/** Логотип-кнопка: крутнётся сам и запустит рулетку. */
@Composable
private fun LogoButton(onClick: () -> Unit) {
    val scope = rememberCoroutineScope()
    val turn = remember { Animatable(0f) }
    IconButton(onClick = {
        scope.launch { turn.animateTo(turn.value + 360f, tween(600)) }
        onClick()
    }) {
        AppLogo(Modifier.size(34.dp).graphicsLayer { rotationZ = turn.value })
    }
}

@Composable
fun AppLogo(modifier: Modifier = Modifier) {
    Image(
        painterResource(R.drawable.ic_launcher_art),
        contentDescription = "Крутить рулетку",
        contentScale = ContentScale.Crop,
        modifier = modifier.clip(CircleShape),
    )
}
