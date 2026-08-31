"""Acceptance tests for Spec 001 (QR enrolment, box side). Committed failing first.

AC10 needs `cryptography` and is skipped where it is absent; it MUST be run on a box that has
the library before the slice's gate is called met (see the project notes: a suite only ever seen
skipping is not evidence). Run: python3 -m unittest discover -s boxside/tests -v
"""
import base64
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from vaultsync import signing  # noqa: E402
from vaultsync.enrolment import Enrolment, TokenStore, parse_qr  # noqa: E402
from vaultsync.roll import Roll  # noqa: E402

try:
    from cryptography.hazmat.primitives.asymmetric import ec  # noqa: F401
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

NOW = 1_756_600_000
FAKE_DER = b"not-a-real-spki-der-but-stable-bytes"
FAKE_B64 = base64.b64encode(FAKE_DER).decode("ascii")


def fresh_enrolment(tmp):
    return Enrolment(store_dir=tmp)


def mint(enr, now=NOW, ttl_s=600):
    """Mint a QR the way the console will, returning (qr_text, payload_dict)."""
    qr = enr.mint_qr(
        box="kit", base_url="http://100.64.0.2:8091", channel_pin="ab" * 32,
        device_label="S23", holder="Cpl Bloggs", deployment_scope="OP TELIC",
        clearance_ceiling="OFFICIAL", now=now, ttl_s=ttl_s,
    )
    return qr, parse_qr(qr)


class TokenLifecycle(unittest.TestCase):
    def test_ac1_single_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            enr = fresh_enrolment(tmp)
            _, payload = mint(enr)
            first, why = enr.enrol(payload["tok"], FAKE_B64, now=NOW + 10)
            self.assertEqual(why, "ok")
            self.assertIsNotNone(first)
            second, why2 = enr.enrol(payload["tok"], FAKE_B64, now=NOW + 20)
            self.assertIsNone(second)
            self.assertEqual(why2, "used")

    def test_ac2_expired_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            enr = fresh_enrolment(tmp)
            _, payload = mint(enr, ttl_s=600)
            gone, why = enr.enrol(payload["tok"], FAKE_B64, now=NOW + 601)
            self.assertIsNone(gone)
            self.assertEqual(why, "expired")

    def test_ac3_unknown_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            enr = fresh_enrolment(tmp)
            nothing, why = enr.enrol("no-such-token", FAKE_B64, now=NOW)
            self.assertIsNone(nothing)
            self.assertEqual(why, "unknown")

    def test_ac9_store_persists_across_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tokens.json")
            store = TokenStore(path)
            tok = store.mint(
                box="kit", device_label="S23", holder="Cpl Bloggs",
                deployment_scope="OP TELIC", clearance_ceiling="OFFICIAL",
                now=NOW, ttl_s=600,
            )
            reloaded = TokenStore(path)
            rec, why = reloaded.consume(tok.value, now=NOW + 5)
            self.assertEqual(why, "ok")
            self.assertEqual(rec.device_label, "S23")
            again = TokenStore(path)
            rec2, why2 = again.consume(tok.value, now=NOW + 6)
            self.assertIsNone(rec2)
            self.assertEqual(why2, "used")


class QrPayload(unittest.TestCase):
    def test_ac4_round_trip_and_refusals(self):
        with tempfile.TemporaryDirectory() as tmp:
            enr = fresh_enrolment(tmp)
            qr, payload = mint(enr)
            self.assertEqual(payload["v"], 1)
            self.assertEqual(payload["t"], "vd-enrol")
            self.assertEqual(payload["box"], "kit")
            self.assertEqual(payload["url"], "http://100.64.0.2:8091")
            self.assertEqual(payload["pin"], "ab" * 32)
            self.assertEqual(payload["exp"], NOW + 600)
            self.assertTrue(payload["tok"])
            self.assertIsNone(parse_qr(qr.replace("vd-enrol", "vd-other")))
            self.assertIsNone(parse_qr('{"v": 99, "t": "vd-enrol"}'))
            self.assertIsNone(parse_qr("not json at all"))


