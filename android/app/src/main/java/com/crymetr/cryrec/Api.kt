package com.crymetr.cryrec

import android.util.Base64
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

class ApiException(message: String) : IOException(message)

/** Client for the CryRec server (server/cryrec_server.py). All calls block; run them off the UI thread. */
class Api(private val base: String, private val key: String) {
    constructor(p: Prefs) : this(p.serverUrl, p.serverKey)

    private fun call(method: String, path: String, body: ByteArray? = null, type: String? = null,
                     readTimeoutMs: Int = 30_000): JSONObject {
        if (base.isEmpty() || key.isEmpty()) throw ApiException("Set the server address and key in Settings.")
        val c = URL(base + path).openConnection() as HttpURLConnection
        try {
            c.requestMethod = method
            c.connectTimeout = 15_000
            c.readTimeout = readTimeoutMs
            c.setRequestProperty("Authorization", "Bearer $key")
            if (body != null) {
                c.doOutput = true
                c.setRequestProperty("Content-Type", type ?: "application/octet-stream")
                c.setFixedLengthStreamingMode(body.size)
                c.outputStream.use { it.write(body) }
            }
            val code = c.responseCode
            val text = (if (code < 400) c.inputStream else c.errorStream)?.use { it.readBytes().decodeToString() } ?: ""
            val json = try { JSONObject(text) } catch (e: Exception) { JSONObject() }
            if (code == 401) throw ApiException("The server key is wrong.")
            if (code >= 400) throw ApiException(json.optString("error").ifEmpty { "Server error $code" })
            return json
        } finally {
            c.disconnect()
        }
    }

    private fun json(path: String, obj: JSONObject, readTimeoutMs: Int = 30_000) =
        call("POST", path, obj.toString().toByteArray(), "application/json", readTimeoutMs)

    fun health(): JSONObject = call("GET", "/health")

    fun warm() { call("POST", "/warm") }

    /** Upload one recording (meeting.m4a). Returns the job id. */
    fun submit(audio: File, language: String, name: String, vocabulary: String): String {
        val buf = ByteArrayOutputStream()
        ZipOutputStream(buf).use { z ->
            z.putNextEntry(ZipEntry("meeting.m4a")); audio.inputStream().use { it.copyTo(z) }; z.closeEntry()
            if (vocabulary.isNotBlank()) {
                z.putNextEntry(ZipEntry("vocab.txt")); z.write(vocabulary.toByteArray()); z.closeEntry()
            }
        }
        val q = "language=" + URLEncoder.encode(language, "UTF-8") + "&name=" + URLEncoder.encode(name, "UTF-8")
        return call("POST", "/jobs?$q", buf.toByteArray(), "application/zip", 120_000).getString("id")
    }

    fun notes(transcript: String, backend: String, language: String, myName: String, vocabulary: String): String =
        json("/notes", JSONObject().put("transcript", transcript).put("backend", backend)
            .put("language", if (language == "Auto") "" else language).put("my_name", myName)
            .put("vocabulary", vocabulary)).getString("id")

    fun clean(transcript: String, vocabulary: String): String =
        json("/clean", JSONObject().put("transcript", transcript).put("vocabulary", vocabulary)).getString("id")

    fun job(id: String): JSONObject = call("GET", "/jobs/$id")

    fun cancel(id: String) { call("DELETE", "/jobs/$id") }

    /** Poll a job until it ends. [onProgress] gets the server's status text. */
    fun waitJob(id: String, cancelled: () -> Boolean, onProgress: (String) -> Unit): JSONObject {
        while (true) {
            if (cancelled()) {
                runCatching { cancel(id) }
                throw ApiException("cancelled")
            }
            val j = job(id)
            when (j.optString("status")) {
                "done" -> return j
                "failed" -> throw ApiException(j.optString("error").ifEmpty { "failed" })
            }
            onProgress(j.optString("text"))
            Thread.sleep(2000)
        }
    }

    /** Short clip to text, answered on the same request. */
    fun dictate(audio: ByteArray, language: String, polish: Boolean, vocabulary: String): String =
        json("/dictate", JSONObject().put("audio_b64", Base64.encodeToString(audio, Base64.NO_WRAP))
            .put("ext", "m4a").put("language", language).put("polish", polish).put("vocabulary", vocabulary),
            100_000).optString("text")
}
