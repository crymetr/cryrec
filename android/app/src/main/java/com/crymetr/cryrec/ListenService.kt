package com.crymetr.cryrec

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Listen mode: every CHECK_EVERY ms the mic is opened for CLIP_MS, SpeechCheck decides on the phone whether
 * someone is talking, and the audio is dropped right away (nothing is saved or sent). When 2 of the last 3
 * checks heard speech, or a call/VoIP app holds the audio, it asks "Record this?" with a notification.
 * It never records by itself. Started by the user (Android only lets a visible app open the mic) and stops
 * by itself after Prefs.listenHours.
 */
class ListenService : Service() {
    private lateinit var thread: HandlerThread
    private lateinit var worker: Handler
    private var wakeLock: PowerManager.WakeLock? = null
    private var endsAt = 0L
    private val recent = ArrayDeque<Boolean>()  // worker thread only
    private var quietUntil = 0L                 // worker thread only
    private var lastCallPrompt = -1L            // worker thread only; -1 = no prompt yet
    @Volatile private var stopping = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        thread = HandlerThread("listen").apply { start() }
        worker = Handler(thread.looper)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> { stopping = true; stopSelf(); return START_NOT_STICKY }
            ACTION_SNOOZE -> {
                nm().cancel(PROMPT_ID)
                if (active) worker.post { quietUntil = SystemClock.elapsedRealtime() + SNOOZE_MS; recent.clear() }
                else stopSelf()
                return START_NOT_STICKY
            }
        }
        stopping = false
        val hours = Prefs(this).listenHours
        endsAt = SystemClock.elapsedRealtime() + hours * 3600_000L
        endsAtWall = System.currentTimeMillis() + hours * 3600_000L
        channel()
        startForeground(ONGOING_ID, ongoing(), ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        active = true
        wakeLock?.let { if (it.isHeld) it.release() }  // a restart (new duration) gets a fresh deadline
        wakeLock = (getSystemService(POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "CryRec:listen").apply { acquire(hours * 3600_000L + 60_000) }
        worker.removeCallbacksAndMessages(null)
        worker.post(::tick)
        MainActivity.refreshListen?.invoke()
        return START_NOT_STICKY
    }

    private fun tick() {
        if (SystemClock.elapsedRealtime() >= endsAt) { stopSelf(); return }
        worker.postDelayed(::tick, CHECK_EVERY)
        // our own recording or dictation has the mic: nothing to detect
        if (RecorderService.recording != null || Dictation.busy) { recent.clear(); return }
        if (SystemClock.elapsedRealtime() < quietUntil) return
        val am = getSystemService(AUDIO_SERVICE) as AudioManager
        if (am.mode == AudioManager.MODE_IN_COMMUNICATION || am.mode == AudioManager.MODE_IN_CALL) {
            // a call is on: the mic is silenced for us anyway; ask once per call (speakerphone/room recording)
            if (lastCallPrompt < 0 || SystemClock.elapsedRealtime() - lastCallPrompt > SNOOZE_MS) {
                lastCallPrompt = SystemClock.elapsedRealtime()
                prompt("A call is on", "Tap Record to record the room (Android can't record the other side).")
            }
            return
        }
        val heard = runCatching { sample() }.getOrNull() ?: return
        if (stopping) return
        recent.addLast(heard)
        while (recent.size > 3) recent.removeFirst()
        if (recent.count { it } >= 2) {
            recent.clear()
            quietUntil = SystemClock.elapsedRealtime() + SNOOZE_MS  // don't nag; Not now extends it too
            prompt("Sounds like a conversation", "Record it? Nothing is recorded until you tap Record.")
        }
    }

    /** Open the mic for CLIP_MS, decide, forget the audio. */
    private fun sample(): Boolean {
        val rate = 16000
        val min = AudioRecord.getMinBufferSize(rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val pcm = ShortArray(rate * CLIP_MS / 1000)
        val rec = AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, rate, AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT, maxOf(min, rate))
        try {
            if (rec.state != AudioRecord.STATE_INITIALIZED) return false
            rec.startRecording()
            var got = 0
            val chunk = rate / 10  // 100 ms reads, so a stop, a recording or a dictation takes the mic at once
            while (got < pcm.size) {
                if (stopping || RecorderService.recording != null || Dictation.busy) return false
                val r = rec.read(pcm, got, minOf(chunk, pcm.size - got))
                if (r <= 0) break
                got += r
            }
            if (got < pcm.size / 2) return false
            return SpeechCheck.check(pcm.copyOf(got), rate).speech
        } finally {
            runCatching { rec.stop() }
            rec.release()
        }
    }

    private fun prompt(title: String, text: String) {
        val record = PendingIntent.getActivity(this, 1,
            Intent(this, QuickRecActivity::class.java).putExtra(QuickRecActivity.EXTRA_START_ONLY, true)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val snooze = PendingIntent.getService(this, 2, Intent(this, ListenService::class.java).setAction(ACTION_SNOOZE),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = Notification.Builder(this, PROMPT_CHANNEL)
            .setSmallIcon(R.drawable.ic_mic)
            .setContentTitle(title)
            .setContentText(text)
            .setContentIntent(record)
            .setAutoCancel(true)
            .setTimeoutAfter(SNOOZE_MS)
            .addAction(Notification.Action.Builder(null, "Record", record).build())
            .addAction(Notification.Action.Builder(null, "Not now", snooze).build())
            .build()
        nm().notify(PROMPT_ID, n)
    }

    private fun ongoing(): Notification {
        val stop = PendingIntent.getService(this, 3, Intent(this, ListenService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val open = PendingIntent.getActivity(this, 4, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val until = SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date(endsAtWall))
        return Notification.Builder(this, ONGOING_CHANNEL)
            .setSmallIcon(R.drawable.ic_mic)
            .setContentTitle("Listening for conversations")
            .setContentText("Until $until. Checks a few seconds now and then; nothing is saved.")
            .setContentIntent(open)
            .setOngoing(true)
            .addAction(Notification.Action.Builder(null, "Stop", stop).build())
            .build()
    }

    private fun channel() {
        nm().createNotificationChannel(NotificationChannel(ONGOING_CHANNEL, "Listen mode", NotificationManager.IMPORTANCE_LOW))
        nm().createNotificationChannel(NotificationChannel(PROMPT_CHANNEL, "Conversation found", NotificationManager.IMPORTANCE_HIGH))
    }

    private fun nm() = getSystemService(NotificationManager::class.java)

    override fun onDestroy() {
        stopping = true
        active = false
        worker.removeCallbacksAndMessages(null)
        thread.quitSafely()
        wakeLock?.let { if (it.isHeld) it.release() }
        MainActivity.refreshListen?.invoke()
        super.onDestroy()
    }

    companion object {
        const val ACTION_STOP = "listen_stop"
        const val ACTION_SNOOZE = "listen_snooze"
        private const val CHECK_EVERY = 15_000L
        private const val CLIP_MS = 2000
        private const val SNOOZE_MS = 30 * 60_000L
        private const val ONGOING_CHANNEL = "listen"
        private const val PROMPT_CHANNEL = "listen_prompt"
        private const val ONGOING_ID = 41
        private const val PROMPT_ID = 42
        @Volatile var active = false
        @Volatile var endsAtWall = 0L

        /** Must be called from a visible activity (Android's rule for opening the mic in the background). */
        fun start(context: Context) {
            context.startForegroundService(Intent(context, ListenService::class.java))
        }

        fun stop(context: Context) {
            context.startService(Intent(context, ListenService::class.java).setAction(ACTION_STOP))
        }
    }
}
