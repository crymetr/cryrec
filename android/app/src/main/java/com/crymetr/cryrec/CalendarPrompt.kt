package com.crymetr.cryrec

import android.Manifest
import android.app.AlarmManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.ContentUris
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.provider.CalendarContract

/**
 * "Your meeting is starting. Record it?" from the phone's own calendar (whatever accounts sync into it).
 * One inexact alarm for the next meeting start: without the exact-alarm permission Android stretches the
 * window to 10 minutes, so the alarm is set from 4 minutes before to 6 minutes after the start. When it fires,
 * the calendar is read again, so a moved or deleted meeting doesn't prompt.
 * Never records by itself: Record goes through QuickRecActivity like the widget.
 */
object CalendarPrompt {
    private const val CHANNEL = "calendar_prompt"
    private const val NOTIFY_ID = 43
    private const val LOOK_AHEAD = 24 * 3600_000L
    private const val EARLY = 4 * 60_000L      // the alarm window opens this long before the start
    private const val WINDOW = 10 * 60_000L    // Android's minimum for an inexact window
    private const val LATE = 7 * 60_000L       // still "starting" until 7 minutes after

    data class Event(val id: Long, val title: String, val begin: Long, val end: Long)

    fun allowed(c: Context) = Prefs(c).calendarPrompt &&
        c.checkSelfPermission(Manifest.permission.READ_CALENDAR) == PackageManager.PERMISSION_GRANTED

    /** Timed (not all-day), not declined events overlapping [from, to), earliest first. */
    fun events(c: Context, from: Long, to: Long): List<Event> {
        val uri = CalendarContract.Instances.CONTENT_URI.buildUpon().also {
            ContentUris.appendId(it, from); ContentUris.appendId(it, to)
        }.build()
        val cols = arrayOf(CalendarContract.Instances.EVENT_ID, CalendarContract.Instances.TITLE,
            CalendarContract.Instances.BEGIN, CalendarContract.Instances.END, CalendarContract.Instances.ALL_DAY,
            CalendarContract.Instances.SELF_ATTENDEE_STATUS)
        val out = mutableListOf<Event>()
        c.contentResolver.query(uri, cols, null, null, CalendarContract.Instances.BEGIN + " ASC")?.use { cur ->
            while (cur.moveToNext()) {
                if (cur.getInt(4) == 1) continue
                if (cur.getInt(5) == CalendarContract.Attendees.ATTENDEE_STATUS_DECLINED) continue
                out += Event(cur.getLong(0), cur.getString(1) ?: "", cur.getLong(2), cur.getLong(3))
            }
        }
        return out
    }

    /** Set the alarm for the next meeting start (or clear it). Safe to call often. */
    fun schedule(c: Context) {
        val am = c.getSystemService(AlarmManager::class.java)
        val pi = PendingIntent.getBroadcast(c, 0, Intent(c, CalendarReceiver::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        am.cancel(pi)
        if (!allowed(c)) return
        val now = System.currentTimeMillis()
        val asked = c.getSharedPreferences("cryrec", Context.MODE_PRIVATE).getString("calendar_asked", "")
        val next = runCatching { events(c, now - LATE, now + LOOK_AHEAD) }.getOrDefault(emptyList())
            .firstOrNull { it.begin > now - LATE && "${it.id}@${it.begin}" != asked } ?: return
        am.setWindow(AlarmManager.RTC_WAKEUP, maxOf(now + 5_000, next.begin - EARLY), WINDOW, pi)
    }

    /** Called by the alarm: prompt for a meeting starting about now, then set the next alarm. */
    fun fire(c: Context) {
        try {
            if (!allowed(c)) return
            val now = System.currentTimeMillis()
            val ev = events(c, now - LATE, now + EARLY + 60_000)
                .firstOrNull { it.begin in (now - LATE)..(now + EARLY + 60_000) } ?: return
            val asked = c.getSharedPreferences("cryrec", Context.MODE_PRIVATE)
            val key = "${ev.id}@${ev.begin}"
            if (asked.getString("calendar_asked", "") == key) return
            // mark it handled even while recording, or schedule() would re-arm for the same event right away
            asked.edit().putString("calendar_asked", key).commit()
            if (RecorderService.recording == null) prompt(c, ev)
        } finally {
            schedule(c)
        }
    }

    private fun prompt(c: Context, ev: Event) {
        val nm = c.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Meeting starts", NotificationManager.IMPORTANCE_HIGH))
        val record = PendingIntent.getActivity(c, 5,
            Intent(c, QuickRecActivity::class.java).putExtra(QuickRecActivity.EXTRA_START_ONLY, true)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = Notification.Builder(c, CHANNEL)
            .setSmallIcon(R.drawable.ic_mic)
            .setContentTitle(ev.title.ifBlank { "Your meeting" } + " is starting")
            .setContentText("Record it? Tell the others that you are recording.")
            .setContentIntent(record)
            .setAutoCancel(true)
            .setTimeoutAfter(15 * 60_000L)
            .addAction(Notification.Action.Builder(null, "Record", record).build())
            .build()
        nm.notify(NOTIFY_ID, n)
    }
}

/** Alarm for a meeting start, and boot / app update: re-arm the alarm. */
class CalendarReceiver : BroadcastReceiver() {
    override fun onReceive(c: Context, intent: Intent) {
        val pending = goAsync()
        Thread {
            try {
                if (intent.action == null) CalendarPrompt.fire(c) else CalendarPrompt.schedule(c)
            } finally {
                pending.finish()
            }
        }.start()
    }
}
