package com.crymetr.cryrec

import org.json.JSONObject
import java.io.File

/**
 * speakers.json from the server (Speaker 1..N, suggestion, seconds, sample time, voice vector) and
 * renaming them in the transcript files. Same rules as the desktop app (diarize.apply_names):
 * Me / Them / Speaker N can't be used as names, and two speakers given the same name become one.
 */
object Speakers {
    private val RESERVED = Regex("^(Me|Them|Speaker( \\d+)?)$", RegexOption.IGNORE_CASE)

    /** Android's optString turns a JSON null into the text "null"; this doesn't. */
    fun str(o: JSONObject, key: String): String = if (o.isNull(key)) "" else o.optString(key)

    class Entry(val label: String, val info: JSONObject) {
        val name: String get() = str(info, "name")
        val shown: String get() = name.ifEmpty { label }
        val seconds: Double get() = info.optDouble("seconds", 0.0)
        val sample: Double get() = info.optDouble("sample", 0.0)
        val suggest: String get() = str(info, "suggest")
        val score: Double get() = info.optDouble("score", 0.0)
    }

    fun load(dir: File): List<Entry> = try {
        val sp = JSONObject(File(dir, "speakers.json").readText()).getJSONObject("speakers")
        sp.keys().asSequence().map { Entry(it, sp.getJSONObject(it)) }.toList()
    } catch (e: Exception) { emptyList() }

    fun cleanName(s: String): String? {
        val n = s.replace(":", " ").replace(Regex("\\s+"), " ").trim().take(40)
        return if (n.isEmpty() || RESERVED.matches(n)) null else n
    }

    /** names: label -> typed name. Returns the entries that got a (new) name, for the server's voice memory. */
    fun apply(dir: File, names: Map<String, String>): List<Entry> {
        val file = File(dir, "speakers.json")
        val root = JSONObject(file.readText())
        val sp = root.getJSONObject("speakers")
        val labels = sp.keys().asSequence().toList()
        val final = labels.associateWith { l ->
            (if (l in names) cleanName(names[l] ?: "") else str(sp.getJSONObject(l), "name").ifEmpty { null }) ?: l
        }
        val renames = LinkedHashMap<String, String>()
        val out = LinkedHashMap<File, String>()  // everything is staged, then swapped in together
        val owner = HashMap<String, String>()
        for (l in labels) {
            val info = sp.getJSONObject(l)
            val shown = final.getValue(l)
            val old = str(info, "name").ifEmpty { l }
            if (shown != old) renames[old] = shown
            val keep = owner[shown]
            if (keep != null && shown != l) {  // same name as an earlier speaker: fold into it
                val k = sp.getJSONObject(keep)
                val a = k.getDouble("seconds"); val b = info.getDouble("seconds")
                val va = k.getJSONArray("vec"); val vb = info.getJSONArray("vec")
                val v = DoubleArray(va.length()) { va.getDouble(it) * a + vb.getDouble(it) * b }
                val norm = Math.sqrt(v.sumOf { it * it }) + 1e-9
                k.put("vec", org.json.JSONArray(v.map { it / norm }))
                k.put("seconds", a + b)
                sp.remove(l)
                continue
            }
            owner[shown] = l
            if (shown != l) info.put("name", shown) else info.put("name", JSONObject.NULL)
        }
        if (renames.isNotEmpty()) {
            val alt = renames.keys.sortedByDescending { it.length }.joinToString("|") { Regex.escape(it) }
            val line = Regex("^(\\[\\d\\d:\\d\\d:\\d\\d\\] )($alt): ")
            val srtLine = Regex("^($alt): ")
            for (name in listOf("transcript.txt", "transcript.raw.txt")) {
                val f = File(dir, name)
                if (f.exists()) out[f] = (f.readText().split("\n").joinToString("\n") { ln ->
                    line.replace(ln) { m -> m.groupValues[1] + renames.getValue(m.groupValues[2]) + ": " }
                })
            }
            val srt = File(dir, "transcript.srt")
            if (srt.exists()) out[srt] = (srt.readText().split("\n").joinToString("\n") { ln ->
                srtLine.replace(ln) { m -> renames.getValue(m.groupValues[1]) + ": " }
            })
        }
        out[file] = root.toString(1)
        writeAll(out)
        val named = renames.values.toSet()
        return sp.keys().asSequence().map { Entry(it, sp.getJSONObject(it)) }
            .filter { it.name.isNotEmpty() && it.shown in named }.toList()
    }

    /** Write every file to a temp first, then swap them in, so a full disk leaves the old versions. */
    private fun writeAll(files: Map<File, String>) {
        val tmps = files.mapValues { (f, text) -> File(f.parentFile, f.name + ".tmp").apply { writeText(text) } }
        try {
            for ((f, tmp) in tmps) if (!tmp.renameTo(f)) { f.delete(); check(tmp.renameTo(f)) { "can't replace ${f.name}" } }
        } finally {
            tmps.values.forEach { if (it.exists()) it.delete() }
        }
    }
}
