import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from vaultsync import vaultapply as A

TS = "20260822-120000"


class TestVaultApply(unittest.TestCase):
    def dirs(self, d):
        """A vault and a conflict dir that is OUTSIDE it (siblings)."""
        vault = Path(d, "vault"); vault.mkdir()
        conflict = Path(d, "conflicts")
        return str(vault), str(conflict)

    def test_created_when_absent(self):
        with tempfile.TemporaryDirectory() as d:
            v, c = self.dirs(d)
            action, rel, h = A.apply_incoming(v, "a/note.md", b"hello", None, TS, c)
            self.assertEqual((action, rel), ("created", "a/note.md"))
            self.assertEqual(Path(v, "a/note.md").read_bytes(), b"hello")
            self.assertEqual(h, A.sha256_hex(b"hello"))

    def test_identical_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            v, c = self.dirs(d)
            Path(v, "note.md").write_bytes(b"same")
            action, _rel, _h = A.apply_incoming(v, "note.md", b"same", A.sha256_hex(b"same"), TS, c)
            self.assertEqual(action, "identical")

    def test_updated_when_local_unchanged_since_last_delivery(self):
        with tempfile.TemporaryDirectory() as d:
            v, c = self.dirs(d)
            Path(v, "note.md").write_bytes(b"v1")
            action, _rel, h = A.apply_incoming(v, "note.md", b"v2", A.sha256_hex(b"v1"), TS, c)
            self.assertEqual(action, "updated")
            self.assertEqual(Path(v, "note.md").read_bytes(), b"v2")
            self.assertEqual(h, A.sha256_hex(b"v2"))

    def test_conflict_stages_outside_vault_and_keeps_local(self):
        with tempfile.TemporaryDirectory() as d:
            v, c = self.dirs(d)
            Path(v, "note.md").write_bytes(b"user edit")     # local differs from last delivered
            action, path, h = A.apply_incoming(v, "note.md", b"box update", A.sha256_hex(b"v1"), TS, c)
            self.assertEqual(action, "conflict")
            # the vault keeps its single local version, no conflict copy beside it
            self.assertEqual(Path(v, "note.md").read_bytes(), b"user edit")
            self.assertEqual(list(Path(v).rglob("*.sync-conflict-*")), [])
            # the incoming version is staged OUTSIDE the vault
            self.assertTrue(path.startswith(c))
            self.assertEqual(Path(path).read_bytes(), b"box update")
            self.assertEqual(h, A.sha256_hex(b"v1"))          # index for the note unchanged

    def test_conflict_name_keeps_folder_and_extension(self):
        self.assertEqual(A.conflict_name("x/y/PIR 01.md", TS),
                         "x/y/PIR 01.sync-conflict-%s-MESH.md" % TS)
        self.assertEqual(A.conflict_name("noext", TS), "noext.sync-conflict-%s-MESH" % TS)


if __name__ == "__main__":
    unittest.main()
