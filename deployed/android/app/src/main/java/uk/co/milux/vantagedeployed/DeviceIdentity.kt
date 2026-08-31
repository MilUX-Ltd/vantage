// Ported from milux-vault-sync android/brain DeviceIdentity.kt at 42a89de (ADR-001); the
// product app keeps its own Keystore alias so it coexists with the estate app on one handset.
package uk.co.milux.vantagedeployed

import android.os.Build
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyInfo
import android.security.keystore.KeyProperties
import uk.co.milux.shared.identity.Canonical
import java.security.KeyFactory
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PublicKey
import java.security.Signature
import java.security.spec.ECGenParameterSpec

/**
 * The device identity (ADR-008): an ECDSA P-256 keypair generated in the Android Keystore,
 * non-extractable and hardware-backed (StrongBox where the device has it, otherwise the TEE).
 * The private key never leaves the Keystore; the app holds a handle and asks the Keystore to
 * sign. The public key, and the device id derived from it, are what the box enrols in its roll.
 *
 * The key is generated once and kept: the identity must be stable, because replacing it means
 * re-enrolling in person. The stolen-unlocked-handset residual is accepted for the demo
 * posture (ADR-002 decision 3); user-authentication-bound keys are the named hardening.
 */
object DeviceIdentity {
    private const val ALIAS = "vantage-device-identity"
    private const val KS = "AndroidKeyStore"

    private fun keystore(): KeyStore = KeyStore.getInstance(KS).apply { load(null) }

    fun exists(): Boolean = keystore().containsAlias(ALIAS)

    /** Generate the device key if absent (StrongBox first, TEE fallback), and return the public
     *  key. Idempotent: an existing key is kept so the identity stays stable across launches. */
    fun ensure(): PublicKey {
        val ks = keystore()
        if (ks.containsAlias(ALIAS)) return ks.getCertificate(ALIAS).publicKey
        try {
            generate(strongBox = true)
        } catch (_: Throwable) {
            if (keystore().containsAlias(ALIAS)) keystore().deleteEntry(ALIAS)
            generate(strongBox = false)
        }
        return keystore().getCertificate(ALIAS).publicKey
    }

    private fun generate(strongBox: Boolean) {
        val gen = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, KS)
        val spec = KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_SIGN)
            .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
            .setDigests(KeyProperties.DIGEST_SHA256)
            .apply { if (strongBox && Build.VERSION.SDK_INT >= 28) setIsStrongBoxBacked(true) }
            .build()
        gen.initialize(spec)
        gen.generateKeyPair()
    }

    /** The public key in SubjectPublicKeyInfo (X.509) DER, as the box enrols and derives the id. */
    fun publicKeyDer(): ByteArray = ensure().encoded

    /** The stable device id: the same sha256-of-SPKI derivation the box uses. */
    fun deviceId(): String = Canonical.deviceId(publicKeyDer())

    /** Whether the private key sits in secure hardware (StrongBox or TEE), for the join display. */
    fun inSecureHardware(): Boolean = try {
        val entry = keystore().getEntry(ALIAS, null) as KeyStore.PrivateKeyEntry
        val factory = KeyFactory.getInstance(entry.privateKey.algorithm, KS)
        val info = factory.getKeySpec(entry.privateKey, KeyInfo::class.java)
        @Suppress("DEPRECATION")
        info.isInsideSecureHardware
    } catch (_: Throwable) { false }

    /** Sign the canonical request string with the Keystore key. The DER signature is returned;
     *  the private key never leaves the Keystore. */
    fun sign(
        method: String, pathWithQuery: String, body: ByteArray?, counter: Long,
        challenge: String, channelPin: String,
    ): ByteArray {
        val msg = Canonical.signingString(method, pathWithQuery, body, deviceId(), counter, challenge, channelPin)
        val entry = keystore().getEntry(ALIAS, null) as KeyStore.PrivateKeyEntry
        val s = Signature.getInstance("SHA256withECDSA")
        s.initSign(entry.privateKey)
        s.update(msg)
        return s.sign()
    }
}
