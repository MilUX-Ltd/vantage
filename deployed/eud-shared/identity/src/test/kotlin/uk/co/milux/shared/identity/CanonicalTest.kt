package uk.co.milux.shared.identity

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Test

/**
 * The canonical-string vector, pinned identically to the Python `test_signing` on the box. If either
 * side's serialisation drifts, one of the two tests fails. This is the contract of ADR-008 condition 1.
 */
class CanonicalTest {
    private val vectorDeviceId = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    private val vectorPin = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    private val vectorBody = "{\"x\":1}".toByteArray(Charsets.UTF_8)
    private val expected = (
        "milux-sync/1\n" +
        "POST\n" +
        "/sync/push?op=Exercise%20Bold%20Quest\n" +
        "5041bf1f713df204784353e82f6a4a535931cb64f1f4b4a5aeaffcb720918b22\n" +
        "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08\n" +
        "7\n" +
        "\n" +
        "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    ).toByteArray(Charsets.UTF_8)

    @Test fun signing_string_matches_the_cross_language_vector() {
        val got = Canonical.signingString(
            method = "POST",
            pathWithQuery = "/sync/push?op=Exercise%20Bold%20Quest",
            body = vectorBody,
            deviceId = vectorDeviceId,
            counter = 7,
            challenge = "",
            channelPin = vectorPin,
        )
        assertArrayEquals(expected, got)
    }

    @Test fun empty_body_hashes_the_empty_string() {
        val emptySha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        assertEquals(emptySha, Canonical.bodyHash(null))
        assertEquals(emptySha, Canonical.bodyHash(ByteArray(0)))
    }

    @Test fun device_id_is_deterministic_over_the_spki() {
        assertEquals(Canonical.deviceId("abc".toByteArray()), Canonical.deviceId("abc".toByteArray()))
        assertNotEquals(Canonical.deviceId("abc".toByteArray()), Canonical.deviceId("abd".toByteArray()))
    }

    @Test fun method_upper_and_pin_lower_are_canonical() {
        val a = Canonical.signingString("post", "/x", ByteArray(0), "d", 1, "", "AABB")
        val b = Canonical.signingString("POST", "/x", ByteArray(0), "d", 1, "", "aabb")
        assertArrayEquals(a, b)
    }
}
