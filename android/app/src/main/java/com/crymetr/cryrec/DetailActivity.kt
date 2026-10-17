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
    override fun onPause() { ui.removeCallbacks(tick); super.onPause() }

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
        status.text = dur + when (s) {
            "recording" -> "Recording…"
            "queued" -> "Waiting to send"
            "working" -> st.optString("detail", "working").replaceFirstChar { it.uppercase() } + "…"
            "done" -> "Done"
            "failed" -> "Failed: " + st.optString("error")
            else -> if (Prefs(this).configured) "Saved on this phone" else "Saved on this phone. Set your server in Settings to send it."
        }
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
