package com.crymetr.cryrec

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.OpenableColumns
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import org.json.JSONObject
import java.io.File
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

/**
 * "Send to my PC": anything shared from another app (text, a link, files) plus an optional note
 * goes to the CryRec server, which saves it in its share folder (share_dir in server.json).
 */
class ShareActivity : Activity() {
    private var text = ""
    private var subject = ""
    private val uris = mutableListOf<Uri>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        readIntent(intent)
        val pad = dp(20)
        val box = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(pad, pad, pad, dp(12)) }
        box.addView(TextView(this).apply { text = "Send to my PC"; textSize = 20f })
        box.addView(TextView(this).apply {
            text = summary()
            textSize = 14f
            maxLines = 6
            alpha = 0.7f
            setPadding(0, dp(8), 0, dp(8))
        })
        val note = EditText(this).apply {
            hint = "What should happen with it? (optional)"
            minLines = 2
            maxLines = 6
        }
        box.addView(note)
        val status = TextView(this).apply { textSize = 13f; visibility = View.GONE }
        box.addView(status)
        val buttons = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.END }
        val cancel = Button(this, null, android.R.attr.borderlessButtonStyle).apply { text = "Cancel" }
        val send = Button(this, null, android.R.attr.borderlessButtonStyle).apply { text = "Send" }
        cancel.setOnClickListener { finish() }
        buttons.addView(cancel)
        buttons.addView(send)
        box.addView(buttons)
        setContentView(box)

        fun show(msg: String, error: Boolean) {
            status.text = msg
            status.setTextColor(getColor(if (error) R.color.accent else R.color.muted))
            status.visibility = View.VISIBLE
        }
        val prefs = Prefs(this)
        when {
            !prefs.configured -> { show("Set your server in CryRec Settings first.", true); send.isEnabled = false }
            text.isEmpty() && uris.isEmpty() -> { show("Nothing to send.", true); send.isEnabled = false }
        }
        send.setOnClickListener {
            send.isEnabled = false
            note.isEnabled = false
            show("Sending…", false)
            val noteText = note.text.toString().trim()
            Thread {
                val zip = File(cacheDir, "share-${System.nanoTime()}.zip")
                val result = runCatching {
                    pack(zip, noteText)
                    Api(prefs).share(zip)
                }
                zip.delete()
                runOnUiThread {
                    result.onSuccess {
                        Toast.makeText(this, "Sent to your PC", Toast.LENGTH_SHORT).show()
                        finish()
                    }.onFailure {
                        show(it.message ?: it.javaClass.simpleName, true)
                        send.isEnabled = true
                        note.isEnabled = true
                    }
                }
            }.start()
        }
    }

    @Suppress("DEPRECATION")
    private fun readIntent(i: Intent) {
        text = i.getCharSequenceExtra(Intent.EXTRA_TEXT)?.toString() ?: ""
        subject = i.getStringExtra(Intent.EXTRA_SUBJECT) ?: ""
        when (i.action) {
            Intent.ACTION_SEND -> i.getParcelableExtra<Uri>(Intent.EXTRA_STREAM)?.let { uris.add(it) }
            Intent.ACTION_SEND_MULTIPLE -> i.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM)?.let { uris.addAll(it) }
        }
        if (uris.isEmpty()) i.clipData?.let { c -> (0 until c.itemCount).mapNotNullTo(uris) { c.getItemAt(it).uri } }
    }

    private fun summary(): String {
        val parts = mutableListOf<String>()
        if (subject.isNotEmpty()) parts.add(subject)
        if (text.isNotEmpty()) parts.add(text)
        if (uris.isNotEmpty()) parts.add(uris.joinToString("\n") { "📎 " + name(it) })
        return parts.joinToString("\n")
    }

    private fun name(uri: Uri): String = runCatching {
        contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use {
            if (it.moveToFirst()) it.getString(0) else null
        }
    }.getOrNull() ?: uri.lastPathSegment?.substringAfterLast('/') ?: "file"

    private fun pack(zip: File, note: String) {
        ZipOutputStream(zip.outputStream()).use { z ->
            z.putNextEntry(ZipEntry("share.json"))
            z.write(JSONObject().put("text", text).put("subject", subject).put("note", note).toString().toByteArray())
            z.closeEntry()
            for (u in uris) {
                val n = name(u)
                z.putNextEntry(ZipEntry("files/" + n.replace('/', '_')))
                (contentResolver.openInputStream(u) ?: throw ApiException("Could not read $n")).use { it.copyTo(z) }
                z.closeEntry()
            }
        }
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()
}
