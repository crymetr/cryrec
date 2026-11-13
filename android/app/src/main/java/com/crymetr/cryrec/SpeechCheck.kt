package com.crymetr.cryrec

import kotlin.math.log10

/**
 * "Is someone talking?" for a ~2 s mono clip, on the phone, with no model.
 * Speech is loud bursts (syllables, ~3-8 per second) with quiet gaps between them, and a zero-crossing rate
 * in the voice range. A fan, traffic or a steady tone has no such gaps; silence is too quiet.
 * It only decides whether to *ask* the user, so a TV or a radio saying "yes" now and then is acceptable.
 */
object SpeechCheck {
    const val PITCHED = 0.15  // share of voiced frames with a pitch; AMI test: speech 0.16-0.63, room noise <= 0.11
    data class Result(val speech: Boolean, val voicedFraction: Double, val bursts: Int, val levelDb: Double,
                      val pitched: Double = 0.0)

    /** Strongest normalized autocorrelation at a voice pitch lag (80-300 Hz) over 40 ms from frame start o. */
    private fun periodicity(pcm: ShortArray, o: Int, len: Int, rate: Int): Double {
        if (o + len + rate / 80 > pcm.size) return 0.0
        var e0 = 0.0
        for (i in 0 until len) e0 += pcm[o + i].toDouble() * pcm[o + i]
        if (e0 <= 0) return 0.0
        var best = 0.0
        for (lag in rate / 300..rate / 80) {
            var c = 0.0; var e1 = 0.0
            for (i in 0 until len) {
                val b = pcm[o + i + lag].toDouble()
                c += pcm[o + i] * b; e1 += b * b
            }
            if (e1 > 0) best = maxOf(best, c / Math.sqrt(e0 * e1))
        }
        return best
    }

    fun check(pcm: ShortArray, rate: Int): Result {
        val frame = rate / 50  // 20 ms
        val n = pcm.size / frame
        if (n < 20) return Result(false, 0.0, 0, -120.0)
        val db = DoubleArray(n)
        val zcr = DoubleArray(n)
        for (f in 0 until n) {
            var sum = 0.0
            var cross = 0
            val o = f * frame
            for (i in 0 until frame) {
                val x = pcm[o + i] / 32768.0
                sum += x * x
                if (i > 0 && (pcm[o + i] >= 0) != (pcm[o + i - 1] >= 0)) cross++
            }
            db[f] = 10 * log10(sum / frame + 1e-12)
            zcr[f] = cross.toDouble() / frame
        }
        val sorted = db.sorted()
        val floor = sorted[n / 10]                  // quiet parts
        val level = sorted[n * 9 / 10]              // loud parts
        val gate = maxOf(floor + 10, -55.0)         // a frame must stand out from the background
        val voiced = BooleanArray(n) { db[it] > gate && zcr[it] in 0.01..0.30 }
        val frac = voiced.count { it }.toDouble() / n
        // a voice has a pitch: rustling, typing and chair noise do not
        val voicedIdx = (0 until n).filter { voiced[it] }
        val pitched = if (voicedIdx.isEmpty()) 0.0 else
            voicedIdx.count { periodicity(pcm, it * frame, 2 * frame, rate) > 0.6 }.toDouble() / voicedIdx.size
        // bursts: voiced runs of 60 ms or more, separated by gaps
        var bursts = 0
        var run = 0
        for (v in voiced + false) {
            if (v) run++ else { if (run >= 3) bursts++; run = 0 }
        }
        val seconds = n / 50.0
        val speech = level - floor >= 15 && frac in 0.2..0.92 && bursts >= seconds && bursts <= 10 * seconds &&
            pitched >= PITCHED
        return Result(speech, frac, bursts, level, pitched)
    }
}
