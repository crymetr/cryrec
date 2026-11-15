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
    val scans get() = File(dir, "scans")
    val handwritten get() = File(dir, "handwritten.md")
    /** Voice notes: note.json {title, text, items: [{text, target, sent}]}, added photos in attach/, extras.json. */
    val note get() = File(dir, "note.json")
    val attach get() = File(dir, "attach")
    val extras get() = File(dir, "extras.json")
    /** meeting | note */
    val kind: String get() = state().optString("kind", "meeting")

    fun noteJson(): JSONObject = try { JSONObject(note.readText()) } catch (e: Exception) { JSONObject() }

    fun saveNote(j: JSONObject) = synchronized(LOCK) {
        val tmp = File(dir, "note.tmp")
        tmp.writeText(j.toString(1))
        tmp.renameTo(note)
    }

    /** Things added to a voice note after it was recorded: [{kind: photo|link|text, value}] (photo value = file in attach/). */
    fun extrasList(): org.json.JSONArray = try { org.json.JSONArray(extras.readText()) } catch (e: Exception) { org.json.JSONArray() }

    fun addExtra(kind: String, value: String) = synchronized(LOCK) {
        val a = extrasList().put(JSONObject().put("kind", kind).put("value", value))
        val tmp = File(dir, "extras.tmp")
        tmp.writeText(a.toString(1))
        tmp.renameTo(extras)
    }

    /** Title for lists: the note's own title, else the date. */
    fun title(): String = noteJson().optString("title").ifEmpty { name.replace(Regex("""(\d{2})-(\d{2})-\d{2}$"""), "$1:$2") }

    /** Scanned pages not read yet (handwritten.md has a "### Page N" heading for every page read). */
    fun unreadScans(): List<File> {
        val done = Regex("^### Page (\\d+)", RegexOption.MULTILINE).findAll(readOr(handwritten))
            .map { it.groupValues[1].toInt() }.toSet()
        return (scans.listFiles { f -> f.name.startsWith("page_") } ?: emptyArray()).sortedBy { pageNo(it) }
            .filter { pageNo(it) !in done }
    }

    fun pageNo(f: File) = f.name.removePrefix("page_").substringBefore('.').toIntOrNull() ?: 0
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

        /** The process died mid-way: a half-written m4a can't be read, a half-done job can be redone (or picked up). */
        fun recoverInterrupted(context: Context) {
            if (RecorderService.recording != null || RecorderService.processing.get()) return
            for (s in all(context)) when (s.status) {
                "recording" -> s.update("status" to "failed", "error" to "The recording was interrupted.")
                "working" -> s.update("status" to "queued", "detail" to null)
            }
        }

        fun find(context: Context, name: String) = File(root(context), name).takeIf { it.isDirectory }?.let { Session(it) }
    }
}

fun formatDuration(seconds: Long): String =
    if (seconds >= 3600) "%d:%02d:%02d".format(seconds / 3600, seconds / 60 % 60, seconds % 60)
    else "%d:%02d".format(seconds / 60, seconds % 60)
