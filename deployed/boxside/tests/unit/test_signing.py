import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from vaultsync import signing as S

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    HAVE_CRYPTO = True
except Exception:
    HAVE_CRYPTO = False

# The canonical-string vector, pinned identically in the Kotlin :identity test. If either side's
# serialisation drifts, one of the two tests fails. This is the contract of ADR-008 condition 1.
VECTOR_DEVICE_ID = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
VECTOR_PIN = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
VECTOR_BODY = b'{"x":1}'
EXPECTED = (
    "milux-sync/1\n"
    "POST\n"
    "/sync/push?op=Exercise%20Bold%20Quest\n"
    "5041bf1f713df204784353e82f6a4a535931cb64f1f4b4a5aeaffcb720918b22\n"
    "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08\n"
    "7\n"
    "\n"
    "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
).encode("utf-8")


class TestCanonical(unittest.TestCase):
    def test_signing_string_matches_the_cross_language_vector(self):
        got = S.signing_string(
            method="POST",
            path_with_query="/sync/push?op=Exercise%20Bold%20Quest",
            body=VECTOR_BODY,
            device_id=VECTOR_DEVICE_ID,
            counter=7,
            challenge="",
            channel_pin=VECTOR_PIN,
        )
        self.assertEqual(got, EXPECTED)

    def test_empty_body_hashes_the_empty_string(self):
        self.assertEqual(S.body_hash(b""), S.EMPTY_SHA256)
        self.assertEqual(S.body_hash(None), S.EMPTY_SHA256)

    def test_device_id_is_deterministic_over_the_spki(self):
        self.assertEqual(S.device_id(b"abc"), S.device_id(b"abc"))
        self.assertNotEqual(S.device_id(b"abc"), S.device_id(b"abd"))

    def test_method_upper_and_pin_lower_are_canonical(self):
        a = S.signing_string(method="post", path_with_query="/x", body=b"",
                             device_id="d", counter=1, challenge="", channel_pin="AABB")
        b = S.signing_string(method="POST", path_with_query="/x", body=b"",
                             device_id="d", counter=1, challenge="", channel_pin="aabb")
        self.assertEqual(a, b)


@unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed here; the box has it")
class TestSignVerify(unittest.TestCase):
    def setUp(self):
        self.priv = ec.generate_private_key(ec.SECP256R1())
        self.pub = self.priv.public_key()
        self.spki = self.pub.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.did = S.device_id(self.spki)
        self.fields = dict(method="POST", path_with_query="/sync/push",
                           body=b"hello", device_id=self.did, counter=5,
                           challenge="ab12", channel_pin="ffee")

    def test_round_trip(self):
        sig = S.sign(self.priv, **self.fields)
        self.assertTrue(S.verify(self.pub, sig, **self.fields))

    def test_a_changed_counter_breaks_the_signature(self):
        sig = S.sign(self.priv, **self.fields)
        replay = dict(self.fields); replay["counter"] = 4
        self.assertFalse(S.verify(self.pub, sig, **replay))

    def test_a_changed_channel_pin_breaks_the_signature(self):
        sig = S.sign(self.priv, **self.fields)
        moved = dict(self.fields); moved["channel_pin"] = "0000"
        self.assertFalse(S.verify(self.pub, sig, **moved))

    def test_a_tampered_body_breaks_the_signature(self):
        sig = S.sign(self.priv, **self.fields)
        tampered = dict(self.fields); tampered["body"] = b"hell0"
        self.assertFalse(S.verify(self.pub, sig, **tampered))


if __name__ == "__main__":
    unittest.main()
