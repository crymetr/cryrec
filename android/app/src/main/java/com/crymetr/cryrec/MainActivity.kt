package com.crymetr.cryrec

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.TextView
import org.json.JSONObject

class MainActivity : Activity() {
    private val ui = Handler(Looper.getMainLooper())
    private lateinit var list: LinearLayout
    private lateinit var record: ImageButton
    private lateinit var timer: TextView
    private lateinit var banner: TextView
    private var lastListKey = ""
    private val tick = object : Runnable {
        override fun run() { refresh(); ui.postDelayed(this, 1000) }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        list = findViewById(R.id.list)
        record = findViewById(R.id.record)
        timer = findViewById(R.id.timer)
        banner = findViewById(R.id.banner)
        findViewById<View>(R.id.settings).setOnClickListener { startActivity(Intent(this, SettingsActivity::class.java)) }
        record.setOnClickListener { toggleRecording() }
        recoverInterrupted()
        Updater.schedule(this)
        if (System.currentTimeMillis() - Prefs(this).lastUpdateCheck > 6 * 3600_000L)
            Thread { runCatching { Updater.check(this) } }.start()
        if (intent.getBooleanExtra(EXTRA_ASK_MIC, false) &&
            checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED)
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), REQ_MIC)
    }

    override fun onResume() {
        super.onResume()
        visible = true
        lastListKey = ""
        ui.post(tick)
        // Pick up recordings that were queued while the server was not set or the app was closed.
        if (Prefs(this).configured && Session.all(this).any { it.status == "queued" })
            RecorderService.send(this, RecorderService.ACTION_PROCESS)
    }

    override fun onPause() {
        visible = false
        ui.removeCallbacks(tick)
        super.onPause()
    }

    /** The process died mid-way: a half-written m4a can't be read, a half-done job can be redone. */
    private fun recoverInterrupted() {
        if (RecorderService.recording != null || RecorderService.processing.get()) return
        for (s in Session.all(this)) when (s.status) {
            "recording" -> s.update("status" to "failed", "error" to "The recording was interrupted.")
            "working" -> s.update("status" to "queued", "detail" to null)
        }
    }

    private fun toggleRecording() {
        if (RecorderService.recording != null) {
            RecorderService.send(this, RecorderService.ACTION_STOP)
            ui.postDelayed({ refresh() }, 300)
            return
        }
        val need = listOf(Manifest.permission.RECORD_AUDIO, Manifest.permission.POST_NOTIFICATIONS)
            .filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (need.isNotEmpty()) {
            requestPermissions(need.toTypedArray(), REQ_RECORD)
            return
        }
        RecorderService.send(this, RecorderService.ACTION_RECORD)
        ui.postDelayed({ refresh() }, 300)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        if (requestCode == REQ_RECORD) {
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED)
                RecorderService.send(this, RecorderService.ACTION_RECORD)
            else showBanner("CryRec needs the microphone permission to record.") {
                startActivity(Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.fromParts("package", packageName, null)))
            }
        }
    }

    private fun refresh() {
        val recName = RecorderService.recording
        if (recName != null) {
            record.setImageResource(R.drawable.ic_stop)
            record.contentDescription = "Stop"
            val started = Session.find(this, recName)?.state()?.optLong("started") ?: 0L
            timer.text = formatDuration(((System.currentTimeMillis() - started) / 1000).coerceAtLeast(0))
        } else {
            record.setImageResource(R.drawable.ic_mic)
            record.contentDescription = "Record"
            timer.text = "Tap to record"
        }
        if (!Prefs(this).configured && banner.tag != "update")
            showBanner("Set your server in Settings to get transcripts and notes. Recordings are kept until then.") {
                startActivity(Intent(this, SettingsActivity::class.java))
            }
        else if (banner.tag == "setup") banner.visibility = View.GONE

        val sessions = Session.all(this)
        val rows = sessions.map { it to it.state() }
        val key = rows.joinToString("|") { (s, st) -> s.name + st.optString("status") + st.optString("detail") }
        if (key == lastListKey) return
        lastListKey = key
        list.removeAllViews()
        if (rows.isEmpty()) {
            list.addView(text("No recordings yet.", 14f, R.color.muted).apply { setPadding(dp(16), dp(16), dp(16), dp(16)) })
            return
        }
        rows.forEachIndexed { i, (s, st) ->
            if (i > 0) list.addView(View(this).apply {
                setBackgroundColor(getColor(R.color.line))
                layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 1)
            })
            list.addView(row(s, st))
        }
    }

    private fun row(s: Session, st: JSONObject): View {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(12), dp(16), dp(12))
            val tv = TypedValue()
            theme.resolveAttribute(android.R.attr.selectableItemBackground, tv, true)
            setBackgroundResource(tv.resourceId)
            setOnClickListener {
                startActivity(Intent(this@MainActivity, DetailActivity::class.java).putExtra("name", s.name))
            }
        }
        box.addView(text(s.name.replace(Regex("""(\d{2})-(\d{2})-\d{2}$"""), "$1:$2"), 16f, R.color.text))
        val (line, color) = statusLine(st)
        box.addView(text(line, 13f, color).apply { maxLines = 2 })
        return box
    }

    private fun statusLine(st: JSONObject): Pair<String, Int> {
        val dur = if (st.has("duration")) formatDuration(st.optLong("duration")) + "  ·  " else ""
        return when (st.optString("status")) {
            "recording" -> "Recording…" to R.color.accent
            "queued" -> dur + "Waiting to send" to R.color.muted
            "working" -> dur + st.optString("detail", "working").replaceFirstChar { it.uppercase() } + "…" to R.color.muted
            "done" -> dur + "Transcript ready" to R.color.ok
            "failed" -> dur + "Failed: " + st.optString("error") to R.color.accent
            else -> dur + "Saved on this phone" to R.color.muted
        }
    }

    private fun text(s: String, size: Float, color: Int) = TextView(this).apply {
        text = s
        textSize = size
        setTextColor(getColor(color))
    }

    private fun showBanner(msg: String, tag: String = "setup", onClick: () -> Unit) {
        banner.text = msg
        banner.tag = tag
        banner.gravity = Gravity.START
        banner.visibility = View.VISIBLE
        banner.setOnClickListener { onClick() }
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    companion object {
        private const val REQ_RECORD = 1
        private const val REQ_MIC = 2
        const val EXTRA_ASK_MIC = "ask_mic"
        const val REPO = "crymetr/cryrec"
        /** For the updater: show the install prompt directly only while the app is on screen. */
        @Volatile var visible = false
    }
}
