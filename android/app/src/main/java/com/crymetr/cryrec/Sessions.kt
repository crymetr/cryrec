package com.crymetr.cryrec

import android.content.Context
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * One recording = one folder in app storage, like the desktop app:
 * meeting.m4a, transcript.txt (+ .srt, .raw.txt), notes.md, state.json.
 */
class Session(val dir: File) {
    val name: String get() = dir.name
    val audio get() = File(dir, "meeting.m4a")
    val transcript get() = File(dir, "transcript.txt")
    val srt get() = File(dir, "transcript.srt")
    val rawTranscript get() = File(dir, "transcript.raw.txt")
    val notes get() = File(dir, "notes.md")
    private val stateFile get() = File(dir, "state.json")

    /** status: recording | queued | working | done | failed | saved (no server set) */
    fun state(): JSONObject = try { JSONObject(stateFile.readText()) } catch (e: Exception) { JSONObject() }

    fun update(vararg pairs: Pair<String, Any?>) = synchronized(LOCK) {
        val s = state()
        for ((k, v) in pairs) if (v == null) s.remove(k) else s.put(k, v)
        val tmp = File(dir, "state.tmp")
        tmp.writeText(s.toString())
        tmp.renameTo(stateFile)
    }

    val status: String get() = state().optString("status", "saved")

    fun readOr(f: File) = if (f.exists()) f.readText() else ""

    companion object {
        private val LOCK = Any()

        fun root(context: Context) = File(context.filesDir, "recordings").apply { mkdirs() }

        fun create(context: Context): Session {
            val stamp = SimpleDateFormat("yyyy-MM-dd HH-mm-ss", Locale.US).format(Date())
            return Session(File(root(context), stamp).apply { mkdirs() })
        }

        fun all(context: Context): List<Session> =
            (root(context).listFiles { f -> f.isDirectory } ?: emptyArray()).sortedByDescending { it.name }.map { Session(it) }

        fun find(context: Context, name: String) = File(root(context), name).takeIf { it.isDirectory }?.let { Session(it) }
    }
}

fun formatDuration(seconds: Long): String =
    if (seconds >= 3600) "%d:%02d:%02d".format(seconds / 3600, seconds / 60 % 60, seconds % 60)
    else "%d:%02d".format(seconds / 60, seconds % 60)
