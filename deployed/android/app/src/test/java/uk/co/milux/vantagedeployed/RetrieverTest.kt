package uk.co.milux.vantagedeployed

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * Parity of the on-device retriever with the box `vaultqa.py`, over the same fixtures as
 * `tests/unit/test_vaultqa.py`. The expected BM25 scores are the Python reference values, so this
 * proves the phone retrieves what the box retrieves, not merely that it retrieves something.
 */
class RetrieverTest {
    private fun makeVault(): File {
        val d = Files.createTempDirectory("vault").toFile()
        File(d, "Bold Quest").mkdirs()
        File(d, "Bold Quest/Kit power-up.md").writeText(
            "# Kit power-up sequence\n\nRouter on first, then the MINIX, wait two minutes.\n" +
            "Radios last, antennas on before power.\n")
        File(d, "Bold Quest/overview.md").writeText(
            "# Bold Quest overview\n\nDemonstration serial. Confirm the deployed picture on all clients.\n")
        File(d, "TAK Chat/Hackathon").mkdirs()
        File(d, "TAK Chat/Hackathon/2026-08-21.md").writeText(
            "# TAK chat: Hackathon, 2026-08-21\n\n" +
            "- **14:30:05** ALPHA: Moving to grid 123456 _(at 39.77340, -84.06140)_\n" +
            "- **14:32:10** BRAVO: Holding at the north gate, awaiting orders.\n")
        return d
    }

    @Test
    fun loadAndChunkSkipsDotDirs() {
        val d = makeVault()
        val chunks = Retriever.loadVault(d)
        assertTrue(chunks.any { it.note.endsWith("Kit power-up.md") })
        assertTrue(chunks.any { "antennas" in it.text })
        File(d, ".obsidian").mkdirs()
        File(d, ".obsidian/workspace.md").writeText("# noise\n\nignore me\n")
        assertFalse(Retriever.loadVault(d).any { "ignore me" in it.text })
    }

    @Test
    fun bm25RanksPowerUpFirstWithPythonScores() {
        val idx = Retriever.Index(Retriever.loadVault(makeVault()))
        val top = idx.search("what order do I power up the radios and router", k = 3)
        assertEquals(3, top.size)
        // Python reference: Kit power-up 4.600900, overview 0.163727, TAK chat 0.109999
        assertEquals(
            listOf("Bold Quest/Kit power-up.md", "Bold Quest/overview.md", "TAK Chat/Hackathon/2026-08-21.md"),
            top.map { it.first.note },
        )
        assertEquals(4.600900, top[0].second, 1e-4)
        assertEquals(0.163727, top[1].second, 1e-4)
        assertEquals(0.109999, top[2].second, 1e-4)
    }

    @Test
    fun bm25FindsTakChatContent() {
        val idx = Retriever.Index(Retriever.loadVault(makeVault()))
        val top = idx.search("where is BRAVO holding", k = 3)
        assertTrue(top.isNotEmpty())
        assertTrue("north gate" in top[0].first.text)
        assertTrue("TAK Chat" in top[0].first.note)
        assertEquals(1.615951, top[0].second, 1e-4)   // Python reference score
    }

    @Test
    fun noMatchReturnsEmpty() {
        val idx = Retriever.Index(Retriever.loadVault(makeVault()))
        assertTrue(idx.search("quantum chromodynamics helicopter").isEmpty())
    }

    @Test
    fun promptAndSources() {
        val idx = Retriever.Index(Retriever.loadVault(makeVault()))
        val hits = idx.search("power up sequence", k = 2)
        val prompt = Retriever.buildPrompt("What is the power up order?", hits)
        assertTrue("only this context" in prompt)
        assertTrue("QUESTION: What is the power up order?" in prompt)
        assertTrue(Retriever.sources(hits).isNotEmpty())
    }
}
