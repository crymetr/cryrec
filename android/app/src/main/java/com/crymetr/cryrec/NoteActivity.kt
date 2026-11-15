package com.crymetr.cryrec

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.OpenableColumns
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

/**
 * One voice note: the clean text (not the audio), its to-do items with where each one goes, and things added later
 * (photos, links, text). Send = one share to the PC, where the watcher files each item in its place.
 */
class NoteActivity : Activity() {
    private lateinit var session: Session
    private lateinit var content: LinearLayout
    private val ui = Handler(Looper.getMainLooper())
    private var lastKey = ""
    private var showTranscript = false
    private var mp: android.media.MediaPlayer? = null
    private val tick = object : Runnable {
        override fun run() { refresh(); ui.postDelayed(this, 1000) }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        session = Session.find(this, intent.getStringExtra("name") ?: "") ?: return finish()
        content = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(16), dp(16), dp(16), dp(16)) }
        setContentView(android.widget.FrameLayout(this).apply {
            fitsSystemWindows = true
            addView(ScrollView(this@NoteActivity).apply { addView(content) })
        })
    }

    override fun onResume() { super.onResume(); lastKey = ""; ui.post(tick) }
    override fun onPause() {
        ui.removeCallbacks(tick)
        mp?.release(); mp = null
        super.onPause()
    }

    private fun refresh() {
        if (!session.dir.exists()) return finish()
        val st = session.state()
        val s = st.optString("status", "saved")
        val note = session.noteJson()
        val extras = session.extrasList()
        val key = listOf(s, st.optString("detail"), note.toString().length, extras.length(), showTranscript, mp?.isPlaying)
            .joinToString("|")
        if (key == lastKey) return
        lastKey = key
        content.removeAllViews()

        content.addView(text(session.title(), 20f, R.color.text))
        val dur = if (st.has("duration")) formatDuration(st.optLong("duration")) + "  ·  " else ""
        val when_ = session.name.replace(Regex("""(\d{2})-(\d{2})-\d{2}$"""), "$1:$2")
        content.addView(text(when_ + "  ·  " + dur + when (s) {
            "recording" -> "Recording…"
            "queued" -> "Waiting to send"
            "working" -> st.optString("detail", "working").replaceFirstChar { it.uppercase() } + "…"
            "done" -> "Note ready"
            "failed" -> "Failed: " + st.optString("error")
            else -> "Saved on this phone. Set your server in Settings to send it."
        }, 13f, if (s == "failed") R.color.accent else R.color.muted).apply { setPadding(0, dp(2), 0, dp(10)) })

        // the note itself (or the raw transcript on request)
        val shown = if (showTranscript) session.readOr(session.transcript) else note.optString("text")
        content.addView(card(text(shown.ifEmpty { "Nothing here yet." }, 15f, if (shown.isEmpty()) R.color.muted else R.color.text)
            .apply { setTextIsSelectable(true); setLineSpacing(0f, 1.15f) }))

        // to-do items
        val items = note.optJSONArray("items") ?: JSONArray()
        if (s == "done") {
            content.addView(header("To do"))
            val box = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
            if (items.length() == 0) box.addView(text("No tasks in this note. Add one with + Task.", 14f, R.color.muted))
            for (i in 0 until items.length()) {
                if (i > 0) box.addView(line())
                box.addView(itemRow(items, i))
            }
            content.addView(card(box))
        }

        // things added after recording
        if (extras.length() > 0) {
            content.addView(header("Added"))
            val box = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
            for (i in 0 until extras.length()) {
                if (i > 0) box.addView(line())
                box.addView(extraRow(extras.getJSONObject(i), i))
            }
            content.addView(card(box))
        }

        val actions = Rows()
        if (s == "done" || s == "saved" || s == "failed") {
            actions.add(pill("📷 Photo") { pickPhoto() })
            actions.add(pill("🔗 Link") { askText("Add a link", "https://…", true) { session.addExtra("link", it); changed() } })
            actions.add(pill("✏️ Text") { askText("Add text", "Anything to go with this note", false) { session.addExtra("text", it); changed() } })
        }
        if (s == "done") {
            actions.add(pill("+ Task") { editItem(items, -1) })
            val unsent = (0 until items.length()).count { !items.getJSONObject(it).has("sent") }
            if (unsent > 0 && Prefs(this).configured) actions.add(pill("Send $unsent ▸") { send() })
        }
        if ((s == "failed" || s == "saved") && Prefs(this).configured && session.audio.exists())
            actions.add(pill(if (s == "saved") "Send to server" else "Retry") {
                session.update("status" to "queued", "error" to null)
                RecorderService.send(this, RecorderService.ACTION_PROCESS)
            })
        if (s == "working" || s == "queued") actions.add(pill("Cancel") {
            if (s == "queued") session.update("status" to "failed", "error" to "Cancelled.")
            else RecorderService.send(this, RecorderService.ACTION_CANCEL, session.name)
        })
        if (s != "recording") actions.add(pill("⋯") { more(s) })
        content.addView(actions.box)
    }

    private fun itemRow(items: JSONArray, i: Int): View {
        val it = items.getJSONObject(i)
        val sent = it.has("sent")
        val row = LinearLayout(this).apply { gravity = Gravity.CENTER_VERTICAL; setPadding(0, dp(6), 0, dp(6)) }
        row.addView(text((if (sent) "✓  " else "") + it.optString("text"), 15f, if (sent) R.color.muted else R.color.text).apply {
            if (!sent) setOnClickListener { editItem(items, i) }
        }, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        val target = pill(TARGETS[it.optString("target")] ?: "CryTodo") { pickTarget(items, i) }
        target.isEnabled = !sent
        target.alpha = if (sent) 0.5f else 1f
        (target.layoutParams as LinearLayout.LayoutParams).apply { marginStart = dp(8); marginEnd = 0 }
        row.addView(target)
        return row
    }

    private fun extraRow(e: JSONObject, i: Int): View {
        val row = LinearLayout(this).apply { gravity = Gravity.CENTER_VERTICAL; setPadding(0, dp(6), 0, dp(6)) }
        val v = e.optString("value")
        when (e.optString("kind")) {
            "photo" -> {
                row.addView(ImageView(this).apply {
                    val opts = BitmapFactory.Options().apply { inSampleSize = 8 }
                    setImageBitmap(BitmapFactory.decodeFile(File(session.attach, v).path, opts))
                    scaleType = ImageView.ScaleType.CENTER_CROP
                }, LinearLayout.LayoutParams(dp(56), dp(56)).apply { marginEnd = dp(12) })
                row.addView(text(v, 14f, R.color.muted))
            }
            "link" -> row.addView(text("🔗  $v", 14f, R.color.text))
            else -> row.addView(text("✏️  $v", 14f, R.color.text))
        }
        row.setOnLongClickListener {
            AlertDialog.Builder(this).setMessage("Remove this from the note?").setPositiveButton("Remove") { _, _ ->
                val a = session.extrasList()
                if (e.optString("kind") == "photo") File(session.attach, v).delete()
                a.remove(i)
                File(session.dir, "extras.json").writeText(a.toString(1))
                changed()
            }.setNegativeButton("Keep", null).show()
            true
        }
        return row
    }

    /** Edit or delete one task (i = -1 adds a new one). The AI can split or miss things; this is the fix. */
    private fun editItem(items: JSONArray, i: Int) {
        val edit = EditText(this).apply {
            setText(if (i >= 0) items.getJSONObject(i).optString("text") else "")
            hint = "What needs doing"
            minLines = 2
        }
        val b = AlertDialog.Builder(this).setTitle(if (i >= 0) "Task" else "New task")
            .setView(LinearLayout(this).apply { setPadding(dp(20), dp(8), dp(20), 0); addView(edit, LinearLayout.LayoutParams(-1, -2)) })
            .setPositiveButton("Save") { _, _ ->
                val t = edit.text.toString().trim()
                if (t.isEmpty()) return@setPositiveButton
                val n = session.noteJson()
                val a = n.optJSONArray("items") ?: JSONArray().also { n.put("items", it) }
                if (i >= 0) a.getJSONObject(i).put("text", t) else a.put(JSONObject().put("text", t).put("target", "crytodo"))
                session.saveNote(n)
                changed()
            }
            .setNegativeButton("Cancel", null)
        if (i >= 0) b.setNeutralButton("Delete") { _, _ ->
            val n = session.noteJson()
            n.optJSONArray("items")?.remove(i)
            session.saveNote(n)
            changed()
        }
        b.show()
    }

    private fun pickTarget(items: JSONArray, i: Int) {
        val keys = TARGETS.keys.toList()
        AlertDialog.Builder(this).setTitle("Where does it go?")
            .setItems(keys.map { TARGET_HINTS[it] }.toTypedArray()) { _, k ->
                val n = session.noteJson()
                n.optJSONArray("items")?.optJSONObject(i)?.put("target", keys[k])
                session.saveNote(n)
                changed()
            }.show()
    }

    private fun send() {
        val note = session.noteJson()
        val items = note.optJSONArray("items") ?: JSONArray()
        val unsent = (0 until items.length()).filter { !items.getJSONObject(it).has("sent") }
        val hasClaude = unsent.any { items.getJSONObject(it).optString("target") == "claude" }
        if (!hasClaude) return doSend(unsent, "brief")
        AlertDialog.Builder(this).setTitle("Claude Code tasks")
            .setItems(arrayOf("Write a brief and a CryTodo card (I start it)", "Start working on it now")) { _, k ->
                doSend(unsent, if (k == 0) "brief" else "now")
            }.show()
    }

    private fun doSend(indexes: List<Int>, claudeMode: String) {
        val p = Prefs(this)
        Toast.makeText(this, "Sending…", Toast.LENGTH_SHORT).show()
        Thread {
            val zip = File(cacheDir, "note-${System.nanoTime()}.zip")
            val r = runCatching {
                val note = session.noteJson()
                val items = note.optJSONArray("items") ?: JSONArray()
                val extras = session.extrasList()
                val photos = (0 until extras.length()).map { extras.getJSONObject(it) }
                    .filter { it.optString("kind") == "photo" }.map { File(session.attach, it.optString("value")) }.filter { it.exists() }
                val vn = JSONObject().put("title", session.title()).put("text", note.optString("text"))
                    .put("recorded", session.name).put("claude_mode", claudeMode)
                    .put("items", JSONArray().apply { for (i in indexes) put(JSONObject()
                        .put("text", items.getJSONObject(i).optString("text"))
                        .put("target", items.getJSONObject(i).optString("target", "crytodo"))) })
                    .put("extras", JSONArray().apply {
                        for (i in 0 until extras.length()) extras.getJSONObject(i).takeIf { it.optString("kind") != "photo" }?.let { put(it) }
                    })
                    .put("photos", JSONArray(photos.map { it.name }))
                ZipOutputStream(zip.outputStream()).use { z ->
                    z.putNextEntry(ZipEntry("share.json"))
                    z.write(JSONObject().put("kind", "voice-note").put("voice_note", vn).put("subject", session.title())
                        .put("text", note.optString("text")).put("note", "").toString().toByteArray())
                    z.closeEntry()
                    for (f in photos) { z.putNextEntry(ZipEntry("files/" + f.name)); f.inputStream().use { it.copyTo(z) }; z.closeEntry() }
                }
                val id = Api(p).share(zip)
                val n = session.noteJson()
                val a = n.optJSONArray("items") ?: JSONArray()
                for (i in indexes) a.optJSONObject(i)?.put("sent", id)
                session.saveNote(n)
            }
            zip.delete()
            runOnUiThread {
                r.onSuccess { Toast.makeText(this, "Sent to your PC", Toast.LENGTH_SHORT).show() }
                    .onFailure { Toast.makeText(this, "Could not send: ${it.message}", Toast.LENGTH_LONG).show() }
                changed()
            }
        }.start()
    }

    private fun more(status: String) {
        val opts = mutableListOf<Pair<String, () -> Unit>>()
        if (session.transcript.exists()) opts.add((if (showTranscript) "Show the note" else "Show the transcript") to {
            showTranscript = !showTranscript; changed()
        })
        if (session.audio.exists()) opts.add((if (mp?.isPlaying == true) "Stop the audio" else "Play the audio") to { togglePlay() })
        if (status == "done") opts.add("Write the note again" to {
            session.note.delete()
            session.update("status" to "queued")
            RecorderService.send(this, RecorderService.ACTION_PROCESS)
        })
        opts.add("Share the text" to {
            startActivity(Intent.createChooser(Intent(Intent.ACTION_SEND).apply {
                type = "text/plain"
                putExtra(Intent.EXTRA_SUBJECT, session.title())
                putExtra(Intent.EXTRA_TEXT, session.noteJson().optString("text").ifEmpty { session.readOr(session.transcript) })
            }, null))
        })
        if (status != "working") opts.add("Delete" to {
            AlertDialog.Builder(this).setMessage("Delete this voice note, its audio and what you added?")
                .setPositiveButton("Delete") { _, _ -> session.dir.deleteRecursively(); finish() }
                .setNegativeButton("Keep", null).show()
        })
        AlertDialog.Builder(this).setItems(opts.map { it.first }.toTypedArray()) { _, i -> opts[i].second() }.show()
    }

    private fun togglePlay() {
        mp?.let { it.release(); mp = null; changed(); return }
        mp = runCatching {
            android.media.MediaPlayer().apply {
                setDataSource(session.audio.path); prepare(); start()
                setOnCompletionListener { release(); mp = null; changed() }
            }
        }.getOrNull()
        changed()
    }

    private fun pickPhoto() {
        AlertDialog.Builder(this).setItems(arrayOf("Take a photo", "Pick from the gallery")) { _, k ->
            if (k == 1) startActivityForResult(Intent(Intent.ACTION_GET_CONTENT).setType("image/*")
                .addCategory(Intent.CATEGORY_OPENABLE).putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true), REQ_PICK)
            else camera()
        }.show()
    }

    /** The phone's camera through Google's document scanner (no file provider needed); it also crops paper and screens. */
    private fun camera() {
        val options = com.google.mlkit.vision.documentscanner.GmsDocumentScannerOptions.Builder()
            .setScannerMode(com.google.mlkit.vision.documentscanner.GmsDocumentScannerOptions.SCANNER_MODE_FULL)
            .setResultFormats(com.google.mlkit.vision.documentscanner.GmsDocumentScannerOptions.RESULT_FORMAT_JPEG)
            .setGalleryImportAllowed(false)
            .setPageLimit(10)
            .build()
        com.google.mlkit.vision.documentscanner.GmsDocumentScanning.getClient(options).getStartScanIntent(this)
            .addOnSuccessListener { sender ->
                runCatching { startIntentSenderForResult(sender, REQ_SCAN, null, 0, 0, 0) }
                    .onFailure { Toast.makeText(this, "Can't open the camera: ${it.message}", Toast.LENGTH_LONG).show() }
            }
            .addOnFailureListener { Toast.makeText(this, "The camera needs Google Play services: ${it.message}", Toast.LENGTH_LONG).show() }
    }

    @Deprecated("Activity result without AndroidX")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (resultCode != RESULT_OK) return
        val uris = mutableListOf<Uri>()
        if (requestCode == REQ_SCAN) com.google.mlkit.vision.documentscanner.GmsDocumentScanningResult
            .fromActivityResultIntent(data)?.pages?.mapTo(uris) { it.imageUri }
        if (requestCode == REQ_PICK) {
            data?.clipData?.let { c -> (0 until c.itemCount).mapTo(uris) { c.getItemAt(it).uri } } ?: data?.data?.let { uris.add(it) }
        }
        if (uris.isEmpty()) return
        Thread {
            session.attach.mkdirs()
            for (u in uris) runCatching {
                val ext = if ((contentResolver.getType(u) ?: "").contains("png")) "png" else "jpg"
                var n = (session.attach.listFiles()?.size ?: 0) + 1
                var f: File
                do { f = File(session.attach, "photo_%02d.$ext".format(n++)) } while (f.exists())
                contentResolver.openInputStream(u)?.use { inp -> f.outputStream().use { inp.copyTo(it) } }
                session.addExtra("photo", f.name)
            }
            runOnUiThread { changed() }
        }.start()
    }

    private fun askText(title: String, hint: String, link: Boolean, done: (String) -> Unit) {
        val edit = EditText(this).apply {
            this.hint = hint
            if (link) inputType = android.text.InputType.TYPE_TEXT_VARIATION_URI or android.text.InputType.TYPE_CLASS_TEXT
            else minLines = 2
        }
        AlertDialog.Builder(this).setTitle(title)
            .setView(LinearLayout(this).apply { setPadding(dp(20), dp(8), dp(20), 0); addView(edit, LinearLayout.LayoutParams(-1, -2)) })
            .setPositiveButton("Add") { _, _ -> edit.text.toString().trim().takeIf { it.isNotEmpty() }?.let(done) }
            .setNegativeButton("Cancel", null).show()
    }

    private fun changed() { lastKey = ""; refresh() }

    /** The buttons, three to a row. */
    private inner class Rows {
        val box = LinearLayout(this@NoteActivity).apply { orientation = LinearLayout.VERTICAL; setPadding(0, dp(14), 0, 0) }
        private var row: LinearLayout? = null
        fun add(v: View) {
            val r = row?.takeIf { it.childCount < 3 } ?: LinearLayout(this@NoteActivity).apply {
                setPadding(0, 0, 0, dp(8)); box.addView(this); row = this
            }
            r.addView(v)
        }
    }

    private fun header(s: String) = text(s, 13f, R.color.muted).apply {
        setTypeface(typeface, android.graphics.Typeface.BOLD)
        setPadding(dp(4), dp(18), 0, dp(6))
    }

    private fun card(v: View) = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setBackgroundResource(R.drawable.card)
        setPadding(dp(14), dp(10), dp(14), dp(10))
        addView(v, LinearLayout.LayoutParams(-1, -2))
    }

    private fun line() = View(this).apply {
        setBackgroundColor(getColor(R.color.line))
        layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 1)
    }

    private fun text(s: String, size: Float, color: Int) = TextView(this).apply {
        text = s; textSize = size; setTextColor(getColor(color))
    }

    private fun pill(label: String, onClick: () -> Unit) = Button(this, null, 0, android.R.style.Widget_DeviceDefault_Button_Borderless).apply {
        text = label
        isAllCaps = false
        textSize = 14f
        setTextColor(getColor(R.color.text))
        setBackgroundResource(R.drawable.pill)
        minHeight = dp(40); minimumHeight = dp(40)
        setPadding(dp(14), 0, dp(14), 0)
        layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, dp(40)).apply { marginEnd = dp(8) }
        setOnClickListener { onClick() }
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    companion object {
        private const val REQ_PICK = 21
        private const val REQ_SCAN = 22
        val TARGETS = linkedMapOf("tracker" to "METS Tracker", "crytodo" to "CryTodo", "claude" to "Claude Code")
        val TARGET_HINTS = mapOf(
            "tracker" to "METS Action Tracker (work)",
            "crytodo" to "CryTodo (personal)",
            "claude" to "Claude Code (software work)")
    }
}
