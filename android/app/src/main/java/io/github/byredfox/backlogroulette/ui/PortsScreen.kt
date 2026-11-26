package io.github.byredfox.backlogroulette.ui

import android.content.Context
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import coil3.compose.AsyncImage
import io.github.byredfox.backlogroulette.data.AppLog
import io.github.byredfox.backlogroulette.data.InstalledPort
import io.github.byredfox.backlogroulette.data.PortCatalog
import io.github.byredfox.backlogroulette.data.PortEntry
import io.github.byredfox.backlogroulette.data.PortRelease
import io.github.byredfox.backlogroulette.data.Ports
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** Состояние раздела — на всё время работы приложения: скачивание не бросается, если уйти на другой экран. */
private object PortsState {
    val releases = mutableStateMapOf<String, PortRelease>()
    val errors = mutableStateMapOf<String, String>()
    val progress = mutableStateMapOf<String, Float>() // идёт скачивание: доля от 0 до 1
    val notices = mutableStateMapOf<String, String>()
    var loading by mutableStateOf(false)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

    fun refresh(force: Boolean) {
        if (loading) return
        loading = true
        scope.launch {
            for (entry in PortCatalog.entries) {
                if (!force && releases.containsKey(entry.id)) continue
                try {
                    releases[entry.id] = withContext(Dispatchers.IO) { Ports.latest(entry) }
                    errors.remove(entry.id)
                } catch (e: Exception) {
                    errors[entry.id] = e.message ?: e.javaClass.simpleName
                    AppLog.warning("Порты: «${entry.title}» — релиз не получен: ${errors[entry.id]}")
                }
            }
            loading = false
        }
    }

    /** Скачать (или взять уже скачанный), сверить и открыть системное окно установки. */
    fun install(screen: Context, entry: PortEntry) {
        val context = screen.applicationContext // скачивание переживает уход с экрана — держим не экран, а приложение
        val release = releases[entry.id] ?: return
        val asset = release.asset
        if (asset == null) {
            errors[entry.id] = "В последнем релизе нет APK для Android"
            return
        }
        if (progress.containsKey(entry.id)) return
        progress[entry.id] = 0f
        errors.remove(entry.id)
        notices.remove(entry.id)
        scope.launch {
            try {
                val apk = withContext(Dispatchers.IO) {
                    Ports.cached(context, entry, asset) ?: run {
                        AppLog.info("Порты: «${entry.title}» — скачиваю ${asset.name} (${release.tag})")
                        var shown = 0f
                        Ports.download(context, entry, asset, { share ->
                            if (share - shown >= 0.01f) { // полоска — по процентам, а не на каждый кусок файла
                                shown = share
                                progress[entry.id] = share
                            }
                        }, { !progress.containsKey(entry.id) })
                    }
                }
                val archive = withContext(Dispatchers.IO) { Ports.inspect(context, entry, apk) }
                AppLog.info(
                    "Порты: «${entry.title}» — ${asset.name}: SHA-256 совпала с GitHub, пакет ${entry.packageName}, " +
                        "версия ${archive.versionName} (${archive.versionCode}), подпись ${archive.signer.ifEmpty { "?" }}",
                )
                Ports.remember(context, entry, release.tag, archive.versionCode)
                if (!Ports.install(context, apk)) {
                    notices[entry.id] = "Разрешите программе «Установку неизвестных приложений» и нажмите «Установить» ещё раз. " +
                        "Ставить будет Android — каждый раз с вашим подтверждением."
                }
            } catch (e: Exception) {
                errors[entry.id] = e.message ?: e.javaClass.simpleName
                AppLog.warning("Порты: «${entry.title}» — ${errors[entry.id]}")
            } finally {
                progress.remove(entry.id)
            }
        }
    }

    fun cancel(entry: PortEntry) {
        progress.remove(entry.id)
    }
}

/** Что с портом на телефоне сейчас — для подписи и кнопок. */
private data class PortStatus(val installed: InstalledPort?, val update: Boolean?, val text: String)

