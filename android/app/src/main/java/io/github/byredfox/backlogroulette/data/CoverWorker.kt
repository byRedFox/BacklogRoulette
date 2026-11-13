package io.github.byredfox.backlogroulette.data

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.work.CoroutineWorker
import androidx.work.ForegroundInfo
import androidx.work.WorkerParameters
import io.github.byredfox.backlogroulette.BacklogApp
import io.github.byredfox.backlogroulette.MainActivity
import io.github.byredfox.backlogroulette.R

/**
 * Подкачка обложек фоновой задачей Android. Пока качает — уведомление с ходом загрузки: с ним Android не
 * усыпляет задачу при погасшем экране. Если запустить уведомление нельзя (Android не разрешает это свёрнутому
 * приложению), задача всё равно идёт, просто короче, и Android перезапустит её сам.
 */
class CoverWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    private var foreground = true // уведомление показать получилось или ещё не пробовали

    override suspend fun doWork(): Result {
        val store = (applicationContext as BacklogApp).store
        store.load()
        if (!store.prefetchOn || store.library == null) return Result.success()
        Covers.prefetchAll(applicationContext, store) { done, total ->
            if (!foreground) return@prefetchAll
            try {
                setForeground(info(done, total))
            } catch (e: kotlinx.coroutines.CancellationException) {
                throw e
            } catch (e: Exception) {
                foreground = false
                AppLog.info("Обложки качаются без уведомления: ${e.javaClass.simpleName}")
            }
        }
        return Result.success()
    }

    // Нужен WorkManager'у, если задачу надо срочно поднять до уведомления
    override suspend fun getForegroundInfo(): ForegroundInfo = info(0, 0)

    private fun info(done: Int, total: Int): ForegroundInfo {
        val manager = applicationContext.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL, "Подкачка обложек", NotificationManager.IMPORTANCE_LOW)
        )
        val open = PendingIntent.getActivity(
            applicationContext, 0, Intent(applicationContext, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = NotificationCompat.Builder(applicationContext, CHANNEL)
            .setSmallIcon(R.drawable.ic_launcher_monochrome)
            .setContentTitle("Качаю обложки")
            .setContentText(if (total > 0) "На телефоне $done из $total" else "Готовлюсь")
            .setProgress(total, done, total == 0)
            .setContentIntent(open)
            .setOngoing(true)
            .setSilent(true)
            .build()
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            ForegroundInfo(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            ForegroundInfo(NOTIFICATION_ID, notification)
        }
    }

    private companion object {
        const val CHANNEL = "covers"
        const val NOTIFICATION_ID = 1
    }
}
