package uk.co.milux.vantagedeployed

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * The clearance filter is a control, so it is tested in both directions with a control, mirroring
 * `tests/unit/test_classification_wiring.py` and the fail-closed behaviour of `classification.py`.
 * A SECRET note must be returned at SECRET clearance and withheld at OFFICIAL, and an unreadable
 * marking must be withheld, not leaked.
 */
class ClassificationTest {
    private fun vaultWithLevels(): File {
        val d = Files.createTempDirectory("vaultlvl").toFile()
        File(d, "official note.md").writeText(
            "---\nclassification: OFFICIAL\n---\n\n# Routine\n\nThe resupply drop is at grid 100200.\n")
        File(d, "secret note.md").writeText(
            "---\nclassification: SECRET\n---\n\n# Plan\n\nThe assault on OBJ FALCON begins at H-hour.\n")
        return d
    }

    @Test
    fun officialQueryCannotSeeSecret() {
        val idx = Retriever.Index(Retriever.loadVault(vaultWithLevels()))
        // unrestricted retrieval finds the SECRET passage
        assertTrue(idx.search("when does the assault on FALCON begin").any { "FALCON" in it.first.text })
        // an OFFICIAL-cleared query must not
        assertFalse(idx.search("when does the assault on FALCON begin", clearance = "OFFICIAL")
            .any { "FALCON" in it.first.text })
    }

    @Test
    fun secretClearanceSeesEverything() {
        val idx = Retriever.Index(Retriever.loadVault(vaultWithLevels()))
        assertTrue(idx.search("assault on FALCON", clearance = "SECRET").any { "FALCON" in it.first.text })
    }

    @Test
    fun chunksCarryLevel() {
        val byNote = Retriever.loadVault(vaultWithLevels()).associate { it.note to it.level }
        assertEquals("OFFICIAL", byNote["official note.md"])
        assertEquals("SECRET", byNote["secret note.md"])
    }

    @Test
    fun normaliseAndNatoEquivalents() {
        assertEquals("SECRET", Classification.normalise("secret"))
        assertEquals("OFFICIAL-SENSITIVE", Classification.normalise("OFFICIAL SENSITIVE"))
        assertEquals("SECRET", Classification.normalise("NATO CONFIDENTIAL"))
    }

    @Test
    fun leadingLevelStripsCaveats() {
        assertEquals("SECRET", Classification.leadingLevel("SECRET REL TO GBR USA"))
        assertEquals("OFFICIAL-SENSITIVE", Classification.leadingLevel("OFFICIAL-SENSITIVE//LOCSEN"))
    }

    @Test
    fun unreadableMarkingFailsClosedToTopSecret() {
        val m = Classification.readMarkingSafe("---\nclassification: BANANAS\n---\n\nbody\n")
        assertEquals("TOP SECRET", m.level)
    }

    @Test
    fun noClassificationDefaultsOfficial() {
        assertEquals("OFFICIAL", Classification.readMarkingSafe("no frontmatter here\n").level)
    }

    @Test
    fun clearanceThatCannotBeResolvedFailsClosed() {
        val idx = Retriever.Index(Retriever.loadVault(vaultWithLevels()))
        // an unknown clearance string must return nothing, never everything
        assertTrue(idx.search("resupply", clearance = "BANANAS").isEmpty())
    }
}
