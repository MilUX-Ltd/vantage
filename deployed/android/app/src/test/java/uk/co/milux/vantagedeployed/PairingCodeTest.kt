package uk.co.milux.vantagedeployed

import org.junit.Assert.assertEquals
import org.junit.Test

/** The same pinned vector as the box's PairingCodeVector test, so the derivations never drift. */
class PairingCodeTest {
    @Test
    fun pinnedVector() {
        assertEquals("393562", PairingCode.forDevice("ab".repeat(32)))
        assertEquals("393 562", PairingCode.display("ab".repeat(32)))
    }
}
