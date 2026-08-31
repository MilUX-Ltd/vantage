package uk.co.milux.shared.identity

import java.security.MessageDigest

/**
 * The canonical string every sync request is signed over, serialised byte-for-byte the same as the
 * Python `vaultsync.signing` on the box, so the EUD (which signs) and the box (which verifies) never
 * disagree about what was signed. ADR-008, condition 1.
 *
 * The string (UTF-8, newline-joined, no trailing newline), in order:
 *
 *     milux-sync/1                  protocol tag and version
 *     <METHOD>                      HTTP method, upper case
 *     <PATH?QUERY>                  path INCLUDING the query string, exactly as sent
 *     <body sha256, lower hex>      sha256 of the raw body bytes (empty body hashes the empty string)
 *     <device id>                   sha256 of the key's SubjectPublicKeyInfo DER, lower hex
 *     <counter>                     per-device monotonic request counter, decimal
 *     <challenge or "">             server-issued challenge (hex), or empty in counter-only mode
 *     <channel pin, lower hex>      the pinned EDGE/FIRMBASE certificate fingerprint
 *
 * Binding the counter and channel pin into the signature is what gives replay and freshness: a
 * captured signature is useless on a different channel or at or below a counter the server has seen.
 * This object holds no key material; signing is done by the platform (Android Keystore on the EUD).
 */
object Canonical {
    const val PROTO = "milux-sync/1"

    private fun sha256hex(b: ByteArray): String {
        val d = MessageDigest.getInstance("SHA-256").digest(b)
        val sb = StringBuilder(d.size * 2)
        for (x in d) { val v = x.toInt() and 0xFF; sb.append("0123456789abcdef"[v ushr 4]); sb.append("0123456789abcdef"[v and 0x0F]) }
        return sb.toString()
    }

    /** Lower-hex sha256 of the raw body bytes; a null or empty body hashes the empty string. */
    fun bodyHash(body: ByteArray?): String = sha256hex(body ?: ByteArray(0))

    /** The stable device id: lower-hex sha256 of the SubjectPublicKeyInfo (X.509) DER of the key. */
    fun deviceId(spkiDer: ByteArray): String = sha256hex(spkiDer)

    /** The exact bytes both sides sign and verify. See the object docstring for the field order. */
    fun signingString(
        method: String,
        pathWithQuery: String,
        body: ByteArray?,
        deviceId: String,
        counter: Long,
        challenge: String = "",
        channelPin: String = "",
    ): ByteArray = listOf(
        PROTO,
        method.uppercase(),
        pathWithQuery,
        bodyHash(body),
        deviceId,
        counter.toString(),
        challenge,
        channelPin.lowercase(),
    ).joinToString("\n").toByteArray(Charsets.UTF_8)
}
