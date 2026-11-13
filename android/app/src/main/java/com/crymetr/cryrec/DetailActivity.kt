package com.crymetr.cryrec

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

/** One recording: notes / transcript, share, retry, cancel, delete. */
class DetailActivity : Activity() {
    private lateinit var session: Session
    private lateinit var status: TextView
    private lateinit var body: TextView
    private lateinit var tabNotes: Button
    private lateinit var tabTranscript: Button
    private lateinit var actions: LinearLayout
    private lateinit var player: LinearLayout
    private lateinit var playButton: Button
    private lateinit var seek: android.widget.SeekBar
    private lateinit var playTime: TextView
    private var mp: android.media.MediaPlayer? = null
    private val playTick = object : Runnable {
        override fun run() { showPosition(); if (mp?.isPlaying == true) ui.postDelayed(this, 250) }
    }
    private var showNotes = true
    private var lastKey = ""
    private val ui = Handler(Looper.getMainLooper())
    private val tick = object : Runnable {
        override fun run() { refresh(); ui.postDelayed(this, 1000) }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        session = Session.find(this, intent.getStringExtra("name") ?: "") ?: return finish()
        val pad = dp(16)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, 0)
        }
        root.addView(TextView(this).apply {
            text = session.name.replace(Regex("""(\d{2})-(\d{2})-\d{2}$"""), "$1:$2")
            textSize = 20f
            setTextColor(getColor(R.color.text))
        })
        status = TextView(this).apply { textSize = 13f; setPadding(0, dp(2), 0, dp(10)) }
        root.addView(status)

        // Listen to the recording: play/pause, position, seek.
        player = LinearLayout(this).apply { gravity = Gravity.CENTER_VERTICAL; setPadding(0, 0, 0, dp(10)) }
        playButton = pill("▶  Listen") { togglePlay() }
        seek = android.widget.SeekBar(this).apply {
            setOnSeekBarChangeListener(object : android.widget.SeekBar.OnSeekBarChangeListener {
                override fun onProgressChanged(sb: android.widget.SeekBar, v: Int, fromUser: Boolean) {
                    if (fromUser) { prepare()?.seekTo(v); showPosition() }
                }
                override fun onStartTrackingTouch(sb: android.widget.SeekBar) {}
                override fun onStopTrackingTouch(sb: android.widget.SeekBar) {}
            })
        }
        playTime = TextView(this).apply { textSize = 13f; setTextColor(getColor(R.color.muted)); fontFeatureSettings = "tnum" }
        player.addView(playButton)
        player.addView(seek, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        player.addView(playTime)
        root.addView(player)

        val tabs = LinearLayout(this)
        tabNotes = pill("Notes") { showNotes = true; lastKey = ""; refresh() }
        tabTranscript = pill("Transcript") { showNotes = false; lastKey = ""; refresh() }
        tabs.addView(tabNotes); tabs.addView(tabTranscript)
        root.addView(tabs)

        body = TextView(this).apply {
            textSize = 15f
            setTextColor(getColor(R.color.text))
            setTextIsSelectable(true)
            setLineSpacing(0f, 1.15f)
            setPadding(dp(14), dp(14), dp(14), dp(14))
            setBackgroundResource(R.drawable.card)
        }
        root.addView(ScrollView(this).apply {
            addView(body)
            setPadding(0, dp(10), 0, dp(10))
            clipToPadding = false
        }, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f))

        actions = LinearLayout(this).apply { gravity = Gravity.END; setPadding(0, 0, 0, dp(12)) }
        root.addView(actions)
        // fitsSystemWindows replaces padding, so it goes on a wrapper.
        setContentView(android.widget.FrameLayout(this).apply { fitsSystemWindows = true; addView(root) })
    }

    override fun onResume() { super.onResume(); ui.post(tick) }
    override fun onPause() {
        ui.removeCallbacks(tick)
        ui.removeCallbacks(playTick)
        mp?.release(); mp = null
        playButton.text = "▶  Listen"
        super.onPause()
    }

    /** The player for meeting.m4a, created on first use. Null if the file can't be played. */
    private fun prepare(): android.media.MediaPlayer? {
        mp?.let { return it }
        return runCatching {
            android.media.MediaPlayer().apply {
                setDataSource(session.audio.path)
                prepare()
                setOnCompletionListener { playButton.text = "▶  Listen"; seekTo(0); showPosition() }
            }
        }.onFailure { playTime.text = "Can't play this file" }.getOrNull()?.also {
            mp = it
            seek.max = it.duration
        }
    }

    private fun togglePlay() {
        val m = prepare() ?: return
        if (m.isPlaying) { m.pause(); playButton.text = "▶  Listen" }
        else { m.start(); playButton.text = "❚❚  Pause"; ui.post(playTick) }
    }

    private fun showPosition() {
        val m = mp ?: return
        seek.progress = m.currentPosition
        playTime.text = formatDuration(m.currentPosition / 1000L) + " / " + formatDuration(m.duration / 1000L)
    }

    private fun refresh() {
        if (!session.dir.exists()) return finish()
        val st = session.state()
        val s = st.optString("status", "saved")
        val notes = session.readOr(session.notes)
        val transcript = session.readOr(session.transcript)
        val key = listOf(s, st.optString("detail"), showNotes, notes.length, transcript.length).joinToString("|")
        if (key == lastKey) return
        lastKey = key

        val dur = if (st.has("duration")) formatDuration(st.optLong("duration")) + "  ·  " else ""
        player.visibility = if (s != "recording" && session.audio.exists()) View.VISIBLE else View.GONE
        if (mp == null && player.visibility == View.VISIBLE) playTime.text = ""
        val mic = if (st.has("mic")) "\n" + st.optString("mic") +
            (st.optInt("gain_db").let { if (it > 0) ", +$it dB gain" else "" }) +
            (st.optDouble("clipped", 0.0).let { if (it >= 1) ", mic overloaded for ${it.toInt()} s" else "" }) else ""
        status.text = dur + when (s) {
            "recording" -> "Recording…"
            "queued" -> "Waiting to send"
            "working" -> st.optString("detail", "working").replaceFirstChar { it.uppercase() } + "…"
            "done" -> "Done"
            "failed" -> "Failed: " + st.optString("error")
            else -> if (Prefs(this).configured) "Saved on this phone" else "Saved on this phone. Set your server in Settings to send it."
        } + mic
        status.setTextColor(getColor(if (s == "failed") R.color.accent else R.color.muted))

        if (notes.isEmpty()) showNotes = false
        tabNotes.visibility = if (notes.isEmpty()) View.GONE else View.VISIBLE
        tabTranscript.visibility = if (transcript.isEmpty()) View.GONE else View.VISIBLE
        tabNotes.alpha = if (showNotes) 1f else 0.55f
        tabTranscript.alpha = if (showNotes) 0.55f else 1f
        val shown = if (showNotes) notes else transcript
        body.text = if (shown.isEmpty()) "Nothing here yet." else if (showNotes) markdown(shown) else shown
        body.setTextColor(getColor(if (shown.isEmpty()) R.color.muted else R.color.text))

        actions.removeAllViews()
        if (shown.isNotEmpty()) actions.addView(pill("Share") { share(shown) })
        if (s == "working" || s == "queued") actions.addView(pill("Cancel") {
            if (s == "queued") session.update("status" to "failed", "error" to "Cancelled.")
            else RecorderService.send(this, RecorderService.ACTION_CANCEL, session.name)
        })
        if ((s == "failed" || s == "saved") && Prefs(this).configured && session.audio.exists())
            actions.addView(pill(if (s == "saved") "Send" else "Retry") {
                session.update("status" to "queued", "error" to null)
                RecorderService.send(this, RecorderService.ACTION_PROCESS)
            })
        if (s == "done" && Speakers.load(session.dir).isNotEmpty()) actions.addView(pill("Names") { showNames() })
        if (s == "done" && Prefs(this).notesEngine != "off") actions.addView(pill("Redo notes") {
            session.notes.delete()
            session.update("status" to "queued")
            RecorderService.send(this, RecorderService.ACTION_PROCESS)
        })
        if (s != "recording" && s != "working") actions.addView(pill("Delete") { confirmDelete() })
    }

    /** Just enough Markdown for the notes: headings, bullets, checkboxes, **bold**. */
    private fun markdown(md: String): CharSequence {
        val out = android.text.SpannableStringBuilder()
        val bold = { from: Int -> out.setSpan(android.text.style.StyleSpan(android.graphics.Typeface.BOLD), from, out.length, 0) }
        for (line in md.trimEnd().lines()) {
            val heading = Regex("^#{1,6} ").find(line)
            if (heading != null) {
                val start = out.length
                out.append(line.substring(heading.value.length).replace("**", ""))
                bold(start)
                out.setSpan(android.text.style.RelativeSizeSpan(1.12f), start, out.length, 0)
            } else {
                out.append(when {
                    line.startsWith("- [ ] ") -> "☐  " + line.substring(6)
                    line.startsWith("- [x] ", true) -> "☑  " + line.substring(6)
                    line.startsWith("- ") || line.startsWith("* ") -> "•  " + line.substring(2)
                    else -> line
                }.split("**").let { parts ->  // odd parts were between ** pairs
                    val b = android.text.SpannableStringBuilder()
                    parts.forEachIndexed { i, part ->
                        val st = b.length
                        b.append(part)
                        if (i % 2 == 1) b.setSpan(android.text.style.StyleSpan(android.graphics.Typeface.BOLD), st, b.length, 0)
                    }
                    b
                })
            }
            out.append('\n')
        }
        return out
    }

    private fun share(text: String) {
        startActivity(Intent.createChooser(Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_SUBJECT, (if (showNotes) "Meeting notes " else "Transcript ") + session.name)
            putExtra(Intent.EXTRA_TEXT, text)
        }, null))
    }

    /** "Who is who?": play each speaker's sample, type a name. Same name twice = same person. */
    private fun showNames() {
        val entries = Speakers.load(session.dir)
        val list = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(20), dp(8), dp(20), 0) }
        list.addView(TextView(this).apply {
            text = "Play a sample, then type the name. Give two speakers the same name if they are one person."
            textSize = 13f; setTextColor(getColor(R.color.muted)); setPadding(0, 0, 0, dp(8))
        })
        val fields = LinkedHashMap<String, android.widget.EditText>()
        for (e in entries) {
            val row = LinearLayout(this).apply { gravity = Gravity.CENTER_VERTICAL; setPadding(0, dp(4), 0, dp(4)) }
            row.addView(pill("▶") {
                val m = prepare() ?: return@pill
                m.seekTo(((e.sample + 1) * 1000).toInt())
                if (!m.isPlaying) togglePlay() else showPosition()
            })
            val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
            col.addView(TextView(this).apply {
                text = e.label + "  ·  " + formatDuration(e.seconds.toLong()) +
                    if (e.name.isEmpty() && e.suggest.isNotEmpty()) "  ·  suggested" else ""
                textSize = 12f; setTextColor(getColor(R.color.muted))
            })
            val edit = android.widget.EditText(this).apply {
                setText(e.name.ifEmpty { e.suggest })
                hint = "Name"
                setSingleLine()
                textSize = 15f
                setTextColor(getColor(R.color.text))
            }
            col.addView(edit)
            row.addView(col, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
            list.addView(row)
            fields[e.label] = edit
        }
        AlertDialog.Builder(this)
            .setTitle("Who is who?")
            .setView(ScrollView(this).apply { addView(list) })
            .setPositiveButton("Save names") { _, _ ->
                val typed = fields.mapValues { it.value.text.toString() }
                val p = Prefs(this)
                Thread {  // file rewrites and uploads off the UI thread
                    val named = runCatching { Speakers.apply(session.dir, typed) }
                    runOnUiThread {
                        named.onFailure {
                            android.widget.Toast.makeText(this, "Can't save: ${it.message}", android.widget.Toast.LENGTH_LONG).show()
                        }.onSuccess { list ->
                            lastKey = ""
                            refresh()
                            if (list.isNotEmpty() && p.notesEngine != "off" && session.notes.exists())
                                AlertDialog.Builder(this).setMessage("Names saved. Rewrite the notes with these names?")
                                    .setPositiveButton("Rewrite") { _, _ ->
                                        session.notes.delete()
                                        session.update("status" to "queued")
                                        RecorderService.send(this, RecorderService.ACTION_PROCESS)
                                    }
                                    .setNegativeButton("Not now", null).show()
                        }
                    }
                    if (p.configured) for (e in named.getOrDefault(emptyList()))  // the server remembers the voices
                        runCatching { Api(p).voice(e.shown, e.info.getJSONArray("vec"), e.seconds) }
                }.start()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun confirmDelete() {
        AlertDialog.Builder(this)
            .setMessage("Delete this recording, its transcript and notes?")
            .setPositiveButton("Delete") { _, _ -> session.dir.deleteRecursively(); finish() }
            .setNegativeButton("Keep", null)
            .show()
    }

    private fun pill(label: String, onClick: () -> Unit) = Button(this, null, 0, android.R.style.Widget_DeviceDefault_Button_Borderless).apply {
        text = label
        isAllCaps = false
        textSize = 14f
        setTextColor(getColor(R.color.text))
        setBackgroundResource(R.drawable.pill)
        minHeight = dp(40); minimumHeight = dp(40)
        setPadding(dp(16), 0, dp(16), 0)
        layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, dp(40)).apply { marginEnd = dp(8) }
        setOnClickListener { onClick() }
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()
}
