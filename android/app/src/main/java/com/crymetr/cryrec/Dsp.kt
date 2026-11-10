package com.crymetr.cryrec

import kotlin.math.abs
import kotlin.math.log10
import kotlin.math.pow
import kotlin.math.sqrt
import kotlin.math.tanh

/**
 * Gain, soft limiter and level measurement for 16-bit mono PCM, in place. Pure Kotlin (unit-tested on the JVM).
 * Levels are of the samples after gain and limiter, i.e. what ends up in the file, over 50 ms windows, in dBFS
 * (0 dBFS = a full-scale sample; a full-scale sine has peak 0 and RMS -3.01).
 */
class Dsp(rate: Int, @Volatile var gainDb: Int = 0) {
    private val window = rate / 20
    @Volatile var peakDb = SILENT
        private set
    @Volatile var rmsDb = SILENT
        private set
    /** 50 ms windows where the mic itself reached full scale: real clipping, gain can not undo it. */
    @Volatile var clipWindows = 0
        private set
    /** 50 ms windows where the boosted signal would have gone over full scale and the limiter held it down. */
    @Volatile var limitWindows = 0
        private set
    private var winPeak = 0f
    private var winSq = 0.0
    private var winN = 0
    private var winClip = false
    private var winLimit = false

    fun process(buf: ShortArray, n: Int) {
        val g = 10.0.pow(gainDb / 20.0).toFloat()
        for (i in 0 until n) {
            val x = buf[i] / 32768f
            if (abs(x) >= 0.999f) winClip = true
            var y = x * g
            val a = abs(y)
            if (a > KNEE) {  // soft knee: unchanged up to -3 dBFS, then bends smoothly toward (never past) CEIL
                if (a >= 1f) winLimit = true
                y = Math.copySign(KNEE + (CEIL - KNEE) * tanh(((a - KNEE) / (CEIL - KNEE)).toDouble()).toFloat(), y)
            }
            buf[i] = (y * 32767f).toInt().toShort()
            if (abs(y) > winPeak) winPeak = abs(y)
            winSq += (y * y).toDouble()
            if (++winN == window) {
                peakDb = db(winPeak.toDouble())
                rmsDb = db(sqrt(winSq / winN))
                if (winClip) clipWindows++
                if (winLimit) limitWindows++
                winPeak = 0f; winSq = 0.0; winN = 0; winClip = false; winLimit = false
            }
        }
    }

    fun reset() { peakDb = SILENT; rmsDb = SILENT }

    companion object {
        const val SILENT = -120.0
        /** 0.708 = -3 dBFS: below it the signal passes untouched. */
        const val KNEE = 0.708f
        /** 0.977 = -0.2 dBFS: the limiter's output never goes above this. */
        const val CEIL = 0.977f
        fun db(v: Double) = if (v <= 1e-6) SILENT else 20 * log10(v)
    }
}
