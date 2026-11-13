package com.crymetr.cryrec

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

class SpeakersTest {
    private fun folder(): File {
        val d = Files.createTempDirectory("spk").toFile()
        File(d, "transcript.txt").writeText(
            "Meeting x\nSpeakers: Speaker 1, Speaker 2\n\n[00:00:01] Speaker 1: hello\n[00:00:05] Speaker 2: hi\n" +
                "[00:00:09] Speaker 1: Speaker 2: is not a label here\n")
        File(d, "transcript.srt").writeText(
            "1\n00:00:01,000 --> 00:00:03,000\nSpeaker 1: hello\n\n2\n00:00:05,000 --> 00:00:07,000\nSpeaker 2: hi\n")
        File(d, "speakers.json").writeText("""{"track":"Speaker","speakers":{
            "Speaker 1":{"name":null,"suggest":"Mohammad","score":0.87,"seconds":60,"sample":1,"vec":[1,0,0]},
            "Speaker 2":{"name":null,"suggest":null,"score":0.1,"seconds":20,"sample":5,"vec":[0,1,0]}}}""")
        return d
    }

    private fun labels(d: File) = File(d, "transcript.txt").readLines().filter { it.startsWith("[") }
        .map { it.substringAfter("] ").substringBefore(":") }.toSet()

    @Test fun namesSwapAndReserved() {
        val d = folder()
        var named = Speakers.apply(d, mapOf("Speaker 1" to "Me", "Speaker 2" to "Bob"))
        assertEquals(setOf("Speaker 1", "Bob"), labels(d))
        assertEquals(listOf("Bob"), named.map { it.shown })
        named = Speakers.apply(d, mapOf("Speaker 1" to "Bob", "Speaker 2" to "Alice"))  // swap-like
        assertEquals(setOf("Bob", "Alice"), labels(d))
        assertTrue(File(d, "transcript.srt").readText().contains("\nBob: hello"))
        assertTrue(File(d, "transcript.srt").readText().contains("\nAlice: hi"))
        assertTrue(File(d, "transcript.txt").readText().contains("Bob: Speaker 2: is not a label here"))
    }

    @Test fun sameNameMerges() {
        val d = folder()
        Speakers.apply(d, mapOf("Speaker 1" to "Alex", "Speaker 2" to "Alex"))
        assertEquals(setOf("Alex"), labels(d))
        val left = Speakers.load(d)
        assertEquals(1, left.size)
        assertEquals(80.0, left[0].seconds, 0.01)
    }
}
