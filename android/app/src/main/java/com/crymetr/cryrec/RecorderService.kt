package com.crymetr.cryrec

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.MediaRecorder
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Records the phone mic to meeting.m4a, then sends every queued recording to the server:
 * transcript, optional cleanup, optional notes. Stays in the foreground while it works.
 */
class RecorderService : Service() {
    private var recorder: MediaRecorder? = null
    private var session: Session? = null
    private var startedAt = 0L
    private var wakeLock: PowerManager.WakeLock? = null
    private val worker = Executors.newSingleThreadExecutor()
    @Volatile private var workingOn: String? = null
    @Volatile private var workText = ""

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_RECORD -> startRecording()
            ACTION_STOP -> stopRecording()
            ACTION_CANCEL -> intent.getStringExtra(EXTRA_NAME)?.let { cancelled.add(it) }
        }
        processQueue()
        refreshForeground()
        return START_NOT_STICKY
    }

    private fun startRecording() {
        if (recorder != null) return
        val s = Session.create(this)
        val r = MediaRecorder(this)
        try {
            r.setAudioSource(MediaRecorder.AudioSource.MIC)
            r.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            r.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            r.setAudioChannels(1)
            r.setAudioSamplingRate(32_000)
            r.setAudioEncodingBitRate(48_000)
            r.setOutputFile(s.audio.path)
            r.prepare()
            r.start()
        } catch (e: Exception) {
            r.release()
            s.update("status" to "failed", "error" to "The microphone is busy or blocked (${e.message}).")
            return
        }
        recorder = r
        session = s
        startedAt = SystemClock.elapsedRealtime()
        recording = s.name
        s.update("status" to "recording", "started" to System.currentTimeMillis())
        wakeLock = (getSystemService(POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "CryRec:record").apply { acquire(6 * 3600_000L) }
    }

    private fun stopRecording() {
        val r = recorder ?: return
        val s = session!!
        val seconds = (SystemClock.elapsedRealtime() - startedAt) / 1000
        try { r.stop() } catch (e: RuntimeException) { /* stop right after start: file has no audio */ }
        r.release()
        recorder = null
        session = null
        recording = null
        wakeLock?.let { if (it.isHeld) it.release() }
        if (!s.audio.exists() || s.audio.length() < 1000) {
            s.update("status" to "failed", "error" to "Nothing was recorded.", "duration" to seconds)
            return
        }
        s.update("status" to if (Prefs(this).configured) "queued" else "saved", "duration" to seconds)
    }

    /** Run the queue on the worker thread (one at a time). */
    private fun processQueue() {
        if (!Prefs(this).configured || !processing.compareAndSet(false, true)) return
        worker.execute {
            try {
                while (true) {
                    val next = Session.all(this).lastOrNull { it.status == "queued" } ?: break
                    process(next)
                }
            } finally {
                processing.set(false)
                workingOn = null
                mainExecutor.execute { refreshForeground() }
            }
        }
    }

    private fun process(s: Session) {
        val p = Prefs(this)
        val api = Api(p)
        val isCancelled = { s.name in cancelled }
        fun step(text: String) {
            workText = text
            s.update("status" to "working", "detail" to text)
            mainExecutor.execute { refreshForeground() }
        }
        workingOn = s.name
        try {
            if (!s.transcript.exists()) {
                step("uploading")
                val id = api.submit(s.audio, p.language, s.name, p.vocabulary)
                val j = api.waitJob(id, isCancelled) { step(it.ifEmpty { "transcribing" }) }
                s.srt.writeText(j.optString("srt"))
                s.transcript.writeText(j.optString("txt"))
            }
            if (p.cleanEngine != "off" && !s.rawTranscript.exists()) {
                step("cleaning up the transcript")
                val raw = s.transcript.readText()
                val j = api.waitJob(api.clean(raw, p.vocabulary), isCancelled) { step(it.ifEmpty { "cleaning up" }) }
                s.rawTranscript.writeText(raw)
                s.transcript.writeText(j.optString("txt"))
            }
            if (p.notesEngine != "off" && !s.notes.exists()) {
                step("writing notes")
                val backend = if (p.notesEngine == "server_local") "local" else "claude"
                val id = api.notes(s.transcript.readText(), backend, p.language, p.myName, p.vocabulary)
                val j = api.waitJob(id, isCancelled) { step(it.ifEmpty { "writing notes" }) }
                s.notes.writeText(j.optString("md"))
            }
            s.update("status" to "done", "detail" to null, "error" to null)
        } catch (e: Exception) {
            s.update("status" to "failed", "detail" to null,
                "error" to if (isCancelled()) "Cancelled." else (e.message ?: e.javaClass.simpleName))
        } finally {
            cancelled.remove(s.name)
        }
    }

    private fun refreshForeground() {
        val rec = recorder != null
        val work = processing.get()
        if (!rec && !work) {
            // startForegroundService() demands a startForeground() call even when there is nothing to do.
            startForeground(NOTIFICATION_ID, buildNotification(false), ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
            return
        }
        val type = (if (rec) ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else 0) or
            (if (work) ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC else 0)
        startForeground(NOTIFICATION_ID, buildNotification(rec), type)
    }

    private fun buildNotification(rec: Boolean): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Recording", NotificationManager.IMPORTANCE_LOW))
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val b = Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_rec)
            .setContentIntent(open)
            .setOngoing(true)
            .setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE)
        if (rec) {
            val stop = PendingIntent.getService(this, 1, Intent(this, RecorderService::class.java).setAction(ACTION_STOP),
                PendingIntent.FLAG_IMMUTABLE)
            b.setContentTitle("Recording")
                .setUsesChronometer(true)
                .setWhen(System.currentTimeMillis() - (SystemClock.elapsedRealtime() - startedAt))
                .addAction(Notification.Action.Builder(null, "Stop", stop).build())
            if (processing.get()) b.setContentText("Also working on ${workingOn ?: "the last recording"}")
        } else {
            b.setContentTitle("Working on ${workingOn ?: "recordings"}").setContentText(workText)
        }
        return b.build()
    }

    override fun onDestroy() {
        if (recorder != null) stopRecording()
        worker.shutdownNow()
        super.onDestroy()
    }

    companion object {
        const val ACTION_RECORD = "record"
        const val ACTION_STOP = "stop"
        const val ACTION_PROCESS = "process"
        const val ACTION_CANCEL = "cancel"
        const val EXTRA_NAME = "name"
        private const val CHANNEL = "cryrec"
        private const val NOTIFICATION_ID = 1

        /** Name of the session being recorded right now, for the UI. */
        @Volatile var recording: String? = null
        /** True while the queue is being worked on in this process. */
        val processing = AtomicBoolean(false)
        val cancelled: MutableSet<String> = java.util.Collections.synchronizedSet(mutableSetOf())

        fun send(context: Context, action: String, name: String? = null) {
            context.startForegroundService(Intent(context, RecorderService::class.java).setAction(action)
                .apply { name?.let { putExtra(EXTRA_NAME, it) } })
        }
    }
}
