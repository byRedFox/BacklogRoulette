package io.github.byredfox.backlogroulette.ui

import android.annotation.SuppressLint
import android.content.Context
import android.net.Uri
import android.view.ViewGroup
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import io.github.byredfox.backlogroulette.data.AppLog

const val STEAM_STORE_URL = "https://store.steampowered.com/"
private val QUICK_LINKS = listOf("Магазин Steam" to STEAM_STORE_URL, "Plati.Market" to "https://plati.market/")

/**
 * Встроенный браузер — системный WebView. Открывает только страницы http/https: ссылки на другие приложения
 * (steam://, intent://) и файлы не открываются, загрузок нет. Вход на сайты (куки) хранится в приложении.
 */
class BrowserHolder(private val context: Context) {
    var address by mutableStateOf(STEAM_STORE_URL)
    var progress by mutableIntStateOf(100)
    var canGoBack by mutableStateOf(false)
    private var view: WebView? = null

    @SuppressLint("SetJavaScriptEnabled") // магазину Steam без JavaScript не работать
    fun webView(): WebView = view ?: WebView(context).apply {
        settings.javaScriptEnabled = true
        settings.domStorageEnabled = true
        settings.allowFileAccess = false
        settings.allowContentAccess = false
        settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
        webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val scheme = request.url.scheme?.lowercase()
                if (scheme == "https" || scheme == "http") return false
                AppLog.info("Браузер: ссылка «$scheme:» не открыта — только страницы сайтов")
                return true
            }

            override fun doUpdateVisitedHistory(view: WebView, url: String?, isReload: Boolean) {
                this@BrowserHolder.address = url.orEmpty()
                this@BrowserHolder.canGoBack = view.canGoBack()
            }
        }
        webChromeClient = object : WebChromeClient() {
            override fun onProgressChanged(view: WebView, newProgress: Int) {
                this@BrowserHolder.progress = newProgress // у самого WebView тоже есть progress — нужен наш
            }
        }
        loadUrl(this@BrowserHolder.address)
        this@BrowserHolder.view = this
    }

    fun open(url: String) {
        address = url
        view?.loadUrl(url)
    }

    fun destroy() {
        view?.destroy()
        view = null
    }
}

/** Что ввели в адресную строку: адрес сайта — открыть, иначе — поиск в магазине Steam. */
private fun addressFor(text: String): String {
    val value = text.trim()
    return when {
        value.startsWith("https://", true) || value.startsWith("http://", true) -> value
        value.contains('.') && !value.contains(' ') -> "https://$value"
        else -> "https://store.steampowered.com/search/?term=" + Uri.encode(value)
    }
}

@Composable
fun BrowserScreen(browser: BrowserHolder) {
    val focus = LocalFocusManager.current
    var typed by remember { mutableStateOf(browser.address) }
    LaunchedEffect(browser.address) { typed = browser.address }
    BackHandler(enabled = browser.canGoBack) { browser.webView().goBack() }

    Column(Modifier.fillMaxSize()) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 4.dp), verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = { browser.webView().goBack() }, enabled = browser.canGoBack) {
                Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Назад")
            }
            OutlinedTextField(
                value = typed,
                onValueChange = { typed = it },
                singleLine = true,
                shape = CircleShape,
                placeholder = { Text("Адрес или поиск в Steam") },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri, imeAction = ImeAction.Go),
                keyboardActions = KeyboardActions(onGo = {
                    browser.open(addressFor(typed))
                    focus.clearFocus()
                }),
                modifier = Modifier.weight(1f),
            )
            IconButton(onClick = { browser.webView().reload() }) {
                Icon(Icons.Filled.Refresh, contentDescription = "Обновить")
            }
        }
        Row(
            Modifier.horizontalScroll(rememberScrollState()).padding(horizontal = 12.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            QUICK_LINKS.forEach { (title, url) ->
                AssistChip(onClick = { browser.open(url) }, label = { Text(title) })
            }
        }
        if (browser.progress < 100) {
            LinearProgressIndicator(progress = { browser.progress / 100f }, modifier = Modifier.fillMaxWidth())
        }
        AndroidView(
            factory = {
                browser.webView().also { web -> (web.parent as? ViewGroup)?.removeView(web) }
            },
            modifier = Modifier.fillMaxSize(),
        )
    }
}