private fun status(context: Context, entry: PortEntry, release: PortRelease?): PortStatus {
    val installed = Ports.installed(context, entry) ?: return PortStatus(null, null, "Не установлен")
    val tag = Ports.installedTag(context, entry, installed)
    val version = installed.versionName.ifEmpty { installed.versionCode.toString() }
    return when {
        tag == null -> PortStatus(installed, null, "Установлен, версия $version")
        release != null && release.tag != tag -> PortStatus(installed, true, "Есть обновление: ${release.tag} (стоит $tag)")
        else -> PortStatus(installed, false, "Установлен, последняя версия ($tag)")
    }
}

private fun cardImage(entry: PortEntry) = "https://opengraph.githubassets.com/1/${entry.repo}"

@Composable
fun PortsScreen(open: (String) -> Unit) {
    var selected by rememberSaveable { mutableStateOf<String?>(null) }
    // Порт ставят и удаляют в окнах Android — раз в две секунды смотрим, что изменилось
    var tick by remember { mutableIntStateOf(0) }
    LaunchedEffect(Unit) { PortsState.refresh(force = false) }
    LaunchedEffect(Unit) {
        while (true) {
            delay(2000)
            tick++
        }
    }
    BackHandler(enabled = selected != null) { selected = null }
    val entry = PortCatalog.entries.firstOrNull { it.id == selected }
    if (entry == null) PortsList(tick) { selected = it.id } else PortDetail(entry, tick, open)
}

@Composable
private fun PortsList(tick: Int, onOpen: (PortEntry) -> Unit) {
    val context = LocalContext.current
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text(
            "Нативные порты игр для Android с GitHub. Программа скачивает только APK из релизов этих репозиториев, " +
                "сверяет контрольную сумму GitHub, а ставит его Android — с вашим подтверждением. Игр в портах нет: " +
                "где нужна игра, порт попросит свою копию.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("Портов в каталоге: ${PortCatalog.entries.size}", style = MaterialTheme.typography.labelLarge)
            Spacer(Modifier.weight(1f))
            if (PortsState.loading) CircularProgressIndicator(Modifier.padding(end = 8.dp).width(20.dp).height(20.dp), strokeWidth = 2.dp)
            TextButton(onClick = { PortsState.refresh(force = true) }, enabled = !PortsState.loading) { Text("Обновить") }
        }
        PortCatalog.entries.forEach { entry ->
            val release = PortsState.releases[entry.id]
            val state = remember(tick, release) { status(context, entry, release) }
            Card(
                onClick = { onOpen(entry) },
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainerHigh),
                modifier = Modifier.fillMaxWidth(),
            ) {
                AsyncImage(
                    model = cardImage(entry),
                    contentDescription = null,
                    contentScale = ContentScale.Crop,
                    modifier = Modifier.fillMaxWidth().aspectRatio(2f),
                )
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text(entry.title, style = MaterialTheme.typography.titleMedium)
                    Text(entry.platform, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    val line = when {
                        PortsState.progress.containsKey(entry.id) -> "Скачиваю…"
                        state.update == true -> state.text
                        state.installed != null -> state.text
                        release != null -> "Не установлен · ${release.tag}"
                        PortsState.errors[entry.id] != null -> "Нет связи с GitHub"
                        else -> "Смотрю последнюю версию…"
                    }
                    Text(
                        line,
                        style = MaterialTheme.typography.labelLarge,
                        color = if (state.update == true) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurface,
                    )
                }
            }
        }
    }
}

