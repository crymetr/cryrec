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
    private const val SLACK = 60_000L
    private const val RECHECK = 6 * 3600_000L  // re-read the calendar at least this often
    private const val LATE = 10 * 60_000L      // still "starting" until 10 minutes after (alarms can be late)

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
        val asked = asked(c)
        // only events past the current "starting" window: fire() deals with everything inside it in one go
        val next = runCatching { events(c, now - LATE, now + LOOK_AHEAD) }.getOrDefault(emptyList())
            .firstOrNull { it.begin > now - LATE && key(it) !in asked }
        // a meeting already starting: fire in a moment (fire() marks everything in its window, so no loop).
        // Also look again every few hours: a meeting added later, or nothing in the next day.
        val at = minOf(next?.let { maxOf(now + 5_000, it.begin - EARLY) } ?: Long.MAX_VALUE, now + RECHECK)
        // inexact, but allowed while the phone dozes on the desk
        am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pi)
    }

    private fun key(e: Event) = "${e.id}@${e.begin}"

    private fun asked(c: Context): List<String> =
        c.getSharedPreferences("cryrec", Context.MODE_PRIVATE).getString("calendar_asked", "")!!
            .split("|").filter { it.isNotEmpty() }

    /** Called by the alarm: prompt for a meeting starting about now, then set the next alarm. */
    fun fire(c: Context) {
        try {
            if (!allowed(c)) return
            val now = System.currentTimeMillis()
            val asked = asked(c)
            // every meeting starting about now (two calendars often hold the same one): one prompt for all
            val evs = events(c, now - LATE, now + EARLY + SLACK)
                .filter { it.begin in (now - LATE)..(now + EARLY + SLACK) && key(it) !in asked }
            if (evs.isEmpty()) return
            // marked handled even while recording, so schedule() moves on to later meetings
            c.getSharedPreferences("cryrec", Context.MODE_PRIVATE).edit()
                .putString("calendar_asked", (asked + evs.map { key(it) }).takeLast(30).joinToString("|")).commit()
            if (RecorderService.recording == null) prompt(c, evs.first())
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
