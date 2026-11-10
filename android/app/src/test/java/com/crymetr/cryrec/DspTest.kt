package com.crymetr.cryrec

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.PI
import kotlin.math.roundToInt
import kotlin.math.sin

class DspTest {
    private val rate = 32_000

    /** 1 s of a 1 kHz sine with the given peak (0..1) through Dsp with the given gain. */
    private fun run(amp: Double, gain: Int): Pair<Dsp, ShortArray> {
        val d = Dsp(rate, gain)
        val buf = ShortArray(rate) { (amp * 32767 * sin(2 * PI * 1000 * it / rate)).roundToInt().toShort() }
        d.process(buf, buf.size)
        return d to buf
    }

    @Test fun sineAtMinus20() {
        val (d, _) = run(0.1, 0)
        assertEquals(-20.0, d.peakDb, 0.1)
        assertEquals(-23.01, d.rmsDb, 0.1)  // sine RMS = peak - 3.01 dB
        assertEquals(0, d.limitWindows)
        assertEquals(0, d.clipWindows)
    }

    @Test fun gainAddsExactDb() {
        for (g in listOf(6, 12, 18)) {
            val (d, _) = run(0.05, g)  // -26 dBFS
            assertEquals(-26.02 + g, d.peakDb, 0.1)
            assertEquals(-29.03 + g, d.rmsDb, 0.1)
        }
    }

    @Test fun limiterNeverClips() {
        val (d, buf) = run(0.5, 24)  // -6 dBFS + 24 dB would be +18 dBFS
        assertTrue(d.peakDb <= -0.19)
        assertTrue(buf.all { kotlin.math.abs(it.toInt()) <= (Dsp.CEIL * 32767).toInt() })
        assertEquals(20, d.limitWindows)  // every 50 ms window of the second
        assertEquals(0, d.clipWindows)    // the mic itself was fine
    }

    @Test fun micClipDetected() {
        val (d, _) = run(1.0, 0)
        assertEquals(20, d.clipWindows)
    }

    @Test fun silence() {
        val d = Dsp(rate, 12)
        d.process(ShortArray(rate), rate)
        assertEquals(Dsp.SILENT, d.peakDb, 0.0)
    }
}
