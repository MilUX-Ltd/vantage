package uk.co.milux.vantagedeployed

import java.math.BigInteger
import java.security.MessageDigest

/**
 * The 6-digit operator read-back code (ADR-002 decision 4): the Bluetooth numeric-comparison
 * and Signal safety-number pattern, because a 64-hex fingerprint is not a human ceremony.
 * Derived from the device fingerprint identically to the box's `pairing_code` in
 * vaultsync.enrolment, pinned to one shared test vector so the two sides can never drift.
 * The code only has to distinguish which device consumed the single-use token; a raced token
 * announces itself because the real holder's own enrolment is then refused as used.
 */
object PairingCode {
    fun forDevice(fingerprintHex: String): String {
        val digest = MessageDigest.getInstance("SHA-256")
            .digest(fingerprintHex.toByteArray(Charsets.US_ASCII))
        val n = BigInteger(1, digest).mod(BigInteger.valueOf(1_000_000)).toInt()
        return "%06d".format(n)
    }

    /** As spoken and displayed: "393 562". */
    fun display(fingerprintHex: String): String =
        forDevice(fingerprintHex).let { "${it.take(3)} ${it.drop(3)}" }
}