class PendingAndRoll(unittest.TestCase):
    def test_ac5_pending_not_on_roll(self):
        with tempfile.TemporaryDirectory() as tmp:
            enr = fresh_enrolment(tmp)
            _, payload = mint(enr)
            pending, why = enr.enrol(payload["tok"], FAKE_B64, now=NOW + 10)
            self.assertEqual(why, "ok")
            self.assertEqual(pending.device_label, "S23")
            self.assertEqual(pending.holder, "Cpl Bloggs")
            self.assertEqual(pending.deployment_scope, "OP TELIC")
            self.assertEqual(pending.clearance_ceiling, "OFFICIAL")
            self.assertEqual(pending.public_key_b64, FAKE_B64)
            self.assertEqual(pending.fingerprint, signing.device_id(FAKE_DER))
            roll = Roll(version=1, generated_at=NOW)
            self.assertIsNone(roll.active(pending.fingerprint))
            self.assertEqual([p.fingerprint for p in enr.pending()], [pending.fingerprint])

    def test_ac6_confirm_writes_roll_and_bumps(self):
        with tempfile.TemporaryDirectory() as tmp:
            enr = fresh_enrolment(tmp)
            _, payload = mint(enr)
            pending, _ = enr.enrol(payload["tok"], FAKE_B64, now=NOW + 10)
            old = Roll(version=3, generated_at=NOW)
            new = enr.confirm(pending.fingerprint, old, now=NOW + 60, box="kit")
            rec = new.active(pending.fingerprint)
            self.assertIsNotNone(rec)
            self.assertTrue(rec.enrolment_provenance.startswith("qr:kit:"))
            self.assertEqual(rec.deployment_scope, "OP TELIC")
            self.assertEqual(rec.anchor, old.content_hash())
            self.assertTrue(old.accepts(new))
            self.assertFalse(new.accepts(old))
            self.assertEqual(enr.pending(), [])

    def test_ac7_reject_discards_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            enr = fresh_enrolment(tmp)
            _, payload = mint(enr)
            pending, _ = enr.enrol(payload["tok"], FAKE_B64, now=NOW + 10)
            self.assertTrue(enr.reject(pending.fingerprint))
            self.assertEqual(enr.pending(), [])
            self.assertFalse(enr.reject(pending.fingerprint))

    def test_ac8_revocation_is_a_tombstone(self):
        with tempfile.TemporaryDirectory() as tmp:
            enr = fresh_enrolment(tmp)
            _, payload = mint(enr)
            pending, _ = enr.enrol(payload["tok"], FAKE_B64, now=NOW + 10)
            roll = enr.confirm(pending.fingerprint, Roll(version=1, generated_at=NOW),
                               now=NOW + 60, box="kit")
            self.assertTrue(roll.revoke(pending.fingerprint, "end of deployment", now=NOW + 90))
            self.assertIsNone(roll.active(pending.fingerprint))
            kept = roll.record(pending.fingerprint)
            self.assertIsNotNone(kept)
            self.assertEqual(kept.public_key_b64, FAKE_B64)


@unittest.skipUnless(HAVE_CRYPTO, "needs cryptography; run on the box (AC10)")
class RealKeyOnBox(unittest.TestCase):
    def test_ac10_real_key_fingerprint_and_signed_roll(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec as ecc

        device_key = ecc.generate_private_key(ecc.SECP256R1())
        spki = device_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        spki_b64 = base64.b64encode(spki).decode("ascii")

        with tempfile.TemporaryDirectory() as tmp:
            enr = fresh_enrolment(tmp)
            _, payload = mint(enr)
            pending, why = enr.enrol(payload["tok"], spki_b64, now=NOW + 10)
            self.assertEqual(why, "ok")
            self.assertEqual(pending.fingerprint, signing.device_id(spki))

            old = Roll(version=1, generated_at=NOW)
            new = enr.confirm(pending.fingerprint, old, now=NOW + 60, box="kit")

            firmbase_key = ecc.generate_private_key(ecc.SECP256R1())
            sig = new.sign_document(firmbase_key)
            self.assertTrue(new.verify_document(firmbase_key.public_key(), sig))
            self.assertFalse(new.accepts(old))


if __name__ == "__main__":
    unittest.main()


class CrossProcess(unittest.TestCase):
    """Regression for the live defect of 30 Aug 2026: the operator CLI and the server are
    separate processes over one state dir, so a token minted by one must be honoured by an
    Enrolment instance that was already open before the mint."""

    def test_ac11_live_instance_sees_another_processes_mint(self):
        with tempfile.TemporaryDirectory() as tmp:
            server_side = Enrolment(store_dir=tmp)   # opened first, like the running server
            cli_side = Enrolment(store_dir=tmp)      # the operator's separate process
            qr = cli_side.mint_qr(
                box="kit", base_url="http://test.invalid", channel_pin="ab" * 32,
                device_label="S23", holder="Cpl Bloggs", deployment_scope="OP TELIC",
                clearance_ceiling="OFFICIAL", now=NOW, ttl_s=600)
            payload = parse_qr(qr)
            pending, why = server_side.enrol(payload["tok"], FAKE_B64, now=NOW + 5)
            self.assertEqual(why, "ok")
            self.assertEqual([p.fingerprint for p in cli_side.pending()],
                             [pending.fingerprint])


class PairingCodeVector(unittest.TestCase):
    """The 6-digit read-back code (ADR-002 decision 4), pinned to one vector shared with the
    app's Kotlin PairingCode test so the two derivations can never drift."""

    def test_pinned_vector(self):
        from vaultsync.enrolment import pairing_code
        self.assertEqual(pairing_code("ab" * 32), "393562")