@Composable
private fun PortDetail(entry: PortEntry, tick: Int, open: (String) -> Unit) {
    val context = LocalContext.current
    val release = PortsState.releases[entry.id]
    val state = remember(tick, release) { status(context, entry, release) }
    val progress = PortsState.progress[entry.id]
    // Поставили то, что скачали, — скачанный файл больше не нужен
    LaunchedEffect(state.update, state.installed) {
        if (state.installed != null && state.update == false) withContext(Dispatchers.IO) { Ports.apkFile(context, entry).delete() }
    }
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        AsyncImage(
            model = cardImage(entry),
            contentDescription = null,
            contentScale = ContentScale.Crop,
            modifier = Modifier.fillMaxWidth().aspectRatio(2f).clip(RoundedCornerShape(16.dp)),
        )
        Text(entry.title, style = MaterialTheme.typography.headlineSmall)
        Text(entry.platform, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(
            state.text,
            style = MaterialTheme.typography.titleSmall,
            color = if (state.update == true) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurface,
        )

        if (progress != null) {
            LinearProgressIndicator(progress = { progress }, modifier = Modifier.fillMaxWidth())
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Скачано ${(progress * 100).toInt()} %", style = MaterialTheme.typography.bodySmall)
                Spacer(Modifier.weight(1f))
                TextButton(onClick = { PortsState.cancel(entry) }) { Text("Отменить") }
            }
        } else {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
                val canDownload = release?.asset != null
                when {
                    state.installed == null -> Button(onClick = { PortsState.install(context, entry) }, enabled = canDownload) {
                        Text("Установить")
                    }
                    else -> {
                        Button(onClick = {
                            if (!Ports.launch(context, entry)) PortsState.errors[entry.id] = "Android не дал запустить порт"
                            else AppLog.info("Порты: запущен «${entry.title}»")
                        }) { Text("Играть") }
                        if (state.update != false) {
                            OutlinedButton(onClick = { PortsState.install(context, entry) }, enabled = canDownload) {
                                Text(if (state.update == true) "Обновить" else "Поставить последнюю")
                            }
                        }
                        TextButton(onClick = { Ports.uninstall(context, entry) }) { Text("Удалить") }
                    }
                }
            }
        }
        PortsState.notices[entry.id]?.let { Text(it, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.primary) }
        PortsState.errors[entry.id]?.let { Text(it, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.error) }

        if (entry.needs.isNotEmpty()) {
            Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.secondaryContainer)) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text("Что понадобится", style = MaterialTheme.typography.titleSmall)
                    Text(entry.needs, style = MaterialTheme.typography.bodyMedium)
                }
            }
        }
        Text(entry.about, style = MaterialTheme.typography.bodyMedium)

        if (release != null) {
            Text("Что нового в ${release.tag}", style = MaterialTheme.typography.titleSmall)
            Text(plainNotes(release.notes), style = MaterialTheme.typography.bodySmall)
        }

        Text("Техническое", style = MaterialTheme.typography.titleSmall)
        TechLine("Репозиторий", entry.repo)
        TechLine("Пакет", entry.packageName)
        if (release != null) {
            TechLine("Релиз", "${release.tag} от ${release.published}")
            release.asset?.let { asset ->
                TechLine("Файл", "${asset.name}, ${"%.1f".format(asset.size / 1048576.0)} МБ")
                TechLine("SHA-256", asset.sha256.ifEmpty { "нет — установка недоступна" })
            }
        }
        state.installed?.let { TechLine("На телефоне", "${it.versionName} (сборка ${it.versionCode})") }
        TextButton(onClick = { open(release?.url?.ifEmpty { null } ?: "https://github.com/${entry.repo}") }) {
            Text("Страница на GitHub")
        }
    }
}

@Composable
private fun TechLine(label: String, value: String) {
    Column {
        Text(label, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(value, style = MaterialTheme.typography.bodySmall, fontFamily = FontFamily.Monospace)
    }
}

/** Заметки релиза из GitHub (Markdown) — простым текстом: без решёток, звёздочек и ссылок в скобках. */
private fun plainNotes(text: String): String = text
    .replace(Regex("""\[([^\]]*)]\([^)]*\)"""), "$1")
    .replace(Regex("""(?m)^#{1,6}\s*"""), "")
    .replace("**", "")
    .replace(Regex("""(?m)^\s*[-*]\s+"""), "• ")
    .replace(Regex("""\n{3,}"""), "\n\n")
    .trim()
