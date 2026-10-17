package com.crymetr.cryrec

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.MediaRecorder
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import java.io.File

/** One dictation at a time: record a short clip, send it to the server, hand back the text. Main thread only. */
class Dictation(private val context: Context, private val listener: Listener) {
    interface Listener {
        fun onTick(seconds: Long) {}
        fun onSending() {}
        fun onText(text: String)
        fun onError(message: String)
        /** Back to idle without text (cancelled, too short, silence). */
        fun onIdle() {}
    }

    private val ui = Handler(Looper.getMainLooper())
    private var recorder: MediaRecorder? = null
    private var startedAt = 0L
    private var generation = 0  // bumps on cancel so a late answer is dropped
    private val clip = File(context.cacheDir, "dictate-${System.identityHashCode(this)}.m4a")

    val recording get() = recorder != null
    var sending = false
        private set

    private val tick = object : Runnable {
        override fun run() {
            if (recorder == null) return
            val s = (SystemClock.elapsedRealtime() - startedAt) / 1000
            if (s >= MAX_SECONDS) return stopAndSend()
            listener.onTick(s)
            ui.postDelayed(this, 500)
        }
    }

    fun micAllowed() = context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

    /** Start recording. False if the mic is missing, blocked or busy (onError says why). */
    fun start(): Boolean {
        if (recorder != null || sending) return false
        warmUp(context)
        val r = MediaRecorder(context)
        try {
            r.setAudioSource(MediaRecorder.AudioSource.VOICE_RECOGNITION)
            r.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            r.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            r.setAudioChannels(1)
            r.setAudioSamplingRate(16_000)
            r.setAudioEncodingBitRate(32_000)
            r.setOutputFile(clip.path)
            r.prepare()
            r.start()
        } catch (e: Exception) {
            r.release()
            listener.onError("Microphone is busy")
            return false
        }
        recorder = r
        busy = true
        startedAt = SystemClock.elapsedRealtime()
        ui.post(tick)
        return true
    }

    /** Stop the recorder; true if the clip is usable. */
    private fun stopRecorder(): Boolean {
        val r = recorder ?: return false
        recorder = null
        ui.removeCallbacks(tick)
        val ok = try { r.stop(); true } catch (e: RuntimeException) { false }  // too short
        r.release()
        return ok && SystemClock.elapsedRealtime() - startedAt > 400
    }

    fun cancel() {
        generation++
        busy = false
        val active = recorder != null || sending
        stopRecorder()
        clip.delete()
        sending = false
        if (active) listener.onIdle()
    }

    fun stopAndSend() {
        if (!stopRecorder()) { clip.delete(); busy = false; listener.onIdle(); return }
        val audio = clip.readBytes()
        clip.delete()
        val p = Prefs(context)
        val gen = generation
        sending = true
        listener.onSending()
        Thread {
            val result = runCatching { Api(p).dictate(audio, p.language, p.polish, p.vocabulary) }
            ui.post {
                if (gen != generation) return@post
                sending = false
                busy = false
                result.onSuccess { text ->
                    val t = text.trim()
                    if (t.none { it.isLetterOrDigit() }) listener.onIdle()  // silence comes back as "" or "..."
                    else listener.onText(t)
                }
                result.onFailure { listener.onError("Failed: ${it.message ?: it.javaClass.simpleName}") }
            }
        }.start()
    }

    companion object {
        const val MAX_SECONDS = 300L
        /** A dictation is recording or waiting for text (the updater waits for it). */
        @Volatile var busy = false
        private var lastWarm = 0L

        /** Load the server's speech model ahead of time (at most once a minute). */
        fun warmUp(context: Context) {
            val p = Prefs(context)
            val now = SystemClock.elapsedRealtime()
            if (!p.configured || now - lastWarm < 60_000) return
            lastWarm = now
            Thread { runCatching { Api(p).warm() } }.start()
        }
    }
}
