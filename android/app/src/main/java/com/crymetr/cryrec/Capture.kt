package com.crymetr.cryrec

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaMuxer
import android.media.MediaRecorder
import java.io.File
import java.nio.ByteOrder
import kotlin.math.max

/**
 * Mic -> gain -> soft limiter -> AAC in an .m4a. The level meter measures the samples that are actually
 * written (after gain), in dBFS: 0 dBFS = the loudest a 16-bit sample can be. dB SPL would need a
 * calibrated mic, so it is not shown.
 */
class Capture(context: Context, private val out: File, mic: String, gainDb: Int) {
    private val dsp = Dsp(RATE, gainDb)
    /** Gain in dB, may change while recording. */
    var gainDb: Int
        get() = dsp.gainDb
        set(v) { dsp.gainDb = v }
    /** Levels of the last 50 ms written, see Dsp. */
    val peakDb get() = dsp.peakDb
    val rmsDb get() = dsp.rmsDb
    val clipWindows get() = dsp.clipWindows
    val limitWindows get() = dsp.limitWindows
    /** The mic Android actually routed to, e.g. "Phone mic". */
    @Volatile var micLabel = "…"
        private set

    private val record: AudioRecord
    private val codec: MediaCodec
    private val muxer = MediaMuxer(out.path, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
    private var track = -1
    private var muxing = false
    private var frames = 0L
    @Volatile private var running = true
    private val thread = Thread({ loop() }, "CryRec-capture")

    init {
        val minBuf = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        @SuppressLint("MissingPermission")  // the service checks RECORD_AUDIO before it gets here
        val r = AudioRecord(MediaRecorder.AudioSource.MIC, RATE, AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT, max(minBuf * 4, RATE / 2 * 2))
        record = r
        if (r.state != AudioRecord.STATE_INITIALIZED) { r.release(); muxer.release(); throw IllegalStateException("mic not available") }
        pickDevice(context, mic)?.let { r.preferredDevice = it }
        r.addOnRoutingChangedListener({ micLabel = label(it.routedDevice) }, null)
        codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_AAC)
        codec.configure(MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_AAC, RATE, 1).apply {
            setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC)
            setInteger(MediaFormat.KEY_BIT_RATE, 48_000)
            setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, CHUNK * 2)
        }, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        codec.start()
        r.startRecording()
        if (r.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
            r.release(); codec.release(); muxer.release()
            throw IllegalStateException("the microphone is busy")
        }
        micLabel = label(r.routedDevice)
        thread.start()
    }

    /** Stop and finish the file. Blocks until the encoder has written everything (normally a few ms). */
    fun stop() {
        running = false
        thread.join(5000)
    }

    private fun loop() {
        val buf = ShortArray(CHUNK)
        try {
            while (running) {
                val n = record.read(buf, 0, CHUNK)
                if (n <= 0) continue
                dsp.process(buf, n)
                encode(buf, n, false)
            }
            encode(buf, 0, true)
        } finally {
            runCatching { record.stop() }
            record.release()
            runCatching { codec.stop() }
            codec.release()
            if (muxing) runCatching { muxer.stop() }
            muxer.release()
            dsp.reset()
        }
    }

    private fun encode(pcm: ShortArray, n: Int, end: Boolean) {
        var off = 0
        do {
            val idx = codec.dequeueInputBuffer(10_000)
            if (idx >= 0) {
                val ib = codec.getInputBuffer(idx)!!
                ib.clear()
                val count = minOf(n - off, ib.capacity() / 2)
                ib.order(ByteOrder.LITTLE_ENDIAN).asShortBuffer().put(pcm, off, count)
                val pts = frames * 1_000_000L / RATE
                frames += count
                off += count
                val last = end && off >= n
                codec.queueInputBuffer(idx, 0, count * 2, pts, if (last) MediaCodec.BUFFER_FLAG_END_OF_STREAM else 0)
                if (last) break
            }
            drain(false)
        } while (off < n || (end && idx < 0))
        if (end) drain(true)
    }

    private fun drain(untilEnd: Boolean) {
        val info = MediaCodec.BufferInfo()
        val deadline = System.currentTimeMillis() + 3000
        while (true) {
            val idx = codec.dequeueOutputBuffer(info, if (untilEnd) 10_000 else 0)
            when {
                idx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                    track = muxer.addTrack(codec.outputFormat)
                    muxer.start()
                    muxing = true
                }
                idx >= 0 -> {
                    val ob = codec.getOutputBuffer(idx)!!
                    if (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0) info.size = 0
                    if (info.size > 0 && muxing) {
                        ob.position(info.offset); ob.limit(info.offset + info.size)
                        muxer.writeSampleData(track, ob, info)
                    }
                    codec.releaseOutputBuffer(idx, false)
                    if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) return
                }
                !untilEnd -> return
                System.currentTimeMillis() > deadline -> return
            }
        }
    }

    companion object {
        const val RATE = 32_000
        private const val CHUNK = 1024
        val GAINS = listOf(0, 6, 12, 18, 24)
        /** auto = whatever Android picks (a plugged-in headset wins), phone = the phone's own mic, headset = wired/USB headset. */
        val MICS = linkedMapOf("phone" to "Phone mic (records the room)", "headset" to "Headset mic (wired or USB)",
            "auto" to "Automatic (Android picks)")

        fun pickDevice(context: Context, mic: String): AudioDeviceInfo? {
            val devices = context.getSystemService(AudioManager::class.java).getDevices(AudioManager.GET_DEVICES_INPUTS)
            return when (mic) {
                "phone" -> devices.firstOrNull { it.type == AudioDeviceInfo.TYPE_BUILTIN_MIC }
                "headset" -> devices.firstOrNull { it.type in HEADSETS }
                else -> null
            }
        }

        private val HEADSETS = setOf(AudioDeviceInfo.TYPE_WIRED_HEADSET, AudioDeviceInfo.TYPE_USB_HEADSET,
            AudioDeviceInfo.TYPE_USB_DEVICE, AudioDeviceInfo.TYPE_BLE_HEADSET, AudioDeviceInfo.TYPE_BLUETOOTH_SCO)

        fun label(d: AudioDeviceInfo?): String = when (d?.type) {
            null -> "…"
            AudioDeviceInfo.TYPE_BUILTIN_MIC -> "Phone mic"
            AudioDeviceInfo.TYPE_WIRED_HEADSET -> "Wired headset mic"
            AudioDeviceInfo.TYPE_USB_HEADSET, AudioDeviceInfo.TYPE_USB_DEVICE -> "USB mic"
            AudioDeviceInfo.TYPE_BLUETOOTH_SCO, AudioDeviceInfo.TYPE_BLE_HEADSET -> "Bluetooth headset mic"
            else -> d.productName?.toString()?.ifBlank { null } ?: "Other mic"
        }
    }
}
