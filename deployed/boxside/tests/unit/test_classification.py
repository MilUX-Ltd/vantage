import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from vaultsync import classification as C


def note(classification=None, extra=""):
    fm = "---\ntype: report\n"
    if classification:
        fm += f"classification: {classification}\n"
    if extra:
        fm += extra
    fm += "---\n\n# A report\n\nbody\n"
    return fm


class TestClassification(unittest.TestCase):
    def test_ladder_and_normalise(self):
        self.assertEqual(C.normalise("official"), "OFFICIAL")
        self.assertEqual(C.normalise("NATO RESTRICTED"), "OFFICIAL-SENSITIVE")
        self.assertEqual(C.normalise("cosmic top secret"), "TOP SECRET")
        with self.assertRaises(C.ClassificationError):
            C.normalise("BURN BEFORE READING")

    def test_default_is_official(self):
        m = C.read_marking(note(None))
        self.assertEqual(m.level, "OFFICIAL")

    def test_read_sensitive_and_rel(self):
        m = C.read_marking(note("OFFICIAL-SENSITIVE", "releasable_to: GBR/USA\n"))
        self.assertEqual(m.level, "OFFICIAL-SENSITIVE")
        self.assertEqual(m.releasable_to, frozenset({"GBR", "USA"}))

    def test_rel_inline(self):
        m = C.read_marking(note("SECRET REL TO GBR, USA"))
        self.assertEqual(m.level, "SECRET")
        self.assertEqual(m.releasable_to, frozenset({"GBR", "USA"}))

    def test_ceiling_guard(self):
        mesh = C.Bearer("mesh", "OFFICIAL")
        self.assertTrue(C.may_traverse(C.read_marking(note("OFFICIAL")), mesh).allowed)
        d = C.may_traverse(C.read_marking(note("SECRET")), mesh)
        self.assertFalse(d.allowed)
        self.assertIn("exceeds", d.reason)
        # OFFICIAL-SENSITIVE also refused on an OFFICIAL-only bearer
        self.assertFalse(C.may_traverse(C.read_marking(note("OFFICIAL-SENSITIVE")), mesh).allowed)

    def test_accredited_bearer_carries_secret(self):
        tac = C.Bearer("mpu5", "SECRET", frozenset({"GBR"}))
        self.assertTrue(C.may_traverse(C.read_marking(note("SECRET")), tac).allowed)
        self.assertFalse(C.may_traverse(C.read_marking(note("TOP SECRET")), tac).allowed)

    def test_uk_eyes_only_blocked_on_coalition_bearer(self):
        coalition = C.Bearer("coalition", "SECRET", frozenset({"GBR", "USA"}))
        m = C.read_marking(note("SECRET", "caveat: UK EYES ONLY\n"))
        self.assertFalse(C.may_traverse(m, coalition).allowed)
        uk_only = C.Bearer("national", "SECRET", frozenset({"GBR"}))
        self.assertTrue(C.may_traverse(m, uk_only).allowed)

    def test_releasability_blocks_non_rel_audience(self):
        m = C.read_marking(note("OFFICIAL-SENSITIVE", "releasable_to: GBR\n"))
        coalition = C.Bearer("coalition", "SECRET", frozenset({"GBR", "USA"}))
        d = C.may_traverse(m, coalition)
        self.assertFalse(d.allowed)
        self.assertIn("USA", d.reason)

    def test_fail_closed_on_bad_marking(self):
        m = C.read_marking_safe(note("NONSENSE"))
        self.assertEqual(m.level, "TOP SECRET")  # withheld, not leaked


if __name__ == "__main__":
    unittest.main()
