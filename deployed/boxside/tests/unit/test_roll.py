import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from vaultsync.roll import Record, Roll

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    HAVE_CRYPTO = True
except Exception:
    HAVE_CRYPTO = False


def a_record(device_id="dev1", scope="Exercise Bold Quest", ceiling="OFFICIAL"):
    return Record(
        device_id=device_id, public_key_b64="AAAA", label="S23-A", holder="Matt",
        deployment_scope=scope, clearance_ceiling=ceiling,
        enrolment_provenance="qr:FIRMBASE:2026-08-24", chain_id="chain-1", anchor="anchor-1",
        enrolled_at=1_700_000_000,
    )


class TestTombstoneRevocation(unittest.TestCase):
    def test_revoke_keeps_the_key_and_the_row(self):
        roll = Roll()
        roll.add(a_record())
        self.assertIsNotNone(roll.active("dev1"))
        self.assertTrue(roll.revoke("dev1", "handset lost", now=1_700_100_000))
        # Refused for new requests...
        self.assertIsNone(roll.active("dev1"))
        # ...but the row and key are retained so past signatures still verify (condition 3).
        rec = roll.record("dev1")
        self.assertIsNotNone(rec)
        self.assertTrue(rec.revoked)
        self.assertEqual(rec.public_key_b64, "AAAA")
        self.assertEqual(rec.revoked_reason, "handset lost")
        self.assertEqual(rec.revoked_at, 1_700_100_000)

    def test_revoking_an_unknown_device_is_false(self):
        self.assertFalse(Roll().revoke("nope", "x", now=1))


class TestVersioningAntiRollback(unittest.TestCase):
    def test_bumped_increments_version_and_copies_records(self):
        roll = Roll(version=3, generated_at=1_700_000_000)
        roll.add(a_record())
        nxt = roll.bumped(now=1_700_000_500)
        self.assertEqual(nxt.version, 4)
        self.assertEqual(nxt.generated_at, 1_700_000_500)
        self.assertIsNotNone(nxt.active("dev1"))
        # deep copy: revoking in the new generation does not touch the old
        nxt.revoke("dev1", "x", now=1_700_000_600)
        self.assertIsNotNone(roll.active("dev1"))

    def test_accepts_only_a_strictly_greater_version(self):
        held = Roll(version=5, generated_at=0)
        self.assertTrue(held.accepts(Roll(version=6)))
        self.assertFalse(held.accepts(Roll(version=5)))   # replay of the same
        self.assertFalse(held.accepts(Roll(version=4)))   # rollback to re-admit a revoked device

    def test_document_is_deterministic(self):
        r1 = Roll(version=2, generated_at=10); r1.add(a_record("b")); r1.add(a_record("a"))
        r2 = Roll(version=2, generated_at=10); r2.add(a_record("a")); r2.add(a_record("b"))
        self.assertEqual(r1.document(), r2.document())      # order-independent
        self.assertEqual(r1.content_hash(), r2.content_hash())

    def test_round_trips_through_json(self):
        roll = Roll(version=7, generated_at=123)
        roll.add(a_record())
        roll.revoke("dev1", "test", now=200)
        back = Roll.from_json(roll.to_json())
        self.assertEqual(back.version, 7)
        self.assertEqual(back.document(), roll.document())
        self.assertTrue(back.record("dev1").revoked)


class TestStaleness(unittest.TestCase):
    def test_is_stale_past_the_bound(self):
        roll = Roll(version=1, generated_at=1_000)
        self.assertFalse(roll.is_stale(now=1_500, max_age_seconds=1_000))
        self.assertTrue(roll.is_stale(now=2_100, max_age_seconds=1_000))


@unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed here; the box has it")
class TestSignedDocument(unittest.TestCase):
    def test_firmbase_signs_edge_verifies_and_a_changed_roll_fails(self):
        priv = ec.generate_private_key(ec.SECP256R1())
        pub = priv.public_key()
        roll = Roll(version=2, generated_at=10)
        roll.add(a_record())
        sig = roll.sign_document(priv)
        self.assertTrue(roll.verify_document(pub, sig))
        # EDGE re-verifies before serving; a tampered roll (extra device) fails.
        roll.add(a_record("dev2"))
        self.assertFalse(roll.verify_document(pub, sig))


if __name__ == "__main__":
    unittest.main()
