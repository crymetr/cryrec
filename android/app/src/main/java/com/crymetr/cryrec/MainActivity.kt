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
    private lateinit var levels: View
    private lateinit var meter: LevelMeter
    private lateinit var levelText: TextView
    private lateinit var gainValue: TextView
    private var quietSince = 0L
    private var lastClip = 0
    private var lastLimit = 0
    private var clipSeen = 0L
    private var limitSeen = 0L
    private val meterTick = object : Runnable {
        override fun run() { updateMeter(); ui.postDelayed(this, 66) }
    }
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
        listen = findViewById(R.id.listen)
        listen.setOnClickListener { toggleListen() }
        listen.setOnLongClickListener { pickListenHours(); true }
        levels = findViewById(R.id.levels)
        levelText = findViewById(R.id.level_text)
        gainValue = findViewById(R.id.gain_value)
        meter = LevelMeter(this)
        findViewById<android.widget.FrameLayout>(R.id.meter_box).addView(meter)
        findViewById<View>(R.id.gain_down).setOnClickListener { stepGain(-1) }
        findViewById<View>(R.id.gain_up).setOnClickListener { stepGain(1) }
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
        showListen()
        refreshListen = { runOnUiThread { showListen() } }
        Thread { runCatching { CalendarPrompt.schedule(applicationContext) } }.start()
        ui.post(tick)
        ui.post(meterTick)
        // Pick up recordings that were queued while the server was not set or the app was closed.
        if (Prefs(this).configured && Session.all(this).any { it.status == "queued" })
            RecorderService.send(this, RecorderService.ACTION_PROCESS)
    }

    override fun onPause() {
        visible = false
        refreshListen = null
        ui.removeCallbacks(tick)
        ui.removeCallbacks(meterTick)
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

    private lateinit var listen: TextView

    /** Listen mode: asks "Record this?" when it hears a conversation. Long press = how long it stays on. */
    private fun toggleListen() {
        if (ListenService.active) { ListenService.stop(this); ui.postDelayed({ showListen() }, 300); return }
        val need = listOf(Manifest.permission.RECORD_AUDIO, Manifest.permission.POST_NOTIFICATIONS)
            .filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (need.isNotEmpty()) { requestPermissions(need.toTypedArray(), REQ_LISTEN); return }
        ListenService.start(this)
        ui.postDelayed({ showListen() }, 300)
    }

    private fun pickListenHours() {
        val options = intArrayOf(1, 2, 3, 4, 6, 8)
        android.app.AlertDialog.Builder(this).setTitle("Listen mode stays on for")
            .setItems(options.map { "$it hour" + if (it > 1) "s" else "" }.toTypedArray()) { _, i ->
                Prefs(this).listenHours = options[i]
                if (ListenService.active) ListenService.start(this)  // restart the timer
                showListen()
            }.show()
    }

    private fun showListen() {
        listen.text = if (ListenService.active)
            "Listening until " + java.text.SimpleDateFormat("HH:mm", java.util.Locale.getDefault())
                .format(java.util.Date(ListenService.endsAtWall)) + "  ·  tap to stop"
        else "Listen for conversations (${Prefs(this).listenHours} h)"
        listen.alpha = if (ListenService.active) 1f else 0.75f
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
        if (requestCode == REQ_LISTEN) {
            // it only asks through notifications, so it needs both
            if (listOf(Manifest.permission.RECORD_AUDIO, Manifest.permission.POST_NOTIFICATIONS)
                    .all { checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED }) {
                ListenService.start(this)
                ui.postDelayed({ showListen() }, 300)
            } else showBanner("Listen mode needs the microphone and notifications (it asks you with a notification).", "listen") {
                startActivity(Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.fromParts("package", packageName, null)))
            }
        }
        if (requestCode == REQ_RECORD) {
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED)
                RecorderService.send(this, RecorderService.ACTION_RECORD)
            else showBanner("CryRec needs the microphone permission to record.") {
                startActivity(Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.fromParts("package", packageName, null)))
            }
        }
    }

    /** Gain steps 0..24 dB; applies to the running recording right away and is kept for the next one. */
    private fun stepGain(dir: Int) {
        val p = Prefs(this)
        val i = (Capture.GAINS.indexOf(p.gainDb).coerceAtLeast(0) + dir).coerceIn(0, Capture.GAINS.size - 1)
        p.gainDb = Capture.GAINS[i]
        RecorderService.capture?.gainDb = p.gainDb
        updateMeter()
    }

    private fun updateMeter() {
        val c = RecorderService.capture
        levels.visibility = if (c == null) View.GONE else View.VISIBLE
        if (c == null) { quietSince = 0L; return }
        val now = System.currentTimeMillis()
        val rms = c.rmsDb
        val peak = c.peakDb
        meter.set(rms, peak)
        gainValue.text = if (c.gainDb == 0) "0 dB" else "+${c.gainDb} dB"
        if (c.clipWindows != lastClip) { lastClip = c.clipWindows; clipSeen = now }
        if (c.limitWindows != lastLimit) { lastLimit = c.limitWindows; limitSeen = now }
        if (rms >= LevelMeter.QUIET) quietSince = 0L else if (quietSince == 0L) quietSince = now
        fun f(db: Double) = if (db <= -120) "−∞" else "%.0f".format(db).replace("-", "−")
        val line = "${c.micLabel}  ·  peak ${f(peak)} dBFS  ·  RMS ${f(rms)}"
        val (warn, color) = when {
            now - clipSeen < 3000 -> "The mic itself is overloaded. Move the phone back; more gain can't fix this." to R.color.accent
            now - limitSeen < 3000 -> "Loud parts are being held down. If it happens a lot, lower the gain." to R.color.warn
            quietSince != 0L && now - quietSince > 8000 && c.gainDb < Capture.GAINS.last() ->
                "Very quiet for a while. If people are talking, turn the gain up." to R.color.warn
            else -> null to R.color.muted
        }
        levelText.text = if (warn == null) line else "$line\n$warn"
        levelText.setTextColor(getColor(color))
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
        private const val REQ_LISTEN = 3
        /** Set while this screen is visible, so listen mode can update the button. */
        @Volatile var refreshListen: (() -> Unit)? = null
        const val EXTRA_ASK_MIC = "ask_mic"
        const val REPO = "crymetr/cryrec"
        /** For the updater: show the install prompt directly only while the app is on screen. */
        @Volatile var visible = false
    }
}
