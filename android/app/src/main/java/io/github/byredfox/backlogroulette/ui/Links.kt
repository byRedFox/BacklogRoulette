package io.github.byredfox.backlogroulette.ui

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import io.github.byredfox.backlogroulette.data.AppLog

const val GITHUB_URL = "https://github.com/byRedFox/BacklogRoulette"
const val ISSUES_URL = "$GITHUB_URL/issues"
const val AUTHOR_URL = "https://github.com/byRedFox"

private const val STEAM_APP = "com.valvesoftware.android.steam.community"
private val STEAM_HOSTS = setOf("store.steampowered.com", "steamcommunity.com", "s.team")

/**
 * Ссылка — наружу: страницы Steam — в приложении Steam, если оно стоит, остальное — в браузере телефона.
 * Только http и https: другие схемы (file:, intent:, javascript:) не открываются.
 */
fun openLink(context: Context, url: String) {
    val uri = Uri.parse(url)
    if (uri.scheme?.lowercase() !in setOf("https", "http")) {
        AppLog.warning("Ссылка не открыта: не http/https")
        return
    }
    val view = Intent(Intent.ACTION_VIEW, uri).addCategory(Intent.CATEGORY_BROWSABLE)
    if (uri.host?.lowercase()?.removePrefix("www.") in STEAM_HOSTS) {
        try {
            context.startActivity(Intent(view).setPackage(STEAM_APP))
            return
        } catch (e: ActivityNotFoundException) {
            // Приложения Steam нет — откроем в браузере
        }
    }
    try {
        context.startActivity(view)
    } catch (e: ActivityNotFoundException) {
        AppLog.warning("Нет приложения, чтобы открыть ссылку ${uri.host}")
    }
}
