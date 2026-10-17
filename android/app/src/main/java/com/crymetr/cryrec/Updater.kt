package com.crymetr.cryrec

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.job.JobInfo
import android.app.job.JobParameters
import android.app.job.JobScheduler
import android.app.job.JobService
import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

/**
 * Self-update from GitHub Releases. Downloads the newer APK, checks it is ours and newer, installs it
 * through PackageInstaller. Android asks once (the first update after a browser install); after that
 * CryRec is its own installer and later updates go through without a prompt (Android 12+).
 */
object Updater {
    private const val CHANNEL = "updates"
    private const val NOTE_ID = 2
    private const val JOB_ID = 7

    /** Debug builds can point at a test server (pref "update_api"). */
    private fun apiUrl(c: Context): String =
        Prefs(c).updateApi.takeIf { BuildConfig.DEBUG && it.isNotEmpty() }
            ?: "https://api.github.com/repos/${MainActivity.REPO}/releases/latest"

    fun schedule(c: Context) {
        val js = c.getSystemService(JobScheduler::class.java)
        if (js.getPendingJob(JOB_ID) != null) return
        js.schedule(JobInfo.Builder(JOB_ID, ComponentName(c, UpdateJob::class.java))
            .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
            .setPeriodic(12 * 3600_000L)
            .setPersisted(true)
            .build())
    }

    fun currentVersion(c: Context): String = c.packageManager.getPackageInfo(c.packageName, 0).versionName ?: "0"

    /** Not while recording, sending a recording or dictating. */
    private fun busy() = RecorderService.recording != null || RecorderService.processing.get() || Dictation.busy

    /** Blocking. Returns a short status line for the UI. Without [force], skips if it ran in the last 10 minutes. */
    @Synchronized
    fun check(c: Context, force: Boolean = false): String {
        val p = Prefs(c)
        if (!force && System.currentTimeMillis() - p.lastUpdateCheck < 10 * 60_000L) return "Checked a moment ago."
        val release = try {
            JSONObject(URL(apiUrl(c)).readText())
        } catch (e: Exception) {
            return "Could not reach GitHub."
        }
        p.lastUpdateCheck = System.currentTimeMillis()
        val latest = release.optString("tag_name").removePrefix("v")
        if (!newer(latest, currentVersion(c))) return "Up to date (${currentVersion(c)})."
        val assets = release.optJSONArray("assets") ?: return "No APK in release $latest."
        val url = (0 until assets.length()).map { assets.getJSONObject(it) }
            .firstOrNull { it.optString("name").endsWith(".apk") }?.optString("browser_download_url")
            ?: return "No APK in release $latest."
        if (busy()) return "$latest found; will install when CryRec is idle."
        if (!c.packageManager.canRequestPackageInstalls()) {
            notify(c, "CryRec $latest is ready", "Tap to let CryRec install its own updates.",
                Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${c.packageName}")))
            return "$latest found. Allow \"Install unknown apps\" for CryRec first."
        }
        val apk = File(c.cacheDir, "update.apk")
        try {
            download(url, apk)
        } catch (e: Exception) {
            return "Download failed."
        }
        val info = c.packageManager.getPackageArchiveInfo(apk.path, 0)
        if (info?.packageName != c.packageName || !newer(info.versionName ?: "0", currentVersion(c))) {
            apk.delete()
            return "The downloaded file is not a newer CryRec."
        }
        install(c, apk)
        return "Installing $latest…"
    }

    private fun download(url: String, to: File) {
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.connectTimeout = 15_000
        conn.readTimeout = 60_000
        conn.instanceFollowRedirects = true
        try {
            if (conn.responseCode != 200) throw java.io.IOException("HTTP ${conn.responseCode}")
            conn.inputStream.use { inp -> to.outputStream().use { inp.copyTo(it) } }
        } finally {
            conn.disconnect()
        }
    }

    private fun install(c: Context, apk: File) {
        val pi = c.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
            setAppPackageName(c.packageName)
            if (Build.VERSION.SDK_INT >= 31) setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED)
        }
        val id = pi.createSession(params)
        pi.openSession(id).use { s ->
            s.openWrite("cryrec.apk", 0, apk.length()).use { out -> apk.inputStream().use { it.copyTo(out) }; s.fsync(out) }
            val status = PendingIntent.getBroadcast(c, id, Intent(c, UpdateReceiver::class.java),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE)
            s.commit(status.intentSender)
        }
        apk.delete()
    }

    fun notify(c: Context, title: String, text: String, open: Intent) {
        val nm = c.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Updates", NotificationManager.IMPORTANCE_DEFAULT))
        val pi = PendingIntent.getActivity(c, 3, open.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        nm.notify(NOTE_ID, Notification.Builder(c, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_rec)
            .setContentTitle(title)
            .setContentText(text)
            .setContentIntent(pi)
            .setAutoCancel(true)
            .build())
    }

    fun newer(a: String, b: String): Boolean {
        val x = a.split(".").map { it.toIntOrNull() ?: 0 }
        val y = b.split(".").map { it.toIntOrNull() ?: 0 }
        for (i in 0 until maxOf(x.size, y.size)) {
            val d = x.getOrElse(i) { 0 } - y.getOrElse(i) { 0 }
            if (d != 0) return d > 0
        }
        return false
    }
}

/** Install result. Android wants a confirmation the first time: show it now, or as a notification. */
class UpdateReceiver : BroadcastReceiver() {
    override fun onReceive(c: Context, intent: Intent) {
        when (intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                @Suppress("DEPRECATION")
                val confirm = (if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java)
                    else intent.getParcelableExtra(Intent.EXTRA_INTENT)) ?: return
                if (MainActivity.visible) c.startActivity(confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                else Updater.notify(c, "CryRec update ready", "Tap to install it.", confirm)
            }
            PackageInstaller.STATUS_SUCCESS -> {}
            else -> Prefs(c).lastUpdateError = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE) ?: "install failed"
        }
    }
}

/** After an update: say so once. */
class UpdatedReceiver : BroadcastReceiver() {
    override fun onReceive(c: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        Prefs(c).lastUpdateError = ""
        Updater.schedule(c)
        Updater.notify(c, "CryRec updated to ${Updater.currentVersion(c)}", "Tap to open.",
            Intent(c, MainActivity::class.java))
    }
}

class UpdateJob : JobService() {
    override fun onStartJob(params: JobParameters): Boolean {
        Thread {
            runCatching { Updater.check(this) }
            jobFinished(params, false)
        }.start()
        return true
    }

    override fun onStopJob(params: JobParameters) = true
}
