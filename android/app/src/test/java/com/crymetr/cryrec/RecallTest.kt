package com.crymetr.cryrec

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

class RecallTest {
    private fun session(name: String, transcript: String, notes: String = ""): Session {
        val d = File(Files.createTempDirectory("rec").toFile(), name).apply { mkdirs() }
        File(d, "transcript.txt").writeText(transcript)
        if (notes.isNotEmpty()) File(d, "notes.md").writeText(notes)
        return Session(d)
    }

    @Test fun foldAndKeywords() {
        assertEquals("kisa gorusme istanbul", Recall.fold("Kısa Görüşme İstanbul"))
        assertEquals(listOf("renamed", "portal", "EB-X49"), Recall.keywords("Why was the portal renamed for EB-X49?").let {
            listOf("renamed", "portal", "EB-X49").filter { w -> w in it }
        })
        assertTrue("why" !in Recall.keywords("Why was the portal renamed?").map { it.lowercase() })
    }

    @Test fun passagesFindContextAndNotes() {
        val a = session("2026-11-10 10-00-00", "Meeting x\n\n[00:00:05] Speaker 1: hello all\n" +
            "[00:01:00] Speaker 2: the optical engine must be replaced\n[00:01:20] Speaker 1: calibration only helps a little\n" +
            "[00:02:00] Speaker 2: next topic\n[00:03:00] Speaker 1: something else\n[00:09:00] Speaker 1: far away line\n",
            "## Decisions\n- Replace the optical engine\n")
        val b = session("2026-11-11 10-00-00", "[00:00:01] Speaker: kısa bir toplantı, bütçe konuşuldu\n")
        val ps = Recall.passages(listOf(a, b), listOf("optical engine", "replaced"))
        assertTrue(ps.isNotEmpty())
        val top = ps.first()
        assertEquals("2026-11-10 10-00-00", top.session)
        assertEquals(5, top.seconds)  // two lines of context before the hit at 01:00
        assertTrue(top.text.contains("must be replaced") && top.text.contains("calibration"))
        assertTrue(ps.any { it.seconds == -1 && it.text.startsWith("Notes: Replace") })
        assertEquals((1..ps.size).toList(), ps.map { it.n })
        // Turkish letters fold: "kisa butce" finds "kısa ... bütçe"
        assertEquals("2026-11-11 10-00-00", Recall.passages(listOf(a, b), listOf("kisa", "butce")).first().session)
        assertTrue(Recall.passages(listOf(a, b), listOf("zebra")).isEmpty())
    }
}
