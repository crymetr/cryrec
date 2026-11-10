package com.crymetr.cryrec

import android.Manifest
import android.app.Activity
import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.SystemClock
import android.view.View
import android.widget.RemoteViews

/** Home screen button: one tap records, the next tap stops. */
class QuickRecWidget : AppWidgetProvider() {
    override fun onUpdate(context: Context, manager: AppWidgetManager, ids: IntArray) = update(context)

    companion object {
        /** Redraw every placed widget from the recorder's current state. */
        fun update(context: Context) {
            val manager = AppWidgetManager.getInstance(context)
            val ids = manager.getAppWidgetIds(ComponentName(context, QuickRecWidget::class.java))
            if (ids.isEmpty()) return
            val rec = RecorderService.recording != null
            val v = RemoteViews(context.packageName, R.layout.widget_rec)
            v.setImageViewResource(R.id.widget_button, if (rec) R.drawable.ic_stop else R.drawable.ic_mic)
            v.setInt(R.id.widget_button, "setBackgroundResource",
                if (rec) R.drawable.widget_circle_rec else R.drawable.widget_circle)
            v.setContentDescription(R.id.widget_button, if (rec) "Stop recording" else "Record")
            v.setViewVisibility(R.id.widget_label, if (rec) View.GONE else View.VISIBLE)
            v.setViewVisibility(R.id.widget_timer, if (rec) View.VISIBLE else View.GONE)
            v.setChronometer(R.id.widget_timer, RecorderService.startedElapsed, null, rec)
            // An activity, not a service: Android only lets a foreground app open the mic.
            val tap = PendingIntent.getActivity(context, 0,
                Intent(context, QuickRecActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            v.setOnClickPendingIntent(R.id.widget_root, tap)
            manager.updateAppWidget(ids, v)
        }
    }
}

/** Invisible trampoline for the widget: toggles recording and closes. Asks for the mic in the app if needed. */
class QuickRecActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        when {
            RecorderService.recording != null -> RecorderService.send(this, RecorderService.ACTION_STOP)
            checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED ->
                RecorderService.send(this, RecorderService.ACTION_RECORD)
            else -> startActivity(Intent(this, MainActivity::class.java).putExtra(MainActivity.EXTRA_ASK_MIC, true))
        }
        finish()
        overridePendingTransition(0, 0)
    }
}
