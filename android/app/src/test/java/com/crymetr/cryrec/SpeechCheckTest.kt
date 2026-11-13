package com.crymetr.cryrec

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.PI
import kotlin.math.sin
import kotlin.random.Random

class SpeechCheckTest {
    private val rate = 16000

    /** 8 real 2 s speech clips then 4 real no-speech clips from the AMI ES2004a meeting, via 24 kbps Opus. */
    private fun ami(): List<ShortArray> {
        val bytes = javaClass.classLoader!!.getResourceAsStream("ami_clips.raw")!!.readBytes()
        val all = ShortArray(bytes.size / 2)
        ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN).asShortBuffer().get(all)
        return all.toList().chunked(2 * rate).map { it.toShortArray() }
    }

    @Test fun realSpeechMostlyYes() {
        val clips = ami().take(8)
        val yes = clips.count { SpeechCheck.check(it, rate).speech }
        clips.forEach { println(SpeechCheck.check(it, rate)) }
        assertTrue("speech clips detected: $yes/8", yes >= 6)
    }

    @Test fun realQuietNo() {
        val clips = ami().drop(8)
        clips.forEach { println(SpeechCheck.check(it, rate)) }
        assertEquals(0, clips.count { SpeechCheck.check(it, rate).speech })
    }

    @Test fun steadySoundsNo() {
        val r = Random(1)
        val noise = ShortArray(2 * rate) { (r.nextGaussian() * 3000).toInt().toShort() }
        val tone = ShortArray(2 * rate) { (8000 * sin(2 * PI * 220 * it / rate)).toInt().toShort() }
        val silence = ShortArray(2 * rate)
        assertFalse(SpeechCheck.check(noise, rate).speech)
        assertFalse(SpeechCheck.check(tone, rate).speech)
        assertFalse(SpeechCheck.check(silence, rate).speech)
    }

    private fun Random.nextGaussian(): Double {
        val u = nextDouble().coerceAtLeast(1e-12); val v = nextDouble()
        return kotlin.math.sqrt(-2 * kotlin.math.ln(u)) * kotlin.math.cos(2 * PI * v)
    }
}
